"""
wfp_food_prices_fetch.py
------------------------
Download annual **Global WFP Food Prices** CSVs from HDX
(`global-wfp-food-prices` dataset), concatenate them, and keep rows for the
same priority countries as ``iom_dtm_fetch.COUNTRIES`` (ISO3).

Source dataset: https://data.humdata.org/dataset/global-wfp-food-prices

Outputs (default ``./wfp_food_prices_results/``):
  wfp_food_prices_countries_of_interest_long.csv  — combined filtered table
  fetch_manifest.json                           — resource URLs + row counts
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from urllib.parse import quote, urlparse
import io
import urllib.error
import urllib.request
from typing import Any

import pandas as pd

from iom_dtm_fetch import COUNTRIES

HDX_PACKAGE_SHOW = "https://data.humdata.org/api/3/action/package_show"
ALLOWED_HDX_HOSTS = frozenset({"data.humdata.org", "humdata.org"})
PACKAGE_ID = "global-wfp-food-prices"
PACKAGE_ID_RE = re.compile(r"^[a-z0-9-]{1,200}$")
DEFAULT_OUT = "wfp_food_prices_results"
YEARLY_NAME_RE = re.compile(r"^Global WFP food prices (\d{4})$")
REQUEST_TIMEOUT = 300
REQUEST_PAUSE = 0.3

# HDX WFP annual CSV public schema (matches HDX exports / client reference layout).
WFP_STANDARD_COLUMNS: list[str] = [
    "countryiso3",
    "date",
    "admin1",
    "admin2",
    "market",
    "market_id",
    "latitude",
    "longitude",
    "category",
    "commodity",
    "commodity_id",
    "unit",
    "priceflag",
    "pricetype",
    "currency",
    "price",
    "usdprice",
]


def target_iso3() -> set[str]:
    return {info["iso3"] for info in COUNTRIES.values()}


def to_standard_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Drop helper columns; keep only WFP_STANDARD_COLUMNS in order (all years)."""
    missing = [c for c in WFP_STANDARD_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Input missing columns: {missing}")
    out = df[WFP_STANDARD_COLUMNS].copy()
    return out


def write_paired_exports(
    combined: pd.DataFrame,
    output_dir: str,
    logger: logging.Logger,
    panel_start: str = "2018-01-01",
    panel_end: str = "2026-12-31",
) -> None:
    """
    Write (1) full-history long CSV and (2) 2018–2026 panel CSV matching
    ``wfp_food_prices_countries_of_interest_2018_2026.csv`` layout (17 columns, no provenance).
    """
    std = to_standard_schema(combined)
    long_path = os.path.join(output_dir, "wfp_food_prices_countries_of_interest_long.csv")
    std.to_csv(long_path, index=False)
    logger.info("Wrote %s (%d rows)", long_path, len(std))

    dt = pd.to_datetime(std["date"], errors="coerce")
    mask = (dt >= pd.Timestamp(panel_start)) & (dt <= pd.Timestamp(panel_end))
    panel = std.loc[mask].reset_index(drop=True)
    panel_path = os.path.join(output_dir, "wfp_food_prices_countries_of_interest_2018_2026.csv")
    panel.to_csv(panel_path, index=False)
    logger.info("Wrote %s (%d rows)", panel_path, len(panel))


def reformat_existing_csv(input_csv: str, output_dir: str, logger: logging.Logger) -> None:
    """Re-read a prior combined CSV and emit standard long + 2018–2026 panel."""
    df = pd.read_csv(input_csv, low_memory=False)
    write_paired_exports(df, output_dir, logger)


def _validate_package_id(package_id: str) -> None:
    if not PACKAGE_ID_RE.match(package_id or ""):
        raise SystemExit("--package-id must match ^[a-z0-9-]{1,200}$ (HDX dataset name).")


def _validate_download_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in ALLOWED_HDX_HOSTS:
        raise ValueError(f"Refusing non-HDX download URL: {url!r}")


def fetch_package(package_id: str) -> dict[str, Any]:
    _validate_package_id(package_id)
    url = f"{HDX_PACKAGE_SHOW}?id={quote(package_id, safe='')}"
    with urllib.request.urlopen(url, timeout=120) as resp:
        return json.load(resp)["result"]


def yearly_csv_resources(pkg: dict) -> list[tuple[int, str, str]]:
    """Return sorted list of (year, download_url, resource_id)."""
    out: list[tuple[int, str, str]] = []
    for res in pkg.get("resources", []):
        name = (res.get("name") or "").strip()
        m = YEARLY_NAME_RE.match(name)
        if not m or (res.get("format") or "").upper() != "CSV":
            continue
        y = int(m.group(1))
        u = res.get("url") or ""
        if not u:
            continue
        try:
            _validate_download_url(u)
        except ValueError:
            continue
        rid = res.get("id") or ""
        out.append((y, u, rid))
    out.sort(key=lambda x: x[0])
    return out


def read_csv_url(url: str, logger: logging.Logger) -> pd.DataFrame:
    _validate_download_url(url)
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
            return pd.read_csv(io.BytesIO(raw), low_memory=False)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = e
            logger.warning("  read attempt %s/3 failed: %s", attempt, e)
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"Failed to read {url}: {last_err}") from last_err


