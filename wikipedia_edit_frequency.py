"""
wikipedia_edit_frequency.py
----------------------------
Scrapes Wikipedia revision (edit) frequency over the last 10 years for pages
related to food security, famine, conflict, and displacement in the priority
countries identified for the INFORM Novel Data Consultancy.

Usage:
    python wikipedia_edit_frequency.py

    # Run only specific countries:
    python wikipedia_edit_frequency.py --countries Ethiopia Cuba

    # Run only specific themes:
    python wikipedia_edit_frequency.py --themes conflict food_security

    # Custom output directory:
    python wikipedia_edit_frequency.py --output-dir ./wikipedia_results

    # Rebuild national z-scores only (no API) from a prior edit_counts_monthly.csv:
    python wikipedia_edit_frequency.py --national-only --output-dir ./wikipedia_results
    python wikipedia_edit_frequency.py --national-only --monthly-csv ./path/to/edit_counts_monthly.csv --output-dir ./wikipedia_results

Output:
    - wikipedia_results/edit_counts_monthly.csv   : monthly edit counts per page
    - wikipedia_results/edit_counts_annual.csv    : annual edit counts per page
    - wikipedia_results/page_metadata.csv         : page IDs, titles, existence flags
    - wikipedia_results/anomaly_scores.csv        : z-score anomaly per page per month
    - wikipedia_results/edit_counts_national_monthly.csv : total edits per country-month +
        national z-scores vs that country's full in-sample history
    - wikipedia_results/run_log.txt               : API call log and any errors

Dependencies:
    pip install requests pandas tqdm

Notes:
    - Uses the Wikipedia Action API (free, no key required).
    - Respects the Wikimedia rate limit: max 200 requests/sec for anon users.
      A 0.1s delay between requests keeps us well under that.
    - rvlimit=500 per request; the script auto-paginates using 'rvcontinue'.
    - All timestamps are UTC.
    - Pages that do not exist on English Wikipedia are flagged and skipped;
      the script can optionally fall back to search for the closest match.
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests


def _log_safe(msg: str) -> str:
    """Make log text safe for StreamHandler when the console uses cp1252 (Windows)."""
    enc = getattr(sys.stderr, "encoding", None) or getattr(sys.stdout, "encoding", None) or "utf-8"
    return msg.encode(enc, errors="replace").decode(enc)


# ── Configuration ─────────────────────────────────────────────────────────────

# Date range: 10 years back from today
END_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
START_DATE = f"{datetime.now(timezone.utc).year - 10}-01-01T00:00:00Z"

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
REQUEST_DELAY = 0.15   # seconds between API calls — stay under rate limits
MAX_REVISIONS_PER_REQUEST = 500  # Wikipedia API max per call

# ── Priority countries ─────────────────────────────────────────────────────────
# Training/validation: Bangladesh, Lebanon, Yemen, South Sudan, Malawi,
#                      Mozambique, Kenya
# Extrapolation (priority order): Ethiopia, Iran, Cuba, Myanmar, Guatemala, Afghanistan

COUNTRIES = {
    # Extrapolation targets (primary interest)
    "Ethiopia": {
        "iso": "ET",
        "priority": 1,
        "group": "extrapolation",
    },
    "Iran": {
        "iso": "IR",
        "priority": 2,
        "group": "extrapolation",
    },
    "Cuba": {
        "iso": "CU",
        "priority": 3,
        "group": "extrapolation",
    },
    "Myanmar": {
        "iso": "MM",
        "priority": 4,
        "group": "extrapolation",
    },
    "Guatemala": {
        "iso": "GT",
        "priority": 5,
        "group": "extrapolation",
    },
    # Training/validation countries
    "Bangladesh": {"iso": "BD", "priority": 6, "group": "training"},
    "Lebanon":    {"iso": "LB", "priority": 7, "group": "training"},
    "Yemen":      {"iso": "YE", "priority": 8, "group": "training"},
    "South Sudan":{"iso": "SS", "priority": 9, "group": "training"},
    "Malawi":     {"iso": "MW", "priority": 10, "group": "training"},
    "Mozambique": {"iso": "MZ", "priority": 11, "group": "training"},
    "Kenya":      {"iso": "KE", "priority": 12, "group": "training"},
    "Afghanistan": {"iso": "AF", "priority": 13, "group": "extrapolation"},
}

# ── Page definitions ───────────────────────────────────────────────────────────
# Format: { theme_key: { country_name: [list of Wikipedia page titles] } }
# Pages are defined per-country because Wikipedia coverage is country-specific.
# Titles are exact English Wikipedia article titles where possible.
# Where a country-specific article doesn't exist, a regional/thematic article
# is used as a fallback (marked with a comment).

PAGES = {
    "conflict": {
        "Ethiopia": [
            "Tigray War",
            "Amhara conflict (2023)",
            "Ethiopian Civil War (2018–present)",
            "Oromo Liberation Army",
        ],
        "Iran": [
            "Iran–Israel proxy conflict",
            "2022 Iranian protests",
            "Mahsa Amini protests",
            "Iran under economic sanctions",
        ],
        "Cuba": [
            "2021 Cuban protests",
            "Cuba–United States relations",
            "Political repression in Cuba",
        ],
        "Myanmar": [
            "2021 Myanmar coup d'état",
            "Myanmar civil war (2021–present)",
            "Rohingya genocide",
            "Sagaing Region offensive",
        ],
        "Guatemala": [
            "Guatemalan Civil War",
            "Crime in Guatemala",
            "Gang violence in Guatemala",
        ],
        "Bangladesh": [
            "Rohingya refugee crisis",
            "2024 Bangladeshi political crisis",
            "Jamaat-ul-Mujahideen Bangladesh",
        ],
        "Lebanon": [
            "2019–2021 Lebanese protests",
            "2023 Israel–Hamas war",
            "Hezbollah",
            "Lebanese Civil War",
        ],
        "Yemen": [
            "Yemeni civil war (2014–present)",
            "Saudi Arabian-led intervention in Yemen",
            "Houthi movement",
        ],
        "South Sudan": [
            "South Sudanese Civil War (2013–2020)",
            "South Sudanese Civil War (2016)",
            "Human rights in South Sudan",
        ],
        "Malawi": [
            "Politics of Malawi",
            "2020 Malawian presidential election",
        ],
        "Mozambique": [
            "Insurgency in Cabo Delgado",
            "Mozambican Civil War",
            "RENAMO",
        ],
        "Kenya": [
            "2022 Kenyan presidential election protests",
            "Al-Shabaab (militant group)",
            "Crime in Kenya",
        ],
        "Afghanistan": [
            "Taliban",
            "Taliban takeover of Afghanistan",
            "War in Afghanistan (2001–2021)",
            "Islamic Emirate of Afghanistan",
        ],
    },

    "food_security": {
        "Ethiopia": [
            "Famine in Ethiopia",
            "2021–2022 Ethiopian drought",
            "Food security in Ethiopia",
        ],
        "Iran": [
            "Economy of Iran",
            "Iranian food security",           # may not exist — will be searched
            "Agriculture in Iran",
        ],
        "Cuba": [
            "Economy of Cuba",
            "Food shortages in Cuba",
            "Special Period in Time of Peace",  # historical analogue for food crisis
        ],
        "Myanmar": [
            "Food security in Myanmar",         # may not exist
            "Famine in Myanmar",
            "Agriculture in Myanmar",
        ],
        "Guatemala": [
            "Food security in Guatemala",
            "Malnutrition in Guatemala",
            "Agriculture in Guatemala",
        ],
        "Bangladesh": [
            "1974 Bangladesh famine",
            "Food security in Bangladesh",
            "Agriculture in Bangladesh",
        ],
        "Lebanon": [
            "2019–2021 Lebanese economic crisis",
            "Beirut port explosion",
            "Food security in Lebanon",
        ],
        "Yemen": [
            "Famine in Yemen (2016–present)",
            "Humanitarian crisis in Yemen",
            "Food security in Yemen",
        ],
        "South Sudan": [
            "Famine in South Sudan (2017)",
            "Humanitarian crisis in South Sudan",
            "Food security in South Sudan",
        ],
        "Malawi": [
            "2001–2002 Malawian food crisis",
            "Agriculture in Malawi",
            "Food security in Malawi",
        ],
        "Mozambique": [
            "Cyclone Idai",
            "Cyclone Kenneth",
            "Food security in Mozambique",
        ],
        "Kenya": [
            "2011 East Africa drought",
            "2022 East African drought",
            "Food security in Kenya",
        ],
        "Afghanistan": [
            "Food security in Afghanistan",
            "2021–2022 Afghan humanitarian crisis",
            "Agriculture in Afghanistan",
        ],
    },

    "displacement": {
        "Ethiopia": [
            "Internal displacement in Ethiopia",
            "Ethiopian refugees",
            "Tigray refugee crisis",
        ],
        "Iran": [
            "Afghan refugees in Iran",
            "Iranian diaspora",
            "Refugees in Iran",
        ],
        "Cuba": [
            "Cuban exile",
            "Cuban migration to the United States",
            "Balsero crisis",
        ],
        "Myanmar": [
            "Rohingya people",
            "Internal displacement in Myanmar",
            "Refugees from Myanmar",
        ],
        "Guatemala": [
            "Guatemalan diaspora",
            "Guatemalan exodus",
            "Central American migrant caravans",
        ],
        "Bangladesh": [
            "Rohingya refugee crisis",
            "Cox's Bazar refugee camps",
            "Bangladesh–Myanmar border",
        ],
        "Lebanon": [
            "Syrian refugees in Lebanon",
            "Palestinian refugees in Lebanon",
            "Lebanese diaspora",
        ],
        "Yemen": [
            "Yemeni diaspora",
            "Internal displacement in Yemen",
            "Refugees in Yemen",
        ],
        "South Sudan": [
            "South Sudanese refugee crisis",
            "Internal displacement in South Sudan",
        ],
        "Malawi": [
            "Refugees in Malawi",
            "Dzaleka Refugee Camp",
        ],
        "Mozambique": [
            "Internal displacement in Mozambique",
            "Mozambican refugees",
        ],
        "Kenya": [
            "Dadaab",
            "Kakuma Refugee Camp",
            "Somali refugees in Kenya",
        ],
        "Afghanistan": [
            "Afghan refugee crisis",
            "Internal displacement in Afghanistan",
            "Afghan diaspora",
        ],
    },

    "famine_nutrition": {
        "Ethiopia": [
            "Famine in Ethiopia",
            "1983–1985 Ethiopian famine",
            "Malnutrition in Ethiopia",
        ],
        "Iran": [
            "Malnutrition in Iran",
            "Public health in Iran",
        ],
        "Cuba": [
            "Health in Cuba",
            "Malnutrition in Cuba",
        ],
        "Myanmar": [
            "Malnutrition in Myanmar",
            "Health in Myanmar",
        ],
        "Guatemala": [
            "Malnutrition in Guatemala",
            "Stunting in Guatemala",
        ],
        "Bangladesh": [
            "Malnutrition in Bangladesh",
            "1974 Bangladesh famine",
        ],
        "Lebanon": [
            "Malnutrition in Lebanon",
            "Health in Lebanon",
        ],
        "Yemen": [
            "Famine in Yemen (2016–present)",
            "Malnutrition in Yemen",
        ],
        "South Sudan": [
            "Famine in South Sudan (2017)",
            "Malnutrition in South Sudan",
        ],
        "Malawi": [
            "Malnutrition in Malawi",
            "2001–2002 Malawian food crisis",
        ],
        "Mozambique": [
            "Malnutrition in Mozambique",
            "Public health in Mozambique",
        ],
        "Kenya": [
            "Malnutrition in Kenya",
            "2011 East Africa drought",
        ],
        "Afghanistan": [
            "Famine in Afghanistan",
            "Malnutrition in Afghanistan",
            "2021–2022 Afghan humanitarian crisis",
        ],
    },
}

# ── Helper functions ──────────────────────────────────────────────────────────

def setup_logging(output_dir: str) -> logging.Logger:
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "run_log.txt")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger(__name__)


def resolve_page_title(title: str, session: requests.Session, logger: logging.Logger) -> dict:
    """
    Check whether a Wikipedia page exists with the given title.
    Returns a dict with keys: exists (bool), resolved_title (str), page_id (int|None).
    If the exact title redirects, follows the redirect automatically.
    If the page doesn't exist, attempts a search for the closest match.
    """
    params = {
        "action": "query",
        "titles": title,
        "redirects": 1,          # follow redirects
        "format": "json",
    }
    try:
        resp = session.get(WIKIPEDIA_API, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning(_log_safe(f"  API error resolving '{title}': {e}"))
        return {"exists": False, "resolved_title": title, "page_id": None}

    pages = data["query"]["pages"]
    # Wikipedia returns {"-1": {...}} for missing pages
    for page_id, page in pages.items():
        if page_id == "-1" or "missing" in page:
            # Try a search fallback
            logger.info(_log_safe(f"  '{title}' not found - attempting search fallback"))
            return search_fallback(title, session, logger)
        return {
            "exists": True,
            "resolved_title": page.get("title", title),
            "page_id": int(page_id),
        }
    return {"exists": False, "resolved_title": title, "page_id": None}


def search_fallback(query: str, session: requests.Session, logger: logging.Logger) -> dict:
    """Search Wikipedia for the closest matching article title."""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 1,
        "format": "json",
    }
    try:
        resp = session.get(WIKIPEDIA_API, params=params, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("query", {}).get("search", [])
        if results:
            best = results[0]
            logger.info(_log_safe(f"  Search fallback: '{query}' -> '{best['title']}'"))
            return {
                "exists": True,
                "resolved_title": best["title"],
                "page_id": best["pageid"],
                "was_fallback": True,
            }
    except requests.RequestException as e:
        logger.warning(_log_safe(f"  Search fallback failed for '{query}': {e}"))
    return {"exists": False, "resolved_title": query, "page_id": None}


def fetch_revision_timestamps(
    page_title: str,
    session: requests.Session,
    logger: logging.Logger,
    start: str = START_DATE,
    end: str = END_DATE,
) -> list[str]:
    """
    Fetch all revision timestamps for a page within [start, end].
    Handles API pagination automatically via 'rvcontinue'.
    Returns a list of ISO timestamp strings (UTC).
    """
    timestamps = []
    params = {
        "action": "query",
        "titles": page_title,
        "prop": "revisions",
        "rvprop": "timestamp",
        "rvlimit": MAX_REVISIONS_PER_REQUEST,
        "rvstart": start,
        "rvend": end,
        "rvdir": "newer",          # oldest first so pagination is predictable
        "format": "json",
    }

    page_count = 0
    while True:
        try:
            time.sleep(REQUEST_DELAY)
            resp = session.get(WIKIPEDIA_API, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning(_log_safe(f"  API error fetching revisions for '{page_title}': {e}"))
            break

        pages = data.get("query", {}).get("pages", {})
        for _, page in pages.items():
            if "missing" in page:
                logger.warning(_log_safe(f"  Page '{page_title}' missing during revision fetch"))
                return []
            revs = page.get("revisions", [])
            timestamps.extend([r["timestamp"] for r in revs])

        page_count += 1
        # Check for continuation token
        if "continue" in data:
            params["rvcontinue"] = data["continue"]["rvcontinue"]
            if page_count % 5 == 0:
                logger.info(_log_safe(f"    ... paginating '{page_title}' ({len(timestamps)} revisions so far)"))
        else:
            break  # no more pages

    return timestamps


def timestamps_to_monthly_counts(timestamps: list[str]) -> dict[str, int]:
    """
    Convert a list of ISO timestamp strings to a dict of {YYYY-MM: count}.
    """
    counts = defaultdict(int)
    for ts in timestamps:
        # Timestamps are like "2023-04-15T18:22:00Z"
        month_key = ts[:7]  # "YYYY-MM"
        counts[month_key] += 1
    return dict(counts)


def compute_national_monthly_with_z(monthly_df: pd.DataFrame) -> pd.DataFrame:
    """
    Sum edit_count to country × month, then z-score each country vs its full series.
    """
    national = (
        monthly_df.groupby(["country", "country_group", "year_month"], as_index=False)["edit_count"]
        .sum()
        .rename(columns={"edit_count": "total_edit_count"})
        .sort_values(["country", "year_month"])
    )

    def _national_z(s: pd.Series) -> pd.Series:
        s = s.astype(float)
        if len(s) < 2:
            return pd.Series(0.0, index=s.index)
        mu, sd = s.mean(), s.std(ddof=1)
        if sd == 0 or np.isnan(sd):
            return pd.Series(0.0, index=s.index)
        return (s - mu) / sd

    national["z_score_national"] = national.groupby("country")["total_edit_count"].transform(
        _national_z
    )
    national["is_anomaly_national"] = national["z_score_national"].abs() > 2.0
    national["z_score_national"] = national["z_score_national"].round(4)
    return national


def run_national_only(monthly_csv: str, output_dir: str) -> None:
    """Write edit_counts_national_monthly.csv from an existing page-level monthly CSV."""
    if not os.path.isfile(monthly_csv):
        raise SystemExit(f"Missing monthly CSV: {monthly_csv}")
    os.makedirs(output_dir, exist_ok=True)
    logger = setup_logging(output_dir)
    df = pd.read_csv(monthly_csv, low_memory=False)
    need = {"country", "country_group", "year_month", "edit_count"}
    if not need.issubset(df.columns):
        raise SystemExit(f"{monthly_csv} must have columns: {sorted(need)}")
    national = compute_national_monthly_with_z(df)
    out = os.path.join(output_dir, "edit_counts_national_monthly.csv")
    national.to_csv(out, index=False)
    logger.info("National monthly+z (from existing CSV) -> %s (%d rows)", out, len(national))
    print(f"Wrote {out} ({len(national)} rows)")


def compute_anomaly_scores(monthly_series: pd.Series) -> pd.Series:
    """
    Compute z-score anomaly for a monthly edit count series.
    Uses a rolling 12-month mean and std to make the baseline adaptive.
    Falls back to global mean/std if fewer than 13 months of data.
    Returns a Series of z-scores (NaN where std=0).
    """
    if len(monthly_series) < 13:
        mu = monthly_series.mean()
        sigma = monthly_series.std()
        if sigma == 0:
            return pd.Series(0.0, index=monthly_series.index)
        return (monthly_series - mu) / sigma

    rolling_mean = monthly_series.rolling(window=12, min_periods=6).mean()
    rolling_std = monthly_series.rolling(window=12, min_periods=6).std()
    z = (monthly_series - rolling_mean) / rolling_std.replace(0, float("nan"))
    return z


# ── Main scraper ──────────────────────────────────────────────────────────────

def run_scraper(
    countries_filter: list[str] | None,
    themes_filter: list[str] | None,
    output_dir: str,
    start_date: str = None,
    end_date: str = None,
):
    start_date = start_date or START_DATE
    end_date = end_date or END_DATE
    logger = setup_logging(output_dir)
    logger.info("=" * 60)
    logger.info("Wikipedia Edit Frequency Scraper - INFORM Novel Data")
    logger.info(f"Date range: {start_date} -> {end_date}")
    logger.info(f"Output dir: {output_dir}")
    logger.info("=" * 60)

    # Apply filters
    countries_to_run = {
        k: v for k, v in COUNTRIES.items()
        if countries_filter is None or k in countries_filter
    }
    themes_to_run = {
        k: v for k, v in PAGES.items()
        if themes_filter is None or k in themes_filter
    }

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "INFORM-NovelData-Research/1.0 "
            "(humanitarian data analysis; contact: research@example.org)"
        )
    })

    # ── Phase 1: Resolve all page titles ──────────────────────────────────────
    logger.info("\n--- Phase 1: Resolving page titles ---")
    page_metadata = []   # rows for metadata CSV
    resolved_pages = {}  # (theme, country, original_title) → resolved_title

    for theme, country_pages in themes_to_run.items():
        for country, titles in country_pages.items():
            if country not in countries_to_run:
                continue
            for title in titles:
                logger.info(_log_safe(f"  Resolving: [{theme}] {country} / '{title}'"))
                time.sleep(REQUEST_DELAY)
                info = resolve_page_title(title, session, logger)
                key = (theme, country, title)
                resolved_pages[key] = info

                page_metadata.append({
                    "theme": theme,
                    "country": country,
                    "country_group": COUNTRIES[country]["group"],
                    "original_title": title,
                    "resolved_title": info.get("resolved_title", title),
                    "page_id": info.get("page_id"),
                    "exists": info.get("exists", False),
                    "was_fallback": info.get("was_fallback", False),
                })

    metadata_df = pd.DataFrame(page_metadata)
    meta_path = os.path.join(output_dir, "page_metadata.csv")
    metadata_df.to_csv(meta_path, index=False)
    logger.info(f"\nMetadata saved -> {meta_path}")
    logger.info(
        f"  {metadata_df['exists'].sum()} of {len(metadata_df)} pages resolved successfully"
    )

    # ── Phase 2: Fetch revision timestamps ────────────────────────────────────
    logger.info("\n--- Phase 2: Fetching revision timestamps ---")

    # Build complete monthly date range (YYYY-MM strings)
    start_dt = datetime.strptime(start_date[:7], "%Y-%m")
    end_dt = datetime.strptime(end_date[:7], "%Y-%m")
    all_months = pd.period_range(start=start_dt, end=end_dt, freq="M").astype(str).tolist()

    # We'll collect: one row per (theme, country, page, month)
    monthly_rows = []
    # Deduplicate: same resolved_title might appear under multiple themes/countries
    fetched_cache = {}  # resolved_title → list of timestamps

    existing_pages = metadata_df[metadata_df["exists"]].itertuples(index=False)
    total = int(metadata_df["exists"].sum())

    for i, row in enumerate(existing_pages, 1):
        rtitle = row.resolved_title
        logger.info(
            _log_safe(f"  [{i}/{total}] Fetching: [{row.theme}] {row.country} / '{rtitle}'")
        )

        if rtitle in fetched_cache:
            logger.info("    (cached - already fetched)")
            timestamps = fetched_cache[rtitle]
        else:
            timestamps = fetch_revision_timestamps(rtitle, session, logger, start=start_date, end=end_date)
            fetched_cache[rtitle] = timestamps
            logger.info(f"    -> {len(timestamps)} revisions in range")

        monthly_counts = timestamps_to_monthly_counts(timestamps)

        for month in all_months:
            monthly_rows.append({
                "theme": row.theme,
                "country": row.country,
                "country_group": row.country_group,
                "page_title": rtitle,
                "original_title": row.original_title,
                "year_month": month,
                "year": int(month[:4]),
                "month": int(month[5:7]),
                "edit_count": monthly_counts.get(month, 0),
            })

    # ── Phase 3: Build DataFrames and compute summaries ───────────────────────
    logger.info("\n--- Phase 3: Building output tables ---")

    monthly_df = pd.DataFrame(monthly_rows)

    if monthly_df.empty:
        logger.warning("No data collected - check network connectivity and page titles.")
        return

    # Monthly edit counts
    monthly_path = os.path.join(output_dir, "edit_counts_monthly.csv")
    monthly_df.to_csv(monthly_path, index=False)
    logger.info(f"Monthly edit counts -> {monthly_path}")

    # Annual rollup
    annual_df = (
        monthly_df
        .groupby(["theme", "country", "country_group", "page_title", "year"])["edit_count"]
        .sum()
        .reset_index()
        .rename(columns={"edit_count": "annual_edit_count"})
    )
    annual_path = os.path.join(output_dir, "edit_counts_annual.csv")
    annual_df.to_csv(annual_path, index=False)
    logger.info(f"Annual edit counts  -> {annual_path}")

    national = compute_national_monthly_with_z(monthly_df)
    national_monthly_path = os.path.join(output_dir, "edit_counts_national_monthly.csv")
    national.to_csv(national_monthly_path, index=False)
    logger.info(f"National monthly+z  -> {national_monthly_path}")

    # Anomaly scores — computed per page over its monthly series
    logger.info("  Computing anomaly scores...")
    anomaly_rows = []
    for (theme, country, page), group in monthly_df.groupby(["theme", "country", "page_title"]):
        group_sorted = group.sort_values("year_month").set_index("year_month")
        z_scores = compute_anomaly_scores(group_sorted["edit_count"])
        for ym, z in z_scores.items():
            anomaly_rows.append({
                "theme": theme,
                "country": country,
                "page_title": page,
                "year_month": ym,
                "edit_count": group_sorted.loc[ym, "edit_count"],
                "z_score": round(z, 3) if pd.notna(z) else None,
                "is_anomaly": bool(z > 2.0) if pd.notna(z) else False,
            })

    anomaly_df = pd.DataFrame(anomaly_rows)
    anomaly_path = os.path.join(output_dir, "anomaly_scores.csv")
    anomaly_df.to_csv(anomaly_path, index=False)
    logger.info(f"Anomaly scores      -> {anomaly_path}")

    # Summary: top anomaly months per country
    top_anomalies = (
        anomaly_df[anomaly_df["is_anomaly"]]
        .sort_values("z_score", ascending=False)
        .groupby("country")
        .head(5)
    )
    logger.info("\n--- Top anomaly months by country (z > 2.0) ---")
    if not top_anomalies.empty:
        for _, row in top_anomalies.iterrows():
            logger.info(
                _log_safe(
                    f"  {row['country']:15s} {row['year_month']}  "
                    f"z={row['z_score']:5.2f}  {row['page_title'][:50]}"
                )
            )
    else:
        logger.info("  No anomalies detected (may need more data).")

    logger.info("\n--- Run complete ---")
    logger.info(f"All outputs saved to: {output_dir}/")


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape Wikipedia edit frequency for INFORM Novel Data Consultancy."
    )
    parser.add_argument(
        "--countries",
        nargs="+",
        default=None,
        metavar="COUNTRY",
        help=(
            "Limit run to specific countries (space-separated). "
            "Use quotes for names with spaces: --countries Ethiopia 'South Sudan'"
        ),
    )
    parser.add_argument(
        "--themes",
        nargs="+",
        default=None,
        metavar="THEME",
        choices=list(PAGES.keys()),
        help=(
            f"Limit run to specific themes. "
            f"Options: {', '.join(PAGES.keys())}"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="./wikipedia_results",
        metavar="DIR",
        help="Directory for output CSVs and log file (default: ./wikipedia_results)",
    )
    parser.add_argument(
        "--start-date",
        default=START_DATE,
        metavar="YYYY-MM-DDT00:00:00Z",
        help=f"Override start date (default: {START_DATE})",
    )
    parser.add_argument(
        "--end-date",
        default=END_DATE,
        metavar="YYYY-MM-DDT00:00:00Z",
        help=f"Override end date (default: {END_DATE})",
    )
    parser.add_argument(
        "--national-only",
        action="store_true",
        help=(
            "Do not call the Wikipedia API. Read --monthly-csv (or "
            "<output-dir>/edit_counts_monthly.csv) and write edit_counts_national_monthly.csv."
        ),
    )
    parser.add_argument(
        "--monthly-csv",
        default=None,
        metavar="PATH",
        help="Input for --national-only (default: <output-dir>/edit_counts_monthly.csv)",
    )

    args = parser.parse_args()

    if args.national_only:
        mpath = args.monthly_csv or os.path.join(args.output_dir, "edit_counts_monthly.csv")
        run_national_only(mpath, args.output_dir)
        return

    run_scraper(
        countries_filter=args.countries,
        themes_filter=args.themes,
        output_dir=args.output_dir,
        start_date=args.start_date,
        end_date=args.end_date,
    )


if __name__ == "__main__":
    main()
