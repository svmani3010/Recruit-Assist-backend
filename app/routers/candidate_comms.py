"""HR privacy events + communication maintenance endpoints."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import AuthPrincipal, assert_candidate_access, require_hr
from app.rate_limit import limiter
from app.services.candidate_comms import (
    notify_candidate_milestone,
    process_pending_sms_fallbacks,
    process_queued_quiet_hour_sms,
)
from fastapi import Request

router = APIRouter(tags=["candidate-comms"])


class PrivacyEventPayload(BaseModel):
    jobId: Optional[str] = Field(default=None, alias="jobId")
    applicationId: Optional[str] = None

    model_config = {"populate_by_name": True}


def _fire(event: str, candidate_id: str, job_id: str, principal: AuthPrincipal) -> dict:
    return notify_candidate_milestone(
        event=event,
        candidate_id=candidate_id,
        job_id=job_id or "",
        data={
            "hr_subject": principal.subject,
            "status": event,
        },
    )


@router.post("/api/candidates/{candidate_id}/events/view-score")
@limiter.limit("30/minute")
async def event_view_score(
    request: Request,
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
    payload: PrivacyEventPayload | None = None,
):
    body = payload or PrivacyEventPayload()
    job_id = body.jobId or ""
    if job_id:
        assert_candidate_access(principal, candidate_id, job_id)
    result = _fire("recruiter_viewed_profile", candidate_id, job_id, principal)
    return {"success": True, "event": "recruiter_viewed_profile", "dispatch": result}


@router.post("/api/candidates/{candidate_id}/events/download-resume")
@limiter.limit("30/minute")
async def event_download_resume(
    request: Request,
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
    payload: PrivacyEventPayload | None = None,
):
    body = payload or PrivacyEventPayload()
    job_id = body.jobId or ""
    if job_id:
        assert_candidate_access(principal, candidate_id, job_id)
    result = _fire("recruiter_downloaded_resume", candidate_id, job_id, principal)
    return {"success": True, "event": "recruiter_downloaded_resume", "dispatch": result}


@router.post("/api/internal/comms/flush")
@limiter.limit("10/minute")
async def flush_comms_queues(request: Request, principal: Annotated[AuthPrincipal, Depends(require_hr)]):
    """Process WA→SMS fallbacks and quiet-hour SMS queue (callable by cron/HR)."""
    _ = principal
    return {
        "fallbackSent": process_pending_sms_fallbacks(),
        "quietHourSent": process_queued_quiet_hour_sms(),
    }
