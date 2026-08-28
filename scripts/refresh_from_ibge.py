#!/usr/bin/env python3
"""Refresh data/*.json from IBGE's current official municipal & state mesh.

This repo's data/*.json files were built from an IBGE export from roughly
2015. IBGE routinely revises municipal boundaries (it revised ~784 of them
in 2025 alone) and this repo was also missing several municipalities
entirely (see GitHub issues #2 and #7). This script replaces the *entire*
dataset -- every data/<UF>.json plus data/Brasil.json -- with IBGE's
current official mesh, joined against IBGE's current official municipality
list, so the geometry and the roster of municipalities are both up to date.

It is meant to be re-run whenever the data needs refreshing again (e.g. the
next time IBGE revises boundaries), not a one-off migration script.

Endpoints used (IBGE's public "servicodados" API, no auth required):
  - Bulk municipality roster (name + code + UF), one call, all ~5,571 rows:
      GET /api/v1/localidades/municipios
  - Bulk state (UF) roster, one call, all 27 rows:
      GET /api/v1/localidades/estados
  - Per-state municipal mesh, one call per state, one Feature per
    municipality, geometry only + a `codarea` code (no name -- must be
    joined against the municipality roster above):
      GET /api/v3/malhas/estados/{UF}?formato=application/vnd.geo+json&intrarregiao=municipio
  - National mesh at UF granularity, one call, 27 Features (one per state +
    DF), geometry only + a `codarea` code (joined against the state roster):
      GET /api/v3/malhas/paises/BR?formato=application/vnd.geo+json&intrarregiao=UF

Output schema (must match this repo's existing convention exactly):
  data/<UF>.json:
    {"type": "FeatureCollection", "features": [
      {"type": "Feature",
       "properties": {"GEOCODIGO": "<7-digit code, as string>",
                      "NOME": "<official name, accents included>",
                      "UF": "<2-letter sigla>"},
       "geometry": {...}},
      ...
    ]}
  data/Brasil.json: same FeatureCollection shape, one Feature per state + DF:
      {"type": "Feature",
       "properties": {"UF": "<sigla>", "ESTADO": "<nome>",
                      "REGIAO": "<2-letter region code>"},
       "geometry": {...}}

  No top-level `crs` member (already removed repo-wide -- see issue #6 /
  the crs-removal cleanup -- and must not be reintroduced).

On-disk formatting matches the style scripts/dedupe_polygons.py already
writes (and that data/*.json already carries post-dedupe): one Feature
per line, `json.dumps(..., separators=(", ", ": "))` with a space padding
inside `{ }`, no indentation, features comma-joined with the last one bare.

Requires only the Python 3 standard library -- no third-party
dependencies, so there's nothing to `pip install` to re-run this.

Usage:
    python3 scripts/refresh_from_ibge.py
    python3 scripts/refresh_from_ibge.py --states AC RO RR   # subset, for testing
    python3 scripts/refresh_from_ibge.py --skip-brasil        # states only
    python3 scripts/refresh_from_ibge.py --cache-dir /tmp/ibge_cache  # cache
        raw IBGE responses across runs so a re-run after a partial failure
        doesn't re-fetch states that already succeeded.

Exit status is non-zero if any state's mesh could not be fetched/joined --
the script still writes out every state that *did* succeed, and prints a
clear "FAILED" list for the ones that didn't so nothing is silently
dropped or fabricated.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

MUNICIPIOS_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
ESTADOS_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/estados"
MALHA_ESTADO_URL = (
    "https://servicodados.ibge.gov.br/api/v3/malhas/estados/{uf}"
    "?formato=application/vnd.geo+json&intrarregiao=municipio"
)
MALHA_BR_URL = (
    "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
    "?formato=application/vnd.geo+json&intrarregiao=UF"
)

# All 27 UFs (26 states + DF), the full set this repo tracks one file per.
ALL_UFS = [
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
]

# IBGE's own `regiao.sigla` values (N/NE/CO/SE/S) don't match this repo's
# existing REGIAO convention in data/Brasil.json (NO/NE/CO/SE/SU) -- confirmed
# by reading the pre-refresh file. Map official -> repo convention.
REGIAO_SIGLA_MAP = {"N": "NO", "NE": "NE", "CO": "CO", "SE": "SE", "S": "SU"}

USER_AGENT = "municipal-brazilian-geodata-refresh/1.0 (+github.com/luizpedone/municipal-brazilian-geodata)"


def fetch_json(url: str, retries: int = 4, timeout: int = 90, backoff: float = 3.0):
    """GET a URL and parse it as JSON, retrying transient failures.

    Raises RuntimeError with the last error if all attempts fail -- callers
    are expected to catch this per-state so one bad state doesn't abort the
    whole refresh.
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                encoding = resp.headers.get("Content-Encoding", "")
                if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                elif encoding == "deflate":
                    raw = zlib.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ConnectionError, json.JSONDecodeError, OSError, zlib.error) as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"failed to fetch {url!r} after {retries} attempts: {last_err!r}")


