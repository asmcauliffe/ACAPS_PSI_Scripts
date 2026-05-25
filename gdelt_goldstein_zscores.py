"""
gdelt_goldstein_zscores.py
--------------------------
Compute **national** (country-level) monthly Goldstein scale as an event-count-weighted
average across CAMEO groups, then **z-scores per country** using that country's full
in-sample time series (same logic as Wikipedia national z-scores).

Input : events_monthly.csv (long format from gdelt_fetch Part 1)
Output: beside input, or --output path:
        goldstein_national_monthly.csv

Usage:
  python gdelt_goldstein_zscores.py
  python gdelt_goldstein_zscores.py --events-csv ./gdelt_results/production/events_monthly.csv
"""

from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd


def weighted_goldstein_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (country, country_group, year_month) with weighted avg Goldstein."""
    d = df.copy()
    d["event_count"] = pd.to_numeric(d["event_count"], errors="coerce").fillna(0)
    d["avg_goldstein"] = pd.to_numeric(d["avg_goldstein"], errors="coerce")
    ec, ag = d["event_count"], d["avg_goldstein"]
    valid = (ec > 0) & ag.notna()
    d["_num"] = np.where(valid, ag * ec, 0.0)
    d["_den"] = np.where(valid, ec, 0.0)
    g = d.groupby(["country", "country_group", "year_month"], as_index=False).agg(
        _num_sum=("_num", "sum"),
        _den_sum=("_den", "sum"),
        total_events=("event_count", "sum"),
    )
    g["weighted_avg_goldstein"] = np.where(
        g["_den_sum"] > 0, g["_num_sum"] / g["_den_sum"], np.nan
    )
    g = g.drop(columns=["_num_sum", "_den_sum"])
    return g.sort_values(["country", "year_month"])


def national_z(s: pd.Series) -> pd.Series:
    s = s.astype(float)
    if len(s) < 2:
        return pd.Series(0.0, index=s.index)
    mu, sd = s.mean(), s.std(ddof=1)
    if sd == 0 or np.isnan(sd):
        return pd.Series(0.0, index=s.index)
    return (s - mu) / sd


def main() -> None:
    p = argparse.ArgumentParser(description="National Goldstein z-scores from GDELT events_monthly.")
    p.add_argument(
        "--events-csv",
        default=None,
        help="Path to events_monthly.csv (default: ./gdelt_results/events_monthly.csv if exists)",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Output CSV path (default: <events_dir>/goldstein_national_monthly.csv)",
    )
    args = p.parse_args()

    candidates = [
        args.events_csv,
        os.path.join("gdelt_results", "production", "events_monthly.csv"),
        os.path.join("gdelt_results", "events_monthly.csv"),
    ]
    path = next((c for c in candidates if c and os.path.isfile(c)), None)
    if not path:
        raise SystemExit(
            "No events_monthly.csv found. Pass --events-csv or place file under gdelt_results/."
        )

    df = pd.read_csv(path, low_memory=False)
    need = {"country", "country_group", "year_month", "event_count", "avg_goldstein"}
    if not need.issubset(df.columns):
        raise SystemExit(f"events CSV must have columns: {sorted(need)}")

    ym = df["year_month"].astype(str)
    df = df[ym.str.match(r"^\d{4}-\d{2}$", na=False)].copy()

    nat = weighted_goldstein_monthly(df)
    nat["z_score_goldstein_national"] = nat.groupby("country")["weighted_avg_goldstein"].transform(
        national_z
    )
    nat["is_anomaly_goldstein"] = nat["z_score_goldstein_national"].abs() > 2.0
    nat["z_score_goldstein_national"] = nat["z_score_goldstein_national"].round(4)
    nat["weighted_avg_goldstein"] = nat["weighted_avg_goldstein"].round(6)

    out = args.output or os.path.join(os.path.dirname(os.path.abspath(path)), "goldstein_national_monthly.csv")
    nat.to_csv(out, index=False)
    print(f"Wrote {out} ({len(nat)} rows)")


if __name__ == "__main__":
    main()
