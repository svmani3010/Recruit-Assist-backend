"""Dashboard notification endpoints for HR and candidates."""

from __future__ import annotations

from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from app.auth import AuthPrincipal, Role, require_candidate, require_hr
from app.database import get_db
from app.services.notifications import serialize_notification

router = APIRouter(tags=["notifications"])


def _notifications_collection():
    return get_db()["dashboard_notifications"]


@router.get("/api/notifications/hr")
async def list_hr_notifications(
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
    limit: int = 30,
):
    query: dict = {"audience": "hr"}
    if principal.role == Role.HR:
        query["hr_user_id"] = principal.subject
    docs = list(
        _notifications_collection()
        .find(query)
        .sort("created_at", -1)
        .limit(min(limit, 100))
    )
    return {"notifications": [serialize_notification(d) for d in docs]}


@router.get("/api/notifications/candidate")
async def list_candidate_notifications(
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
    limit: int = 30,
):
    candidate_id = principal.candidate_id or principal.subject
    candidate_doc = get_db()["candidates"].find_one({"_id": ObjectId(candidate_id)})
    candidate_email = (candidate_doc or {}).get("email", "").lower()

    query = {
        "audience": "candidate",
        "$or": [{"candidate_id": candidate_id}],
    }
    if candidate_email:
        query["$or"].append({"candidate_email": candidate_email})

    docs = list(
        _notifications_collection()
        .find(query)
        .sort("created_at", -1)
        .limit(min(limit, 100))
    )
    return {"notifications": [serialize_notification(d) for d in docs]}


@router.patch("/api/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    result = _notifications_collection().update_one(
        {
            "_id": ObjectId(notification_id),
            "audience": "hr",
            "hr_user_id": principal.subject,
        },
        {"$set": {"read": True}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"success": True}


@router.post("/api/notifications/hr/read-all")
async def mark_all_hr_notifications_read(
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    query: dict = {"audience": "hr", "read": False}
    if principal.role == Role.HR:
        query["hr_user_id"] = principal.subject
    result = _notifications_collection().update_many(query, {"$set": {"read": True}})
    return {"success": True, "updated": result.modified_count}


@router.post("/api/notifications/candidate/read-all")
async def mark_all_candidate_notifications_read(
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    candidate_id = principal.candidate_id or principal.subject
    candidate_doc = get_db()["candidates"].find_one({"_id": ObjectId(candidate_id)})
    candidate_email = (candidate_doc or {}).get("email", "").lower()

    query: dict = {
        "audience": "candidate",
        "read": False,
        "$or": [{"candidate_id": candidate_id}],
    }
    if candidate_email:
        query["$or"].append({"candidate_email": candidate_email})

    result = _notifications_collection().update_many(query, {"$set": {"read": True}})
    return {"success": True, "updated": result.modified_count}
