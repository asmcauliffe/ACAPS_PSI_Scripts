"""
iom_dtm_fetch.py
----------------
Pulls IDP and returnee displacement figures from the IOM DTM API
(https://dtmapi.iom.int) for all priority countries in the INFORM
Novel Data Consultancy, at all three administrative levels, since 2016.

PURPOSE IN THE ANALYSIS PLAN
-----------------------------
IOM DTM displacement data is a PRIMARY ESTIMATION INPUT in the
driver-first PiN workflow:

  Conflict driver → IDP count as % of WorldPop baseline population
                  → Proxy for population uprooted from normal food systems
                  → One link in the causal chain: conflict → displacement
                    → food access failure → IPC Phase 3+

Data is aggregated to IPC assessment period windows (3–4 month chunks)
for alignment with training labels.

API OVERVIEW (v2, August 2024)
-------------------------------
Base URL : https://dtmapi.iom.int/api
Auth     : None required (public API)
Method   : GET
Format   : JSON

Endpoints used:
  /displacement/country-list  → discover available countries
  /displacement/operation-list → discover available operations
  /displacement/admin0        → country-level IDP totals
  /displacement/admin1        → Admin 1 (province/state) IDP totals
  /displacement/admin2        → Admin 2 (district) IDP totals

Key date filter: FromReportingDate=2016-01-01

OUTPUTS (in ./dtm_results/)
-----------------------------
  admin0_raw.csv          Country-level IDP/returnee figures, all rounds
  admin1_raw.csv          Admin 1 figures
  admin2_raw.csv          Admin 2 figures (where available)
  combined_all_levels.csv Unified long-format file across all levels
  ipc_period_aligned.csv  Data aggregated to IPC assessment period windows
  coverage_report.csv     Which countries/admin levels have data + date spans
  run_log.txt             Full execution log

USAGE
-----
  pip install requests pandas tqdm

  # Full run — all countries, all levels, since 2016:
  python iom_dtm_fetch.py

  # Extrapolation countries only, country-level only (fastest):
  python iom_dtm_fetch.py --countries Ethiopia Iran Cuba Myanmar Guatemala --levels 0

  # Custom date range:
  python iom_dtm_fetch.py --from-date 2020-01-01 --to-date 2024-12-31

  # Resume / skip countries that already have a complete file:
  python iom_dtm_fetch.py --resume

NOTES ON COVERAGE
-----------------
- Iran, Cuba: DTM has limited or no coverage. The script will log this
  explicitly in coverage_report.csv. For these countries, absence of
  DTM data is itself a signal (consistent with the reliability scoring
  framework: Low on data completeness).
- Myanmar: DTM coverage exists but may be incomplete post-2021 coup.
- Ethiopia: Good coverage; multiple operations (Countrywide monitoring,
  Site assessment, etc.). The script pulls all operations and flags them.
- Admin 2 coverage is sparser than Admin 0/1 — check the DTM data
  coverage page if queries return empty.
"""

import argparse
import io
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

# ── Configuration ─────────────────────────────────────────────────────────────

DTM_BASE   = "https://dtmapi.iom.int/v3"

# API key — set via environment variable DTM_API_KEY or pass --api-key on CLI.
# Primary key from the DTM Developer Portal (Ocp-Apim-Subscription-Key).
# Store in your shell: set DTM_API_KEY=<your_key>  (Windows)
#                      export DTM_API_KEY=<your_key> (Mac/Linux)
DTM_API_KEY = os.environ.get("DTM_API_KEY", "")
FROM_DATE  = "2016-01-01"   # 10-year window aligned with Wikipedia/GDELT scripts
TO_DATE    = date.today().strftime("%Y-%m-%d")
DELAY      = 0.5            # seconds between requests — be polite to the API
TIMEOUT    = 30             # seconds per request

