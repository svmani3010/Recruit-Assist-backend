"""Branded short links with click tracking (AC3)."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi import HTTPException

from app.database import get_db
from app.settings import get_settings

router = APIRouter(tags=["short-links"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _links():
    return get_db()["short_links"]


def _clicks():
    return get_db()["short_link_clicks"]


def create_short_link(
    *,
    destination_url: str,
    candidate_id: str = "",
    event: str = "",
    job_id: str = "",
) -> str:
    """Return absolute short URL on branded domain (or /r/{code} fallback)."""
    settings = get_settings()
    code = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]
    _links().insert_one(
        {
            "code": code,
            "destination": destination_url,
            "candidate_id": candidate_id,
            "job_id": job_id,
            "event": event,
            "createdAt": _now(),
            "clicks": 0,
        }
    )
    base = (getattr(settings, "short_link_base_url", None) or "").strip().rstrip("/")
    if not base:
        public_api = (getattr(settings, "public_api_url", None) or "").strip().rstrip("/")
        if public_api:
            base = f"{public_api}/r"
        else:
            base = f"{(settings.frontend_url or '').rstrip('/')}/r"
    return f"{base}/{code}"


def resolve_short_link(code: str, *, ip: str = "", user_agent: str = "") -> str:
    doc = _links().find_one({"code": str(code or "").strip()})
    if not doc:
        raise HTTPException(status_code=404, detail="Link not found")
    _links().update_one({"_id": doc["_id"]}, {"$inc": {"clicks": 1}})
    _clicks().insert_one(
        {
            "code": code,
            "candidate_id": doc.get("candidate_id") or "",
            "event": doc.get("event") or "",
            "ip": ip or "unknown",
            "user_agent": (user_agent or "")[:400],
            "createdAt": _now(),
        }
    )
    return str(doc.get("destination") or "/")


@router.get("/r/{code}")
async def redirect_short_link(code: str, request: Request):
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
        or "unknown"
    )
    ua = request.headers.get("user-agent", "") or ""
    dest = resolve_short_link(code, ip=ip, user_agent=ua)
    return RedirectResponse(url=dest, status_code=302)
