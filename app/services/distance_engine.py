"""Phase 2 — core distance engine (Haversine + proximity sort).

Pure geo math. No DB, no onboarding, no job listing logic.
Given a reference point and candidate points, return them sorted by
ascending distance (km).
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

EARTH_RADIUS_KM = 6371.0088  # mean Earth radius (IUGG)

PointLike = Union[Mapping[str, Any], tuple[Any, ...], list[Any]]


def _as_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {label}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Non-finite {label}: {value!r}")
    return number


def validate_lat_lon(lat: Any, lon: Any) -> tuple[float, float]:
    """Normalize and validate latitude/longitude."""
    lat_f = _as_float(lat, "lat")
    lon_f = _as_float(lon, "lon")
    if not (-90.0 <= lat_f <= 90.0):
        raise ValueError(f"lat out of range [-90, 90]: {lat_f}")
    if not (-180.0 <= lon_f <= 180.0):
        raise ValueError(f"lon out of range [-180, 180]: {lon_f}")
    return lat_f, lon_f


def haversine_km(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    *,
    radius_km: float = EARTH_RADIUS_KM,
) -> float:
    """Great-circle distance between two WGS84 points in kilometres."""
    φ1, λ1 = validate_lat_lon(lat1, lon1)
    φ2, λ2 = validate_lat_lon(lat2, lon2)

    φ1_r = math.radians(φ1)
    φ2_r = math.radians(φ2)
    Δφ = math.radians(φ2 - φ1)
    Δλ = math.radians(λ2 - λ1)

    sin_dφ = math.sin(Δφ / 2.0)
    sin_dλ = math.sin(Δλ / 2.0)
    a = sin_dφ * sin_dφ + math.cos(φ1_r) * math.cos(φ2_r) * sin_dλ * sin_dλ
    # Clamp for floating-point edge cases near antipodes / identical points.
    a = min(1.0, max(0.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return radius_km * c


def _extract_point(item: PointLike, index: int) -> tuple[Any, float, float, dict[str, Any]]:
    """Normalize a candidate into (id, lat, lon, extras)."""
    if isinstance(item, Mapping):
        point_id = item.get("id", item.get("code", item.get("jobId", index)))
        if "lat" in item and "lon" in item:
            lat, lon = validate_lat_lon(item["lat"], item["lon"])
        elif "latitude" in item and "longitude" in item:
            lat, lon = validate_lat_lon(item["latitude"], item["longitude"])
        else:
            raise ValueError(f"Point missing lat/lon at index {index}: {item!r}")
        extras = {k: v for k, v in item.items() if k not in {"lat", "lon", "latitude", "longitude", "distanceKm"}}
        extras.setdefault("id", point_id)
        return point_id, lat, lon, extras

    if isinstance(item, (tuple, list)):
        if len(item) < 3:
            raise ValueError(f"Tuple/list point needs (id, lat, lon), got: {item!r}")
        point_id, lat_raw, lon_raw = item[0], item[1], item[2]
        lat, lon = validate_lat_lon(lat_raw, lon_raw)
        return point_id, lat, lon, {"id": point_id}

    raise TypeError(f"Unsupported point type at index {index}: {type(item)!r}")


def sort_by_proximity(
    reference_lat: float,
    reference_lon: float,
    points: Sequence[PointLike],
    *,
    radius_km: float = EARTH_RADIUS_KM,
    skip_invalid: bool = False,
) -> list[dict[str, Any]]:
    """Sort points by ascending Haversine distance from the reference.

    Each result dict includes the original fields plus ``distanceKm``.
    """
    ref_lat, ref_lon = validate_lat_lon(reference_lat, reference_lon)
    ranked: list[dict[str, Any]] = []

    for index, item in enumerate(points or ()):
        try:
            _point_id, lat, lon, extras = _extract_point(item, index)
            distance = haversine_km(ref_lat, ref_lon, lat, lon, radius_km=radius_km)
        except (TypeError, ValueError):
            if skip_invalid:
                continue
            raise
        row = dict(extras)
        row["lat"] = lat
        row["lon"] = lon
        row["distanceKm"] = round(distance, 3)
        ranked.append(row)

    ranked.sort(key=lambda row: (float(row["distanceKm"]), str(row.get("id", ""))))
    return ranked


def sort_country_codes_by_proximity(
    reference_country_code: str,
    candidate_country_codes: Iterable[str],
    *,
    country_point_fn=None,
) -> list[dict[str, Any]]:
    """Convenience: sort ISO country codes by proximity using geo_dataset points.

    ``country_point_fn`` is injectable for unit tests (defaults to geo_dataset).
    """
    if country_point_fn is None:
        from app.services.geo_dataset import country_point as country_point_fn

    ref = country_point_fn(reference_country_code)
    if ref is None:
        raise ValueError(f"Unknown reference country: {reference_country_code!r}")

    points: list[dict[str, Any]] = []
    for code in candidate_country_codes:
        code_norm = str(code or "").strip().upper()
        if not code_norm:
            continue
        point = country_point_fn(code_norm)
        if point is None:
            continue
        points.append({"id": code_norm, "code": code_norm, "lat": point[0], "lon": point[1]})

    return sort_by_proximity(ref[0], ref[1], points)


def resolve_region_point(
    region_code: str,
    region_type: str = "country",
    *,
    country_point_fn=None,
    cluster_centroid_fn=None,
) -> Optional[tuple[float, float]]:
    """Resolve a country or cluster code to a (lat, lon) reference point."""
    kind = (region_type or "country").strip().lower()
    code = (region_code or "").strip()
    if not code:
        return None

    if kind == "country":
        if country_point_fn is None:
            from app.services.geo_dataset import country_point as country_point_fn
        return country_point_fn(code)

    if kind == "cluster":
        if cluster_centroid_fn is None:
            from app.services.geo_dataset import cluster_centroid as cluster_centroid_fn
        return cluster_centroid_fn(code)

    raise ValueError(f"Unsupported regionType: {region_type!r}")