def cached_fetch(url: str, cache_dir: Path | None, cache_name: str):
    """fetch_json, optionally reading/writing a local cache file first."""
    if cache_dir is not None:
        cache_path = cache_dir / cache_name
        if cache_path.exists():
            with cache_path.open("r", encoding="utf-8") as f:
                return json.load(f)
    data = fetch_json(url)
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / cache_name
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    return data


def dumps_spaced(obj) -> str:
    """json.dumps with a space after '{' and before '}', matching the
    existing on-disk style (e.g. '{ "type": "Feature", ... }') that
    scripts/dedupe_polygons.py already writes."""
    s = json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))
    return s.replace("{", "{ ").replace("}", " }")


def write_feature_collection(path: Path, features: list) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write("{\n")
        f.write('"type": "FeatureCollection",\n')
        f.write("\n")
        f.write('"features": [\n')
        for i, feat in enumerate(features):
            line = dumps_spaced(feat)
            f.write(line + (",\n" if i < len(features) - 1 else "\n"))
        f.write("]\n")
        f.write("}\n")


def _municipio_uf_sigla(m: dict) -> str:
    """Most municipalities carry microrregiao -> mesorregiao -> UF. At least
    one (5101837, Boa Esperança do Norte/MT -- notably one of the 7
    municipalities this repo was previously missing entirely, see issue
    #2/#7) has `microrregiao: null` in the current IBGE roster, presumably
    because it hasn't been assigned a microrregiao since some IBGE
    reorganization. Fall back to the regiao-imediata -> regiao-intermediaria
    -> UF path, which every roster entry carries either way."""
    microrregiao = m.get("microrregiao")
    if microrregiao is not None:
        return microrregiao["mesorregiao"]["UF"]["sigla"]
    return m["regiao-imediata"]["regiao-intermediaria"]["UF"]["sigla"]


def build_municipio_lookup(municipios: list) -> dict:
    """code (str) -> {"nome": ..., "uf": <sigla>}"""
    lookup = {}
    for m in municipios:
        code = str(m["id"])
        uf_sigla = _municipio_uf_sigla(m)
        lookup[code] = {"nome": m["nome"], "uf": uf_sigla}
    return lookup


def build_estado_lookup(estados: list) -> dict:
    """UF code (str, e.g. "11") -> {"sigla", "nome", "regiao"}"""
    lookup = {}
    for e in estados:
        code = str(e["id"])
        official_regiao = e["regiao"]["sigla"]
        regiao = REGIAO_SIGLA_MAP.get(official_regiao, official_regiao)
        lookup[code] = {"sigla": e["sigla"], "nome": e["nome"], "regiao": regiao}
    return lookup


def refresh_state(uf: str, municipio_lookup: dict, cache_dir: Path | None):
    """Fetch UF's municipal mesh and join against municipio_lookup.

    Returns (features, missing_codes). missing_codes is non-empty if a
    codarea in the mesh wasn't found in the bulk municipality roster --
    that's a data-integrity problem worth surfacing, not silently dropping.
    """
    url = MALHA_ESTADO_URL.format(uf=uf)
    mesh = cached_fetch(url, cache_dir, f"malha_{uf}.json")
    features = []
    missing_codes = []
    for feat in mesh["features"]:
        code = feat["properties"]["codarea"]
        info = municipio_lookup.get(code)
        if info is None:
            missing_codes.append(code)
            continue
        features.append({
            "type": "Feature",
            "properties": {"GEOCODIGO": code, "NOME": info["nome"], "UF": info["uf"]},
            "geometry": feat["geometry"],
        })
    return features, missing_codes