def setup_logging(out_dir: str) -> logging.Logger:
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "run_log.txt")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return logging.getLogger(__name__)


def run(
    output_dir: str,
    package_id: str,
    min_year: int | None,
    max_year: int | None,
) -> None:
    _validate_package_id(package_id)
    output_dir = os.path.abspath(output_dir)
    logger = setup_logging(output_dir)
    iso3 = target_iso3()
    logger.info("Target ISO3 (%d): %s", len(iso3), sorted(iso3))

    pkg = fetch_package(package_id)
    resources = yearly_csv_resources(pkg)
    if min_year is not None:
        resources = [t for t in resources if t[0] >= min_year]
    if max_year is not None:
        resources = [t for t in resources if t[0] <= max_year]
    logger.info(
        "Annual CSV resources to fetch: %d (%s .. %s)",
        len(resources),
        resources[0][0],
        resources[-1][0],
    )

    manifest: list[dict[str, Any]] = []
    parts: list[pd.DataFrame] = []

    for year, url, rid in resources:
        logger.info("Year %s ...", year)
        time.sleep(REQUEST_PAUSE)
        df = read_csv_url(url, logger)
        if "countryiso3" not in df.columns:
            logger.error("  Missing countryiso3 column; columns=%s", list(df.columns))
            manifest.append({"year": year, "resource_id": rid, "url": url, "error": "no countryiso3"})
            continue
        n_all = len(df)
        sub = df.loc[df["countryiso3"].astype(str).str.strip().isin(iso3)].copy()
        sub["_source_year_file"] = year
        n_kept = len(sub)
        parts.append(sub)
        logger.info("  rows=%d kept=%d", n_all, n_kept)
        manifest.append({
            "year": year,
            "resource_id": rid,
            "url": url,
            "rows_total": n_all,
            "rows_countries_of_interest": n_kept,
        })

    if not parts:
        raise SystemExit("No data loaded; check package_id and filters.")

    combined = pd.concat(parts, ignore_index=True)
    sort_cols = [c for c in ("countryiso3", "date", "market_id", "commodity_id") if c in combined.columns]
    if sort_cols:
        combined = combined.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)

    write_paired_exports(combined, output_dir, logger)

    man_path = os.path.join(output_dir, "fetch_manifest.json")
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "package_id": package_id,
                "hdx_dataset": f"https://data.humdata.org/dataset/{package_id}",
                "min_year": min_year,
                "max_year": max_year,
                "countries_iso3": sorted(iso3),
                "resources": manifest,
                "combined_rows": len(combined),
                "combined_csv": "wfp_food_prices_countries_of_interest_long.csv",
                "panel_2018_2026_csv": "wfp_food_prices_countries_of_interest_2018_2026.csv",
            },
            f,
            indent=2,
        )
    logger.info("Wrote %s", man_path)


def main() -> None:
    p = argparse.ArgumentParser(description="HDX WFP global food prices → countries of interest.")
    p.add_argument("--output-dir", default=DEFAULT_OUT, help="Output directory")
    p.add_argument("--package-id", default=PACKAGE_ID, help="HDX package name / id")
    p.add_argument(
        "--min-year",
        type=int,
        default=1990,
        help="Minimum calendar year file to include (default: 1990; HDX also lists 1900).",
    )
    p.add_argument("--max-year", type=int, default=None, help="Maximum year inclusive (default: no cap)")
    p.add_argument(
        "--reformat-only",
        metavar="PATH",
        default=None,
        help="Read an existing combined CSV (with or without _source_year_file) and "
        "rewrite standard long + 2018–2026 panel; skip HDX download.",
    )
    args = p.parse_args()
    if args.reformat_only:
        out = os.path.abspath(args.output_dir)
        logger = setup_logging(out)
        reformat_existing_csv(os.path.abspath(args.reformat_only), out, logger)
        return
    run(
        output_dir=args.output_dir,
        package_id=args.package_id,
        min_year=args.min_year,
        max_year=args.max_year,
    )


if __name__ == "__main__":
    main()
