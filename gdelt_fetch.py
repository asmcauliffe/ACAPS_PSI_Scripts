"""
gdelt_fetch.py
--------------
Fetches GDELT data relevant to the INFORM Novel Data Consultancy's
driver-specific proxy indicator framework. Produces monthly aggregates
across three signal types for all priority countries.

WHAT THIS FETCHES
-----------------
1. GDELT 2.0 Events (raw CSV files from Google Storage)
   - Conflict/violence events (CAMEO codes 18–20: assault, fight, use of force)
   - Protest events (CAMEO code 14)
   - Humanitarian aid events (CAMEO code 08)
   Per country per month: event count, avg GoldsteinScale, avg AvgTone,
   total mentions, total sources.

2. GDELT DOC API — Timeline (recent 3 months, or historical via chunked calls)
   - Volume of coverage (timelinevolraw) for food/famine/displacement themes
   - Average tone (timelinetone) for those same themes
   GKG themes queried: FOOD_SECURITY, FAMINE, HUMANITARIAN_CRISIS,
   DISPLACEMENT, REFUGEES, NATURAL_DISASTER, ECONOMIC_CRISIS

ARCHITECTURE DECISION
---------------------
The GDELT DOC API officially supports only the last ~3 months live.
For historical depth (default 2018 through present), we download the raw
GDELT 2.0 Event files directly from:
  http://data.gdeltproject.org/gdeltv2/{YYYYMMDDHHMMSS}.export.CSV.zip
These files are published every 15 minutes. We download one file per day
(the 000000 file = midnight) to get a representative daily sample, then
aggregate to monthly. This avoids downloading 96 files/day × 10 years.

For the DOC API (themes/tone), we chunk calls into windows (default 3 months;
use --doc-chunk-months 1 for lighter per-request loads). If a window still fails
after retries, use --doc-subchunk-on-failure to split into calendar months, and/or
--doc-volume-only-on-failure to keep article volume with tone left empty.

DOC API pacing (quota-sensitive)
--------------------------------
GDELT limits hosted API traffic (often described as about one request every
~5 seconds). Throughput should stay *below* that limit with margin: small
increases in QPS can sharply raise 429 rates (see GDELT's note on quota
sensitivity: https://blog.gdeltproject.org/behind-the-scenes-api-quotas-the-impact-of-a-fraction-of-a-qps/).
We therefore enforce a minimum gap between *every* DOC HTTP call (vol and
tone are separate requests), add jitter, and pause briefly after each
completed chunk so bursts do not hug the quota.

OUTPUTS (in ./gdelt_results/)
------------------------------
  events_monthly.csv         Monthly event counts/scores by country & CAMEO group
  themes_monthly.csv         Monthly theme volume and tone by country & theme
  combined_monthly.csv       Wide-format merge of both signals (one row per country-month)
  anomaly_scores.csv         Z-score anomalies for all numeric indicators
  run_log.txt                Full execution log

USAGE
-----
  pip install requests pandas tqdm gdeltdoc

  The PyPI package ``gdeltdoc`` is the GDELT 2.0 Doc API client from
  https://github.com/alex9smith/gdelt-doc-api — this script already uses it
  (``Filters``, ``GdeltDoc``) for Part 2. To refresh from PyPI or install the
  latest main branch: ``pip install "gdeltdoc>=1.12" --upgrade`` or
  ``pip install git+https://github.com/alex9smith/gdelt-doc-api.git``.
  That does not bypass API rate limits; it is the same HTTP API.

  # Full run (2018 -> present, all 12 countries, monthly outputs):
  python gdelt_fetch.py

  # Quick test — recent 3 months, extrapolation countries only:
  python gdelt_fetch.py --mode recent --countries Ethiopia Iran Cuba Myanmar Guatemala

  # Historical events only (no DOC API), custom date range:
  python gdelt_fetch.py --mode historical --start-year 2020 --end-year 2023

  # Skip already-downloaded raw files (resume interrupted run):
  python gdelt_fetch.py --resume

RUNTIME ESTIMATE
----------------
  --mode recent  : ~5–10 minutes
  --mode historical (~8+ years from 2018, daily sample + DOC API): often several hours
    (downloads ~3,650 zip files × ~2MB each; ~7GB total bandwidth)
    Use --resume to continue after interruption.
"""

import argparse
import csv
import io
import logging
import os
import pickle
import random
import sys
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

# Transient TCP / TLS / timeout failures — use longer backoff (connect timeout is separate from read).
def _doc_is_network_transient(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    )

# ── Attempt gdeltdoc import (DOC API only) ────────────────────────────────────
_MISSING_RATE_LIMIT_TYPE = type("_MissingRL", (), {})

try:
    from gdeltdoc import Filters, GdeltDoc
    from gdeltdoc._version import version as GDELTDOC_VERSION
    from gdeltdoc.errors import RateLimitError as GDELT_RATE_LIMIT_ERROR
    from gdeltdoc.errors import raise_response_error as _gdeltdoc_raise_response_error
    from gdeltdoc.helpers import load_json as _gdeltdoc_load_json

    class GdeltDocWithTimeout(GdeltDoc):
        """
        gdeltdoc's client uses requests.get(..., timeout=None), which can hang a long
        time on connect/read. Bound both so a bad chunk fails faster and retries can run.
        """

        def __init__(
            self,
            connect_timeout: float = 20.0,
            read_timeout: float = 180.0,
            json_parsing_max_depth: int = 100,
        ) -> None:
            super().__init__(json_parsing_max_depth=json_parsing_max_depth)
            self._http_timeout = (float(connect_timeout), float(read_timeout))

        def _query(self, mode: str, query_string: str) -> dict:
            if mode not in [
                "artlist",
                "timelinevol",
                "timelinevolraw",
                "timelinetone",
                "timelinelang",
                "timelinesourcecountry",
            ]:
                raise ValueError(f"Mode {mode} not in supported API modes")
            headers = {
                "User-Agent": (
                    f"GDELT DOC Python API client {GDELTDOC_VERSION} "
                    f"(INFORM-NovelData; timeout) - https://github.com/alex9smith/gdelt-doc-api"
                )
            }
            url = (
                f"https://api.gdeltproject.org/api/v2/doc/doc"
                f"?query={query_string}&mode={mode}&format=json"
            )
            response = requests.get(url, headers=headers, timeout=self._http_timeout)
            _gdeltdoc_raise_response_error(response=response)
            ct = response.headers.get("content-type", "")
            if "text/html" in ct.lower():
                raise ValueError(
                    f"The query was not valid. The API error message was: {response.text.strip()}"
                )
            return _gdeltdoc_load_json(response.content, self.max_depth_json_parsing)

    GDELTDOC_AVAILABLE = True
except ImportError:
    GDELTDOC_AVAILABLE = False
    GDELT_RATE_LIMIT_ERROR = _MISSING_RATE_LIMIT_TYPE
    GdeltDocWithTimeout = None  # type: ignore[misc, assignment]