def refresh_brasil(estado_lookup: dict, cache_dir: Path | None):
    mesh = cached_fetch(MALHA_BR_URL, cache_dir, "malha_BR.json")
    features = []
    missing_codes = []
    for feat in mesh["features"]:
        code = feat["properties"]["codarea"]
        info = estado_lookup.get(code)
        if info is None:
            missing_codes.append(code)
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "UF": info["sigla"],
                "ESTADO": info["nome"],
                "REGIAO": info["regiao"],
            },
            "geometry": feat["geometry"],
        })
    return features, missing_codes


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--states", nargs="*", default=ALL_UFS,
        help="subset of UF codes to refresh (default: all 27)",
    )
    parser.add_argument(
        "--skip-brasil", action="store_true",
        help="skip rebuilding data/Brasil.json (states only)",
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="optional directory to cache raw IBGE responses in, so a "
             "re-run after a partial failure doesn't re-fetch states that "
             "already succeeded",
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir).resolve() if args.cache_dir else None

    print("Fetching bulk municipality roster (localidades/municipios)...")
    municipios = cached_fetch(MUNICIPIOS_URL, cache_dir, "municipios.json")
    print(f"  {len(municipios)} municipalities in the current IBGE roster.")
    municipio_lookup = build_municipio_lookup(municipios)

    print("Fetching bulk state roster (localidades/estados)...")
    estados = cached_fetch(ESTADOS_URL, cache_dir, "estados.json")
    print(f"  {len(estados)} states/DF in the current IBGE roster.")
    estado_lookup = build_estado_lookup(estados)

    failed_states = []
    total_features = 0
    seen_geocodigos = set()

    DATA_DIR.mkdir(exist_ok=True)

    for uf in args.states:
        print(f"Fetching municipal mesh for {uf}...")
        try:
            features, missing = refresh_state(uf, municipio_lookup, cache_dir)
        except Exception as e:
            print(f"  FAILED to fetch/process {uf}: {e}", file=sys.stderr)
            failed_states.append(uf)
            continue

        if missing:
            print(
                f"  WARNING: {uf} mesh has {len(missing)} codarea(s) with no "
                f"match in the municipality roster: {missing}",
                file=sys.stderr,
            )

        dup_in_state = [c for c in (f["properties"]["GEOCODIGO"] for f in features)
                         if c in seen_geocodigos]
        if dup_in_state:
            print(f"  WARNING: {uf} contains GEOCODIGO(s) already seen in "
                  f"another state: {dup_in_state}", file=sys.stderr)
        seen_geocodigos.update(f["properties"]["GEOCODIGO"] for f in features)

        out_path = DATA_DIR / f"{uf}.json"
        write_feature_collection(out_path, features)
        total_features += len(features)
        print(f"  wrote {out_path.relative_to(REPO_ROOT)}: {len(features)} municipalities")

    if not args.skip_brasil:
        print("Fetching national UF-level mesh for Brasil.json...")
        try:
            br_features, br_missing = refresh_brasil(estado_lookup, cache_dir)
            if br_missing:
                print(f"  WARNING: Brasil mesh has unmatched codarea(s): {br_missing}",
                      file=sys.stderr)
            out_path = DATA_DIR / "Brasil.json"
            write_feature_collection(out_path, br_features)
            print(f"  wrote {out_path.relative_to(REPO_ROOT)}: {len(br_features)} states")
        except Exception as e:
            print(f"  FAILED to fetch/process Brasil.json: {e}", file=sys.stderr)
            failed_states.append("BR")

    print()
    print(f"Total municipalities written across {len(args.states) - len(failed_states)} "
          f"state file(s): {total_features}")
    if failed_states:
        print(f"FAILED states (not refreshed, left as-is): {failed_states}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
