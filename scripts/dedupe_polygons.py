#!/usr/bin/env python3
"""
Dedupe/merge duplicate municipality Features in data/*.json.

Background (GitHub issue #5): each file in data/*.json (except Brasil.json,
which is state-level, not municipal) is a GeoJSON FeatureCollection where each
Feature should represent one Brazilian municipality (properties GEOCODIGO,
NOME, UF). In practice, many municipalities are split across multiple
Features that share the same GEOCODIGO/NOME/UF, each carrying a single-ring
Polygon geometry, instead of being merged into one Feature with a
MultiPolygon geometry. This is the confirmed root cause of issue #1
("cidades repetidas").

This script groups features by GEOCODIGO within each file and merges each
group into a single Feature:

  - A group with exactly one feature is kept as-is (geometry untouched).
  - A group with multiple features is merged into one Feature whose
    geometry.type is "MultiPolygon" and whose coordinates array contains
    each source feature's full Polygon coordinates array (i.e. one entry
    per source polygon, each entry itself the polygon's own list of rings
    including any interior rings/holes it already had). No ring or
    coordinate is dropped, altered, or added.

Only the top-level `features` array is touched; `type`, `crs`, and any other
top-level keys on the FeatureCollection are preserved verbatim.

Before merging, the script verifies that all features sharing a GEOCODIGO
within a file have identical `properties` (GEOCODIGO, NOME, UF). If any
group's properties differ, the script stops and reports the offending file
and GEOCODIGO instead of silently picking one set of properties.

Usage:
    python3 scripts/dedupe_polygons.py [--check]

    --check   Dry run: report what would change per file, without writing.

Scope: processes every data/*.json file except data/Brasil.json (state-level
polygons, not municipalities). Does not touch minified/ (tracked separately
in issue #6).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import OrderedDict, defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
EXCLUDED_FILES = {"Brasil.json"}


def merge_feature_group(geocodigo, features):
    """Merge a list of Feature dicts sharing the same GEOCODIGO into one Feature.

    Raises ValueError if the features' properties are not all identical.
    """
    first_props = features[0]["properties"]
    for feat in features[1:]:
        if feat["properties"] != first_props:
            raise ValueError(
                f"properties mismatch for GEOCODIGO={geocodigo!r}: "
                f"{first_props!r} != {feat['properties']!r}"
            )

    if len(features) == 1:
        # Single feature: keep as-is, geometry untouched.
        return features[0]

    # Multiple features: merge into one MultiPolygon feature.
    # A MultiPolygon's coordinates array is an array of Polygon coordinate
    # arrays -- so each source Polygon's full `coordinates` (its list of
    # rings, including any holes) becomes one entry.
    merged_coordinates = []
    for feat in features:
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            merged_coordinates.append(geom["coordinates"])
        elif geom["type"] == "MultiPolygon":
            # Defensive: if a duplicate is already a MultiPolygon, flatten
            # its polygon entries into the merged list rather than nesting.
            merged_coordinates.extend(geom["coordinates"])
        else:
            raise ValueError(
                f"unexpected geometry type {geom['type']!r} for "
                f"GEOCODIGO={geocodigo!r}"
            )

    return {
        "type": "Feature",
        "properties": first_props,
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": merged_coordinates,
        },
    }


def _dumps_spaced(obj):
    """json.dumps with a space after '{' and before '}', to match the
    existing on-disk style (e.g. '{ "type": "Feature", ... }')."""
    s = json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))
    s = s.replace("{", "{ ").replace("}", " }")
    # Collapse the doubled space that results from adjacent braces, e.g.
    # "{ { " -> "{ { " is fine, but "{  " from "{" + "{" already spaced is ok;
    # only need to avoid "{ }" -> "{  }" for empty objects (none expected here).
    return s


def process_file(path, check_only=False):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f, object_pairs_hook=OrderedDict)

    features = data["features"]
    groups = OrderedDict()
    for feat in features:
        gc = feat["properties"]["GEOCODIGO"]
        groups.setdefault(gc, []).append(feat)

    before_count = len(features)
    unique_count = len(groups)
    dup_groups = {gc: feats for gc, feats in groups.items() if len(feats) > 1}

    merged_features = []
    for gc, feats in groups.items():
        merged_features.append(merge_feature_group(gc, feats))

    if len(merged_features) != unique_count:
        raise AssertionError(
            f"{path}: merged feature count {len(merged_features)} != "
            f"unique GEOCODIGO count {unique_count}"
        )

    result = {
        "before_count": before_count,
        "after_count": len(merged_features),
        "dup_group_count": len(dup_groups),
        "max_dup_group_size": max((len(v) for v in dup_groups.values()), default=1),
        "changed": len(dup_groups) > 0,
    }

    if not check_only and dup_groups:
        # Only rewrite files that actually had duplicates to merge, so files
        # with no duplicates are left byte-for-byte untouched.
        header_parts = [f'"type": {json.dumps(data["type"])}']
        if "crs" in data:
            header_parts.append(f'"crs": {_dumps_spaced(data["crs"])}')
        for key in data:
            if key in ("type", "crs", "features"):
                continue
            header_parts.append(f"{json.dumps(key)}: {_dumps_spaced(data[key])}")

        with open(path, "w", encoding="utf-8") as f:
            f.write("{\n")
            for part in header_parts:
                f.write(part + ",\n")
            f.write("\n")
            f.write('"features": [\n')
            for i, feat in enumerate(merged_features):
                line = _dumps_spaced(feat)
                if i < len(merged_features) - 1:
                    f.write(line + ",\n")
                else:
                    f.write(line + "\n")
            f.write("]\n")
            f.write("}\n")

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry run: report what would change without writing files.",
    )
    args = parser.parse_args()

    paths = sorted(glob.glob(os.path.join(DATA_DIR, "*.json")))
    paths = [p for p in paths if os.path.basename(p) not in EXCLUDED_FILES]

    if not paths:
        print("No data/*.json files found (excluding Brasil.json).", file=sys.stderr)
        sys.exit(1)

    had_error = False
    for path in paths:
        name = os.path.basename(path)
        try:
            result = process_file(path, check_only=args.check)
        except (ValueError, AssertionError) as e:
            had_error = True
            print(f"ERROR processing {name}: {e}", file=sys.stderr)
            continue

        print(
            f"{name}: {result['before_count']} -> {result['after_count']} features "
            f"({result['dup_group_count']} duplicate groups, "
            f"largest group size {result['max_dup_group_size']})"
        )

    if had_error:
        print(
            "\nOne or more files had errors and were left untouched where "
            "processing failed. See ERROR lines above.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