# ── Configuration ─────────────────────────────────────────────────────────────

GDELT_BASE_URL = "http://data.gdeltproject.org/gdeltv2"
REQUEST_DELAY  = 0.5   # seconds between raw file downloads
DOC_API_DELAY  = 12.0  # default seconds between DOC calls; combined with min interval below
# Official guidance is ~1 request / 5s; stay under with margin (see GDELT blog on QPS vs 429).
DOC_API_MIN_INTERVAL = 6.0  # floor: never send DOC requests closer than this (plus jitter)
DOC_API_JITTER_MAX = 2.0    # random extra delay per gap to avoid synchronized retry bursts
DOC_API_CHUNK_COOLDOWN = 3.0  # extra pause after each successful chunk (2 HTTP calls)
DOC_API_RETRIES = 8    # attempts per chunk (rate limits need patience)
DOC_API_RETRY_BACKOFF = 45  # base seconds between retries (scaled by attempt)
DOC_API_RATE_LIMIT_MIN_WAIT = 180  # minimum extra sleep on HTTP 429
DOC_API_RL_EXTRA_NEXT_GAP = 12.0  # one-shot extra gap after a 429 before next attempt
# Connect and read are independent in requests; a low connect timeout causes failures
# while waiting on TLS/TCP even when read-timeout is high.
DOC_API_CONNECT_TIMEOUT = 90.0   # seconds (TCP + TLS handshake to api.gdeltproject.org)
DOC_API_READ_TIMEOUT = 180.0     # seconds (read); large historical DOC queries can be slow
THEMES_STATE_FILE = "_themes_doc_state.pkl"  # resume payload for Part 2

# ── Priority countries ─────────────────────────────────────────────────────────
# GDELT uses FIPS 2-letter country codes (different from ISO 3166-1 alpha-2).
# ActionGeo_CountryCode in GDELT events uses FIPS.
# DOC API country filter also uses FIPS.

COUNTRIES = {
    # Extrapolation targets
    "Ethiopia":    {"fips": "ET", "iso2": "ET", "priority": 1, "group": "extrapolation"},
    "Iran":        {"fips": "IR", "iso2": "IR", "priority": 2, "group": "extrapolation"},
    "Cuba":        {"fips": "CU", "iso2": "CU", "priority": 3, "group": "extrapolation"},
    "Myanmar":     {"fips": "BM", "iso2": "MM", "priority": 4, "group": "extrapolation"},  # FIPS BM ≠ ISO MM
    "Guatemala":   {"fips": "GT", "iso2": "GT", "priority": 5, "group": "extrapolation"},
    # Training / validation
    "Bangladesh":  {"fips": "BG", "iso2": "BD", "priority": 6,  "group": "training"},
    "Lebanon":     {"fips": "LE", "iso2": "LB", "priority": 7,  "group": "training"},
    "Yemen":       {"fips": "YM", "iso2": "YE", "priority": 8,  "group": "training"},
    "South Sudan": {"fips": "OD", "iso2": "SS", "priority": 9,  "group": "training"},
    "Malawi":      {"fips": "MI", "iso2": "MW", "priority": 10, "group": "training"},
    "Mozambique":  {"fips": "MZ", "iso2": "MZ", "priority": 11, "group": "training"},
    "Kenya":       {"fips": "KE", "iso2": "KE", "priority": 12, "group": "training"},
    "Afghanistan": {"fips": "AF", "iso2": "AF", "priority": 13, "group": "extrapolation"},
}

# Build lookup sets for fast filtering of raw event files
FIPS_CODES = {v["fips"] for v in COUNTRIES.values()}
FIPS_TO_NAME = {v["fips"]: k for k, v in COUNTRIES.items()}

# ── CAMEO event code groups ────────────────────────────────────────────────────
# Root codes: GDELT EventRootCode is the 2-digit prefix of the full CAMEO code.
# We group by humanitarian-analysis-relevant categories.
CAMEO_GROUPS = {
    "conflict_assault":     {"14", "18"},   # Protest + Physical assault
    "armed_violence":       {"19", "20"},   # Use conventional forces, engage in unconventional mass violence
    "humanitarian_aid":     {"07", "08"},   # Provide aid, yield
    "diplomatic_express":   {"01", "02"},   # Make statement, appeal
    "protest":              {"14"},         # Protest (subset for standalone signal)
    "coerce":               {"17"},         # Coerce
    "sanctions_embargo":    {"16"},         # Impose embargo, sanctions
}

# ── GKG themes for DOC API ─────────────────────────────────────────────────────
# DOC API theme filter + humanitarian domain mapping for INFORM SI alignment.
THEMES = {
    "food_security":      "FOOD_SECURITY",
    "famine":             "FAMINE",
    "humanitarian_crisis":"HUMANITARIAN_CRISIS",
    "displacement":       "REFUGEES",           # GDELT REFUGEES covers both
    "natural_disaster":   "NATURAL_DISASTER",
    "economic_crisis":    "ECON_DEBT",
    "food_prices":        "FOOD_PRICE",
    "conflict_violence":  "ARMEDCONFLICT",
    "protest_civil":      "PROTEST",
}

# Substrings for bulk GKG V2Themes (gkg_bulk_themes.py); see LOOKUP-GKGTHEMES.TXT.
THEMES_GKG_SUBSTRINGS: dict[str, tuple[str, ...]] = {
    "food_security": ("FOOD_SECURITY",),
    "famine": ("FAMINE",),
    "humanitarian_crisis": ("HUMANITARIAN_CRISIS", "SELF_IDENTIFIED_HUMANITARIAN_CRISIS"),
    "displacement": ("REFUGEES", "DISPLACED"),
    "natural_disaster": ("NATURAL_DISASTER",),
    "economic_crisis": (
        "ECON_DEBT",
        "ECON_INFLATION",
        "ECON_BANKRUPTCY",
        "WB_1104_MACROECONOMIC_VULNERABILITY_AND_DEBT",
        "EPU_CATS_SOVEREIGN_DEBT_CURRENCY_CRISES",
    ),
    "food_prices": ("FOOD_PRICE", "TAX_FOODSTAPLES", "TAX_ECON_PRICE"),
    "conflict_violence": ("ARMEDCONFLICT",),
    "protest_civil": ("PROTEST",),
}

