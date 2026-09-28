"""Attach Mongo query filters from an effective permission scope."""

from __future__ import annotations

from typing import Any, Optional

from app.rbac import constants as C


def managed_team_ids(user_id: str, org_id: str) -> list[str]:
    """Team ids where the user is marked manager (scaffolding)."""
    from app.database import get_db

    db = get_db()
    memberships = list(
        db[C.COL_TEAM_MEMBERSHIPS].find(
            {"user_id": str(user_id), "is_manager": True},
            {"team_id": 1},
        )
    )
    team_ids = [str(m["team_id"]) for m in memberships if m.get("team_id")]
    if not team_ids:
        return []
    # Restrict to teams in this org
    teams = db[C.COL_TEAMS].find(
        {"_id": {"$in": _as_oids(team_ids)}, "org_id": str(org_id)},
        {"_id": 1},
    )
    return [str(t["_id"]) for t in teams]


def _as_oids(ids: list[str]) -> list:
    from bson import ObjectId

    out = []
    for i in ids:
        try:
            out.append(ObjectId(i))
        except Exception:
            continue
    return out


def build_scope_filter(
    *,
    scope: str,
    user_id: str,
    org_id: Optional[str],
    collection: str = "jobview",
) -> dict[str, Any]:
    """Build a Mongo filter for an allowed Yes/No decision.

    Yes/No matrix: ``yes`` and any legacy Full/Team/Own/Assigned/Config value
    collapse to the organisation bucket. ``none`` / ``no`` → deny-all filter.
    Dual-path: include legacy ``hr_user_id`` so pre-backfill rows stay visible.
    """
    user_id = str(user_id)
    org_id = str(org_id) if org_id else None
    scope = str(scope or C.SCOPE_NONE).strip().lower()

    if scope in {C.SCOPE_NONE, "no", "n", "false", "0"} or not org_id:
        return {"_id": {"$exists": False}}

    # Allowed (yes) → organisation bucket (+ legacy hr_user_id).
    return {
        "$or": [
            {"organization_id": org_id},
            {"org_id": org_id},
            {"hr_user_id": user_id},
        ]
    }


def _own_filter(user_id: str, org_id: str) -> dict[str, Any]:
    return {
        "$or": [
            {"created_by": user_id, "organization_id": org_id},
            {"hr_user_id": user_id},
        ]
    }


def merge_filters(*parts: dict[str, Any]) -> dict[str, Any]:
    """AND together filter dicts, wrapping in $and when needed."""
    cleaned = [p for p in parts if p]
    if not cleaned:
        return {}
    if len(cleaned) == 1:
        return cleaned[0]
    return {"$and": cleaned}
