"""Phase 1 geo dataset — static countries + continent/sub-region clusters.

This module is the single source of truth for region resolution and the
distance engine. It does not call the network at runtime.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "geo"


def _load_json(name: str) -> Any:
    path = _DATA_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing geo dataset file: {path}. "
            "Run: python scripts/build_geo_dataset.py"
        )
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def list_countries() -> tuple[dict[str, Any], ...]:
    """Return all countries: {code, name, lat, lon, clusters[]}."""
    rows = _load_json("countries.json")
    if not isinstance(rows, list):
        raise ValueError("countries.json must be a list")
    return tuple(rows)


@lru_cache(maxsize=1)
def list_clusters() -> tuple[dict[str, Any], ...]:
    """Return all clusters: {id, name, type, memberCountryCodes[]}."""
    rows = _load_json("clusters.json")
    if not isinstance(rows, list):
        raise ValueError("clusters.json must be a list")
    return tuple(rows)


@lru_cache(maxsize=1)
def _countries_by_code() -> dict[str, dict[str, Any]]:
    return {str(c["code"]).upper(): c for c in list_countries() if c.get("code")}


@lru_cache(maxsize=1)
def _clusters_by_id() -> dict[str, dict[str, Any]]:
    return {str(c["id"]).lower(): c for c in list_clusters() if c.get("id")}


@lru_cache(maxsize=1)
def _countries_by_name() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for country in list_countries():
        name = str(country.get("name") or "").strip()
        code = str(country.get("code") or "").strip().upper()
        if name and code:
            mapping[name.casefold()] = code
    return mapping


def lookup_country_code(name_or_code: str) -> str:
    """Resolve a free-text country name or ISO alpha-2 to a dataset code."""
    text = str(name_or_code or "").strip()
    if not text or text.casefold() in {"not specified", "n/a", "na", "none"}:
        return ""
    if len(text) == 2 and text.isalpha():
        code = text.upper()
        return code if get_country(code) else ""
    return _countries_by_name().get(text.casefold(), "")


def get_country(code: str) -> Optional[dict[str, Any]]:
    """Lookup one country by ISO alpha-2 code."""
    return _countries_by_code().get((code or "").strip().upper())


def get_cluster(cluster_id: str) -> Optional[dict[str, Any]]:
    """Lookup one cluster by id (e.g. 'asia', 'southern-asia')."""
    return _clusters_by_id().get((cluster_id or "").strip().lower())


def country_point(code: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) for a country code, or None."""
    country = get_country(code)
    if not country:
        return None
    try:
        return float(country["lat"]), float(country["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def cluster_member_codes(cluster_id: str) -> tuple[str, ...]:
    """Return sorted member country codes for a cluster."""
    cluster = get_cluster(cluster_id)
    if not cluster:
        return ()
    members = cluster.get("memberCountryCodes") or []
    return tuple(str(c).upper() for c in members if str(c).strip())


def cluster_centroid(cluster_id: str) -> Optional[tuple[float, float]]:
    """Average of member country centroids (simple mean of lat/lon)."""
    points: list[tuple[float, float]] = []
    for code in cluster_member_codes(cluster_id):
        point = country_point(code)
        if point:
            points.append(point)
    if not points:
        return None
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    return lat, lon
