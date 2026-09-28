"""Interview self-scheduling endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.auth import assert_candidate_access, get_current_principal, require_hr
from app.database import get_db
from app.helpers import generate_ics_content
from app.schemas import (
    ConfirmInterviewBookingRequest,
    CreateInterviewInviteRequest,
    InterviewOutcomeRequest,
    RescheduleInterviewInviteRequest,
    ShareInterviewInviteRequest,
)
from app.services.hr_calendar_oauth import get_hr_email_for_user_id
from app.services.interview_scheduling import (
    confirm_interview_booking,
    create_interview_invite,
    get_available_slots_for_invite,
    get_invite_by_token,
    hr_book_interview_slot,
    list_invites_for_job,
    reschedule_interview_invite,
    send_share_invitations,
    set_interview_outcome,
    assert_invite_owned_by_candidate,
    _serialize_invite_public,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["interviews"])


@router.post("/api/interviews/invite")
async def create_invite_api(
    payload: CreateInterviewInviteRequest,
    principal=Depends(require_hr),
):
    """HR creates a scheduling window, approves candidate, and emails booking link."""
    from datetime import datetime, timezone

    from email_utils import notify_candidate

    assert_candidate_access(principal, payload.candidate_id, payload.job_id)

    db = get_db()
    db["Scores"].update_one(
        {"candidate_id": payload.candidate_id, "job_id": payload.job_id},
        {"$set": {"status": "approved", "l1_approved_at": datetime.now(timezone.utc)}},
    )

    interviewers_payload = payload.interviewers or []
    if interviewers_payload:
        main_reviews = [
            {"name": i.name, "email": i.email} for i in interviewers_payload
        ]
        db["candidateInformation"].update_one(
            {"candidate_id": payload.candidate_id},
            {"$set": {"main_reviews": main_reviews}},
        )

    body = payload.model_dump(exclude_unset=True)
    hr_email = get_hr_email_for_user_id(principal.subject)
    if hr_email:
        body["hr_calendar_email"] = hr_email
    if interviewers_payload:
        body["interviewers"] = [
            {"name": i.name, "email": i.email} for i in interviewers_payload
        ]

    result = create_interview_invite(body, hr_principal_user_id=principal.subject)

    company_name = ""
    try:
        job_doc = db["jobview"].find_one({"jobid": payload.job_id}) or {}
        if not job_doc:
            try:
                job_doc = db["jobview"].find_one({"jobid": int(payload.job_id)}) or {}
            except (TypeError, ValueError):
                job_doc = {}
        company_name = (
            job_doc.get("companyName")
            or job_doc.get("company")
            or job_doc.get("company_name")
            or ""
        )
    except Exception:
        company_name = ""

    mail_payload = {
        "candidate_id": payload.candidate_id,
        "job_id": payload.job_id,
        "candidate_name": payload.candidate_name or result.get("candidate_name") or "Candidate",
        "job_role": payload.position or result.get("position") or "this role",
        "booking_url": result["booking_url"],
        "date_range_start": payload.date_range_start,
        "date_range_end": payload.date_range_end,
        "company_name": company_name or "Company",
        "duration_minutes": int(payload.duration_minutes or 60),
        "interview_mode": (payload.interview_mode or "remote"),
        "interview_level": payload.interview_level or "Level 2",
        "location_address": payload.location_address or "",
    }
    candidate_email = result.get("candidate_email") or payload.candidate_email
    try:
        from app.services.candidate_comms import notify_candidate_milestone

        notify_candidate_milestone(
            event="interview_invitation",
            candidate_id=payload.candidate_id,
            job_id=payload.job_id,
            booking_token=str(result.get("token") or ""),
            data={
                **mail_payload,
                "candidate_email": candidate_email or "",
            },
            force_email_action="interview_schedule",
        )
    except Exception as exc:
        logger.warning("Interview invite candidate notify failed: %s", exc)
        try:
            notify_candidate("interview_schedule", mail_payload, candidate_email)
        except Exception as email_exc:
            logger.warning("Interview invite candidate email failed: %s", email_exc)

    try:
        from email_utils import notify_hr
        from app.services.l2_questions_pdf import (
            l2_pdf_recipients,
            l2_questions_pdf_attachment,
        )

        attachment = l2_questions_pdf_attachment(
            payload.candidate_id,
            payload.job_id,
            candidate_name=mail_payload["candidate_name"],
            job_role=mail_payload["job_role"],
        )
        hr_recipients = l2_pdf_recipients(
            candidate_email=candidate_email,
            hr_calendar_email=hr_email,
            interviewers=body.get("interviewers") or [],
        )
        notify_hr(
            "interview_schedule",
            {**mail_payload, "l2_pdf_attached": bool(attachment)},
            recipients=hr_recipients or None,
            attachments=[attachment] if attachment else None,
        )
    except Exception as exc:
        logger.warning("Interview invite HR/interviewer email failed: %s", exc)

    return result


@router.post("/api/interviews/reschedule")
async def reschedule_invite_api(
    payload: RescheduleInterviewInviteRequest,
    principal=Depends(require_hr),
):
    """HR picks a new date range for an existing invite, fetches latest free times,
    cancels the previous calendar event if booked, and emails a reschedule link.
    """
    assert_candidate_access(principal, payload.candidate_id, payload.job_id)

    db = get_db()
    interviewers_payload = payload.interviewers or []
    if interviewers_payload:
        main_reviews = [
            {"name": i.name, "email": i.email} for i in interviewers_payload
        ]
        db["candidateInformation"].update_one(
            {"candidate_id": payload.candidate_id},
            {"$set": {"main_reviews": main_reviews}},
        )

    body = payload.model_dump(exclude_unset=True)
    hr_email = get_hr_email_for_user_id(principal.subject)
    if hr_email:
        body["hr_calendar_email"] = hr_email
    if interviewers_payload:
        body["interviewers"] = [
            {"name": i.name, "email": i.email} for i in interviewers_payload
        ]

    if payload.scheduled_date and payload.scheduled_time:
        return hr_book_interview_slot(body, hr_principal_user_id=principal.subject)

    if not payload.date_range_start or not payload.date_range_end:
        raise HTTPException(
            status_code=400,
            detail="Provide scheduled_date and scheduled_time, or a date range for a new booking link.",
        )

    result = reschedule_interview_invite(body, hr_principal_user_id=principal.subject)

    try:
        from app.helpers import get_candidate_profile_picture
        from app.services.notifications import create_notification

        create_notification(
            audience="candidate",
            notification_type="interview_rescheduled",
            message=(
                f"Your interview for {payload.position or 'this role'} has been "
                f"rescheduled. Please pick a new time from the latest available slots."
            ),
            candidate_id=payload.candidate_id,
            candidate_email=result.get("candidate_email") or payload.candidate_email,
            candidate_picture=get_candidate_profile_picture(payload.candidate_id),
            job_id=payload.job_id,
            job_role=payload.position,
        )
    except Exception as notify_exc:
        logger.warning("Reschedule in-app notification failed: %s", notify_exc)

    return result


@router.get("/api/interviews/job/{job_id}")
async def list_job_interviews_api(job_id: str, principal=Depends(require_hr)):
    """HR list of interview invites for a job — used by the applicant L2 tab."""
    from app.hr_tenant import require_hr_job

    db = get_db()
    require_hr_job(principal, job_id, db)
    return {"interviews": list_invites_for_job(job_id)}


@router.post("/api/interviews/{invite_id}/outcome")
async def set_interview_outcome_api(
    invite_id: str,
    payload: InterviewOutcomeRequest,
    principal=Depends(require_hr),
):
    """HR marks a booked interview as completed or cancelled."""
    from app.hr_tenant import require_hr_job

    db = get_db()
    invite = db["interviewInvites"].find_one({"invite_id": invite_id})
    if not invite:
        raise HTTPException(status_code=404, detail="Interview not found")
    job_id = str(invite.get("job_id") or "")
    if job_id:
        require_hr_job(principal, job_id, db)
        assert_candidate_access(principal, invite.get("candidate_id") or "", job_id)
    return set_interview_outcome(invite_id, payload.status, job_id=job_id)


@router.get("/api/interviews/booking/{token}")
async def get_booking_invite_api(
    token: str, principal=Depends(get_current_principal)
):
    invite = get_invite_by_token(token)
    assert_invite_owned_by_candidate(invite, principal)
    return _serialize_invite_public(invite)


@router.get("/api/interviews/booking/{token}/slots")
async def get_booking_slots_api(
    token: str,
    date: str | None = None,
    principal=Depends(get_current_principal),
):
    invite = get_invite_by_token(token)
    assert_invite_owned_by_candidate(invite, principal)
    return get_available_slots_for_invite(token, date=date)


@router.post("/api/interviews/booking/{token}/confirm")
async def confirm_booking_api(
    token: str,
    payload: ConfirmInterviewBookingRequest,
    principal=Depends(get_current_principal),
):
    invite = get_invite_by_token(token)
    assert_invite_owned_by_candidate(invite, principal)
    return confirm_interview_booking(token, payload.date, payload.time)


@router.get("/api/interviews/booking/{token}/ics")
async def download_booking_ics_api(
    token: str,
    principal=Depends(get_current_principal),
):
    """Download a calendar (.ics) file for a confirmed interview booking."""
    invite = get_invite_by_token(token)
    assert_invite_owned_by_candidate(invite, principal)
    if invite.get("status") != "scheduled":
        raise HTTPException(
            status_code=400,
            detail="Interview must be confirmed before downloading a calendar file.",
        )
    if not invite.get("scheduled_date") or not invite.get("scheduled_time"):
        raise HTTPException(status_code=400, detail="Interview is missing a scheduled time.")

    mode = invite.get("interview_mode") or "remote"
    meeting_link = invite.get("meeting_link") or ""
    location = meeting_link
    if mode == "in_person":
        location = invite.get("location_address") or invite.get("maps_url") or meeting_link or "In-person"

    ics_content = generate_ics_content(
        {
            "date": invite.get("scheduled_date"),
            "time": invite.get("scheduled_time"),
            "duration_minutes": int(invite.get("duration_minutes") or 60),
            "interview_level": invite.get("interview_level") or "Interview",
            "candidate_name": invite.get("candidate_name") or "Candidate",
            "candidate_email": invite.get("candidate_email"),
            "position": invite.get("position") or "Interview",
            "meeting_link": meeting_link or location,
            "location": location,
            "interviewers": invite.get("interviewers") or [],
            "timezone": invite.get("timezone"),
            "meeting_provider": invite.get("meeting_provider"),
            "calendar_provider": invite.get("calendar_provider"),
        }
    )
    if not ics_content:
        raise HTTPException(status_code=500, detail="Could not generate calendar file.")

    filename = f"interview-{(invite.get('position') or 'booking').replace(' ', '-').lower()[:40]}.ics"
    return Response(
        content=ics_content,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/interviews/booking/{token}/share")
async def share_booking_api(
    token: str,
    payload: ShareInterviewInviteRequest,
    principal=Depends(require_hr),
):
    from app.hr_tenant import require_hr_job

    invite = get_invite_by_token(token)
    job_id = str(invite.get("job_id") or "")
    if not job_id:
        raise HTTPException(status_code=400, detail="Interview is missing a job id")
    db = get_db()
    require_hr_job(principal, job_id, db)
    assert_candidate_access(principal, invite.get("candidate_id") or "", job_id)
    return send_share_invitations(token, extra_emails=payload.emails)