# ── GDELT 2.0 Events column schema (tab-separated, no header) ─────────────────
# Only the columns we actually use are mapped; the rest are ignored.
# Full schema: http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf
GDELT_COLS = {
    0:  "GlobalEventID",
    1:  "Day",               # YYYYMMDD
    2:  "MonthYear",         # YYYYMM
    3:  "Year",
    4:  "FractionDate",
    5:  "Actor1Code",
    6:  "Actor1Name",
    7:  "Actor1CountryCode",
    14: "Actor2Code",
    15: "Actor2Name",
    16: "Actor2CountryCode",
    26: "EventCode",         # Full CAMEO code
    27: "EventBaseCode",     # Root CAMEO code (2 digits)
    28: "EventRootCode",
    29: "QuadClass",         # 1=Verbal Coop, 2=Material Coop, 3=Verbal Conflict, 4=Material Conflict
    30: "GoldsteinScale",    # -10 to +10; negative = destabilizing
    31: "NumMentions",
    32: "NumSources",
    33: "NumArticles",
    34: "AvgTone",           # Negative = more negative coverage
    35: "Actor1Geo_Type",
    36: "Actor1Geo_FullName",
    37: "Actor1Geo_CountryCode",  # FIPS
    44: "Actor2Geo_Type",
    45: "Actor2Geo_FullName",
    46: "Actor2Geo_CountryCode",  # FIPS
    51: "ActionGeo_Type",
    52: "ActionGeo_FullName",
    53: "ActionGeo_CountryCode",  # FIPS — PRIMARY country filter
    60: "SOURCEURL",
}
USED_COLS = sorted(GDELT_COLS.keys())


# ── Logging setup ──────────────────────────────────────────────────────────────


class _FlushingStreamHandler(logging.StreamHandler):
    """
    When stdout is redirected to a file (e.g. overnight `python ... >> log.txt`),
    the stream is block-buffered and log lines may not appear for hours. Flush
    after every record so tail -f and background logs stay readable.
    """

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except OSError:
            pass


