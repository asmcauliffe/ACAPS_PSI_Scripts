#!/usr/bin/env python3
"""Rebuild the cleaned PSI tables from data/psi/indicator_components.csv.

That CSV is the admin-2 extract shipped with the Shiny dashboard
(asmcauliffe/acaps_inform_proxy_pin, indicator_components.rds). This script
does not invent inputs and does not rebuild the pixel PiN. The files named
in the dashboard README — calculation_agg.R, pin_proxy_update.R, and the
tier CSVs — were never pushed. This is the aggregation those steps were
described as feeding, reconstructed from the extract that did ship.

It labels missingness, keeps the geography fields Airtable dropped, and
computes two readings of the same rows:

* dashboard — what R/psi_helpers.R does at the default weights
* reconstructed — conflict counted once, severity columns rather than PiN
  gates, IOM alone where Facebook is an explicit zero, missing drivers
  dropped from the national mean

A third national figure, psi_without_cyclone, also drops cyclone. For Iran
that is the reading that sits within 0.03 of the Airtable totals
3.50, 3.41 and 2.93.

Outputs land in data/psi/ (the tables in the repository) and docs/data/
(the copies the GitHub Pages site loads).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "data" / "psi" / "indicator_components.csv"

# Within-driver shares. 0.45 and 0.35 renormalise to 0.5625 and 0.4375.
ACLED_C = 0.45 / 0.80
UCDP_C = 0.35 / 0.80
PROTEST = 0.25 / 0.75
NLR = 0.50 / 0.75
RAIN = 0.45
GDACS_FLOOD = 0.55
SPEI = 0.50
NDVI_CROP = 0.30
NDVI_GRASS = 0.20
IOM_W = 0.60
FB_W = 0.40
WIKI_W = 0.20
GDELT_W = 0.25

DRIVERS = [
    "Conflict",
    "Political",
    "Flood",
    "Drought",
    "Earthquake",
    "Cyclone",
    "Displacement",
]
DRIVER_W = np.array([0.20, 0.15, 0.10, 0.15, 0.10, 0.10, 0.20])

PERIOD_START = {
    "March 2022": "2022-03-01",
    "April 2022": "2022-04-01",
    "May 2022": "2022-05-01",
    "July 2025": "2025-07-01",
    "August 2025": "2025-08-01",
    "January 2026": "2026-01-01",
    "February 2026": "2026-02-01",
    "March 2026": "2026-03-01",
}

SPLIT_RATIO = 0.45 / 0.35


def r3(x: float | None) -> float | None:
    if x is None or not np.isfinite(x):
        return None
    return round(float(x), 3)


def r6(x: float) -> float:
    return round(float(x), 6)


def is_split(a: np.ndarray, u: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.divide(a, u, out=np.full_like(a, np.nan, dtype=float), where=u > 0)
    return (a > 0) & (u > 0) & np.isclose(ratio, SPLIT_RATIO, rtol=1e-4, atol=1e-6)


def pop_mean(values: np.ndarray, pop: np.ndarray, nan_as_zero: bool) -> float:
    """Population-weighted mean.

    nan_as_zero reproduces the dashboard: sum(x * pop, na.rm=TRUE) / sum(pop)
    turns an all-missing driver into 0. The reconstructed reading returns NaN
    when every district is missing so the driver can be dropped.
    """
    values = values.astype(float)
    pop = pop.astype(float)
    ok = np.isfinite(values) & np.isfinite(pop)
    if not ok.any():
        return 0.0 if nan_as_zero else float("nan")
    if nan_as_zero:
        filled = np.where(ok, values, 0.0)
        return float(np.sum(filled * np.where(np.isfinite(pop), pop, 0.0)) / np.sum(pop[np.isfinite(pop)]))
    return float(np.sum(values[ok] * pop[ok]) / np.sum(pop[ok]))


def national_psi(
    scores: np.ndarray,
    present: np.ndarray,
) -> dict:
    w = DRIVER_W.copy()
    w = np.where(present, w, 0.0)
    w = w / w.sum()
    finite = np.where(np.isfinite(scores), scores, 0.0)
    wmean = float(np.sum(finite * w))
    mx = float(np.max(np.where(present, scores, -np.inf)))
    psi = 0.6 * mx + 0.4 * wmean
    return {
        "psi": psi,
        "max": mx,
        "wmean": wmean,
        "weights": {name: float(weight) for name, weight in zip(DRIVERS, w)},
        "scores": {name: float(score) for name, score in zip(DRIVERS, scores)},
    }


def main() -> None:
    if not COMPONENTS.exists():
        raise SystemExit(f"Missing {COMPONENTS}. Export indicator_components.rds to that path.")
    frame = pd.read_csv(COMPONENTS)
    frame["period_start"] = frame["period"].map(PERIOD_START)
    if frame["period_start"].isna().any():
        missing = sorted(frame.loc[frame["period_start"].isna(), "period"].unique())
        raise SystemExit(f"Unmapped periods: {missing}")

    district_rows: list[dict] = []
    national_rows: list[dict] = []

    for (country, period), group in frame.groupby(["country", "period"], sort=False):
        group = group.copy()
        acled = group["acled_conflict_adm2_sev"].to_numpy(dtype=float)
        ucdp = group["ucdp_conflict_adm2_sev"].to_numpy(dtype=float)
        protests = group["acled_protests_adm2_sev"].to_numpy(dtype=float)
        protests_clean = np.maximum(protests, 0.0)
        nlr_sev = group["nlr_severity"].to_numpy(dtype=float)
        nlr_gate = group["nlr_gate"].to_numpy(dtype=float)
        rain = group["high_rainfall_adm2_sev"].to_numpy(dtype=float)
        flood_sev = group["gdacs_flood_sev"].to_numpy(dtype=float)
        glofas = group["glofas_gate"].to_numpy(dtype=float)
        spei = group["spei_adm2_sev"].to_numpy(dtype=float)
        crop_sev = group["ndvi_crop_severity"].to_numpy(dtype=float)
        grass_sev = group["ndvi_grass_severity"].to_numpy(dtype=float)
        crop_gate = group["ndvi_crop_gate"].to_numpy(dtype=float)
        grass_gate = group["ndvi_grass_gate"].to_numpy(dtype=float)
        eq = group["gdacs_eq_sev"].to_numpy(dtype=float)
        eq_gate = group["earthquake_gate"].to_numpy(dtype=float)
        cyc = group["gdacs_cyc_sev"].to_numpy(dtype=float)
        cyc_gate = group["cyclone_gate"].to_numpy(dtype=float)
        iom = group["iom_severity"].to_numpy(dtype=float)
        fbook = group["fbook_severity"].to_numpy(dtype=float)
        idp = group["idp_cnt"].to_numpy(dtype=float)
        pop = group["tltpop"].to_numpy(dtype=float)
        split = is_split(acled, ucdp)
        period_ucdp_all_zero = bool(np.all(ucdp == 0))

        if period_ucdp_all_zero and float(np.max(acled)) > 5.625 + 1e-6:
            conflict_storage = "acled_holds_full_score"
        elif bool(split.any()):
            conflict_storage = "pre_split"
        else:
            conflict_storage = "independent"

        conflict_dashboard = ACLED_C * acled + UCDP_C * ucdp
        if conflict_storage == "acled_holds_full_score":
            conflict_reconstructed = acled.copy()
            conflict_rule = np.array(["acled_holds_full_score"] * len(group))
        else:
            conflict_reconstructed = np.where(split, acled + ucdp, conflict_dashboard)
            conflict_rule = np.where(split, "pre_split_sum", "weighted_once")

        political_dashboard = PROTEST * protests + NLR * nlr_gate
        political_reconstructed = PROTEST * protests_clean + NLR * nlr_sev
        flood = RAIN * rain + GDACS_FLOOD * flood_sev
        drought_dashboard = SPEI * spei + NDVI_CROP * crop_gate + NDVI_GRASS * grass_gate
        drought_reconstructed = SPEI * spei + NDVI_CROP * crop_sev + NDVI_GRASS * grass_sev
        displacement_dashboard = np.where(
            np.isfinite(iom),
            IOM_W * np.where(np.isfinite(iom), iom, 0.0) + FB_W * fbook,
            np.nan,
        )
        displacement_reconstructed = iom.copy()

        pin_conflict_dashboard = np.where(np.maximum(acled, ucdp) >= 3.3, pop, 0.0)
        pin_conflict_reconstructed = np.where(conflict_reconstructed >= 3.3, pop, 0.0)
        pin_political = np.maximum(
            np.where(protests >= 3.3, pop, 0.0),
            pop * nlr_gate,
        )
        pin_flood = np.where(rain >= 3.3, pop, 0.0) * glofas
        pin_drought = np.where(spei >= 3.3, pop, 0.0) * ((crop_gate + grass_gate) / 2.0)
        pin_eq = pop * eq_gate
        pin_cyc = pop * cyc_gate

        iom_missing = bool(np.all(~np.isfinite(iom)))
        facebook_all_zero = bool(np.all(fbook == 0))

        def pack(values: np.ndarray, nan_as_zero: bool) -> float:
            return pop_mean(values, pop, nan_as_zero=nan_as_zero)

        dash_scores = np.array(
            [
                pack(conflict_dashboard, True),
                pack(political_dashboard, True),
                pack(flood, True),
                pack(drought_dashboard, True),
                pack(eq, True),
                pack(cyc, True),
                pack(displacement_dashboard, True),
            ]
        )
        wiki = float(group["wikipedia_severity"].iloc[0])
        gdelt = float(group["gdelt_severity"].iloc[0])
        dash_scores[0] = dash_scores[0] * (1 - WIKI_W) + wiki * WIKI_W
        dash_scores[1] = dash_scores[1] * (1 - GDELT_W) + gdelt * GDELT_W
        dash = national_psi(dash_scores, np.ones(7, dtype=bool))

        recon_raw = [
            pack(conflict_reconstructed, False),
            pack(political_reconstructed, False),
            pack(flood, False),
            pack(drought_reconstructed, False),
            pack(eq, False),
            pack(cyc, False),
            pack(displacement_reconstructed, False),
        ]
        recon_scores = np.array(recon_raw, dtype=float)
        recon_present = np.array([np.isfinite(x) for x in recon_scores])
        # Blend only drivers that exist. Wikipedia and GDELT are national constants.
        if recon_present[0]:
            recon_scores[0] = recon_scores[0] * (1 - WIKI_W) + wiki * WIKI_W
        if recon_present[1]:
            recon_scores[1] = recon_scores[1] * (1 - GDELT_W) + gdelt * GDELT_W
        recon = national_psi(recon_scores, recon_present)
        without_cyclone_present = recon_present.copy()
        without_cyclone_present[DRIVERS.index("Cyclone")] = False
        without_cyclone = national_psi(recon_scores, without_cyclone_present)

        notes: list[str] = []
        if conflict_storage == "pre_split":
            notes.append(
                "ACLED and UCDP columns are one combined conflict score split 0.5625/0.4375."
            )
        elif conflict_storage == "acled_holds_full_score":
            notes.append(
                "UCDP is 0 for every district and ACLED exceeds 5.625, so ACLED holds the full conflict score."
            )
        else:
            notes.append("ACLED and UCDP are independent severity series.")
        if iom_missing:
            notes.append("IOM severity is missing. Displacement is left out of the reconstructed mean.")
        if facebook_all_zero:
            notes.append("Facebook mobility is an explicit 0, so the reconstructed displacement score is IOM alone.")
        if float(np.max(cyc)) == 0.0:
            notes.append("Cyclone severity is 0. psi_without_cyclone drops that structural zero from the mean.")
        if bool(np.any(protests < 0)):
            notes.append("One protest severity is negative and is treated as 0 in the reconstructed score only.")

        national_rows.append(
            {
                "country": country,
                "period": period,
                "period_start": PERIOD_START[period],
                "adm2_count": int(group["adm2_id"].nunique()),
                "population": int(round(float(np.nansum(pop)))),
                "psi_dashboard": r3(dash["psi"]),
                "psi_reconstructed": r3(recon["psi"]),
                "psi_without_cyclone": r3(without_cyclone["psi"]),
                "max_dashboard": r3(dash["max"]),
                "wmean_dashboard": r3(dash["wmean"]),
                "max_reconstructed": r3(recon["max"]),
                "wmean_reconstructed": r3(recon["wmean"]),
                "max_without_cyclone": r3(without_cyclone["max"]),
                "wmean_without_cyclone": r3(without_cyclone["wmean"]),
                "drivers_dashboard": {k: r3(v) for k, v in dash["scores"].items()},
                "drivers_reconstructed": {
                    k: r3(v) if recon_present[DRIVERS.index(k)] else None
                    for k, v in recon["scores"].items()
                },
                "weights_reconstructed": {k: r3(v) for k, v in recon["weights"].items()},
                "weights_without_cyclone": {k: r3(v) for k, v in without_cyclone["weights"].items()},
                "wikipedia_severity": r3(wiki),
                "gdelt_severity": r3(gdelt),
                "pin_conflict_dashboard": int(round(float(pin_conflict_dashboard.sum()))),
                "pin_conflict_district_reconstructed": int(round(float(pin_conflict_reconstructed.sum()))),
                "pin_political_dashboard": int(round(float(pin_political.sum()))),
                "pin_flood_dashboard": int(round(float(pin_flood.sum()))),
                "pin_drought_dashboard": int(round(float(pin_drought.sum()))),
                "pin_earthquake_dashboard": int(round(float(pin_eq.sum()))),
                "pin_cyclone_dashboard": int(round(float(pin_cyc.sum()))),
                "pin_displacement": int(round(float(np.nansum(idp)))),
                "districts_conflict_pin_dashboard": int(np.sum(pin_conflict_dashboard > 0)),
                "districts_conflict_pin_reconstructed": int(np.sum(pin_conflict_reconstructed > 0)),
                "conflict_storage": conflict_storage,
                "iom_missing": iom_missing,
                "facebook_all_zero": facebook_all_zero,
                "notes": notes,
            }
        )

        for i, (_, row) in enumerate(group.iterrows()):
            protest_note = ""
            if protests[i] < 0:
                protest_note = "Negative severity in the source file. Reconstructed score uses 0."
            district_rows.append(
                {
                    "country": country,
                    "period": period,
                    "period_start": PERIOD_START[period],
                    "admin1_name": row["gaul1_name"],
                    "admin2_name": row["gaul2_name"],
                    "adm2_id": int(row["adm2_id"]) if float(row["adm2_id"]).is_integer() else row["adm2_id"],
                    "population": r3(pop[i]),
                    "acled_conflict_adm2_sev": r3(acled[i]),
                    "ucdp_conflict_adm2_sev": r3(ucdp[i]),
                    "conflict_column_rule": conflict_rule[i],
                    "acled_protests_adm2_sev": r3(protests[i]),
                    "acled_protests_note": protest_note,
                    "nlr_severity": r3(nlr_sev[i]),
                    "nlr_gate": r3(nlr_gate[i]),
                    "high_rainfall_adm2_sev": r3(rain[i]),
                    "gdacs_flood_sev": r3(flood_sev[i]),
                    "glofas_gate": r3(glofas[i]),
                    "spei_adm2_sev": r3(spei[i]),
                    "ndvi_crop_severity": r3(crop_sev[i]),
                    "ndvi_grass_severity": r3(grass_sev[i]),
                    "ndvi_crop_gate": r3(crop_gate[i]),
                    "ndvi_grass_gate": r3(grass_gate[i]),
                    "gdacs_eq_sev": r3(eq[i]),
                    "earthquake_gate": r3(eq_gate[i]),
                    "gdacs_cyc_sev": r3(cyc[i]),
                    "cyclone_gate": r3(cyc_gate[i]),
                    "iom_severity": r3(iom[i]),
                    "iom_status": "missing" if not np.isfinite(iom[i]) else "present",
                    "fbook_severity": r3(fbook[i]),
                    "facebook_status": "explicit_zero" if fbook[i] == 0 else "measured",
                    "idp_count": r3(idp[i]),
                    "wikipedia_severity": r3(wiki),
                    "gdelt_severity": r3(gdelt),
                    "conflict_dashboard": r3(conflict_dashboard[i]),
                    "conflict_reconstructed": r3(conflict_reconstructed[i]),
                    "political_dashboard": r3(political_dashboard[i]),
                    "political_reconstructed": r3(political_reconstructed[i]),
                    "flood_severity": r3(flood[i]),
                    "drought_dashboard": r3(drought_dashboard[i]),
                    "drought_reconstructed": r3(drought_reconstructed[i]),
                    "earthquake_severity": r3(eq[i]),
                    "cyclone_severity": r3(cyc[i]),
                    "displacement_dashboard": r3(displacement_dashboard[i]),
                    "displacement_reconstructed": r3(displacement_reconstructed[i]),
                    "pin_conflict_dashboard": int(round(pin_conflict_dashboard[i])),
                    "pin_conflict_district_reconstructed": int(round(pin_conflict_reconstructed[i])),
                    "pin_political_dashboard": int(round(pin_political[i])),
                    "pin_flood_dashboard": int(round(pin_flood[i])),
                    "pin_drought_dashboard": int(round(pin_drought[i])),
                    "pin_earthquake_dashboard": int(round(pin_eq[i])),
                    "pin_cyclone_dashboard": int(round(pin_cyc[i])),
                    "pin_displacement": int(round(idp[i])) if np.isfinite(idp[i]) else 0,
                }
            )

    national_rows.sort(key=lambda r: (r["country"], r["period_start"]))
    district_rows.sort(key=lambda r: (r["country"], r["period_start"], r["admin1_name"], r["admin2_name"]))

    examples = build_examples(district_rows, national_rows)
    check(national_rows, examples)

    tables = ROOT / "data" / "psi"
    site = ROOT / "docs" / "data"
    for path in (tables, site):
        path.mkdir(parents=True, exist_ok=True)

    payload = {
        "source": "data/psi/indicator_components.csv, exported from indicator_components.rds in asmcauliffe/acaps_inform_proxy_pin",
        "formula": "PSI = 0.6 * highest driver + 0.4 * weighted mean",
        "published_iran_airtable": {
            "January 2026": 3.50,
            "February 2026": 3.41,
            "March 2026": 2.93,
        },
        "rows": national_rows,
    }
    text = json.dumps(payload, indent=2)
    (tables / "national.json").write_text(text)
    (site / "national.json").write_text(text)
    browser_keys = [
        "country", "period", "admin1_name", "admin2_name", "adm2_id", "population",
        "conflict_dashboard", "conflict_reconstructed", "conflict_column_rule",
        "acled_conflict_adm2_sev", "ucdp_conflict_adm2_sev",
        "political_dashboard", "political_reconstructed",
        "drought_dashboard", "drought_reconstructed",
        "flood_severity", "earthquake_severity", "cyclone_severity",
        "displacement_dashboard", "displacement_reconstructed",
        "iom_severity", "iom_status", "fbook_severity",
        "pin_conflict_dashboard", "pin_conflict_district_reconstructed", "pin_displacement",
        "spei_adm2_sev", "acled_protests_note",
    ]
    browser_rows = [{key: row[key] for key in browser_keys} for row in district_rows]
    districts_text = json.dumps(browser_rows)
    (tables / "districts.json").write_text(districts_text)
    (site / "districts.json").write_text(districts_text)
    examples_text = json.dumps(examples, indent=2)
    (tables / "examples.json").write_text(examples_text)
    (site / "examples.json").write_text(examples_text)

    write_csv(tables / "psi_national_for_airtable.csv", flatten_national(national_rows))
    write_csv(tables / "psi_adm2_for_airtable.csv", district_rows)
    write_csv(tables / "psi_data_dictionary.csv", dictionary_rows())
    for name in (
        "psi_national_for_airtable.csv",
        "psi_adm2_for_airtable.csv",
        "psi_data_dictionary.csv",
        "indicator_components.csv",
    ):
        (site / name).write_bytes((tables / name).read_bytes())

    print(f"national rows: {len(national_rows)}")
    print(f"district rows: {len(district_rows)}")
    for row in national_rows:
        print(
            f"{row['country']:12} {row['period']:16} "
            f"dash {row['psi_dashboard']:.3f}  recon {row['psi_reconstructed']:.3f}  "
            f"no-cyc {row['psi_without_cyclone']:.3f}  "
            f"conflict PiN dash {row['pin_conflict_dashboard']:,}  "
            f"district {row['pin_conflict_district_reconstructed']:,}"
        )


def build_examples(districts: list[dict], national: list[dict]) -> dict:
    def find(**kwargs: str) -> dict:
        for row in districts:
            if all(row[key] == value for key, value in kwargs.items()):
                return row
        raise SystemExit(f"Example row not found: {kwargs}")

    dand = find(country="Afghanistan", period="March 2026", admin2_name="Dand Wa Patan")
    kabul_july = find(country="Afghanistan", period="July 2025", admin2_name="Kabul", admin1_name="Kabul")
    bagrami_july = find(country="Afghanistan", period="July 2025", admin2_name="Bagrami")
    kabul_aug = find(country="Afghanistan", period="August 2025", admin2_name="Kabul", admin1_name="Kabul")
    bagrami_aug = find(country="Afghanistan", period="August 2025", admin2_name="Bagrami")
    majang = find(country="Ethiopia", period="March 2022", admin2_name="Majang")

    def nat(country: str, period: str) -> dict:
        for row in national:
            if row["country"] == country and row["period"] == period:
                return row
        raise SystemExit(f"Missing national row {country} {period}")

    return {
        "dand_wa_patan": {
            "country": dand["country"],
            "period": dand["period"],
            "admin1": dand["admin1_name"],
            "admin2": dand["admin2_name"],
            "population": dand["population"],
            "acled": dand["acled_conflict_adm2_sev"],
            "ucdp": dand["ucdp_conflict_adm2_sev"],
            "combined": dand["conflict_reconstructed"],
            "dashboard": dand["conflict_dashboard"],
        },
        "kabul_july": {
            "kabul_acled": kabul_july["acled_conflict_adm2_sev"],
            "kabul_ucdp": kabul_july["ucdp_conflict_adm2_sev"],
            "kabul_combined": kabul_july["conflict_reconstructed"],
            "kabul_population": kabul_july["population"],
            "bagrami_combined": bagrami_july["conflict_reconstructed"],
            "bagrami_population": bagrami_july["population"],
            "pin_dashboard": nat("Afghanistan", "July 2025")["pin_conflict_dashboard"],
            "pin_district": nat("Afghanistan", "July 2025")["pin_conflict_district_reconstructed"],
            "districts": nat("Afghanistan", "July 2025")["districts_conflict_pin_reconstructed"],
        },
        "kabul_august": {
            "kabul_acled": kabul_aug["acled_conflict_adm2_sev"],
            "kabul_ucdp": kabul_aug["ucdp_conflict_adm2_sev"],
            "kabul_combined": kabul_aug["conflict_reconstructed"],
            "bagrami_combined": bagrami_aug["conflict_reconstructed"],
            "pin_dashboard": nat("Afghanistan", "August 2025")["pin_conflict_dashboard"],
            "pin_district": nat("Afghanistan", "August 2025")["pin_conflict_district_reconstructed"],
        },
        "majang": {
            "admin1": majang["admin1_name"],
            "admin2": majang["admin2_name"],
            "period": majang["period"],
            "protests": majang["acled_protests_adm2_sev"],
        },
        "iran": {
            period: {
                "dashboard": nat("Iran", period)["psi_dashboard"],
                "reconstructed": nat("Iran", period)["psi_reconstructed"],
                "without_cyclone": nat("Iran", period)["psi_without_cyclone"],
            }
            for period in ("January 2026", "February 2026", "March 2026")
        },
    }


def check(national: list[dict], examples: dict) -> None:
    by = {(r["country"], r["period"]): r for r in national}
    expect = {
        ("Iran", "January 2026"): 3.475,
        ("Iran", "February 2026"): 3.416,
        ("Iran", "March 2026"): 2.922,
    }
    for key, psi in expect.items():
        got = by[key]["psi_without_cyclone"]
        if abs(got - psi) > 0.001:
            raise SystemExit(f"{key} psi_without_cyclone {got} != {psi}")
    july = by[("Afghanistan", "July 2025")]
    if abs(july["psi_dashboard"] - 1.953) > 0.001:
        raise SystemExit(f"Afghanistan July dashboard PSI drifted: {july['psi_dashboard']}")
    if july["pin_conflict_dashboard"] != 0:
        raise SystemExit("July conflict PiN dashboard should be 0")
    if examples["dand_wa_patan"]["combined"] != 10:
        raise SystemExit("Dand Wa Patan combined conflict should be 10")
    if abs(examples["dand_wa_patan"]["dashboard"] - 5.078) > 0.001:
        raise SystemExit("Dand Wa Patan dashboard conflict drifted")
    if len(national) != 9:
        raise SystemExit("Expected 9 country-months")


def flatten_national(rows: list[dict]) -> list[dict]:
    flat = []
    for row in rows:
        item = {k: v for k, v in row.items() if k not in {"drivers_dashboard", "drivers_reconstructed", "weights_reconstructed", "weights_without_cyclone", "notes"}}
        for prefix, block in (
            ("dash", row["drivers_dashboard"]),
            ("recon", row["drivers_reconstructed"]),
        ):
            for driver, value in block.items():
                item[f"{prefix}_{driver.lower()}"] = value
        item["notes"] = " ".join(row["notes"])
        flat.append(item)
    return flat


def dictionary_rows() -> list[dict]:
    entries = [
        ("country", "Country name. Filled on every row."),
        ("period", "Month label, for example March 2026. Filled on every row."),
        ("period_start", "First day of that month, for sorting."),
        ("admin1_name", "GAUL admin-1 name from the dashboard file."),
        ("admin2_name", "GAUL admin-2 name from the dashboard file."),
        ("adm2_id", "GAUL admin-2 code. Use this as the stable key."),
        ("population", "District population (tltpop) used as the national weight."),
        ("acled_conflict_adm2_sev", "Stored ACLED conflict column. In Afghanistan and Iran Jan/Feb this is 56.25% of a combined score."),
        ("ucdp_conflict_adm2_sev", "Stored UCDP conflict column. The paired 43.75% share when the columns are pre-split."),
        ("conflict_column_rule", "pre_split_sum, acled_holds_full_score, or weighted_once."),
        ("conflict_dashboard", "0.5625*ACLED + 0.4375*UCDP. This is what the Shiny app maps."),
        ("conflict_reconstructed", "Combined score when the columns were pre-split; otherwise the weighted score, applied once."),
        ("iom_severity", "IOM displacement tier on the 0, 3.3, 5, 6.7, 10 scale. Blank for Iran."),
        ("iom_status", "present or missing. Missing is not the same as zero."),
        ("fbook_severity", "Facebook mobility severity. Explicit 0 on every row in this delivery."),
        ("facebook_status", "explicit_zero marks a filled zero, not a measurement that happened to be zero in one district."),
        ("displacement_dashboard", "0.6*IOM + 0.4*Facebook. Equals 0.6*IOM while Facebook is 0."),
        ("displacement_reconstructed", "IOM severity alone. Blank where IOM is missing."),
        ("nlr_severity", "Night-light / political severity on the 0–10 tier scale."),
        ("nlr_gate", "PiN fraction for NLR: 0, 0.33, 0.50 or 1. The dashboard uses this inside the severity blend."),
        ("political_dashboard", "Protest share times protest severity, plus NLR share times nlr_gate."),
        ("political_reconstructed", "Same shares, using nlr_severity. A negative protest value is treated as 0."),
        ("drought_dashboard", "SPEI severity blended with NDVI gates (0–1)."),
        ("drought_reconstructed", "SPEI, NDVI cropland and NDVI grassland severities, all on the 0–10 scale."),
        ("pin_conflict_dashboard", "Whole district population if either stored conflict column is at least 3.3, else 0."),
        ("pin_conflict_district_reconstructed", "Whole district population if the reconstructed conflict score is at least 3.3. This is still a district rule, not the pixel PiN."),
        ("pin_displacement", "Raw IOM IDP count. This is the displacement PiN in both readings."),
        ("psi_dashboard", "National PSI the Shiny app shows at default weights."),
        ("psi_reconstructed", "National PSI with conflict counted once, severity scales, IOM alone, and missing drivers dropped. Cyclone stays in as 0."),
        ("psi_without_cyclone", "psi_reconstructed with cyclone also dropped. This is the Iran reading closest to the Airtable totals."),
        ("wikipedia_severity", "One national Wikipedia severity, blended into conflict at 20% after the population-weighted mean."),
        ("gdelt_severity", "One national GDELT / Goldstein severity, blended into political at 25%."),
    ]
    return [{"column": name, "meaning": meaning} for name, meaning in entries]


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise SystemExit(f"No rows for {path}")
    fieldnames: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
