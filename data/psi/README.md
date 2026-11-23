# Cleaned PSI tables

These files are the admin-2 extract behind the Shiny dashboard, plus the national and district tables rebuilt from it.

| File | What it is |
| --- | --- |
| `indicator_components.csv` | Export of `indicator_components.rds` from [acaps_inform_proxy_pin](https://github.com/asmcauliffe/acaps_inform_proxy_pin). 2,730 rows. Country, period, and admin names are filled on every row. |
| `psi_adm2_for_airtable.csv` | The same districts with both readings of each score. Use this to reload Airtable. The GAUL code (`adm2_id`) is the stable key. |
| `psi_national_for_airtable.csv` | Nine country-months: dashboard PSI, reconstructed PSI, and PSI with cyclone dropped. |
| `psi_data_dictionary.csv` | Column definitions. |
| `national.json`, `districts.json`, `examples.json` | The same figures, for the site in `docs/`. |

Rebuild from the repository root:

```bash
python scripts/build_psi_tables.py
```

The script checks Iran’s without-cyclone scores (3.475, 3.416, 2.922), the Afghanistan July dashboard score (1.953), a July dashboard conflict PiN of 0, and the Dand Wa Patan conflict double-count.

`psi_without_cyclone` is the reading that sits within 0.03 of the Iran Airtable totals 3.50, 3.41, and 2.93. Those Airtable hundredths stay the published totals. The dashboard column is the simulator at its default weights. Pixel people-in-need cannot be rebuilt from this extract.

Coverage of this extract only: Ethiopia, 92 admin-2 units, March–May 2022; Afghanistan, 391, July 2025, August 2025, and March 2026; Iran, 427, January–March 2026. The collection scripts cover ten further countries. Those countries are not in this file.
