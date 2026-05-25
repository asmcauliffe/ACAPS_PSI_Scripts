"""
gkg_bulk_themes.py
------------------
Build monthly theme *article counts* and *average document tone* from **raw GDELT 2.0 GKG
files** (HTTP download), instead of the GDELT DOC API.

Why use this
------------
The DOC API is rate-limited and can take days for many countries × years. GKG master files
are large but **one 15-minute slice per day** (~8–30 MB compressed typical) gives a
consistent daily sample without API quotas. You trade bandwidth and disk for throughput.

Data source
-----------
  http://data.gdeltproject.org/gdeltv2/{YYYYMMDDHHMMSS}.gkg.csv.zip

This script uses the **midnight UTC** slice per calendar day: ``{YYYYMMDD}000000``.

Method (approximate)
--------------------
- **Themes:** row matches if the official GKG theme code appears in column **V2Themes**
  (``gdelt_fetch.THEMES_GKG_SUBSTRINGS``).
- **Country:** row matches if FIPS ``#XX#`` appears in the location blob (columns 9–10
  per GKG 2.0 layout: legacy Locations + V2Locations). A document can count toward
  multiple countries if multiple appear.
- **Tone:** first field of **V2Tone** (comma-separated GCAM document scores) is treated
  as document tone (same scale family as DOC timelinetone aggregates).

Outputs ``themes_monthly_gkg.csv`` with columns aligned to ``themes_monthly.csv`` where
possible; ``total_articles_gdelt`` / ``pct_coverage`` are left empty (not defined for
this path).

Usage
-----
  pip install requests pandas tqdm

  # One year, Ethiopia only (test)
  python gkg_bulk_themes.py --start-year 2024 --end-year 2024 --countries Ethiopia

  # Narrow date range (midnight UTC slices are sparse for some countries — use a long span)
  python gkg_bulk_themes.py --start-date 2024-06-01 --end-date 2024-12-31 --countries Ethiopia

  # Full panel (long run; tens of GB over the wire — use resume)
  python gkg_bulk_themes.py --start-year 2018 --end-year 2026 --resume

See also: http://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
import zipfile
import pickle
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterator
from urllib.request import Request, urlopen

import pandas as pd

# Reuse country/theme definitions from gdelt_fetch (same project).
from gdelt_fetch import COUNTRIES, THEMES_GKG_SUBSTRINGS

GKG_BASE = "http://data.gdeltproject.org/gdeltv2"
# Verified against a live file: 27 tab-separated columns; 1-based docs often list 1–27.
COL_DATE = 1
COL_V2THEMES = 8
COL_LOC_LEGACY = 9
COL_V2LOCATIONS = 10
COL_V2TONE = 15
MIN_COLS = 16

DEFAULT_DELAY = 0.35  # seconds between HTTP downloads (be polite to data.gdeltproject.org)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )


def daterange(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def gkg_url_for_day(d: date) -> str:
    ts = d.strftime("%Y%m%d") + "000000"
    return f"{GKG_BASE}/{ts}.gkg.csv.zip"


def location_blob(parts: list[str]) -> str:
    chunks: list[str] = []
    if len(parts) > COL_V2LOCATIONS:
        chunks.append(parts[COL_V2LOCATIONS])
    if len(parts) > COL_LOC_LEGACY:
        chunks.append(parts[COL_LOC_LEGACY])
    return "".join(chunks)


def parse_tone(v2tone: str) -> float | None:
    v2tone = (v2tone or "").strip()
    if not v2tone:
        return None
    first = v2tone.split(",")[0].strip()
    try:
        return float(first)
    except ValueError:
        return None


def parse_gkg_day_file(
    fp: io.BufferedReader,
    countries_fips: dict[str, str],
    theme_substrings: dict[str, tuple[str, ...]],
) -> dict[tuple[str, str, str], dict]:
    """
    Stream one GKG CSV. Returns nested stats:
      (country_name, theme_key, year_month) -> {"n": int, "tone_sum": float, "tone_n": int}
    """
    local: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {"n": 0, "tone_sum": 0.0, "tone_n": 0}
    )
    theme_code_to_keys: dict[str, list[str]] = defaultdict(list)
    for tk, codes in theme_substrings.items():
        for code in codes:
            theme_code_to_keys[code].append(tk)

    for raw in fp:
        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < MIN_COLS:
            continue
        v2themes = parts[COL_V2THEMES]
        if not v2themes:
            continue
        matched_keys: list[str] = []
        for code, keys in theme_code_to_keys.items():
            if code in v2themes:
                matched_keys.extend(keys)
        if not matched_keys:
            continue

        loc = location_blob(parts)
        tone = parse_tone(parts[COL_V2TONE])

        date_s = parts[COL_DATE]
        if len(date_s) < 8:
            continue
        year_month = f"{date_s[:4]}-{date_s[4:6]}"

        for country_name, fips in countries_fips.items():
            needle = f"#{fips}#"
            if needle not in loc:
                continue
            for theme_key in set(matched_keys):
                k = (country_name, theme_key, year_month)
                bucket = local[k]
                bucket["n"] += 1
                if tone is not None:
                    bucket["tone_sum"] += tone
                    bucket["tone_n"] += 1

    return dict(local)


def download_to_memory(url: str, timeout: float = 300.0) -> bytes:
    req = Request(url, headers={"User-Agent": "INFORM-NovelData-GKG/1.0 (research)"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def process_cached_or_download(
    day: date,
    cache_dir: str,
    timeout: float,
    countries_fips: dict[str, str],
) -> dict[tuple[str, str, str], dict] | None:
    url = gkg_url_for_day(day)
    fname = day.strftime("%Y%m%d") + "000000.gkg.csv.zip"
    cache_path = os.path.join(cache_dir, fname)

    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 1000:
        data = open(cache_path, "rb").read()
    else:
        try:
            data = download_to_memory(url, timeout=timeout)
        except Exception as e:
            logging.warning("  download failed %s: %s", url, e)
            return None
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "wb") as out:
            out.write(data)

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        logging.warning("  bad zip for %s", fname)
        return None
    names = zf.namelist()
    if not names:
        return None
    inner = zf.open(names[0], "r")
    stats = parse_gkg_day_file(inner, countries_fips, THEMES_GKG_SUBSTRINGS)
    inner.close()
    zf.close()
    return stats


def _parse_iso_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def run_bulk(
    start_year: int,
    end_year: int,
    output_dir: str,
    countries_filter: list[str] | None,
    resume: bool,
    request_delay: float,
    http_timeout: float,
    limit_days: int | None,
    start_date: str | None,
    end_date: str | None,
) -> None:
    setup_logging()
    os.makedirs(output_dir, exist_ok=True)
    cache_dir = os.path.join(output_dir, "_gkg_cache")
    out_csv = os.path.join(output_dir, "themes_monthly_gkg.csv")
    state_path = os.path.join(output_dir, "_gkg_bulk_state.pkl")

    countries_use = {
        k: v for k, v in COUNTRIES.items()
        if countries_filter is None or k in countries_filter
    }
    logging.info("Countries: %s", list(countries_use.keys()))
    logging.info("Themes: %s", list(THEMES_GKG_SUBSTRINGS.keys()))

    start_d = date(start_year, 1, 1) if start_date is None else _parse_iso_date(start_date)
    end_d = date(end_year, 12, 31) if end_date is None else _parse_iso_date(end_date)
    if start_d > end_d:
        raise SystemExit("start date must be <= end date")
    if end_d > date.today():
        end_d = date.today()

    done_days: set[str] = set()
    global_agg: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {"n": 0, "tone_sum": 0.0, "tone_n": 0}
    )

    if resume and os.path.exists(state_path):
        with open(state_path, "rb") as sf:
            st = pickle.load(sf)
        done_days = set(st.get("done_days", []))
        for key, v in st.get("agg", {}).items():
            k = key if isinstance(key, tuple) else tuple(key)
            global_agg[k] = {
                "n": v["n"],
                "tone_sum": v["tone_sum"],
                "tone_n": v["tone_n"],
            }
        logging.info("Resume: %d days in state, partial rows buffered", len(done_days))

    countries_fips = {k: v["fips"] for k, v in countries_use.items()}

    total_days = (end_d - start_d).days + 1
    processed = 0
    completed = 0
    for d in daterange(start_d, end_d):
        if limit_days is not None and completed >= limit_days:
            break
        ds = d.isoformat()
        if resume and ds in done_days:
            continue
        processed += 1
        if processed == 1 or processed % 25 == 0:
            logging.info(
                "GKG day %s (%d / ~%d in range, %d days done in state)",
                ds,
                processed,
                total_days - len(done_days),
                len(done_days),
            )
        stats = process_cached_or_download(d, cache_dir, http_timeout, countries_fips)
        if stats is None:
            logging.warning(
                "  skipped day %s (download/parse failed) — not marked done; re-run with --resume",
                ds,
            )
            time.sleep(max(request_delay, 5.0))
            continue

        for k, v in stats.items():
            g = global_agg[k]
            g["n"] += v["n"]
            g["tone_sum"] += v["tone_sum"]
            g["tone_n"] += v["tone_n"]

        done_days.add(ds)
        completed += 1
        with open(state_path, "wb") as sf:
            pickle.dump(
                {
                    "done_days": sorted(done_days),
                    "agg": dict(global_agg),
                },
                sf,
            )

        if completed == 1 or completed % 25 == 0:
            rows_out: list[dict] = []
            for (country, theme_key, ym), v in sorted(global_agg.items()):
                tn = v["tone_n"]
                avg_tone = (v["tone_sum"] / tn) if tn > 0 else None
                rows_out.append({
                    "country": country,
                    "theme_key": theme_key,
                    "year_month": ym,
                    "articles": v["n"],
                    "total_articles_gdelt": "",
                    "pct_coverage": "",
                    "avg_tone": avg_tone,
                    "country_group": COUNTRIES[country]["group"],
                })
            pd.DataFrame(rows_out).to_csv(out_csv, index=False)
        time.sleep(request_delay)

    rows_final: list[dict] = []
    for (country, theme_key, ym), v in sorted(global_agg.items()):
        tn = v["tone_n"]
        avg_tone = (v["tone_sum"] / tn) if tn > 0 else None
        rows_final.append({
            "country": country,
            "theme_key": theme_key,
            "year_month": ym,
            "articles": v["n"],
            "total_articles_gdelt": "",
            "pct_coverage": "",
            "avg_tone": avg_tone,
            "country_group": COUNTRIES[country]["group"],
        })
    pd.DataFrame(rows_final).to_csv(out_csv, index=False)
    logging.info("Wrote %s (%d rows)", out_csv, len(rows_final))
    logging.info("Done.")


def main() -> None:
    p = argparse.ArgumentParser(description="Monthly GKG theme/tone from bulk GDELT 2.0 files.")
    p.add_argument("--start-year", type=int, default=2018)
    p.add_argument("--end-year", type=int, default=datetime.now().year)
    p.add_argument("--output-dir", default="./gdelt_results")
    p.add_argument("--countries", nargs="+", default=None, metavar="COUNTRY")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--request-delay", type=float, default=DEFAULT_DELAY)
    p.add_argument("--http-timeout", type=float, default=300.0)
    p.add_argument(
        "--limit-days",
        type=int,
        default=None,
        metavar="N",
        help="Stop after successfully processing N calendar days (for testing).",
    )
    p.add_argument(
        "--start-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Override range start (default: YYYY-01-01 from --start-year).",
    )
    p.add_argument(
        "--end-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Override range end (default: YYYY-12-31 from --end-year, capped at today).",
    )
    args = p.parse_args()

    run_bulk(
        start_year=args.start_year,
        end_year=args.end_year,
        output_dir=args.output_dir,
        countries_filter=args.countries,
        resume=args.resume,
        request_delay=args.request_delay,
        http_timeout=args.http_timeout,
        limit_days=args.limit_days,
        start_date=args.start_date,
        end_date=args.end_date,
    )


if __name__ == "__main__":
    main()