# ── Priority countries ─────────────────────────────────────────────────────────
# iso3: ISO 3166-1 alpha-3 — this is what DTM uses as Admin0Pcode
# dtm_name: exact country name as returned by /Common/GetAllCountryList
#   (confirmed from DTM docs; update if the discovery step returns a different name)

COUNTRIES = {
    # Extrapolation targets (primary interest)
    "Ethiopia": {
        "iso3": "ETH", "dtm_name": "Ethiopia",
        "priority": 1, "group": "extrapolation",
        "notes": "Multiple operations — Countrywide monitoring, Site assessment, "
                 "Tigray-specific rounds. Good coverage.",
    },
    "Iran": {
        "iso3": "IRN", "dtm_name": "Iran (Islamic Republic of)",
        "priority": 2, "group": "extrapolation",
        "notes": "Limited or no DTM coverage expected. Log absence as data gap.",
    },
    "Cuba": {
        "iso3": "CUB", "dtm_name": "Cuba",
        "priority": 3, "group": "extrapolation",
        "notes": "No DTM coverage expected. Log absence.",
    },
    "Myanmar": {
        "iso3": "MMR", "dtm_name": "Myanmar",
        "priority": 4, "group": "extrapolation",
        "notes": "Coverage exists but may be incomplete post-2021 coup. "
                 "Multiple operations.",
    },
    "Guatemala": {
        "iso3": "GTM", "dtm_name": "Guatemala",
        "priority": 5, "group": "extrapolation",
        "notes": "Limited DTM presence; check coverage.",
    },
    # Training / validation countries
    "Bangladesh": {
        "iso3": "BGD", "dtm_name": "Bangladesh",
        "priority": 6, "group": "training",
        "notes": "Rohingya response — good coverage for Cox's Bazar.",
    },
    "Lebanon": {
        "iso3": "LBN", "dtm_name": "Lebanon",
        "priority": 7, "group": "training",
        "notes": "Syrian displacement + internal displacement. Good coverage.",
    },
    "Yemen": {
        "iso3": "YEM", "dtm_name": "Yemen",
        "priority": 8, "group": "training",
        "notes": "Extensive IDP tracking. One of DTM's largest operations.",
    },
    "South Sudan": {
        "iso3": "SSD", "dtm_name": "South Sudan",
        "priority": 9, "group": "training",
        "notes": "Extensive coverage. Multiple rounds.",
    },
    "Malawi": {
        "iso3": "MWI", "dtm_name": "Malawi",
        "priority": 10, "group": "training",
        "notes": "Cyclone displacement tracking. Coverage post-2019.",
    },
    "Mozambique": {
        "iso3": "MOZ", "dtm_name": "Mozambique",
        "priority": 11, "group": "training",
        "notes": "Cabo Delgado conflict + Cyclone Idai/Kenneth displacement.",
    },
    "Kenya": {
        "iso3": "KEN", "dtm_name": "Kenya",
        "priority": 12, "group": "training",
        "notes": "Drought/flood displacement tracking.",
    },
    "Afghanistan": {
        "iso3": "AFG", "dtm_name": "Afghanistan",
        "priority": 13, "group": "extrapolation",
        "notes": "Extensive IDP tracking post-2021 Taliban takeover. One of DTM's largest operations.",
    },
}

# ── IPC assessment period windows ─────────────────────────────────────────────
# Standard IPC assessment windows used globally.
# DTM data will be snapped to the nearest window for alignment.
# Format: (label, start_month, end_month) — year is applied dynamically.
IPC_WINDOWS = [
    ("Jan-Apr", 1, 4),
    ("May-Aug", 5, 8),
    ("Sep-Dec", 9, 12),
]

