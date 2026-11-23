# INFORM Novel Data — ACAPS data pipelines

Python and R scripts used to collect and process humanitarian and media indicators for **13 countries of interest** (2018–present), supporting the INFORM Novel Data consultancy.

The PSI Shiny dashboard lives in a **separate repository**. This repo contains the collection pipelines, the cleaned admin-2 and national tables for the three countries in the dashboard extract, and a static site that answers the client questions about that delivery.

## Guide, cleaned results, and the aggregation

The parent ACAPS workspace named in the dashboard README was never pushed. There is no `calculation_agg.R`, no `pin_proxy_update.R`, and no pixel rasters in this repository. What shipped as an input is the admin-2 extract, exported here as [`data/psi/indicator_components.csv`](data/psi/indicator_components.csv).

| Piece | Where |
| --- | --- |
| Interactive site (GitHub Pages from `/docs`) | [`docs/`](docs/) |
| Cleaned tables, with country, period, and admin names filled | [`data/psi/`](data/psi/) |
| Aggregation reconstructed from that extract | [`scripts/build_psi_tables.py`](scripts/build_psi_tables.py) |

The methodology document is the published result. The dashboard is a weight simulator, not a second official estimate. `build_psi_tables.py` rebuilds the national scores two ways (the dashboard defaults, and a reconstruction that counts conflict once) and a third column that also drops cyclone. That third column is the one within 0.03 of the Iran Airtable totals. It does not invent the pixel people-in-need figure.

```bash
python scripts/build_psi_tables.py
python3 -m http.server 4721 --bind 127.0.0.1 --directory docs
```

GitHub Pages: Settings → Pages → Deploy from a branch → `main` → `/docs`. The site is then [https://asmcauliffe.github.io/ACAPS_PSI_Scripts/](https://asmcauliffe.github.io/ACAPS_PSI_Scripts/). `docs/.nojekyll` is included so every file in that folder is served.

## Countries of interest

Afghanistan, Bangladesh, Cuba, Ethiopia, Guatemala, Iran, Kenya, Lebanon, Malawi, Mozambique, Myanmar, South Sudan, Yemen.

## Prerequisites

- **Python 3.10+**
- **R 4.x** with `sf`, `httr`, `jsonlite`, `dplyr`, `tidyr` (see `requirements.R`)
- **IOM DTM API key** (subscription required) — see [Setup](#setup)

## Setup

```bash
# Python dependencies
pip install -r requirements.txt

# R packages (optional one-time)
Rscript requirements.R

# IOM DTM API key (required for displacement data)
cp .env.example .env
# Edit .env and set DTM_API_KEY to your raw subscription key string only.
# On Windows PowerShell:
#   $env:DTM_API_KEY = "your_key"
```

**Security:** Never commit `.env` or key files. For local development you may keep `iom_key.txt` in your working copy (it is listed in `.gitignore`); use only the `primary_key` value as `DTM_API_KEY`, not the whole file. If keys were ever logged or shared, rotate them in the IOM Developer Portal.

## Boundary setup (before GDACS R pipeline)

```bash
Rscript country_adm2_shapefiles/shapefiles_combination.R
```

See [country_adm2_shapefiles/README.md](country_adm2_shapefiles/README.md) for file list and HDX sources.

## Recommended run order

Run all commands from the **repository root**.

### GDELT (events + GKG themes + Goldstein)

```bash
# 1. GDELT 2.0 events (Part 1 only; DOC API Part 2 is optional and rate-limited)
python gdelt_fetch.py --parts 1

# 2. Bulk GKG themes/tone (long run; use --resume after interruption)
python gkg_bulk_themes.py --start-year 2018 --end-year 2026 --resume

# 3. Combined monthly panel (all 13 countries)
python postprocess_gkg_themes.py --output-dir ./gdelt_results --group all

# 4. National Goldstein z-scores
python gdelt_goldstein_zscores.py --events-csv ./gdelt_results/events_monthly.csv

# 5. Copy canonical deliverables to production/
python organize_gdelt_production.py --output-dir ./gdelt_results
```

### Other sources

```bash
# IOM DTM displacement (requires DTM_API_KEY)
python iom_dtm_fetch.py

# GDACS raw GeoJSON audit trail
python gdacs_fetch_raw_json.py

# GDACS admin2 monthly disaster panel (requires merged boundaries)
Rscript gdacs_processing.R

# Meta movement (HDX)
python meta_movement_fetch.py

# WFP global food prices (HDX)
python wfp_food_prices_fetch.py

# Wikipedia edit frequency + z-scores
python wikipedia_edit_frequency.py
```

## Scripts reference

| Script | Output directory | Main outputs |
|--------|------------------|--------------|
| `scripts/build_psi_tables.py` | `data/psi/` and `docs/data/` | National and admin-2 PSI tables from `indicator_components.csv` |
| `gdelt_fetch.py` | `./gdelt_results/` | `events_monthly.csv`, optional `themes_monthly.csv` |
| `gkg_bulk_themes.py` | `./gdelt_results/` | `themes_monthly_gkg.csv` |
| `postprocess_gkg_themes.py` | `./gdelt_results/` | `combined_monthly_gkg_all_countries.csv` |
| `gdelt_goldstein_zscores.py` | beside events CSV | `goldstein_national_monthly.csv` |
| `organize_gdelt_production.py` | `./gdelt_results/production/` | Curated copies for delivery |
| `iom_dtm_fetch.py` | `./dtm_results/` | Admin0/1/2 displacement CSVs |
| `gdacs_fetch_raw_json.py` | `./gdacs_results/` | Raw API GeoJSON per window |
| `gdacs_processing.R` | `./gdacs_results/` | `gdacs_panel_long.csv`, `gdacs_panel_wide.csv` |
| `meta_movement_fetch.py` | `./meta_mobility_results/` | Filtered movement CSVs |
| `wfp_food_prices_fetch.py` | `./wfp_food_prices_results/` | Long + 2018–2026 panel CSVs |
| `wikipedia_edit_frequency.py` | `./wikipedia_results/` | National and page-level edit metrics + z-scores |

## Example outputs

The [`examples/`](examples/) folder contains snapshot CSVs from the May 2026 run (events, Goldstein z-scores, combined GKG panel). See [examples/README.md](examples/README.md).

## Data sources

| Source | API / access |
|--------|----------------|
| GDELT 2.0 | Public event files + GKG bulk HTTP |
| GDACS | Public REST API |
| IOM DTM | API v3 (subscription key) |
| HDX | WFP food prices, Meta movement |
| Wikipedia | MediaWiki API (public) |

## Known limitations

- **GDELT DOC API** (Part 2 of `gdelt_fetch.py`) is heavily rate-limited; use `gkg_bulk_themes.py` for theme data instead.
- **GKG midnight UTC slices** sometimes return HTTP 404; skipped days are not marked done — re-run with `--resume`.
- **GKG bulk cache** (`gdelt_results/_gkg_cache/`) can exceed 15 GB; it is gitignored. Delete to reclaim disk space after a successful run.
- **IOM DTM** requires a valid `DTM_API_KEY`; use the raw key string, not the multi-line portal export file.
- **Pixel people in need, the tier CSVs, and `calculation_agg.R`** were not in the parent workspace that reached this repo. `build_psi_tables.py` aggregates the dashboard extract only. Do not treat its district conflict PiN as the pixel figure in the methodology document.

## License

See [LICENSE](LICENSE). Use is subject to the consultancy engagement terms.
