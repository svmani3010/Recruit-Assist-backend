"""Phase 3 — candidate / anonymous region data model helpers.

Document fields (candidates collection):
  phone_country_code   ISO alpha-2 from phone picker (e.g. IN)
  country_field        ISO alpha-2 residence country used for resolution
  resolved_region      { regionType, regionCode, lat, lon }
  is_manual_override   bool — true when set via region/override
  region_source        country_field | phone_code | none | manual_override

Anonymous users (no candidate row) persist the same shape in
``geo_region_sessions`` keyed by session_id, with TTL expiry.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database import get_db
from app.services.distance_engine import resolve_region_point
from app.services.geo_dataset import get_cluster, get_country

RegionType = Literal["country", "cluster"]
RegionSource = Literal["country_field", "phone_code", "none", "manual_override"]

REGION_SOURCES: tuple[str, ...] = (
    "country_field",
    "phone_code",
    "none",
    "manual_override",
)
REGION_TYPES: tuple[str, ...] = ("country", "cluster")

# Anonymous session TTL (Mongo TTL index on expiresAt).
REGION_SESSION_TTL_DAYS = 30
REGION_SESSION_COLLECTION = "geo_region_sessions"

MappingLike = dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_country_code(value: Optional[str]) -> str:
    code = str(value or "").strip().upper()
    if not code:
        return ""
    if len(code) != 2 or not code.isalpha():
        return ""
    return code


def _normalize_cluster_id(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


class ResolvedRegion(BaseModel):
    """Already-resolved reference point for the distance engine."""

    model_config = ConfigDict(extra="ignore")

    regionType: RegionType
    regionCode: str
    lat: float
    lon: float

    @field_validator("regionCode")
    @classmethod
    def _code(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("regionCode is required")
        return text


class CandidateRegionState(BaseModel):
    """Region fields stored on a candidate profile (or mirrored in session)."""

    model_config = ConfigDict(extra="ignore")

    phone_country_code: str = ""
    country_field: str = ""
    resolved_region: Optional[ResolvedRegion] = None
    is_manual_override: bool = False
    region_source: RegionSource = "none"


class RegionSessionCreate(BaseModel):
    """Create / replace an anonymous region session document."""

    model_config = ConfigDict(extra="ignore")

    session_id: Optional[str] = None
    phone_country_code: str = ""
    country_field: str = ""
    resolved_region: Optional[ResolvedRegion] = None
    is_manual_override: bool = False
    region_source: RegionSource = "none"


def build_resolved_region(
    region_code: str,
    region_type: RegionType = "country",
) -> Optional[ResolvedRegion]:
    """Build a ResolvedRegion from the static geo dataset, or None if unknown."""
    kind = (region_type or "country").strip().lower()
    if kind == "country":
        code = _normalize_country_code(region_code)
        if not code or not get_country(code):
            return None
        point = resolve_region_point(code, "country")
        if not point:
            return None
        return ResolvedRegion(
            regionType="country",
            regionCode=code,
            lat=float(point[0]),
            lon=float(point[1]),
        )

    if kind == "cluster":
        cluster_id = _normalize_cluster_id(region_code)
        if not cluster_id or not get_cluster(cluster_id):
            return None
        point = resolve_region_point(cluster_id, "cluster")
        if not point:
            return None
        return ResolvedRegion(
            regionType="cluster",
            regionCode=cluster_id,
            lat=float(point[0]),
            lon=float(point[1]),
        )

    return None


def resolve_region_from_signals(
    country_field: str = "",
    phone_country_code: str = "",
) -> dict[str, Any]:
    """Pure region resolution (Phase 4/5).

    Priority: country_field > phone_country_code > none.
    Stateless — no DB writes.
    """
    country = _normalize_country_code(country_field)
    phone = _normalize_country_code(phone_country_code)

    if country:
        resolved = build_resolved_region(country, "country")
        if resolved is not None:
            return {
                "resolvedRegion": resolved.model_dump(),
                "regionSource": "country_field",
            }

    if phone:
        resolved = build_resolved_region(phone, "country")
        if resolved is not None:
            return {
                "resolvedRegion": resolved.model_dump(),
                "regionSource": "phone_code",
            }

    return {"resolvedRegion": None, "regionSource": "none"}


def onboarding_region_persist_fields(
    *,
    country_name_or_code: str = "",
    phone_country_code: str = "",
    explicit_country_field: str = "",
    existing_doc: Optional[MappingLike] = None,
) -> dict[str, Any]:
    """Compute Mongo `$set` fields for onboarding submit (Phase 5).

    If the candidate already has a manual override, keep resolved_region and
    only refresh signal fields for auditability.
    """
    from app.services.geo_dataset import lookup_country_code

    existing = extract_region_state(existing_doc)
    phone = _normalize_country_code(phone_country_code) or existing.phone_country_code
    country_field = (
        _normalize_country_code(explicit_country_field)
        or lookup_country_code(country_name_or_code)
        or existing.country_field
    )

    if existing.is_manual_override and existing.resolved_region is not None:
        return region_fields_for_mongo(
            CandidateRegionState(
                phone_country_code=phone,
                country_field=country_field,
                resolved_region=existing.resolved_region,
                is_manual_override=True,
                region_source="manual_override",
            )
        )

    resolved_payload = resolve_region_from_signals(
        country_field=country_field,
        phone_country_code=phone,
    )
    resolved = None
    if resolved_payload.get("resolvedRegion"):
        resolved = ResolvedRegion.model_validate(resolved_payload["resolvedRegion"])

    return region_fields_for_mongo(
        CandidateRegionState(
            phone_country_code=phone,
            country_field=country_field,
            resolved_region=resolved,
            is_manual_override=False,
            region_source=resolved_payload["regionSource"],
        )
    )


def apply_manual_override(
    *,
    region_code: str,
    region_type: RegionType = "country",
    candidate_id: str = "",
    session_id: str = "",
    phone_country_code: str = "",
    country_field: str = "",
) -> dict[str, Any]:
    """Single write path for changing resolved region (logged-in or anonymous)."""
    resolved = build_resolved_region(region_code, region_type)
    if resolved is None:
        raise ValueError("Unknown regionCode for the given regionType.")

    state = CandidateRegionState(
        phone_country_code=phone_country_code,
        country_field=country_field,
        resolved_region=resolved,
        is_manual_override=True,
        region_source="manual_override",
    )

    cid = str(candidate_id or "").strip()
    sid = str(session_id or "").strip()
    if cid:
        if not apply_region_fields_to_candidate(cid, state):
            raise LookupError("Candidate not found.")
        return {
            "resolvedRegion": resolved.model_dump(),
            "isManualOverride": True,
            "regionSource": "manual_override",
            "candidateId": cid,
        }

    if not sid:
        sid = new_session_id()
    doc = upsert_region_session(
        RegionSessionCreate(
            session_id=sid,
            phone_country_code=state.phone_country_code,
            country_field=state.country_field,
            resolved_region=resolved,
            is_manual_override=True,
            region_source="manual_override",
        )
    )
    return {
        "resolvedRegion": resolved.model_dump(),
        "isManualOverride": True,
        "regionSource": "manual_override",
        "sessionId": doc["session_id"],
    }


def empty_region_state() -> CandidateRegionState:
    return CandidateRegionState()


def region_fields_for_mongo(state: CandidateRegionState) -> dict[str, Any]:
    """Flat fields suitable for candidates.$set / session docs."""
    resolved = None
    if state.resolved_region is not None:
        resolved = state.resolved_region.model_dump()
    return {
        "phone_country_code": _normalize_country_code(state.phone_country_code),
        "country_field": _normalize_country_code(state.country_field),
        "resolved_region": resolved,
        "is_manual_override": bool(state.is_manual_override),
        "region_source": state.region_source
        if state.region_source in REGION_SOURCES
        else "none",
    }


def extract_region_state(doc: Optional[MappingLike] = None) -> CandidateRegionState:
    """Read region fields from a candidate or session document."""
    raw = doc or {}
    phone = _normalize_country_code(
        raw.get("phone_country_code") or raw.get("phoneCountry")
    )
    country = _normalize_country_code(
        raw.get("country_field") or raw.get("countryCode")
    )

    resolved_raw = raw.get("resolved_region")
    resolved: Optional[ResolvedRegion] = None
    if isinstance(resolved_raw, dict):
        try:
            resolved = ResolvedRegion.model_validate(resolved_raw)
        except Exception:
            resolved = None

    source = str(raw.get("region_source") or "none").strip()
    if source not in REGION_SOURCES:
        source = "none"

    return CandidateRegionState(
        phone_country_code=phone,
        country_field=country,
        resolved_region=resolved,
        is_manual_override=bool(raw.get("is_manual_override")),
        region_source=source,  # type: ignore[arg-type]
    )


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def _sessions():
    return get_db()[REGION_SESSION_COLLECTION]


def ensure_region_session_indexes() -> None:
    """TTL + lookup indexes for anonymous region sessions."""
    coll = _sessions()
    coll.create_index("expiresAt", expireAfterSeconds=0)
    coll.create_index("session_id", unique=True)
    coll.create_index([("updatedAt", -1)])


def get_region_session(session_id: str) -> Optional[dict[str, Any]]:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    return _sessions().find_one({"session_id": sid})


def upsert_region_session(payload: RegionSessionCreate) -> dict[str, Any]:
    """Create or replace an anonymous region session document."""
    sid = str(payload.session_id or "").strip() or new_session_id()
    state = CandidateRegionState(
        phone_country_code=payload.phone_country_code,
        country_field=payload.country_field,
        resolved_region=payload.resolved_region,
        is_manual_override=payload.is_manual_override,
        region_source=payload.region_source,
    )
    now = _now()
    doc = {
        "session_id": sid,
        **region_fields_for_mongo(state),
        "updatedAt": now,
        "expiresAt": now + timedelta(days=REGION_SESSION_TTL_DAYS),
    }
    existing = _sessions().find_one({"session_id": sid}, {"createdAt": 1})
    if existing and existing.get("createdAt"):
        doc["createdAt"] = existing["createdAt"]
    else:
        doc["createdAt"] = now

    _sessions().update_one({"session_id": sid}, {"$set": doc}, upsert=True)
    return doc


def delete_region_session(session_id: str) -> bool:
    sid = str(session_id or "").strip()
    if not sid:
        return False
    result = _sessions().delete_one({"session_id": sid})
    return result.deleted_count > 0


def apply_region_fields_to_candidate(
    candidate_id: str,
    state: CandidateRegionState,
) -> bool:
    """Persist region fields onto a candidate profile document."""
    from bson import ObjectId

    cid = str(candidate_id or "").strip()
    if not cid:
        return False
    try:
        oid = ObjectId(cid)
    except Exception:
        return False

    result = get_db()["candidates"].update_one(
        {"_id": oid},
        {"$set": {**region_fields_for_mongo(state), "regionUpdatedAt": _now()}},
    )
    return result.matched_count > 0