# ── API response field mapping ─────────────────────────────────────────────────
# DTM API returns camelCase JSON fields. We rename for consistency.
# Maps logical output columns to one or more possible JSON keys (camelCase / PascalCase).
FIELD_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("operation", ("operation", "Operation")),
    ("country_name", ("countryName", "CountryName")),
    ("iso3", ("admin0Pcode", "Admin0Pcode")),
    ("admin1_name", ("admin1Name", "Admin1Name")),
    ("admin1_pcode", ("admin1Pcode", "Admin1Pcode")),
    ("admin2_name", ("admin2Name", "Admin2Name")),
    ("admin2_pcode", ("admin2Pcode", "Admin2Pcode")),
    ("reporting_date", ("reportingDate", "ReportingDate")),
    ("from_reporting_date", ("fromReportingDate", "FromReportingDate")),
    ("to_reporting_date", ("toReportingDate", "ToReportingDate")),
    ("round_number", ("roundNumber", "RoundNumber")),
    ("idp_individuals", ("idpIndividuals", "IdpIndividuals")),
    ("returnee_individuals", ("returneeIndividuals", "ReturneeIndividuals")),
    ("idp_households", ("idpHouseholds", "IdpHouseholds")),
    ("returnee_households", ("returneeHouseholds", "ReturneeHouseholds")),
    ("others_of_concern", ("othersOfConcern", "OthersOfConcern")),
]


def _dtm_date_column(df: pd.DataFrame) -> str | None:
    """First available standard DTM reporting date column in `df`, if any."""
    for col in ("reporting_date", "to_reporting_date", "from_reporting_date"):
        if col in df.columns:
            return col
    return None

# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging(output_dir: str) -> logging.Logger:
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "run_log.txt")
    # Force UTF-8 on the stream handler so Unicode characters (arrows, box
    # drawing) don't crash on Windows terminals that default to CP1252.
    stream_handler = logging.StreamHandler(
        io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "buffer") else sys.stdout
    )
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[stream_handler, file_handler])
    return logging.getLogger(__name__)


# ── API helpers ────────────────────────────────────────────────────────────────

def _parse_dtm_json_body(
    data: object,
    endpoint: str,
    params: dict,
    logger: logging.Logger,
) -> list[dict]:
    """
    DTM API v3 wraps payloads as {"isSuccess": bool, "errorMessages": [...], "result": [...]}.
    Older responses may be a bare JSON list or a single row object.
    """
    if isinstance(data, dict) and "result" in data:
        if data.get("isSuccess") is False:
            msgs = data.get("errorMessages") or []
            msg = msgs[0] if msgs else "Unknown API error"
            logger.warning(f"  API error [{endpoint}] params={params}: {msg}")
            return []
        inner = data["result"]
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            return [inner]
        logger.warning(f"  Unexpected result type [{endpoint}]: {type(inner)}")
        return []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if any(k in data for k in ("message", "Message", "error", "Error")):
            msg = (
                data.get("message")
                or data.get("Message")
                or data.get("error")
                or data.get("Error")
            )
            logger.warning(f"  API message [{endpoint}] params={params}: {msg}")
            return []
        return [data]

    logger.warning(f"  Unexpected response type: {type(data)}")
    return []


def api_get(
    session: requests.Session,
    endpoint: str,
    params: dict,
    logger: logging.Logger,
    retries: int = 3,
) -> list[dict] | None:
    """
    Make a GET request to the DTM API. Returns parsed JSON list or None on failure.
    Retries up to `retries` times on transient errors (5xx, timeout).
    """
    url = f"{DTM_BASE}/{endpoint}"
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT)

            # DTM returns 200 with an error message string for bad queries
            if resp.status_code == 200:
                data = resp.json()
                return _parse_dtm_json_body(data, endpoint, params, logger)

            elif resp.status_code in (400, 404):
                # Bad request or not found — not worth retrying
                logger.warning(
                    f"  HTTP {resp.status_code} [{endpoint}] params={params}: {resp.text[:200]}"
                )
                return []

            elif resp.status_code >= 500:
                logger.warning(
                    f"  HTTP {resp.status_code} (attempt {attempt}/{retries}) "
                    f"[{endpoint}] — retrying in {attempt * 2}s"
                )
                time.sleep(attempt * 2)

        except requests.exceptions.Timeout:
            logger.warning(f"  Timeout (attempt {attempt}/{retries}) [{endpoint}]")
            time.sleep(attempt * 2)
        except requests.exceptions.RequestException as e:
            logger.warning(f"  Request error (attempt {attempt}/{retries}): {e}")
            time.sleep(attempt * 2)
        except Exception as e:
            logger.error(f"  Unexpected error [{endpoint}]: {e}")
            return None

    logger.error(f"  All {retries} attempts failed for [{endpoint}] params={params}")
    return None


