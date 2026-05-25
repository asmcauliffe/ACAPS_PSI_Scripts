# Example outputs (snapshot)

These files are **reference deliverables** from the May 2026 production run. Re-run the pipelines in the repository root to refresh them.

| File | Rows | Description |
|------|------|-------------|
| `events_monthly.csv` | 6,806 | GDELT 2.0 events by country, month, and CAMEO group |
| `goldstein_national_monthly.csv` | 1,331 | National event-weighted Goldstein scale and z-scores (full in-sample period per country) |
| `combined_monthly_gkg.csv` | 1,312 | Monthly panel: GDELT events + bulk GKG theme article counts and tone (13 countries) |

**Countries:** Afghanistan, Bangladesh, Cuba, Ethiopia, Guatemala, Iran, Kenya, Lebanon, Malawi, Mozambique, Myanmar, South Sudan, Yemen.

**Date range:** 2018-01 through 2026-05 (varies slightly by source).

**Panel columns in `combined_monthly_gkg.csv`:** event totals, Goldstein/tone aggregates, armed violence and protest counts, and theme fields (`conflict_violence`, `displacement`, `famine`, `food_security`, `humanitarian_crisis`, `natural_disaster`, `protest_civil`, `economic_crisis`, `food_prices`) with `_articles` and `_avg_tone` suffixes.
