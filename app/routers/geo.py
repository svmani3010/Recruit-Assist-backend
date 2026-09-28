"""Geo lookup APIs — countries/clusters (public) + cities (HR job posting)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import require_hr
from app.rate_limit import limiter
from app.services import geo_dataset
from app.services.geo_locations import list_cities_for_country

router = APIRouter(tags=["geo"])


@router.get("/api/geo/countries")
@limiter.limit("120/minute")
async def get_countries(request: Request):
    """Public country dataset with lat/lon + cluster membership (Phase 1/4)."""
    _ = request
    countries = []
    for row in geo_dataset.list_countries():
        cluster_ids = list(row.get("clusters") or [])
        cluster_names = []
        for cid in cluster_ids:
            cluster = geo_dataset.get_cluster(cid)
            cluster_names.append(cluster["name"] if cluster else cid)
        countries.append(
            {
                "code": row["code"],
                "name": row["name"],
                "lat": row["lat"],
                "lon": row["lon"],
                "clusters": cluster_names,
                "clusterIds": cluster_ids,
            }
        )
    return {"countries": countries}


@router.get("/api/geo/clusters")
@limiter.limit("120/minute")
async def get_clusters(request: Request):
    """Public continent / sub-region clusters with member country codes."""
    _ = request
    return {"clusters": list(geo_dataset.list_clusters())}


@router.get("/api/geo/cities")
@limiter.limit("60/minute")
async def get_cities(
    request: Request,
    principal=Depends(require_hr),
    country: Optional[str] = Query(None, description="Country name, e.g. India"),
    countryCode: Optional[str] = Query(None, description="ISO alpha-2, e.g. IN"),
):
    """Return cities for one country only (HR job posting; fetched/cached server-side)."""
    _ = principal
    if not (country or countryCode):
        raise HTTPException(
            status_code=400,
            detail="Provide country or countryCode.",
        )
    result = list_cities_for_country(
        country=country or "",
        country_code=countryCode or "",
    )
    if not result.get("country"):
        raise HTTPException(status_code=404, detail="Country not found.")
    return result