def normalise_record(raw: dict, level: int) -> dict:
    """
    Rename API fields to snake_case and add metadata columns.
    `level` is 0, 1, or 2 (administrative level).
    """
    out = {"admin_level": level}
    for our_key, api_keys in FIELD_ALIASES:
        for ak in api_keys:
            if ak in raw and raw[ak] is not None:
                out[our_key] = raw[ak]
                break
    # Ensure numeric fields are numeric
    for num_col in (
        "idp_individuals", "returnee_individuals",
        "idp_households", "returnee_households", "others_of_concern"
    ):
        if num_col in out:
            try:
                out[num_col] = int(out[num_col]) if out[num_col] is not None else None
            except (ValueError, TypeError):
                out[num_col] = None
    return out


def snap_to_ipc_window(reporting_date_str: str, year: int) -> str | None:
    """
    Given a reporting date string (YYYY-MM-DD or similar) and a year,
    return the IPC assessment window label it falls into.
    E.g. "2021-06-15" → "2021 May-Aug"
    """
    try:
        dt = pd.to_datetime(reporting_date_str)
        month = dt.month
        for label, start_m, end_m in IPC_WINDOWS:
            if start_m <= month <= end_m:
                return f"{dt.year} {label}"
    except Exception:
        pass
    return None


# ── Discovery ──────────────────────────────────────────────────────────────────

def discover_available_countries(
    session: requests.Session, logger: logging.Logger
) -> dict[str, str]:
    """
    Call /Common/GetAllCountryList and return a dict of {dtm_name: iso3}.
    Used to validate our country list and detect name mismatches.
    """
    logger.info("  Discovering available countries from DTM API...")
    data = api_get(session, "displacement/country-list", {}, logger)
    if not data:
        logger.warning("  Could not retrieve country list — proceeding with configured names.")
        return {}
    available = {}
    for rec in data:
        name = rec.get("countryName") or rec.get("CountryName", "")
        code = rec.get("admin0Pcode") or rec.get("Admin0Pcode", "")
        if name and code:
            available[name] = code
    logger.info(f"  DTM covers {len(available)} countries.")
    return available


def discover_operations(
    session: requests.Session,
    country_name: str,
    logger: logging.Logger,
) -> list[str]:
    """
    Return the list of DTM operation names active in a given country.
    Used to pull data per-operation where multiple operations exist (e.g. Ethiopia).
    """
    data = api_get(session, "displacement/operation-list", {}, logger)
    if not data:
        return []
    ops = []
    for rec in data:
        rec_country = rec.get("countryName") or rec.get("CountryName", "")
        if rec_country.lower() == country_name.lower():
            op = rec.get("operation") or rec.get("Operation", "")
            if op and op not in ops:
                ops.append(op)
    return ops


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_admin_level(
    session: requests.Session,
    level: int,
    country_name: str,
    iso3: str,
    from_date: str,
    to_date: str,
    logger: logging.Logger,
    operation: str | None = None,
) -> list[dict]:
    """
    Fetch IDP data at the given admin level (0, 1, or 2) for one country.
    Optionally scoped to a specific DTM operation.
    Returns normalised records.
    """
    endpoints = {
        0: "displacement/admin0",
        1: "displacement/admin1",
        2: "displacement/admin2",
    }
    endpoint = endpoints[level]

    # Build params — use Admin0Pcode as the primary country identifier
    # (more reliable than CountryName for exact matching)
    params = {
        "Admin0Pcode":       iso3,
        "FromReportingDate": from_date,
        "ToReportingDate":   to_date,
    }
    if operation:
        params["Operation"] = operation

    raw = api_get(session, endpoint, params, logger)
    if raw is None:
        return []

    records = []
    for rec in raw:
        norm = normalise_record(rec, level)
        # Add our own metadata
        norm["query_country"]   = country_name
        norm["query_operation"] = operation or "all"
        norm["query_level"]     = level
        # Add IPC window alignment
        date_str = norm.get("reporting_date") or norm.get("to_reporting_date", "")
        norm["ipc_window"] = snap_to_ipc_window(date_str, 0)
        records.append(norm)

    return records


