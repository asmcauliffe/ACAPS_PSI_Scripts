"""
postprocess_gkg_themes.py
-------------------------
After ``gkg_bulk_themes.py`` finishes, filter monthly GKG output to **countries of interest**
(extrapolation targets by default) and optionally rebuild the wide **combined_monthly**-style
panel using GKG theme columns + existing ``events_monthly.csv``.

Usage:
  python postprocess_gkg_themes.py --output-dir ./gdelt_results
  python postprocess_gkg_themes.py --output-dir ./gdelt_results --group training
  python postprocess_gkg_themes.py --output-dir ./gdelt_results --group all
    -> themes_monthly_gkg_all_countries.csv, combined_monthly_gkg_all_countries.csv
"""

from __future__ import annotations

import argparse
import os

import pandas as pd

from gdelt_fetch import COUNTRIES, filter_events_year_month


def country_order(group: str) -> list[str]:
    ordered = sorted(COUNTRIES.items(), key=lambda x: x[1]["priority"])
    if group == "all":
        return [k for k, _ in ordered]
    return [k for k, v in ordered if v["group"] == group]


def build_combined(events_df: pd.DataFrame, themes_df: pd.DataFrame) -> pd.DataFrame:
    """Mirror gdelt_fetch Part 3 merge (events pivot + theme pivot)."""
    combined_parts: list[pd.DataFrame] = []

    if not events_df.empty:
        conflict_df = events_df[events_df["cameo_group"] == "armed_violence"].copy()
        protest_df = events_df[events_df["cameo_group"] == "protest"].copy()
        all_df = events_df.groupby(["country", "country_group", "year_month"]).agg(
            total_events=("event_count", "sum"),
            overall_avg_goldstein=("avg_goldstein", "mean"),
            overall_avg_tone=("avg_tone", "mean"),
            total_mentions=("total_mentions", "sum"),
            total_sources=("total_sources", "sum"),
        ).reset_index()
        conflict_pivot = conflict_df[
            ["country", "year_month", "event_count", "avg_goldstein", "avg_tone"]
        ].rename(
            columns={
                "event_count": "armed_violence_events",
                "avg_goldstein": "armed_violence_goldstein",
                "avg_tone": "armed_violence_tone",
            }
        )
        protest_pivot = protest_df[["country", "year_month", "event_count"]].rename(
            columns={"event_count": "protest_events"}
        )
        combined_events = all_df.merge(
            conflict_pivot, on=["country", "year_month"], how="left"
        )
        combined_events = combined_events.merge(
            protest_pivot, on=["country", "year_month"], how="left"
        )
        combined_parts.append(combined_events)

    if not themes_df.empty:
        themes_pivot = themes_df.pivot_table(
            index=["country", "year_month"],
            columns="theme_key",
            values=["articles", "avg_tone"],
            aggfunc="first",
            observed=False,
        )
        themes_pivot.columns = [f"{col[1]}_{col[0]}" for col in themes_pivot.columns]
        themes_pivot = themes_pivot.reset_index()
        combined_parts.append(themes_pivot)

    if not combined_parts:
        return pd.DataFrame()
    if len(combined_parts) == 2:
        combined_df = combined_parts[0].merge(
            combined_parts[1], on=["country", "year_month"], how="outer"
        )
    else:
        combined_df = combined_parts[0]
    return combined_df.sort_values(["country", "year_month"])


def main() -> None:
    p = argparse.ArgumentParser(
        description="Filter GKG monthly themes to priority countries and build combined panel."
    )
    p.add_argument("--output-dir", default="./gdelt_results")
    p.add_argument(
        "--group",
        choices=["extrapolation", "training", "all"],
        default="extrapolation",
        help=(
            "extrapolation=6 targets; training=7 countries; all=13 countries "
            "(priority order)."
        ),
    )
    p.add_argument(
        "--skip-combined",
        action="store_true",
        help="Only write filtered long-format themes CSV, not combined_monthly_gkg_*.csv",
    )
    args = p.parse_args()

    out_dir = args.output_dir
    themes_path = os.path.join(out_dir, "themes_monthly_gkg.csv")
    events_path = os.path.join(out_dir, "events_monthly.csv")

    if not os.path.isfile(themes_path):
        raise SystemExit(f"Missing {themes_path} — run gkg_bulk_themes.py first.")

    df = pd.read_csv(themes_path)
    names = country_order(args.group)
    df_f = df[df["country"].isin(names)].copy()
    df_f["country"] = pd.Categorical(df_f["country"], categories=names, ordered=True)
    df_f = df_f.sort_values(["country", "theme_key", "year_month"])

    drop_empty = [c for c in ("total_articles_gdelt", "pct_coverage") if c in df_f.columns]
    if drop_empty:
        all_na = df_f[drop_empty].isna() | (df_f[drop_empty].astype(str) == "")
        if bool(all_na.all().all()):
            df_f = df_f.drop(columns=drop_empty)

    suffix = "all_countries" if args.group == "all" else args.group
    long_out = os.path.join(out_dir, f"themes_monthly_gkg_{suffix}.csv")
    df_f.to_csv(long_out, index=False)
    print(f"Wrote {long_out} ({len(df_f)} rows, countries: {names})")

    if args.skip_combined:
        return

    ym = df_f["year_month"].astype(str)
    start_year = int(ym.str.slice(0, 4).min())
    end_year = int(ym.str.slice(0, 4).max())

    events_df = pd.DataFrame()
    if os.path.isfile(events_path):
        events_df = pd.read_csv(events_path)
        events_df = events_df[events_df["country"].isin(names)]
        events_df = filter_events_year_month(events_df, start_year, end_year)
    else:
        print(f"Note: no {events_path} — combined output will be themes only.")

    combined = build_combined(events_df, df_f)
    wide_out = os.path.join(out_dir, f"combined_monthly_gkg_{suffix}.csv")
    combined.to_csv(wide_out, index=False)
    print(f"Wrote {wide_out} ({len(combined)} rows)")


if __name__ == "__main__":
    main()
