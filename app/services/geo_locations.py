"""Country / city lookup for HR job posting location dropdowns.

Countries load from pycountry (offline, small).
Cities load per-country on demand and are cached in MongoDB so the frontend
never downloads a full world cities dataset.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

import httpx
import pycountry

from app.database import get_db

logger = logging.getLogger(__name__)

CITIES_CACHE_DAYS = 30
COUNTRIESNOW_CITIES_URL = "https://countriesnow.space/api/v0.1/countries/cities/q"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cache():
    return get_db()["geo_cities_cache"]


@lru_cache(maxsize=1)
def list_countries() -> tuple[dict[str, str], ...]:
    """Return sorted countries as ({code, name}, ...). Cached in-process."""
    rows: list[dict[str, str]] = []
    for country in pycountry.countries:
        name = getattr(country, "common_name", None) or country.name
        rows.append({"code": country.alpha_2, "name": str(name)})
    rows.sort(key=lambda r: r["name"].casefold())
    return tuple(rows)


def resolve_country_name(country: str = "", country_code: str = "") -> str:
    """Normalize to the English country name used by the cities provider."""
    code = (country_code or "").strip().upper()
    name = (country or "").strip()
    if code:
        hit = pycountry.countries.get(alpha_2=code)
        if hit:
            return getattr(hit, "common_name", None) or hit.name
    if not name:
        return ""
    # Exact / fuzzy match against pycountry
    try:
        hit = pycountry.countries.lookup(name)
        return getattr(hit, "common_name", None) or hit.name
    except LookupError:
        return name


def _fetch_cities_remote(country_name: str) -> list[str]:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(
                COUNTRIESNOW_CITIES_URL,
                params={"country": country_name},
            )
        if response.status_code >= 400:
            logger.error(
                "Cities provider HTTP %s for country=%s",
                response.status_code,
                country_name,
            )
            return []
        payload = response.json() if response.content else {}
        if payload.get("error"):
            logger.warning(
                "Cities provider error for country=%s: %s",
                country_name,
                payload.get("msg"),
            )
            return []
        data = payload.get("data") or []
        cities = sorted(
            {str(c).strip() for c in data if str(c).strip()},
            key=lambda c: c.casefold(),
        )
        return cities
    except Exception:
        logger.exception("Failed fetching cities for country=%s", country_name)
        return []


def list_cities_for_country(
    *,
    country: str = "",
    country_code: str = "",
) -> dict[str, Any]:
    country_name = resolve_country_name(country=country, country_code=country_code)
    if not country_name:
        return {"country": "", "countryCode": "", "cities": []}

    code = (country_code or "").strip().upper()
    if not code:
        try:
            hit = pycountry.countries.lookup(country_name)
            code = hit.alpha_2
        except LookupError:
            code = ""

    cache_key = code or country_name.casefold()
    cached = _cache().find_one({"_id": cache_key})
    cached_cities = list(cached.get("cities") or []) if cached else []
    if cached and cached_cities:
        expires = cached.get("expiresAt")
        if not expires or expires > _now():
            return {
                "country": cached.get("country") or country_name,
                "countryCode": cached.get("countryCode") or code,
                "cities": cached_cities,
                "cached": True,
            }

    cities = _fetch_cities_remote(country_name)
    if cities:
        doc = {
            "_id": cache_key,
            "country": country_name,
            "countryCode": code,
            "cities": cities,
            "updatedAt": _now(),
            "expiresAt": _now() + timedelta(days=CITIES_CACHE_DAYS),
        }
        _cache().update_one({"_id": cache_key}, {"$set": doc}, upsert=True)
    elif cached:
        # Drop empty/failed cache entries so the next request can retry.
        _cache().delete_one({"_id": cache_key})
    return {
        "country": country_name,
        "countryCode": code,
        "cities": cities,
        "cached": False,
    }