# ── IPC period aggregation ─────────────────────────────────────────────────────

def aggregate_to_ipc_periods(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate DTM records to IPC assessment period windows.
    For each (country, admin unit, ipc_window), take the maximum IDP count
    (conservative: use peak displacement within the window).
    Also compute:
      - idp_pct_of_pop: placeholder column (requires WorldPop join — done separately)
      - round_count: number of DTM rounds within the window
    """
    if df.empty:
        return df

    # Determine which ID columns exist for this level
    id_cols = ["query_country", "admin_level", "ipc_window"]
    if "admin1_name" in df.columns:
        id_cols.append("admin1_name")
    if "admin2_name" in df.columns:
        id_cols.append("admin2_name")
    if "operation" in df.columns:
        id_cols.append("operation")

    agg_cols = {}
    for col in ("idp_individuals", "returnee_individuals", "idp_households", "returnee_households"):
        if col in df.columns:
            agg_cols[col] = "max"

    agg_cols["round_number"] = "count"
    agg_cols["iso3"] = "first"
    if "admin1_pcode" in df.columns:
        agg_cols["admin1_pcode"] = "first"
    if "admin2_pcode" in df.columns:
        agg_cols["admin2_pcode"] = "first"

    # Drop rows with no IPC window
    df_valid = df[df["ipc_window"].notna()].copy()
    if df_valid.empty:
        return df_valid

    agg = (
        df_valid
        .groupby(id_cols, dropna=False)
        .agg(agg_cols)
        .reset_index()
        .rename(columns={"round_number": "dtm_rounds_in_window"})
    )

    # Placeholder for WorldPop join (done in main analysis pipeline)
    agg["idp_pct_of_worldpop"] = None  # to be filled after WorldPop join

    return agg


# ── Coverage reporting ─────────────────────────────────────────────────────────

def build_coverage_report(all_records: list[dict], countries: dict) -> pd.DataFrame:
    """
    Produce a per-country, per-level coverage summary: date range,
    number of rounds, total IDP figures, and whether data is present.
    """
    rows = []
    df = pd.DataFrame(all_records) if all_records else pd.DataFrame()

    for country_name, info in countries.items():
        for level in (0, 1, 2):
            if df.empty:
                country_df = pd.DataFrame()
            else:
                country_df = df[
                    (df["query_country"] == country_name) &
                    (df["admin_level"] == level)
                ]

            if country_df.empty:
                rows.append({
                    "country": country_name,
                    "group": info["group"],
                    "priority": info["priority"],
                    "admin_level": level,
                    "has_data": False,
                    "earliest_date": None,
                    "latest_date": None,
                    "n_rounds": 0,
                    "max_idp_individuals": None,
                    "operations": None,
                    "notes": info["notes"],
                })
            else:
                date_col = _dtm_date_column(country_df)
                if date_col is not None:
                    dates = pd.to_datetime(country_df[date_col], errors="coerce").dropna()
                else:
                    dates = pd.Series(dtype="datetime64[ns]")
                idp_col = country_df.get("idp_individuals") if "idp_individuals" in country_df.columns else None
                ops = sorted(country_df["operation"].dropna().unique().tolist()) if "operation" in country_df.columns else []

                rows.append({
                    "country": country_name,
                    "group": info["group"],
                    "priority": info["priority"],
                    "admin_level": level,
                    "has_data": True,
                    "earliest_date": dates.min().strftime("%Y-%m-%d") if not dates.empty else None,
                    "latest_date": dates.max().strftime("%Y-%m-%d") if not dates.empty else None,
                    "n_rounds": len(country_df),
                    "max_idp_individuals": int(idp_col.max()) if idp_col is not None and not idp_col.empty else None,
                    "operations": "; ".join(ops),
                    "notes": info["notes"],
                })

    return pd.DataFrame(rows)


# ── Main ───────────────────────────────────────────────────────────────────────

def run(
    from_date: str,
    to_date: str,
    countries_filter: list[str] | None,
    levels: list[int],
    output_dir: str,
    resume: bool,
    discover: bool,
    api_key_arg: str = "",
):
    logger = setup_logging(output_dir)
    logger.info("=" * 60)
    logger.info("IOM DTM API Fetcher — INFORM Novel Data Consultancy")
    logger.info(f"Date range : {from_date} → {to_date}")
    logger.info(f"Admin levels: {levels}")
    logger.info(f"Output dir : {output_dir}")
    logger.info("=" * 60)

    countries_to_run = {
        k: v for k, v in COUNTRIES.items()
        if countries_filter is None or k in countries_filter
    }
    logger.info(f"Countries: {list(countries_to_run.keys())}\n")

    # Resolve API key: CLI arg takes priority over env var (raw key only, not YAML export)
    api_key = (api_key_arg or DTM_API_KEY or "").strip()
    if api_key and ("\n" in api_key or "primary_key:" in api_key):
        raise SystemExit(
            "ERROR: DTM_API_KEY must be the raw subscription key string only.\n"
            "  Do not paste the multi-line YAML export from the Developer Portal."
        )
    if not api_key:
        raise SystemExit(
            "ERROR: No DTM API key found.\n"
            "  Set the DTM_API_KEY environment variable, or pass --api-key <key>.\n"
            "  Example (Windows):  set DTM_API_KEY=your_subscription_key_here\n"
            "  Example (Mac/Linux): export DTM_API_KEY=your_subscription_key_here"
        )

    session = requests.Session()
    session.headers.update({
        "User-Agent": "INFORM-NovelData-Research/1.0 (humanitarian data; contact: research@argot-nova.com)",
        "Accept": "application/json",
        "Ocp-Apim-Subscription-Key": api_key,
    })

    # ── Optional discovery step ────────────────────────────────────────────────
    if discover:
        logger.info("── Discovery step ──")
        available = discover_available_countries(session, logger)
        if available:
            for name, info in countries_to_run.items():
                if info["dtm_name"] not in available:
                    # Try to find a close match
                    close = [k for k in available if info["iso3"] == available[k]]
                    if close:
                        logger.warning(
                            f"  '{info['dtm_name']}' not found by name but ISO3 "
                            f"'{info['iso3']}' matches '{close[0]}' — will use ISO3."
                        )
                    else:
                        logger.warning(
                            f"  '{name}' (ISO3: {info['iso3']}) not found in DTM — "
                            f"data will likely be empty."
                        )
                else:
                    logger.info(f"  ✓ {name} confirmed in DTM as '{info['dtm_name']}'")
        logger.info("")

    # ── Per-country, per-level fetching ───────────────────────────────────────
    all_records: list[dict] = []
    level_dfs: dict[int, list[dict]] = {0: [], 1: [], 2: []}

    total = len(countries_to_run) * len(levels)
    done = 0

    for country_name, info in countries_to_run.items():
        iso3     = info["iso3"]
        dtm_name = info["dtm_name"]

        # Resume: check if this country already has a complete file
        resume_path = os.path.join(output_dir, f"_cache_{country_name.replace(' ', '_')}.pkl")
        if resume and os.path.exists(resume_path):
            cached = pd.read_pickle(resume_path)
            cached_records = cached.to_dict("records")
            all_records.extend(cached_records)
            for lvl in levels:
                lvl_records = [r for r in cached_records if r.get("admin_level") == lvl]
                level_dfs[lvl].extend(lvl_records)
            logger.info(f"  {country_name}: loaded {len(cached_records)} records from cache")
            done += len(levels)
            continue

        country_records: list[dict] = []

        for level in levels:
            done += 1
            logger.info(
                f"[{done}/{total}] {country_name} (ISO3: {iso3}) — "
                f"Admin Level {level}"
            )

            time.sleep(DELAY)

            # Discover operations for this country (only at level 0 to save calls)
            operations = [None]  # default: pull all operations in one call
            if level == 0 and discover:
                ops = discover_operations(session, dtm_name, logger)
                if ops:
                    logger.info(f"  Operations found: {ops}")
                    operations = ops  # pull per-operation for richer metadata
                else:
                    logger.info("  No operations listed — pulling without operation filter.")

            for op in operations:
                op_label = f"operation='{op}'" if op else "all operations"
                logger.info(f"  Fetching level {level}, {op_label}...")
                time.sleep(DELAY)

                records = fetch_admin_level(
                    session, level, country_name, iso3,
                    from_date, to_date, logger, operation=op,
                )

                if records:
                    logger.info(f"    → {len(records)} records")
                    country_records.extend(records)
                    level_dfs[level].extend(records)
                else:
                    logger.info(f"    → 0 records (no data or country not covered)")

        all_records.extend(country_records)

        # Cache this country's records for resume capability
        if country_records:
            pd.DataFrame(country_records).to_pickle(resume_path)
            logger.info(f"  {country_name}: {len(country_records)} total records cached")
        else:
            logger.info(f"  {country_name}: no data found (will appear in coverage report)")
        logger.info("")

    # ── Save raw level files ───────────────────────────────────────────────────
    logger.info("── Saving outputs ──")

    level_names = {0: "admin0", 1: "admin1", 2: "admin2"}
    saved_dfs: dict[int, pd.DataFrame] = {}

    for level in levels:
        if level_dfs[level]:
            df = pd.DataFrame(level_dfs[level])
            # Sort by country, then date
            date_col = _dtm_date_column(df)
            sort_cols = (
                ["query_country", date_col] if date_col is not None else ["query_country"]
            )
            df = df.sort_values(sort_cols).reset_index(drop=True)
            path = os.path.join(output_dir, f"{level_names[level]}_raw.csv")
            df.to_csv(path, index=False)
            logger.info(f"  {level_names[level]}_raw.csv → {len(df)} rows")
            saved_dfs[level] = df
        else:
            logger.info(f"  {level_names[level]}_raw.csv → no data")
            saved_dfs[level] = pd.DataFrame()

    # ── Combined long-format file ──────────────────────────────────────────────
    all_dfs = [df for df in saved_dfs.values() if not df.empty]
    if all_dfs:
        combined = pd.concat(all_dfs, ignore_index=True)
        combined_path = os.path.join(output_dir, "combined_all_levels.csv")
        combined.to_csv(combined_path, index=False)
        logger.info(f"  combined_all_levels.csv → {len(combined)} rows")
    else:
        combined = pd.DataFrame()
        logger.warning("  No data collected across any level.")

    # ── IPC period alignment ───────────────────────────────────────────────────
    if not combined.empty:
        logger.info("  Aggregating to IPC assessment period windows...")
        ipc_dfs = []
        for level in levels:
            if not saved_dfs[level].empty:
                agg = aggregate_to_ipc_periods(saved_dfs[level])
                if not agg.empty:
                    ipc_dfs.append(agg)

        if ipc_dfs:
            ipc_combined = pd.concat(ipc_dfs, ignore_index=True)
            ipc_path = os.path.join(output_dir, "ipc_period_aligned.csv")
            ipc_combined.to_csv(ipc_path, index=False)
            logger.info(f"  ipc_period_aligned.csv → {len(ipc_combined)} rows")

            # Print a quick summary of max IDP figures by country and window
            logger.info("\n── Displacement summary (max IDP individuals per country) ──")
            if "idp_individuals" in ipc_combined.columns:
                summary = (
                    ipc_combined[ipc_combined["admin_level"] == 0]
                    .groupby("query_country")["idp_individuals"]
                    .max()
                    .sort_values(ascending=False)
                    .dropna()
                )
                for country, max_idp in summary.items():
                    logger.info(f"  {country:15s} peak IDPs: {int(max_idp):>10,}")
        else:
            logger.info("  No data to aggregate to IPC periods.")

    # ── Coverage report ────────────────────────────────────────────────────────
    coverage = build_coverage_report(all_records, countries_to_run)
    coverage_path = os.path.join(output_dir, "coverage_report.csv")
    coverage.to_csv(coverage_path, index=False)
    logger.info(f"\n  coverage_report.csv → {len(coverage)} rows")

    # Log countries with no data — these need to be flagged in reliability scores
    no_data = coverage[~coverage["has_data"] & (coverage["admin_level"] == 0)]
    if not no_data.empty:
        logger.info("\n── Countries with NO DTM data (flag in reliability scoring) ──")
        for _, row in no_data.iterrows():
            logger.info(
                f"  {row['country']:15s} [priority {row['priority']}]  — {row['notes']}"
            )

    logger.info("\n── Run complete ──")
    logger.info(f"All outputs saved to: {output_dir}/")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch IOM DTM displacement data for INFORM Novel Data Consultancy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full run — all countries, all levels, since 2016 (recommended first run):
  python iom_dtm_fetch.py

  # Fast test — extrapolation countries only, country-level only:
  python iom_dtm_fetch.py --countries Ethiopia Myanmar Bangladesh Lebanon Yemen --levels 0

  # Admin 1 only (faster than Admin 2, more detailed than Admin 0):
  python iom_dtm_fetch.py --levels 0 1

  # Resume an interrupted run:
  python iom_dtm_fetch.py --resume

  # Skip discovery step (slightly faster; useful when country names are confirmed):
  python iom_dtm_fetch.py --no-discover
        """,
    )
    parser.add_argument(
        "--from-date", default=FROM_DATE, metavar="YYYY-MM-DD",
        help=f"Start of reporting date range (default: {FROM_DATE})"
    )
    parser.add_argument(
        "--to-date", default=TO_DATE, metavar="YYYY-MM-DD",
        help=f"End of reporting date range (default: today, {TO_DATE})"
    )
    parser.add_argument(
        "--countries", nargs="+", default=None, metavar="COUNTRY",
        help="Limit to specific countries (e.g. Ethiopia Myanmar Bangladesh)"
    )
    parser.add_argument(
        "--levels", nargs="+", type=int, default=[0, 1, 2], choices=[0, 1, 2],
        metavar="LEVEL",
        help="Admin levels to fetch: 0=country, 1=province, 2=district (default: all three)"
    )
    parser.add_argument(
        "--output-dir", default="./dtm_results", metavar="DIR",
        help="Output directory (default: ./dtm_results)"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip countries that already have a cached file from a previous run"
    )
    parser.add_argument(
        "--no-discover", action="store_true",
        help="Skip the discovery step (faster; assumes country names are correct)"
    )
    parser.add_argument(
        "--api-key", default="", metavar="KEY",
        help=(
            "DTM API subscription key. If omitted, reads from the DTM_API_KEY "
            "environment variable. CLI argument takes priority over env var."
        )
    )

    args = parser.parse_args()

    run(
        from_date=args.from_date,
        to_date=args.to_date,
        countries_filter=args.countries,
        levels=sorted(set(args.levels)),
        output_dir=args.output_dir,
        resume=args.resume,
        discover=not args.no_discover,
        api_key_arg=args.api_key,
    )


if __name__ == "__main__":
    main()
