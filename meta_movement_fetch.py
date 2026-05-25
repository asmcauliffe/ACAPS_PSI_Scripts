"""
Download Meta Movement Distribution CSVs from HDX (movement-dataset), combine
across all monthly resources, and retain rows for countries aligned with
`iom_dtm_fetch.COUNTRIES` (ISO3 codes).

Source dataset: https://data.humdata.org/dataset/movement-distribution

Outputs (default: ./meta_mobility_results/):
  movement_distribution_iom_countries_long.csv — filtered long table
  fetch_manifest.json — resource ids and period labels parsed from titles

Large run: ~40 monthly zips (~70–400 MB each). Use --max-resources for a smoke test.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pandas as pd
import urllib.request

from iom_dtm_fetch import COUNTRIES

HDX_API = "https://data.humdata.org/api/3/action/package_show"
PACKAGE_ID = "movement-distribution"
DEFAULT_OUT = Path("meta_mobility_results")
CHUNKSIZE = 300_000

TARGET_ISO3 = {info["iso3"] for info in COUNTRIES.values()}

DATE_IN_NAME = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_period_from_title(name: str) -> tuple[str | None, str | None]:
    """Extract start/end YYYY-MM-DD from HDX resource name (title)."""
    dates = DATE_IN_NAME.findall(name)
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1:
        return dates[0], dates[0]
    return None, None


def fetch_package() -> dict:
    url = f"{HDX_API}?id={PACKAGE_ID}"
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)["result"]


def iter_csv_resources(pkg: dict):
    for res in pkg.get("resources", []):
        if res.get("state") != "active":
            continue
        fmt = (res.get("format") or "").upper()
        name_l = (res.get("name") or "").lower()
        if fmt != "CSV" or "readme" in name_l or name_l.endswith(".pdf"):
            continue
        dl = res.get("download_url") or res.get("url")
        if not dl or not dl.endswith((".zip", ".ZIP")):
            continue
        yield res


def download_url(url: str, dest: Path, logger: logging.Logger) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("  Downloading ... %s", url[-80:])
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=600) as resp:
        with open(dest, "wb") as out:
            while True:
                chunk = resp.read(8 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    logger.info("  Saved %.1f MB in %.0fs", dest.stat().st_size / 1e6, time.time() - t0)


def _skip_member(name: str) -> bool:
    return "__MACOSX" in name or name.rsplit("/", 1)[-1].startswith("._")


def _append_filtered_chunks(
    text_io: io.TextIOWrapper,
    log_label: str,
    out_csv: Path,
    write_header: bool,
    resource_id: str,
    resource_name: str,
    period_start: str | None,
    period_end: str | None,
    logger: logging.Logger,
) -> tuple[int, bool]:
    rows_out = 0
    reader = pd.read_csv(
        text_io,
        chunksize=CHUNKSIZE,
        on_bad_lines="skip",
    )
    for chunk in reader:
        if "country" not in chunk.columns:
            logger.warning("  No country column in %s - skipping", log_label)
            return 0, write_header
        c = chunk["country"].astype(str).str.strip()
        sub = chunk[c.isin(TARGET_ISO3)].copy()
        if sub.empty:
            continue
        sub.insert(0, "hdx_resource_id", resource_id)
        sub.insert(1, "hdx_resource_name", resource_name)
        sub.insert(2, "hdx_period_start", period_start)
        sub.insert(3, "hdx_period_end", period_end)
        sub.to_csv(out_csv, mode="a", index=False, header=write_header)
        if write_header:
            write_header = False
        rows_out += len(sub)
    return rows_out, write_header


def _process_zip_tree(
    zf: zipfile.ZipFile,
    out_csv: Path,
    write_header: bool,
    resource_id: str,
    resource_name: str,
    period_start: str | None,
    period_end: str | None,
    logger: logging.Logger,
    path_prefix: str = "",
) -> tuple[int, bool]:
    """
    Some HDX uploads nest CSVs inside inner .zip files (e.g. May 2025).
    Recurse into nested archives; skip macOS metadata.
    """
    rows_out = 0
    for name in sorted(zf.namelist()):
        if _skip_member(name):
            continue
        label = f"{path_prefix}{name}" if path_prefix else name
        ln = name.lower()
        if ln.endswith(".csv"):
            with zf.open(name) as raw:
                text = io.TextIOWrapper(
                    raw, encoding="utf-8", errors="replace", newline=""
                )
                n, write_header = _append_filtered_chunks(
                    text,
                    label,
                    out_csv,
                    write_header,
                    resource_id,
                    resource_name,
                    period_start,
                    period_end,
                    logger,
                )
                rows_out += n
        elif ln.endswith(".zip"):
            inner = zipfile.ZipFile(io.BytesIO(zf.read(name)))
            try:
                n, write_header = _process_zip_tree(
                    inner,
                    out_csv,
                    write_header,
                    resource_id,
                    resource_name,
                    period_start,
                    period_end,
                    logger,
                    path_prefix=label + " / ",
                )
                rows_out += n
            finally:
                inner.close()
    return rows_out, write_header


def process_zip(
    zip_path: Path,
    period_start: str | None,
    period_end: str | None,
    resource_name: str,
    resource_id: str,
    out_csv: Path,
    write_header: bool,
    logger: logging.Logger,
) -> tuple[int, bool]:
    """Append filtered rows from all CSVs inside zip_path (including nested zips)."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        return _process_zip_tree(
            zf,
            out_csv,
            write_header,
            resource_id,
            resource_name,
            period_start,
            period_end,
            logger,
        )