class _FlushingFileHandler(logging.FileHandler):
    """Ensure run_log.txt is visible immediately when tailing (network drives, etc.)."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except OSError:
            pass


def setup_logging(output_dir: str) -> logging.Logger:
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "run_log.txt")
    stream_out = (
        io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "buffer")
        else sys.stdout
    )
    try:
        stream_out.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError, TypeError):
        pass
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = _FlushingFileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = _FlushingStreamHandler(stream_out)
    stream_handler.setFormatter(fmt)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, stream_handler],
        force=True,
    )
    return logging.getLogger(__name__)


# ── Raw event file helpers ─────────────────────────────────────────────────────

def build_daily_urls(start_date: date, end_date: date) -> list[tuple[date, str]]:
    """
    Build a list of (date, url) tuples for one GDELT export file per day.
    We use the 000000 (midnight UTC) 15-minute file as a daily sample.
    Pattern: http://data.gdeltproject.org/gdeltv2/YYYYMMDD000000.export.CSV.zip
    """
    urls = []
    d = start_date
    while d <= end_date:
        ts = d.strftime("%Y%m%d") + "000000"
        url = f"{GDELT_BASE_URL}/{ts}.export.CSV.zip"
        urls.append((d, url))
        d += timedelta(days=1)
    return urls


def download_and_parse_event_file(
    url: str,
    session: requests.Session,
    logger: logging.Logger,
) -> list[dict] | None:
    """
    Download a GDELT 2.0 export ZIP, parse only the columns we need,
    and return a list of dicts filtered to our priority countries.
    Returns None on network/parse error.
    """
    try:
        resp = session.get(url, timeout=60, stream=True)
        if resp.status_code == 404:
            logger.debug(f"  404 (no file for this date): {url}")
            return []
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            # The ZIP contains one CSV file
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as f:
                reader = csv.reader(
                    io.TextIOWrapper(f, encoding="utf-8", errors="replace"),
                    delimiter="\t"
                )
                rows = []
                for raw_row in reader:
                    if len(raw_row) < 54:
                        continue
                    # Fast country pre-filter before building dict
                    try:
                        action_country = raw_row[53]
                    except IndexError:
                        continue
                    if action_country not in FIPS_CODES:
                        continue

                    # Build partial dict with only the columns we need
                    row = {}
                    for idx in USED_COLS:
                        try:
                            row[GDELT_COLS[idx]] = raw_row[idx]
                        except IndexError:
                            row[GDELT_COLS[idx]] = ""
                    rows.append(row)
                return rows

    except zipfile.BadZipFile:
        logger.warning(f"  Bad ZIP: {url}")
        return None
    except requests.RequestException as e:
        logger.warning(f"  Download error: {url} - {e}")
        return None


def cameo_to_group(event_root_code: str) -> str | None:
    """Return the CAMEO group name for a given root code, or None if not tracked."""
    for group, codes in CAMEO_GROUPS.items():
        if event_root_code in codes:
            return group
    return None


def aggregate_events_to_monthly(
    all_rows: list[dict],
) -> dict[tuple[str, str, str], dict]:
    """
    Aggregate raw event rows into monthly stats.
    Key: (country_name, year_month "YYYY-MM", cameo_group)
    Value: {event_count, goldstein_sum, tone_sum, mentions_sum, sources_sum, n_goldstein, n_tone}
    """
    agg = defaultdict(lambda: {
        "event_count": 0,
        "goldstein_sum": 0.0,
        "tone_sum": 0.0,
        "mentions_sum": 0,
        "sources_sum": 0,
        "n_goldstein": 0,
        "n_tone": 0,
    })

    for row in all_rows:
        fips = row.get("ActionGeo_CountryCode", "")
        country = FIPS_TO_NAME.get(fips)
        if not country:
            continue

        cameo_root = row.get("EventRootCode", "")
        group = cameo_to_group(cameo_root)
        if not group:
            group = "other"  # still track under 'other' for volume signal

        # Parse YYYYMM → YYYY-MM
        month_raw = row.get("MonthYear", "")
        if len(month_raw) == 6:
            year_month = f"{month_raw[:4]}-{month_raw[4:]}"
        else:
            year_month = "unknown"

        key = (country, year_month, group)
        bucket = agg[key]
        bucket["event_count"] += 1

        try:
            gs = float(row["GoldsteinScale"])
            bucket["goldstein_sum"] += gs
            bucket["n_goldstein"] += 1
        except (ValueError, KeyError):
            pass

        try:
            tone = float(row["AvgTone"])
            bucket["tone_sum"] += tone
            bucket["n_tone"] += 1
        except (ValueError, KeyError):
            pass

        try:
            bucket["mentions_sum"] += int(row["NumMentions"])
        except (ValueError, KeyError):
            pass

        try:
            bucket["sources_sum"] += int(row["NumSources"])
        except (ValueError, KeyError):
            pass

    return agg


# ── DOC API helpers ────────────────────────────────────────────────────────────


class DocApiPacer:
    """
    Enforce minimum spacing between consecutive DOC API HTTP requests across
    the whole run (vol + tone = two requests per chunk). GDELT quotas are
    sensitive to small QPS increases; jitter reduces alignment with window edges.
    """

    __slots__ = ("_last_end", "_extra_next_gap")

    def __init__(self) -> None:
        self._last_end: float | None = None
        self._extra_next_gap = 0.0

    def penalize_after_rate_limit(self) -> None:
        """Add a one-shot delay before the next request after HTTP 429."""
        self._extra_next_gap = max(self._extra_next_gap, DOC_API_RL_EXTRA_NEXT_GAP)

    def pace_before_request(self, doc_delay: float) -> None:
        jitter = random.uniform(0.0, DOC_API_JITTER_MAX)
        target_gap = (
            max(float(doc_delay), DOC_API_MIN_INTERVAL)
            + jitter
            + self._extra_next_gap
        )
        self._extra_next_gap = 0.0
        now = time.monotonic()
        if self._last_end is None:
            time.sleep(target_gap)
            return
        wait = target_gap - (now - self._last_end)
        if wait > 0:
            time.sleep(wait)

    def mark_after_request(self) -> None:
        self._last_end = time.monotonic()


def fetch_doc_api_timeline(
    theme_key: str,
    gdelt_theme: str,
    country_name: str,
    fips_code: str,
    start_date_str: str,
    end_date_str: str,
    gd: "GdeltDoc",
    logger: logging.Logger,
    doc_delay: float,
    pacer: "DocApiPacer",
    *,
    include_tone: bool = True,
) -> list[dict]:
    """
    Fetch timelinevolraw and (optionally) timelinetone for a given GKG theme + country
    over a date window. The DOC API supports max ~3 months per call.
    Raises on network / API transport errors so callers can retry.
    """
    if not GDELTDOC_AVAILABLE:
        return []

    rows: list[dict] = []
    f = Filters(
        theme=gdelt_theme,
        country=fips_code,
        start_date=start_date_str,
        end_date=end_date_str,
    )

    pacer.pace_before_request(doc_delay)
    try:
        vol_df = gd.timeline_search("timelinevolraw", f)
    finally:
        pacer.mark_after_request()
    if vol_df is not None and not vol_df.empty:
        vol_df["year_month"] = vol_df["datetime"].dt.strftime("%Y-%m")
        vol_monthly = (
            vol_df.groupby("year_month")
            .agg(articles=("Article Count", "sum"), total=("All Articles", "sum"))
            .reset_index()
        )
    else:
        vol_monthly = pd.DataFrame(columns=["year_month", "articles", "total"])

    if include_tone:
        pacer.pace_before_request(doc_delay)
        try:
            tone_df = gd.timeline_search("timelinetone", f)
        finally:
            pacer.mark_after_request()
        if tone_df is not None and not tone_df.empty:
            tone_df["year_month"] = tone_df["datetime"].dt.strftime("%Y-%m")
            tone_monthly = (
                tone_df.groupby("year_month")["Average Tone"]
                .mean()
                .reset_index()
                .rename(columns={"Average Tone": "avg_tone"})
            )
        else:
            tone_monthly = pd.DataFrame(columns=["year_month", "avg_tone"])
    else:
        tone_monthly = pd.DataFrame(columns=["year_month", "avg_tone"])

    merged = vol_monthly.merge(tone_monthly, on="year_month", how="outer")
    for _, mrow in merged.iterrows():
        rows.append({
            "country": country_name,
            "theme_key": theme_key,
            "year_month": mrow["year_month"],
            "articles": mrow.get("articles", 0),
            "total_articles_gdelt": mrow.get("total", 0),
            "pct_coverage": (
                round(mrow.get("articles", 0) / mrow.get("total", 1) * 100, 4)
                if mrow.get("total", 0) > 0 else 0.0
            ),
            "avg_tone": mrow.get("avg_tone", None),
        })

    return rows


def fetch_doc_api_timeline_retry(
    theme_key: str,
    gdelt_theme: str,
    country_name: str,
    fips_code: str,
    start_date_str: str,
    end_date_str: str,
    gd: "GdeltDoc",
    logger: logging.Logger,
    doc_delay: float,
    pacer: "DocApiPacer",
    max_attempts: int,
    backoff_base: float,
    *,
    subchunk_on_failure: bool,
    volume_only_on_failure: bool,
    stop_on_error: bool,
    doc_failure_log: list[tuple[str, str, str, str]] | None,
) -> list[dict]:
    """
    Call fetch_doc_api_timeline with retries. On total failure: optionally split
    the window into single-calendar-month calls, then optionally return volume-only
    rows (avg_tone empty) so the run can progress.
    """
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fetch_doc_api_timeline(
                theme_key,
                gdelt_theme,
                country_name,
                fips_code,
                start_date_str,
                end_date_str,
                gd,
                logger,
                doc_delay,
                pacer,
                include_tone=True,
            )
        except Exception as e:
            last_err = e
            is_rl = GDELT_RATE_LIMIT_ERROR is not _MISSING_RATE_LIMIT_TYPE and isinstance(
                e, GDELT_RATE_LIMIT_ERROR
            )
            if is_rl:
                pacer.penalize_after_rate_limit()
            logger.warning(
                "  DOC API attempt %d/%d failed [%s] %s %s->%s: %s%s",
                attempt,
                max_attempts,
                theme_key,
                country_name,
                start_date_str,
                end_date_str,
                e,
                " (rate limit)" if is_rl else "",
            )
            if attempt < max_attempts:
                if is_rl:
                    wait = max(
                        DOC_API_RATE_LIMIT_MIN_WAIT,
                        backoff_base * (attempt + 3) * 2,
                    )
                else:
                    wait = backoff_base * attempt
                if _doc_is_network_transient(e):
                    wait = max(wait, 90.0)
                logger.info("  ... sleeping %ds before retry", int(wait))
                time.sleep(wait)
    assert last_err is not None

    months = iter_calendar_month_ranges(start_date_str, end_date_str)
    if subchunk_on_failure and len(months) > 1:
        logger.warning(
            "  DOC API: splitting into %d calendar-month sub-window(s) [%s] %s %s->%s",
            len(months),
            theme_key,
            country_name,
            start_date_str,
            end_date_str,
        )
        acc: list[dict] = []
        for ms, me in months:
            acc.extend(
                fetch_doc_api_timeline_retry(
                    theme_key,
                    gdelt_theme,
                    country_name,
                    fips_code,
                    ms,
                    me,
                    gd,
                    logger,
                    doc_delay,
                    pacer,
                    max_attempts,
                    backoff_base,
                    subchunk_on_failure=False,
                    volume_only_on_failure=volume_only_on_failure,
                    stop_on_error=stop_on_error,
                    doc_failure_log=doc_failure_log,
                )
            )
        return acc

    if volume_only_on_failure:
        logger.warning(
            "  DOC API: volume-only fallback (no tone) [%s] %s %s->%s",
            theme_key,
            country_name,
            start_date_str,
            end_date_str,
        )
        try:
            return fetch_doc_api_timeline(
                theme_key,
                gdelt_theme,
                country_name,
                fips_code,
                start_date_str,
                end_date_str,
                gd,
                logger,
                doc_delay,
                pacer,
                include_tone=False,
            )
        except Exception as e2:
            logger.error(
                "  DOC API volume-only also failed [%s] %s %s->%s: %s",
                theme_key,
                country_name,
                start_date_str,
                end_date_str,
                e2,
            )
            if stop_on_error:
                raise last_err from e2
            fk = (country_name, theme_key, start_date_str, end_date_str)
            if doc_failure_log is not None:
                doc_failure_log.append(fk)
            logger.warning(
                "  DOC API: skipping chunk with 0 rows (network/API failure); "
                "chunk marked done — use --resume to retry, or increase "
                "--doc-connect-timeout / --doc-read-timeout [%s] %s %s->%s",
                theme_key,
                country_name,
                start_date_str,
                end_date_str,
            )
            return []

    logger.error(
        "  DOC API giving up [%s] %s %s->%s - fix: wait and re-run with --resume, "
        "or try --doc-subchunk-on-failure / --doc-volume-only-on-failure / --doc-chunk-months 1",
        theme_key,
        country_name,
        start_date_str,
        end_date_str,
    )
    if stop_on_error:
        raise last_err
    fk = (country_name, theme_key, start_date_str, end_date_str)
    if doc_failure_log is not None:
        doc_failure_log.append(fk)
    logger.warning(
        "  DOC API: skipping chunk with 0 rows; marked done. Re-run with --resume to retry [%s] %s %s->%s",
        theme_key,
        country_name,
        start_date_str,
        end_date_str,
    )
    return []


def chunk_date_range(
    start_year: int, start_month: int,
    end_year: int, end_month: int,
    chunk_months: int = 3,
) -> list[tuple[str, str]]:
    """
    Split a date range into chunks of `chunk_months` months for DOC API calls.
    Returns list of (start_date_str, end_date_str) in YYYY-MM-DD format.
    """
    chunks = []
    sy, sm = start_year, start_month
    ey, em = end_year, end_month

    while (sy, sm) <= (ey, em):
        # Chunk end
        ce_month = sm + chunk_months - 1
        ce_year = sy + (ce_month - 1) // 12
        ce_month = (ce_month - 1) % 12 + 1
        # Don't exceed overall end
        if (ce_year, ce_month) > (ey, em):
            ce_year, ce_month = ey, em
        # Last day of ce_month
        if ce_month == 12:
            last_day = 31
        else:
            last_day = (date(ce_year, ce_month + 1, 1) - timedelta(days=1)).day
        start_str = f"{sy:04d}-{sm:02d}-01"
        end_str   = f"{ce_year:04d}-{ce_month:02d}-{last_day:02d}"
        chunks.append((start_str, end_str))
        # Advance
        sm = ce_month + 1
        if sm > 12:
            sm = 1
            sy = ce_year + 1
        else:
            sy = ce_year

    return chunks


def iter_calendar_month_ranges(start_str: str, end_str: str) -> list[tuple[str, str]]:
    """
    Split [start_str, end_str] into contiguous calendar-month segments (inclusive).
    Used to retry a failed multi-month DOC window as smaller API calls.
    """
    s = datetime.strptime(start_str, "%Y-%m-%d").date()
    e = datetime.strptime(end_str, "%Y-%m-%d").date()
    if s > e:
        return []
    out: list[tuple[str, str]] = []
    y, m = s.year, s.month
    while date(y, m, 1) <= e:
        first = date(y, m, 1)
        if m == 12:
            last = date(y, 12, 31)
        else:
            last = date(y, m + 1, 1) - timedelta(days=1)
        seg_s = max(s, first)
        seg_e = min(e, last)
        if seg_s <= seg_e:
            out.append((
                seg_s.strftime("%Y-%m-%d"),
                seg_e.strftime("%Y-%m-%d"),
            ))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def clip_doc_chunk_dates(start_str: str, end_str: str) -> tuple[str, str] | None:
    """
    Clip chunk to data available through yesterday. Return None if the chunk
    is entirely in the future (skip).
    """
    yday = date.today() - timedelta(days=1)
    s = datetime.strptime(start_str, "%Y-%m-%d").date()
    e = datetime.strptime(end_str, "%Y-%m-%d").date()
    if s > yday:
        return None
    e = min(e, yday)
    if s > e:
        return None
    return s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")


def filter_events_year_month(
    df: pd.DataFrame, start_year: int, end_year: int
) -> pd.DataFrame:
    """Remove bad GDELT MonthYear values and restrict to the configured year span."""
    if df.empty or "year_month" not in df.columns:
        return df
    out = df.loc[
        df["year_month"].astype(str).str.match(r"^\d{4}-\d{2}$", na=False)
    ].copy()
    ym = out["year_month"].astype(str)
    lo, hi = f"{start_year:04d}-01", f"{end_year:04d}-12"
    return out.loc[(ym >= lo) & (ym <= hi)]


# ── Anomaly scoring ────────────────────────────────────────────────────────────

def compute_zscore_anomalies(df: pd.DataFrame, value_col: str) -> pd.Series:
    """
    Rolling 12-month z-score anomaly for a time series.
    Falls back to global z-score if fewer than 13 observations.
    """
    s = df[value_col].fillna(0)
    if len(s) < 13:
        mu, sigma = s.mean(), s.std()
        if sigma == 0:
            return pd.Series(0.0, index=s.index)
        return (s - mu) / sigma
    rm = s.rolling(12, min_periods=6).mean()
    rs = s.rolling(12, min_periods=6).std().replace(0, float("nan"))
    return (s - rm) / rs


# ── Main orchestrator ──────────────────────────────────────────────────────────

def run(
    mode: str,
    start_year: int,
    end_year: int,
    countries_filter: list[str] | None,
    output_dir: str,
    resume: bool,
    skip_events: bool,
    skip_themes: bool,
    doc_api_delay: float,
    fresh_themes: bool,
    doc_chunk_months: int,
    doc_subchunk_on_failure: bool,
    doc_volume_only_on_failure: bool,
    doc_connect_timeout: float,
    doc_read_timeout: float,
    doc_stop_on_error: bool,
):
    logger = setup_logging(output_dir)
    cache_dir = os.path.join(output_dir, "_raw_cache")
    os.makedirs(cache_dir, exist_ok=True)

    logger.info("=" * 60)
    logger.info("GDELT Fetcher - INFORM Novel Data Consultancy")
    logger.info(f"Mode: {mode} | Years: {start_year}-{end_year}")
    logger.info(f"Output: {output_dir}")
    logger.info("=" * 60)

    countries_to_run = {
        k: v for k, v in COUNTRIES.items()
        if countries_filter is None or k in countries_filter
    }
    logger.info(f"Countries: {list(countries_to_run.keys())}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "INFORM-NovelData-Research/1.0 (humanitarian research)"
    })

    # ── Part 1: Raw event files ────────────────────────────────────────────────
    events_monthly_rows = []

    if not skip_events:
        logger.info("\n-- Part 1: GDELT 2.0 raw event files --")

        if mode == "recent":
            start_dt = date.today() - timedelta(days=90)
            end_dt   = date.today() - timedelta(days=1)
        else:
            start_dt = date(start_year, 1, 1)
            end_dt   = date(end_year, 12, 31)
            if end_dt > date.today():
                end_dt = date.today() - timedelta(days=1)

        daily_urls = build_daily_urls(start_dt, end_dt)
        logger.info(f"  Date range: {start_dt} -> {end_dt} ({len(daily_urls)} daily files)")

        all_event_rows = []
        skipped = downloaded = errors = 0

        for i, (day, url) in enumerate(daily_urls, 1):
            cache_path = os.path.join(cache_dir, f"{day.strftime('%Y%m%d')}.pkl")

            if resume and os.path.exists(cache_path):
                cached = pd.read_pickle(cache_path)
                all_event_rows.extend(cached.to_dict("records"))
                skipped += 1
                continue

            if i % 50 == 0 or i == 1:
                logger.info(
                    f"  [{i}/{len(daily_urls)}] {day} "
                    f"(downloaded={downloaded}, skipped={skipped}, errors={errors})"
                )

            time.sleep(REQUEST_DELAY)
            rows = download_and_parse_event_file(url, session, logger)

            if rows is None:
                errors += 1
                continue
            if rows:
                all_event_rows.extend(rows)
                # Cache the day's filtered rows
                pd.DataFrame(rows).to_pickle(cache_path)
            downloaded += 1

        logger.info(
            f"  Done. {len(all_event_rows)} country-matched events "
            f"(downloaded={downloaded}, cached={skipped}, errors={errors})"
        )

        # Aggregate
        if all_event_rows:
            logger.info("  Aggregating to monthly stats...")
            agg = aggregate_events_to_monthly(all_event_rows)

            for (country, year_month, cameo_group), bucket in agg.items():
                if country not in countries_to_run:
                    continue
                n_gs   = bucket["n_goldstein"]
                n_tone = bucket["n_tone"]
                events_monthly_rows.append({
                    "country": country,
                    "country_group": COUNTRIES[country]["group"],
                    "year_month": year_month,
                    "cameo_group": cameo_group,
                    "event_count": bucket["event_count"],
                    "avg_goldstein": round(bucket["goldstein_sum"] / n_gs, 3) if n_gs > 0 else None,
                    "avg_tone": round(bucket["tone_sum"] / n_tone, 3) if n_tone > 0 else None,
                    "total_mentions": bucket["mentions_sum"],
                    "total_sources": bucket["sources_sum"],
                })

        events_df = pd.DataFrame(events_monthly_rows)
        events_path = os.path.join(output_dir, "events_monthly.csv")
        events_df.to_csv(events_path, index=False)
        logger.info(f"  Events saved -> {events_path} ({len(events_df)} rows)")

    else:
        logger.info("\n-- Part 1: Skipped (--skip-events) --")
        events_path = os.path.join(output_dir, "events_monthly.csv")
        if not os.path.exists(events_path):
            logger.error(
                "  Missing %s - run without --skip-events first or restore this file.",
                events_path,
            )
            events_df = pd.DataFrame()
        else:
            events_df = pd.read_csv(events_path)
            logger.info("  Loaded %d rows from %s", len(events_df), events_path)

    # ── Part 2: DOC API — themes and tone ─────────────────────────────────────
    themes_monthly_rows: list[dict] = []
    themes_path = os.path.join(output_dir, "themes_monthly.csv")
    state_path = os.path.join(output_dir, THEMES_STATE_FILE)

    if not skip_themes:
        if not GDELTDOC_AVAILABLE:
            logger.warning(
                "\n-- Part 2: gdeltdoc not installed - skipping DOC API. --\n"
                "  Install: pip install gdeltdoc  "
                "(https://github.com/alex9smith/gdelt-doc-api)"
            )
            themes_df = pd.DataFrame()
        else:
            logger.info("\n-- Part 2: GDELT DOC API - GKG themes and tone --")
            eff = max(float(doc_api_delay), DOC_API_MIN_INTERVAL)
            logger.info(
                "  doc_api_delay=%.1fs (effective floor %.1fs + up to %.1fs jitter), "
                "chunk_cooldown=%.1fs, doc_chunk_months=%d, http_timeout=(%.0fs, %.0fs connect/read), "
                "subchunk_on_failure=%s, volume_only_on_failure=%s, stop_on_error=%s, "
                "retries=%d, backoff=%ds",
                doc_api_delay,
                eff,
                DOC_API_JITTER_MAX,
                DOC_API_CHUNK_COOLDOWN,
                doc_chunk_months,
                doc_connect_timeout,
                doc_read_timeout,
                doc_subchunk_on_failure,
                doc_volume_only_on_failure,
                doc_stop_on_error,
                DOC_API_RETRIES,
                DOC_API_RETRY_BACKOFF,
            )

            if fresh_themes:
                for p in (state_path, themes_path):
                    if os.path.exists(p):
                        os.remove(p)
                        logger.info("  Cleared %s (--fresh-themes)", p)

            done_keys: set[tuple[str, str, str, str]] = set()
            failed_doc_keys: set[tuple[str, str, str, str]] = set()
            if resume and os.path.exists(state_path):
                with open(state_path, "rb") as sf:
                    st = pickle.load(sf)
                themes_monthly_rows = st.get("rows", [])
                done_keys = {tuple(x) for x in st.get("done", [])}
                failed_doc_keys = {tuple(x) for x in st.get("failed_doc", [])}
                logger.info(
                    "  Resumed DOC state: %d chunks done, %d rows buffered, %d failed skips on file",
                    len(done_keys),
                    len(themes_monthly_rows),
                    len(failed_doc_keys),
                )

            def save_theme_state() -> None:
                payload = {
                    "rows": themes_monthly_rows,
                    "done": [list(k) for k in done_keys],
                    "failed_doc": [list(k) for k in sorted(failed_doc_keys)],
                }
                with open(state_path, "wb") as sf:
                    pickle.dump(payload, sf)

            def flush_themes_csv() -> None:
                tdf = pd.DataFrame(themes_monthly_rows)
                if not tdf.empty:
                    tdf = tdf.drop_duplicates(
                        subset=["country", "theme_key", "year_month"],
                        keep="last",
                    )
                    tdf["country_group"] = tdf["country"].map(
                        lambda c: COUNTRIES.get(c, {}).get("group", "unknown")
                    )
                tdf.to_csv(themes_path, index=False)

            if GdeltDocWithTimeout is not None:
                gd = GdeltDocWithTimeout(
                    connect_timeout=doc_connect_timeout,
                    read_timeout=doc_read_timeout,
                )
            else:
                gd = GdeltDoc()
            doc_pacer = DocApiPacer()
            doc_failure_log: list[tuple[str, str, str, str]] = []

            if mode == "recent":
                today = date.today()
                chunks = [(
                    (today - timedelta(days=90)).strftime("%Y-%m-%d"),
                    today.strftime("%Y-%m-%d"),
                )]
            else:
                chunks = chunk_date_range(
                    start_year, 1, end_year, 12, chunk_months=doc_chunk_months
                )

            # Count schedulable chunks (after date clip) for progress
            schedulable = 0
            for _cn, _ci in countries_to_run.items():
                for _tk in THEMES:
                    for s0, e0 in chunks:
                        clipped = clip_doc_chunk_dates(s0, e0)
                        if clipped:
                            schedulable += 1
            pending = max(0, schedulable - len(done_keys))
            logger.info(
                "  DOC API plan: %d chunks total, %d already done, ~%d to fetch",
                schedulable,
                len(done_keys),
                pending,
            )
            fetch_i = 0

            for country_name, country_info in countries_to_run.items():
                fips = country_info["fips"]
                for theme_key, gdelt_theme in THEMES.items():
                    for start_str, end_str in chunks:
                        clipped = clip_doc_chunk_dates(start_str, end_str)
                        if not clipped:
                            continue
                        start_str, end_str = clipped
                        chunk_key = (country_name, theme_key, start_str, end_str)
                        if chunk_key in done_keys:
                            continue
                        fetch_i += 1
                        if fetch_i == 1 or fetch_i % 25 == 0:
                            logger.info(
                                "  DOC API fetch #%d: %s / %s / %s (%d/%d chunks complete)",
                                fetch_i,
                                country_name,
                                theme_key,
                                start_str[:7],
                                len(done_keys),
                                schedulable,
                            )
                        doc_failure_log.clear()
                        rows = fetch_doc_api_timeline_retry(
                            theme_key,
                            gdelt_theme,
                            country_name,
                            fips,
                            start_str,
                            end_str,
                            gd,
                            logger,
                            doc_api_delay,
                            doc_pacer,
                            DOC_API_RETRIES,
                            DOC_API_RETRY_BACKOFF,
                            subchunk_on_failure=doc_subchunk_on_failure,
                            volume_only_on_failure=doc_volume_only_on_failure,
                            stop_on_error=doc_stop_on_error,
                            doc_failure_log=doc_failure_log,
                        )
                        themes_monthly_rows.extend(rows)
                        done_keys.add(chunk_key)
                        if not rows and doc_failure_log:
                            failed_doc_keys.add(chunk_key)
                        save_theme_state()
                        flush_themes_csv()
                        time.sleep(
                            DOC_API_CHUNK_COOLDOWN
                            + random.uniform(0.0, DOC_API_JITTER_MAX)
                        )

            themes_df = pd.DataFrame(themes_monthly_rows)
            if not themes_df.empty:
                themes_df = themes_df.drop_duplicates(
                    subset=["country", "theme_key", "year_month"],
                    keep="last",
                )
                themes_df["country_group"] = themes_df["country"].map(
                    lambda c: COUNTRIES.get(c, {}).get("group", "unknown")
                )
            themes_df.to_csv(themes_path, index=False)
            logger.info("  Themes saved -> %s (%d rows)", themes_path, len(themes_df))
            if failed_doc_keys:
                logger.info(
                    "  DOC API chunks with no rows after errors (see failed_doc in %s): %d",
                    THEMES_STATE_FILE,
                    len(failed_doc_keys),
                )
    else:
        logger.info("\n-- Part 2: Skipped (--skip-themes) --")
        themes_df = pd.DataFrame()

    # ── Part 3: Combined wide-format output ───────────────────────────────────
    logger.info("\n-- Part 3: Building combined output --")

    combined_parts = []

    if not events_df.empty:
        _n_before = len(events_df)
        events_df = filter_events_year_month(events_df, start_year, end_year)
        logger.info(
            "  Events panel: %d rows after YYYY-MM filter (%d-%d); dropped %d junk/out-of-range",
            len(events_df),
            start_year,
            end_year,
            _n_before - len(events_df),
        )

    if not events_df.empty:
        # Pivot CAMEO groups into columns
        conflict_df = events_df[events_df["cameo_group"] == "armed_violence"].copy()
        protest_df  = events_df[events_df["cameo_group"] == "protest"].copy()
        all_df      = events_df.groupby(["country", "country_group", "year_month"]).agg(
            total_events=("event_count", "sum"),
            overall_avg_goldstein=("avg_goldstein", "mean"),
            overall_avg_tone=("avg_tone", "mean"),
            total_mentions=("total_mentions", "sum"),
            total_sources=("total_sources", "sum"),
        ).reset_index()
        conflict_pivot = conflict_df[
            ["country", "year_month", "event_count", "avg_goldstein", "avg_tone"]
        ].rename(columns={
            "event_count": "armed_violence_events",
            "avg_goldstein": "armed_violence_goldstein",
            "avg_tone": "armed_violence_tone",
        })
        protest_pivot = protest_df[
            ["country", "year_month", "event_count"]
        ].rename(columns={"event_count": "protest_events"})

        combined_events = all_df.merge(conflict_pivot, on=["country", "year_month"], how="left")
        combined_events = combined_events.merge(protest_pivot, on=["country", "year_month"], how="left")
        combined_parts.append(combined_events)

    if not themes_df.empty:
        # Pivot themes into columns
        themes_pivot = themes_df.pivot_table(
            index=["country", "year_month"],
            columns="theme_key",
            values=["articles", "avg_tone"],
            aggfunc="first",
        )
        themes_pivot.columns = [f"{col[1]}_{col[0]}" for col in themes_pivot.columns]
        themes_pivot = themes_pivot.reset_index()
        combined_parts.append(themes_pivot)

    if combined_parts:
        if len(combined_parts) == 2:
            combined_df = combined_parts[0].merge(
                combined_parts[1], on=["country", "year_month"], how="outer"
            )
        else:
            combined_df = combined_parts[0]
        combined_df = combined_df.sort_values(["country", "year_month"])
        combined_path = os.path.join(output_dir, "combined_monthly.csv")
        combined_df.to_csv(combined_path, index=False)
        logger.info(f"  Combined saved -> {combined_path} ({len(combined_df)} rows)")

        # ── Part 4: Anomaly scores ─────────────────────────────────────────────
        logger.info("\n-- Part 4: Computing anomaly scores --")
        numeric_cols = combined_df.select_dtypes(include="number").columns.tolist()
        anomaly_rows = []

        for country, grp in combined_df.groupby("country"):
            grp_sorted = grp.sort_values("year_month").reset_index(drop=True)
            for col in numeric_cols:
                z = compute_zscore_anomalies(grp_sorted, col)
                for idx, (_, row) in enumerate(grp_sorted.iterrows()):
                    anomaly_rows.append({
                        "country": country,
                        "year_month": row["year_month"],
                        "indicator": col,
                        "value": row[col],
                        "z_score": round(z.iloc[idx], 3) if pd.notna(z.iloc[idx]) else None,
                        "is_anomaly": bool(abs(z.iloc[idx]) > 2.0) if pd.notna(z.iloc[idx]) else False,
                    })

        anomaly_df = pd.DataFrame(anomaly_rows)
        anomaly_path = os.path.join(output_dir, "anomaly_scores.csv")
        anomaly_df.to_csv(anomaly_path, index=False)
        logger.info(f"  Anomalies saved -> {anomaly_path}")

        # Summary of top anomalies
        top = (
            anomaly_df[anomaly_df["is_anomaly"]]
            .sort_values("z_score", key=abs, ascending=False)
            .head(20)
        )
        if not top.empty:
            logger.info("\n-- Top anomaly signals (|z| > 2.0) --")
            for _, r in top.iterrows():
                logger.info(
                    f"  {r['country']:15s} {r['year_month']}  "
                    f"z={r['z_score']:+6.2f}  {r['indicator']}"
                )

    logger.info("\n-- Run complete --")
    logger.info(f"All outputs in: {output_dir}/")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch GDELT data for INFORM Novel Data Consultancy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick test: recent 3 months, extrapolation countries only
  python gdelt_fetch.py --mode recent --countries Ethiopia Iran Cuba Myanmar Guatemala

  # Full historical run (events only — faster):
  python gdelt_fetch.py --mode historical --skip-themes

  # Full run, specific years:
  python gdelt_fetch.py --mode historical --start-year 2020 --end-year 2024

  # Resume an interrupted historical run:
  python gdelt_fetch.py --mode historical --resume

  # Themes-only retry (events already in events_monthly.csv), resume + slower API pace:
  python gdelt_fetch.py --skip-events --resume --fresh-themes --doc-api-delay 15

  # Stubborn DOC API (timeouts/429): smaller windows + monthly split + volume-only last resort:
  python gdelt_fetch.py --skip-events --resume --doc-api-delay 60 --doc-chunk-months 1 --doc-subchunk-on-failure --doc-volume-only-on-failure
        """
    )
    parser.add_argument(
        "--mode",
        choices=["recent", "historical"],
        default="historical",
        help=(
            "'recent' = last 3 months (events + DOC API); "
            "'historical' = raw event files from --start-year through --end-year "
            "plus DOC API theme timelines in 3-month chunks (default)"
        ),
    )
    parser.add_argument(
        "--start-year", type=int, default=2018,
        help="Start year for historical mode (default: 2018)"
    )
    parser.add_argument(
        "--end-year", type=int, default=datetime.now().year,
        help="End year for historical mode (default: current year)"
    )
    parser.add_argument(
        "--countries", nargs="+", default=None, metavar="COUNTRY",
        help="Limit to specific countries (e.g. Ethiopia Iran Cuba)"
    )
    parser.add_argument(
        "--output-dir", default="./gdelt_results", metavar="DIR",
        help="Output directory (default: ./gdelt_results)"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip already-cached daily files (resume interrupted run)"
    )
    parser.add_argument(
        "--skip-events", action="store_true",
        help="Skip raw event file download (Part 1)"
    )
    parser.add_argument(
        "--skip-themes", action="store_true",
        help="Skip DOC API theme/tone fetching (Part 2)"
    )
    parser.add_argument(
        "--doc-api-delay",
        type=float,
        default=DOC_API_DELAY,
        metavar="SEC",
        help=(
            "Target gap between DOC API HTTP requests (vol and tone are separate); "
            f"default {DOC_API_DELAY}. A minimum of {DOC_API_MIN_INTERVAL}s is always enforced, "
            "with jitter and a short cooldown after each chunk (see module docstring). "
            "Increase if you still see HTTP 429."
        ),
    )
    parser.add_argument(
        "--fresh-themes",
        action="store_true",
        help=(
            "Before Part 2: delete themes_monthly.csv and _themes_doc_state.pkl "
            "so DOC API starts clean (use with --skip-events for a full theme re-pull)."
        ),
    )
    parser.add_argument(
        "--doc-chunk-months",
        type=int,
        choices=[1, 2, 3],
        default=3,
        metavar="N",
        help=(
            "DOC API date window size in months per request (default: 3). "
            "Use 1 for smaller, lighter queries when 3-month windows time out or rate-limit. "
            "If you change this vs an earlier run, --resume chunk keys may not match and some "
            "months could be fetched again."
        ),
    )
    parser.add_argument(
        "--doc-subchunk-on-failure",
        action="store_true",
        help=(
            "If a window fails after all retries, retry it as one DOC call per calendar month "
            "(more requests, but smaller payloads)."
        ),
    )
    parser.add_argument(
        "--doc-volume-only-on-failure",
        action="store_true",
        help=(
            "After retries (and optional monthly sub-chunks), keep article volume metrics "
            "but skip tone (avg_tone empty) so the run can continue past stubborn chunks."
        ),
    )
    parser.add_argument(
        "--doc-connect-timeout",
        type=float,
        default=DOC_API_CONNECT_TIMEOUT,
        metavar="SEC",
        help=(
            f"DOC API TCP/TLS connect timeout in seconds (default: {DOC_API_CONNECT_TIMEOUT}). "
            "Independent of --doc-read-timeout; raise this if logs show ConnectTimeout."
        ),
    )
    parser.add_argument(
        "--doc-read-timeout",
        type=float,
        default=DOC_API_READ_TIMEOUT,
        metavar="SEC",
        help=f"DOC API HTTP read timeout (default: {DOC_API_READ_TIMEOUT}).",
    )
    parser.add_argument(
        "--doc-stop-on-error",
        action="store_true",
        help=(
            "Abort the entire run when a DOC chunk still fails after all retries and fallbacks. "
            "Default: skip the chunk (0 rows), mark it done, log to failed_doc in state, and continue."
        ),
    )
    args = parser.parse_args()

    run(
        mode=args.mode,
        start_year=args.start_year,
        end_year=args.end_year,
        countries_filter=args.countries,
        output_dir=args.output_dir,
        resume=args.resume,
        skip_events=args.skip_events,
        skip_themes=args.skip_themes,
        doc_api_delay=args.doc_api_delay,
        fresh_themes=args.fresh_themes,
        doc_chunk_months=args.doc_chunk_months,
        doc_subchunk_on_failure=args.doc_subchunk_on_failure,
        doc_volume_only_on_failure=args.doc_volume_only_on_failure,
        doc_connect_timeout=args.doc_connect_timeout,
        doc_read_timeout=args.doc_read_timeout,
        doc_stop_on_error=args.doc_stop_on_error,
    )


if __name__ == "__main__":
    main()
