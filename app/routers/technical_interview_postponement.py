"""Technical Interview Postponement API endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.auth import AuthPrincipal, assert_candidate_access, get_current_principal
from app.database import get_db
from app.rbac import constants as C
from app.rbac.audit import write_audit
from app.schemas import (
    PostponeTechnicalInterviewRequest,
    PostponeTechnicalInterviewResponse,
    StartTechnicalInterviewRequest,
    StartTechnicalInterviewResponse,
    TechnicalInterviewStateResponse,
    ExtendTechnicalInterviewRequest,
    ExtendTechnicalInterviewResponse,
    ReopenTechnicalInterviewRequest,
    ReopenTechnicalInterviewResponse,
    TechnicalInterviewStatus,
)
from app.services.technical_interview_postponement import (
    get_platform_postponement_config,
    get_org_postponement_config,
    get_job_postponement_config,
    create_postponement_record,
    get_postponement_record,
    get_candidate_postponed_interviews,
    can_postpone,
    can_start_interview,
    start_interview,
    extend_deadline,
    reopen_interview,
    mark_not_attended,
    schedule_interview_reminders,
    get_interview_state,
    format_deadline_local,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["technical_interview_postponement"])


def _require_rbac_action(
    principal: AuthPrincipal,
    action: str,
    *,
    detail_fallback: str = "This action is not permitted for your role.",
) -> None:
    """Enforce matrix permission for postponement actions."""
    from app.rbac.permissions import can

    org_id = getattr(principal, "org_id", None)
    if not org_id:
        raise HTTPException(
            status_code=403,
            detail=detail_fallback,
        )

    decision = can(
        user_id=principal.subject,
        org_id=org_id,
        module=C.MODULE_RECRUITMENT,
        action=action,
        membership_status=getattr(principal, "membership_status", None),
    )
    if decision.allowed:
        return

    write_audit(
        action=C.AUDIT_PERMISSION_REFUSED,
        actor_id=principal.subject,
        org_id=org_id,
        module=C.MODULE_RECRUITMENT,
        permission_action=action,
        target={"reason": decision.reason},
    )
    raise HTTPException(
        status_code=403,
        detail=decision.reason or detail_fallback,
    )


@router.get("/api/applications/{application_id}/technical-interview")
async def get_technical_interview_state(
    application_id: str,
    principal=Depends(get_current_principal),
) -> TechnicalInterviewStateResponse:
    """Get current technical interview state for an application."""
    db = get_db()
    
    # Try to find application by ObjectId first, then by string _id
    application = None
    if ObjectId.is_valid(application_id):
        application = db["candidateInformation"].find_one({"_id": ObjectId(application_id)})
    
    if not application:
        # Try finding by string _id field
        application = db["candidateInformation"].find_one({"_id": application_id})
    
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    
    # If candidate, verify access
    if principal.role == "candidate":
        assert_candidate_access(principal, application.get("candidate_id"), application.get("job_id"))
    
    # Get interview state
    state = get_interview_state(application_id)
    
    # Add job and company info
    job_id = application.get("job_id")
    job = db["jobview"].find_one({"jobid": job_id}) if job_id else None
    
    if job:
        state["jobTitle"] = job.get("jobrole", "Unknown Position")
        state["company"] = job.get("companyName", "Unknown Company")
    else:
        state["jobTitle"] = "Unknown Position"
        state["company"] = "Unknown Company"
    
    return TechnicalInterviewStateResponse(**state)


@router.post("/api/applications/{application_id}/technical-interview/postpone")
async def postpone_technical_interview(
    application_id: str,
    payload: PostponeTechnicalInterviewRequest,
    principal=Depends(get_current_principal),
) -> PostponeTechnicalInterviewResponse:
    """Postpone technical interview for an application."""
    db = get_db()
    
    # Try to find application by ObjectId first, then by string _id
    application = None
    if ObjectId.is_valid(application_id):
        application = db["candidateInformation"].find_one({"_id": ObjectId(application_id)})
    
    if not application:
        # Try finding by string _id field
        application = db["candidateInformation"].find_one({"_id": application_id})
    
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    
    assert_candidate_access(principal, application.get("candidate_id"), application.get("job_id"))
    
    job_id = application.get("job_id")
    
    # Verify technical interview is enabled for the job
    job = db["jobview"].find_one({"jobid": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if not job.get("technicalInterviewEnabled", False):
        raise HTTPException(
            status_code=400,
            detail="Technical interview is not enabled for this job"
    )
    
    # Check if postponement is allowed
    config = get_job_postponement_config(job_id)
    if not config.get("postponeTechnicalInterviewEnabled", True):
        raise HTTPException(
            status_code=400,
            detail="Postponement is not enabled for this job"
    )
    
    # Check if candidate can postpone
    can_postpone_flag, reason = can_postpone(application_id)
    if not can_postpone_flag:
        raise HTTPException(status_code=400, detail=reason)
    
    # Check if interview has already started
    existing_record = get_postponement_record(application_id)
    if existing_record and existing_record.get("startedAt"):
        raise HTTPException(
            status_code=400,
            detail="Cannot postpone interview that has already started"
        )
    
    # Create interview configuration snapshot
    interview_config = {
        "difficultyLevel": job.get("difficulty_level", "Intermediate"),
        "technicalQuestions": job.get("technicalQuestions", []),
        "technicalWeightage": job.get("technicalWeightage", 20),
    }
    
    # Create postponement record
    record = create_postponement_record(
        application_id=application_id,
        candidate_id=application.get("candidate_id"),
        job_id=job_id,
        candidate_timezone=payload.candidateTimezone,
        interview_config=interview_config,
        postponement_config=config
    )
    
    # Schedule reminders
    deadline = record.get("deadlineAt")
    if deadline:
        schedule_interview_reminders(application_id, deadline)
    
    # Write audit event
    write_audit(
        action=C.AUDIT_TECHNICAL_INTERVIEW_POSTPONED,
        actor_id=principal.subject,
        target={
            "applicationId": application_id,
            "jobId": job_id,
            "candidateId": application.get("candidate_id")
        },
        module=C.MODULE_RECRUITMENT,
        permission_action="technical_interview.postpone"
    )
    
    # Format response
    deadline_utc = record.get("deadlineAt")
    deadline_local = format_deadline_local(deadline_utc, payload.candidateTimezone)
    
    return PostponeTechnicalInterviewResponse(
        message="Technical interview postponed successfully",
        applicationId=application_id,
        status=record.get("status"),
        deadlineAt=deadline_utc.isoformat(),
        deadlineLocal=deadline_local,
        timezone=payload.candidateTimezone,
        canPostpone=record.get("postponementNumber", 0) < record.get("maxPostponements", 1),
        canStart=True,
        postponementNumber=record.get("postponementNumber", 0),
        maxPostponements=record.get("maxPostponements", 1)
    )


@router.post("/api/applications/{application_id}/technical-interview/start")
async def start_technical_interview(
    application_id: str,
    payload: StartTechnicalInterviewRequest,
    principal=Depends(get_current_principal),
) -> StartTechnicalInterviewResponse:
    """Start technical interview for an application."""
    db = get_db()
    
    # Try to find application by ObjectId first, then by string _id
    application = None
    if ObjectId.is_valid(application_id):
        application = db["candidateInformation"].find_one({"_id": ObjectId(application_id)})
    
    if not application:
        # Try finding by string _id field
        application = db["candidateInformation"].find_one({"_id": application_id})
    
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    
    assert_candidate_access(principal, application.get("candidate_id"), application.get("job_id"))
    
    # Check if candidate can start
    can_start_flag, reason = can_start_interview(application_id)
    if not can_start_flag:
        raise HTTPException(status_code=400, detail=reason)
    
    # Start interview
    record = start_interview(application_id)
    
    # Write audit event
    write_audit(
        action=C.AUDIT_TECHNICAL_INTERVIEW_STARTED_AFTER_POSTPONEMENT,
        actor_id=principal.subject,
        target={
            "applicationId": application_id,
            "jobId": application.get("job_id"),
            "candidateId": application.get("candidate_id")
        },
        module=C.MODULE_RECRUITMENT,
        permission_action="technical_interview.start"
    )
    
    return StartTechnicalInterviewResponse(
        message="Technical interview started successfully",
        applicationId=application_id,
        status=record.get("status"),
        interviewId=record.get("interviewId")
    )


@router.get("/api/candidate/technical-interviews/postponed")
async def get_candidate_postponed_interviews(
    principal=Depends(get_current_principal),
) -> JSONResponse:
    """Get all active postponed interviews for the current candidate."""
    if principal.role != "candidate":
        raise HTTPException(status_code=403, detail="Only candidates can access their postponed interviews")
    
    interviews = get_candidate_postponed_interviews(principal.subject)
    
    # Enrich with job information
    db = get_db()
    enriched_interviews = []
    
    for interview in interviews:
        job_id = interview.get("jobId")
        job = db["jobview"].find_one({"jobid": job_id})
        
        enriched = {
            "applicationId": interview.get("applicationId"),
            "jobTitle": job.get("jobrole", "Unknown Position") if job else "Unknown Position",
            "company": job.get("companyName", "Unknown Company") if job else "Unknown Company",
            "deadlineAt": interview.get("deadlineAt").isoformat() if interview.get("deadlineAt") else None,
            "status": interview.get("status"),
            "postponementNumber": interview.get("postponementNumber"),
            "maxPostponements": interview.get("maxPostponements"),
        }
        
        # Add formatted deadline
        if interview.get("deadlineAt"):
            timezone_str = interview.get("candidateTimezoneAtPostponement", "UTC")
            enriched["deadlineLocal"] = format_deadline_local(
                interview.get("deadlineAt"),
                timezone_str
            )
            enriched["timezone"] = timezone_str
        
        enriched_interviews.append(enriched)
    
    return JSONResponse(content=enriched_interviews)


@router.post("/api/applications/{application_id}/technical-interview/extend")
async def extend_technical_interview_deadline(
    application_id: str,
    payload: ExtendTechnicalInterviewRequest,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ExtendTechnicalInterviewResponse:
    """Extend technical interview deadline (recruiter action)."""
    db = get_db()
    
    # Try to find application by ObjectId first, then by string _id
    application = None
    if ObjectId.is_valid(application_id):
        application = db["candidateInformation"].find_one({"_id": ObjectId(application_id)})
    
    if not application:
        # Try finding by string _id field
        application = db["candidateInformation"].find_one({"_id": application_id})
    
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    
    # Enforce RBAC permission
    _require_rbac_action(principal, C.ACTION_TECHNICAL_INTERVIEW_EXTEND)
    
    # Extend deadline
    record = extend_deadline(application_id, payload.extensionHours, principal.subject)
    
    # Format response
    old_deadline = record.get("deadlineAt") - timedelta(hours=payload.extension_hours)
    new_deadline = record.get("deadlineAt")
    
    return ExtendTechnicalInterviewResponse(
        message="Technical interview deadline extended successfully",
        applicationId=application_id,
        oldDeadline=old_deadline.isoformat(),
        newDeadline=new_deadline.isoformat(),
        extensionHours=payload.extensionHours
    )


@router.post("/api/applications/{application_id}/technical-interview/reopen")
async def reopen_technical_interview(
    application_id: str,
    payload: ReopenTechnicalInterviewRequest,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ReopenTechnicalInterviewResponse:
    """Reopen a missed technical interview (recruiter action)."""
    db = get_db()
    
    # Try to find application by ObjectId first, then by string _id
    application = None
    if ObjectId.is_valid(application_id):
        application = db["candidateInformation"].find_one({"_id": ObjectId(application_id)})
    
    if not application:
        # Try finding by string _id field
        application = db["candidateInformation"].find_one({"_id": application_id})
    
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    
    # Enforce RBAC permission
    _require_rbac_action(principal, C.ACTION_TECHNICAL_INTERVIEW_REOPEN)
    
    # Reopen interview
    record = reopen_interview(application_id, payload.newDeadlineHours, principal.subject)
    
    return ReopenTechnicalInterviewResponse(
        message="Technical interview reopened successfully",
        applicationId=application_id,
        newDeadline=record.get("deadlineAt").isoformat(),
        status=record.get("status")
    )