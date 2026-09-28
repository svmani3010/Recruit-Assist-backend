"""In-app dashboard notifications for HR and candidates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _collection():
    from app.database import get_db

    return get_db()["dashboard_notifications"]


def create_notification(
    audience: str,
    notification_type: str,
    message: str,
    *,
    candidate_id: Optional[str] = None,
    candidate_email: Optional[str] = None,
    candidate_picture: Optional[str] = None,
    job_id: Optional[str] = None,
    job_role: Optional[str] = None,
    company_name: Optional[str] = None,
    hr_user_id: Optional[str] = None,
) -> str:
    doc: dict[str, Any] = {
        "audience": audience,
        "type": notification_type,
        "message": message,
        "candidate_id": candidate_id,
        "candidate_email": candidate_email,
        "candidate_picture": candidate_picture,
        "job_id": job_id,
        "job_role": job_role,
        "company_name": company_name,
        "hr_user_id": hr_user_id,
        "read": False,
        "created_at": datetime.now(timezone.utc),
    }
    result = _collection().insert_one(doc)
    return str(result.inserted_id)


def _to_utc_iso(value: Any) -> Any:
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def serialize_notification(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "id": str(doc.get("_id", "")),
        "type": doc.get("type", ""),
        "message": doc.get("message", ""),
        "candidate_id": doc.get("candidate_id"),
        "candidate_email": doc.get("candidate_email"),
        "candidate_picture": doc.get("candidate_picture"),
        "job_id": doc.get("job_id"),
        "job_role": doc.get("job_role"),
        "company_name": doc.get("company_name"),
        "read": bool(doc.get("read", False)),
        "created_at": _to_utc_iso(created) if isinstance(created, datetime) else created,
    }
