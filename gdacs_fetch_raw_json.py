"""
gdacs_fetch_raw_json.py
-----------------------
Fetch GDACS geteventlist/SEARCH responses and **persist the raw HTTP body** (GeoJSON),
mirroring ``fetch_gdacs_events.R`` / ``gdacs_processing.R`` (EQ + TC, 2018→present,
year chunks). Does **not** replace the R spatial / admin2 pipeline — it adds an
audit trail and country-filtered extracts for the same priority countries as
``gdelt_fetch.COUNTRIES``.

Outputs (default ./gdacs_results/):
  gdacs_raw/responses/<from>_<to>.json     — exact API payload per window
  gdacs_raw/fetch_manifest.json           — run metadata + file list
  gdacs_raw/features_countries_of_interest.geojson — optional FeatureCollection subset

Usage:
  pip install requests
  python gdacs_fetch_raw_json.py
  python gdacs_fetch_raw_json.py --output-dir ./gdacs_results --no-filtered-geojson
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from typing import Any

import requests

from gdelt_fetch import COUNTRIES

GDACS_SEARCH_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
DEFAULT_EVENT_TYPES = "EQ;TC"
DEFAULT_ALERT_LEVELS = "green;orange;red"
CHUNK_MONTHS = 12
REQUEST_TIMEOUT = 120


def _windows(start: date, end: date, months: int) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = cur
        for _ in range(months):
            if nxt.month == 12:
                nxt = date(nxt.year + 1, 1, 1)
            else:
                nxt = date(nxt.year, nxt.month + 1, 1)
        last = nxt - timedelta(days=1)
        if last > end:
            last = end
        out.append((cur, last))
        cur = last + timedelta(days=1)
    return out


def _country_field_matches_interest(country_prop: str, names: list[str]) -> bool:
    """True if GDACS ``country`` string references any of the 12 countries of interest."""
    if not country_prop or not isinstance(country_prop, str):
        return False
    tl = country_prop.lower()
    if "south sudan" in tl:
        return "South Sudan" in names
    for c in names:
        if c == "South Sudan":
            continue
        if c.lower() in tl:
            return True
    return False


def _filter_geojson_features(raw_text: str, names: list[str]) -> dict[str, Any] | None:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    feats = data.get("features")
    if not isinstance(feats, list):
        return None
    kept = []
    for f in feats:
        props = f.get("properties") or {}
        ctry = props.get("country", "")
        if _country_field_matches_interest(str(ctry), names):
            kept.append(f)
    return {"type": "FeatureCollection", "features": kept}


def main() -> None:
    p = argparse.ArgumentParser(description="Download and store raw GDACS API JSON/GeoJSON.")
    p.add_argument("--output-dir", default="./gdacs_results")
    p.add_argument("--start-date", default="2018-01-01")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD (default: today UTC)")
    p.add_argument("--event-types", default=DEFAULT_EVENT_TYPES)
    p.add_argument("--alert-levels", default=DEFAULT_ALERT_LEVELS)
    p.add_argument("--chunk-months", type=int, default=CHUNK_MONTHS)
    p.add_argument(
        "--no-filtered-geojson",
        action="store_true",
        help="Do not write merged features_countries_of_interest.geojson",
    )
    args = p.parse_args()

    end_d = date.today()
    if args.end_date:
        end_d = date.fromisoformat(args.end_date.strip())
    start_d = date.fromisoformat(args.start_date.strip())

    out_root = os.path.abspath(args.output_dir)
    raw_dir = os.path.join(out_root, "gdacs_raw", "responses")
    os.makedirs(raw_dir, exist_ok=True)

    coi_names = list(COUNTRIES.keys())
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json, application/geo+json, */*",
        "User-Agent": "INFORM-NovelData-GDACS/1.0 (research)",
    })

    windows = _windows(start_d, end_d, args.chunk_months)
    manifest: dict[str, Any] = {
        "url": GDACS_SEARCH_URL,
        "event_types": args.event_types,
        "alert_levels": args.alert_levels,
        "start_date": start_d.isoformat(),
        "end_date": end_d.isoformat(),
        "countries_of_interest": coi_names,
        "chunks": [],
    }
    all_filtered_features: list[dict[str, Any]] = []

    for w0, w1 in windows:
        params = {
            "eventlist": args.event_types,
            "fromdate": w0.isoformat(),
            "todate": w1.isoformat(),
            "alertlevel": args.alert_levels,
        }
        fname = f"{w0.isoformat()}_{w1.isoformat()}.json"
        fpath = os.path.join(raw_dir, fname)
        print(f"GET {params['fromdate']} to {params['todate']} ...")
        r = session.get(GDACS_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        text = r.text
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(text)
        nfeat = None
        try:
            j = json.loads(text)
            nfeat = len(j.get("features", []))
        except json.JSONDecodeError:
            pass
        manifest["chunks"].append({
            "file": os.path.relpath(fpath, out_root),
            "params": params,
            "status": r.status_code,
            "feature_count": nfeat,
        })
        if not args.no_filtered_geojson:
            sub = _filter_geojson_features(text, coi_names)
            if sub:
                all_filtered_features.extend(sub["features"])

    man_path = os.path.join(out_root, "gdacs_raw", "fetch_manifest.json")
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    if not args.no_filtered_geojson and all_filtered_features:
        # Deduplicate by eventid + episodeid if present
        seen: set[tuple[Any, ...]] = set()
        uniq: list[dict[str, Any]] = []
        for feat in all_filtered_features:
            props = feat.get("properties") or {}
            key = (props.get("eventtype"), props.get("eventid"), props.get("episodeid"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(feat)
        fc_path = os.path.join(out_root, "gdacs_raw", "features_countries_of_interest.geojson")
        with open(fc_path, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": uniq}, f)
        print(f"Wrote {fc_path} ({len(uniq)} features)")
    elif not args.no_filtered_geojson:
        print("No features matched countries of interest across chunks.")

    print(f"Wrote {man_path}")
    print(f"Raw responses: {raw_dir} ({len(windows)} file(s))")


if __name__ == "__main__":
    main()
