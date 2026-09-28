"""Build static geo dataset (countries + clusters) from mledoze/countries.

Phase 1 source of truth for region resolution / distance engine.
Regenerate with:
  python -m scripts.build_geo_dataset
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import httpx

SOURCE_URL = "https://raw.githubusercontent.com/mledoze/countries/master/countries.json"
OUT_DIR = Path(__file__).resolve().parents[1] / "app" / "data" / "geo"

# Skip non-geographic / unassigned codes if present.
SKIP_CODES = {"AQ"}  # Antarctic as a "country" is noisy for job proximity


def slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "unknown"


def build() -> dict:
    response = httpx.get(SOURCE_URL, timeout=60.0, follow_redirects=True)
    response.raise_for_status()
    raw = response.json()
    if not isinstance(raw, list):
        raise RuntimeError("Unexpected countries payload shape")

    countries: list[dict] = []
    continent_members: dict[str, set[str]] = defaultdict(set)
    subregion_members: dict[str, set[str]] = defaultdict(set)

    for row in raw:
        code = str(row.get("cca2") or "").strip().upper()
        if not code or code in SKIP_CODES:
            continue
        latlng = row.get("latlng") or []
        if not (isinstance(latlng, list) and len(latlng) >= 2):
            continue
        try:
            lat = float(latlng[0])
            lon = float(latlng[1])
        except (TypeError, ValueError):
            continue

        name_obj = row.get("name") or {}
        name = str(name_obj.get("common") or name_obj.get("official") or code).strip()
        continent = str(row.get("region") or "").strip()
        subregion = str(row.get("subregion") or "").strip()

        cluster_ids: list[str] = []
        if continent:
            cid = slugify(continent)
            continent_members[continent].add(code)
            cluster_ids.append(cid)
        if subregion:
            sid = slugify(subregion)
            subregion_members[subregion].add(code)
            if sid not in cluster_ids:
                cluster_ids.append(sid)

        countries.append(
            {
                "code": code,
                "name": name,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "clusters": cluster_ids,
            }
        )

    countries.sort(key=lambda c: c["name"].casefold())

    clusters: list[dict] = []
    for name, members in sorted(continent_members.items(), key=lambda x: x[0].casefold()):
        clusters.append(
            {
                "id": slugify(name),
                "name": name,
                "type": "continent",
                "memberCountryCodes": sorted(members),
            }
        )
    for name, members in sorted(subregion_members.items(), key=lambda x: x[0].casefold()):
        clusters.append(
            {
                "id": slugify(name),
                "name": name,
                "type": "subregion",
                "memberCountryCodes": sorted(members),
            }
        )

    return {
        "source": SOURCE_URL,
        "countryCount": len(countries),
        "clusterCount": len(clusters),
        "countries": countries,
        "clusters": clusters,
    }


def main() -> int:
    payload = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    countries_path = OUT_DIR / "countries.json"
    clusters_path = OUT_DIR / "clusters.json"
    meta_path = OUT_DIR / "meta.json"

    countries_path.write_text(
        json.dumps(payload["countries"], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    clusters_path.write_text(
        json.dumps(payload["clusters"], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    meta_path.write_text(
        json.dumps(
            {
                "source": payload["source"],
                "countryCount": payload["countryCount"],
                "clusterCount": payload["clusterCount"],
                "skipCodes": sorted(SKIP_CODES),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"Wrote {payload['countryCount']} countries and "
        f"{payload['clusterCount']} clusters to {OUT_DIR}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
