"""
organize_gdelt_production.py
----------------------------
Copy canonical GDELT deliverables into ``<output-dir>/production/`` for upload,
and move stale logs / partial DOC outputs into ``_archive/`` so the tree is unambiguous.

Run after bulk GKG + postprocess:
  python postprocess_gkg_themes.py --output-dir ./gdelt_results --group all
  python postprocess_gkg_themes.py --output-dir ./gdelt_results --group extrapolation
  python organize_gdelt_production.py --output-dir ./gdelt_results
"""

from __future__ import annotations

import argparse
import os
import shutil
from datetime import datetime, timezone


def _copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def _move(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Populate production/ for analysis upload; archive stale GDELT outputs."
    )
    p.add_argument("--output-dir", default="./gdelt_results")
    p.add_argument(
        "--no-archive",
        action="store_true",
        help="Only refresh production/ copies; do not move logs or stale CSVs.",
    )
    args = p.parse_args()

    root = os.path.abspath(args.output_dir)
    prod = os.path.join(root, "production")
    extrap = os.path.join(prod, "extrapolation")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    arch = os.path.join(root, "_archive", stamp)

    required_sources = [
        (os.path.join(root, "events_monthly.csv"), os.path.join(prod, "events_monthly.csv")),
        (os.path.join(root, "themes_monthly_gkg.csv"), os.path.join(prod, "themes_monthly_gkg.csv")),
    ]
    optional_combined = (
        os.path.join(root, "combined_monthly_gkg_all_countries.csv"),
        os.path.join(prod, "combined_monthly_gkg.csv"),
    )
    optional_extrap = [
        (
            os.path.join(root, "themes_monthly_gkg_extrapolation.csv"),
            os.path.join(extrap, "themes_monthly_gkg.csv"),
        ),
        (
            os.path.join(root, "combined_monthly_gkg_extrapolation.csv"),
            os.path.join(extrap, "combined_monthly_gkg.csv"),
        ),
    ]

    missing: list[str] = []
    for src, dst in required_sources:
        if not os.path.isfile(src):
            missing.append(src)
        else:
            _copy(src, dst)
            print(f"Copied -> {dst}")

    c_src, c_dst = optional_combined
    if os.path.isfile(c_src):
        _copy(c_src, c_dst)
        print(f"Copied -> {c_dst}")
    else:
        print(
            f"Note: missing {c_src} — run:\n"
            f"  python postprocess_gkg_themes.py --output-dir {root} --group all"
        )

    for src, dst in optional_extrap:
        if os.path.isfile(src):
            _copy(src, dst)
            print(f"Copied -> {dst}")
        else:
            print(f"Note: optional missing {src} (run postprocess --group extrapolation)")

    gold_src = os.path.join(root, "goldstein_national_monthly.csv")
    gold_dst = os.path.join(prod, "goldstein_national_monthly.csv")
    if os.path.isfile(gold_src):
        _copy(gold_src, gold_dst)
        print(f"Copied -> {gold_dst}")
    else:
        print(
            f"Note: optional missing {gold_src} — run: python gdelt_goldstein_zscores.py"
        )

    manifest = os.path.join(prod, "MANIFEST.txt")
    lines = [
        "GDELT production bundle — upload this folder (or these files) for downstream analysis.",
        "",
        "Core panel (12 countries, 2018+ span as in source files):",
        "  events_monthly.csv       — monthly GDELT Events by CAMEO group (long format).",
        "  themes_monthly_gkg.csv   — monthly GKG theme article counts + avg tone (long format).",
        "  combined_monthly_gkg.csv — wide merge of events + GKG themes (if postprocess --group all ran).",
        "  goldstein_national_monthly.csv — optional; event-weighted monthly Goldstein + national z-scores.",
        "",
        "Extrapolation subset (5 countries):",
        "  extrapolation/themes_monthly_gkg.csv",
        "  extrapolation/combined_monthly_gkg.csv",
        "",
        "Not included (keep locally for resume / debugging; do not upload):",
        "  ../_gkg_cache/",
        "  ../_gkg_bulk_state.pkl",
        "  ../_themes_doc_state.pkl",
        "  ../_raw_cache/",
        "",
        f"Organized at (UTC): {stamp}",
    ]
    os.makedirs(prod, exist_ok=True)
    with open(manifest, "w", encoding="utf-8") as mf:
        mf.write("\n".join(lines) + "\n")
    print(f"Wrote {manifest}")

    if args.no_archive:
        print("Done (--no-archive).")
        return

    to_archive: list[tuple[str, str]] = []
    # Keep themes_monthly_gkg_all_countries* in the repo root so you can re-run this
    # script without repeating postprocess; extrapolation duplicates are safe to archive.
    stale_csv = [
        "combined_monthly.csv",
        "anomaly_scores.csv",
        "themes_monthly.csv",
        "themes_monthly_gkg_extrapolation.csv",
        "combined_monthly_gkg_extrapolation.csv",
    ]
    for name in stale_csv:
        src = os.path.join(root, name)
        if os.path.isfile(src):
            to_archive.append((src, os.path.join(arch, name)))

    logs = [
        "run_log.txt",
        "gkg_bulk_run.log",
        "overnight_run.log",
        "recent_stdout.log",
        "historical_stdout.log",
    ]
    for name in logs:
        src = os.path.join(root, name)
        if os.path.isfile(src):
            to_archive.append((src, os.path.join(arch, "logs", name)))

    for src, dst in to_archive:
        try:
            _move(src, dst)
            print(f"Archived -> {dst}")
        except OSError as e:
            print(f"Skip archive {src}: {e}")

    print(f"Done. Upload: {prod}/")


if __name__ == "__main__":
    main()