def run(
    output_dir: Path,
    max_resources: int | None,
    skip_existing: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "meta_movement_log.txt"
    stream_out = (
        io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "buffer")
        else sys.stdout
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(stream_out),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )
    logger = logging.getLogger(__name__)

    out_csv = output_dir / "movement_distribution_iom_countries_long.csv"
    manifest_path = output_dir / "fetch_manifest.json"

    logger.info("Target ISO3 (%d): %s", len(TARGET_ISO3), sorted(TARGET_ISO3))

    pkg = fetch_package()
    all_csv_zips = list(iter_csv_resources(pkg))
    resources = all_csv_zips[:max_resources] if max_resources is not None else all_csv_zips
    logger.info(
        "Processing %d monthly resources (of %d CSV zips available).",
        len(resources),
        len(all_csv_zips),
    )

    if skip_existing and out_csv.exists():
        logger.info("Output exists and --skip-existing set — exiting.")
        return

    if out_csv.exists():
        out_csv.unlink()

    manifest: list[dict] = []
    total_rows = 0
    write_header = True

    with tempfile.TemporaryDirectory(prefix="meta_mobility_") as tmp:
        tmp_path = Path(tmp)
        for i, res in enumerate(resources, 1):
            name = res.get("name") or ""
            rid = res.get("id") or ""
            p0, p1 = parse_period_from_title(name)
            url = res.get("download_url") or res.get("url")
            logger.info("[%d/%d] %s | period %s -> %s", i, len(resources), name[:70], p0, p1)

            zpath = tmp_path / f"res_{i}.zip"
            try:
                download_url(url, zpath, logger)
            except Exception as e:
                logger.error("  Download failed: %s", e)
                manifest.append(
                    {
                        "resource_id": rid,
                        "name": name,
                        "period_start": p0,
                        "period_end": p1,
                        "error": str(e),
                        "rows_written": 0,
                    }
                )
                continue

            try:
                n, write_header = process_zip(
                    zpath,
                    p0,
                    p1,
                    name,
                    rid,
                    out_csv,
                    write_header,
                    logger,
                )
            except Exception as e:
                logger.error("  Processing failed: %s", e)
                n = 0
                manifest.append(
                    {
                        "resource_id": rid,
                        "name": name,
                        "period_start": p0,
                        "period_end": p1,
                        "error": str(e),
                        "rows_written": 0,
                    }
                )
            else:
                total_rows += n
                manifest.append(
                    {
                        "resource_id": rid,
                        "name": name,
                        "period_start": p0,
                        "period_end": p1,
                        "rows_written": n,
                    }
                )
                logger.info("  -> %s rows (cumulative %s)", f"{n:,}", f"{total_rows:,}")

            zpath.unlink(missing_ok=True)

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Wrote %s (%d rows).", out_csv, total_rows)
    logger.info("Manifest: %s", manifest_path)


def main() -> None:
    p = argparse.ArgumentParser(description="HDX Meta Movement Distribution → long CSV (IOM countries).")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output directory (default: {DEFAULT_OUT})",
    )
    p.add_argument(
        "--max-resources",
        type=int,
        default=None,
        metavar="N",
        help="Only first N monthly zips (for testing).",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="If combined CSV exists, do nothing.",
    )
    args = p.parse_args()
    run(args.output_dir, args.max_resources, args.skip_existing)


if __name__ == "__main__":
    main()
