#!/usr/bin/env python3
"""Regenerate minified/<STATE>.min.json from data/<STATE>.json.

This is the single source of truth for everything under minified/. It used
to be a manual, un-tooled step -- which is how minified/Brasil.min.json sat
for 7+ years with the pre-PR#4 "Paraba"/"Piau" typos even after data/
was fixed (see GitHub issue #6). Re-run this any time data/ changes, and
let .github/workflows/verify-minified.yml catch it if someone forgets.

Usage:
    python3 scripts/build_minified.py

Requires only the Python 3 standard library (json, pathlib) -- no
third-party dependencies.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MINIFIED_DIR = REPO_ROOT / "minified"

# GeoJSON coordinates in this dataset are decimal-degree lon/lat. Source
# precision currently tops out at 6 decimal places (~11cm) with the large
# majority of values at 5 (~1m) -- already sensible for municipal boundary
# data, so this is a *safety cap*, not an active reduction: it guards
# against any future edit that introduces excess float precision (e.g.
# floating-point noise from a GIS export), without ever truncating
# precision the source data actually carries today.
COORDINATE_PRECISION = 6

# Some data/*.json files carry a legacy top-level "crs" member that just
# spells out the default GeoJSON CRS (WGS84 / CRS84 -- see RFC 7946 S4).
# Every conformant GeoJSON reader already assumes this exact CRS when no
# crs member is present at all, so the object carries zero information.
# It's dropped from the minified output as genuinely redundant -- this
# does NOT change how any coordinate is interpreted.
DEFAULT_CRS = {
    "type": "name",
    "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
}


def round_coordinates(node):
    """Recursively round every number under a GeoJSON `coordinates` value
    to COORDINATE_PRECISION decimal places. Works for Point, LineString,
    Polygon, and Multi* geometries alike without assuming a fixed nesting
    depth: a "coordinates" value is a list, and once we reach a list whose
    elements are all numbers we've hit a leaf coordinate tuple."""
    if isinstance(node, list):
        if node and all(isinstance(n, (int, float)) for n in node):
            return [round(n, COORDINATE_PRECISION) for n in node]
        return [round_coordinates(n) for n in node]
    return node


def minify_feature_collection(doc):
    if doc.get("crs") == DEFAULT_CRS:
        doc = {k: v for k, v in doc.items() if k != "crs"}

    for feature in doc.get("features", []):
        geometry = feature.get("geometry")
        if geometry and "coordinates" in geometry:
            geometry["coordinates"] = round_coordinates(geometry["coordinates"])

    return doc


def build_one(state_path: Path) -> str:
    with state_path.open(encoding="utf-8") as f:
        doc = json.load(f)
    doc = minify_feature_collection(doc)
    # Compact separators (no whitespace) is the bulk of the actual size
    # win; ensure_ascii=False keeps accented names (e.g. "Paraíba",
    # "Piauí") as literal UTF-8 instead of \uXXXX escapes, matching the
    # existing minified/ convention.
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def main() -> int:
    if not DATA_DIR.is_dir():
        print(f"error: {DATA_DIR} not found", file=sys.stderr)
        return 1

    state_files = sorted(DATA_DIR.glob("*.json"))
    if not state_files:
        print(f"error: no *.json files found under {DATA_DIR}", file=sys.stderr)
        return 1

    MINIFIED_DIR.mkdir(exist_ok=True)

    for state_path in state_files:
        out_path = MINIFIED_DIR / f"{state_path.stem}.min.json"
        new_content = build_one(state_path)
        out_path.write_text(new_content, encoding="utf-8")
        print(f"wrote {out_path.relative_to(REPO_ROOT)} ({len(new_content)} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
