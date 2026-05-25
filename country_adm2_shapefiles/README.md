# Administrative level 2 boundaries

Per-country ADM2 GeoJSON files for the 13 countries of interest. Sources are OCHA HDX COD-AB packages (administrative boundaries).

| File | Country | ISO3 |
|------|---------|------|
| `afg_admin2.geojson` | Afghanistan | AFG |
| `bgd_admin2.geojson` | Bangladesh | BGD |
| `cub_admin2.geojson` | Cuba | CUB |
| `eth_admin2.geojson` | Ethiopia | ETH |
| `gtm_admin2.geojson` | Guatemala | GTM |
| `irn_admin2.geojson` | Iran | IRN |
| `ken_admin2.geojson` | Kenya | KEN |
| `lbn_admin2.geojson` | Lebanon | LBN |
| `mmr_admin2.geojson` | Myanmar | MMR |
| `moz_admin2.geojson` | Mozambique | MOZ |
| `mwi_admin2_em.geojson` | Malawi | MWI |
| `ssd_admin2.geojson` | South Sudan | SSD |
| `yem_admin2.geojson` | Yemen | YEM |

## Build merged boundaries

The merged file `countries_of_interest.geojson` (~316 MB) is **not** stored in git. Build it before running `gdacs_processing.R`:

```bash
# From repository root
Rscript country_adm2_shapefiles/shapefiles_combination.R
```

This writes `country_adm2_shapefiles/countries_of_interest.geojson` with columns: `adm2_name`, `adm0_name`, `adm2_pcode`, `adm1_name`, `geometry`.

## HDX sources

Download updated boundaries from [data.humdata.org](https://data.humdata.org) COD-AB datasets (search by country ISO3). Replace the corresponding `*_admin2*.geojson` file and re-run the merge script.
