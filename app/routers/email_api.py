"""Email and calendar endpoints."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import assert_candidate_access, get_current_principal, require_hr
from app.database import get_db
from app.helpers import generate_ics_content, get_job_role_helper
from app.schemas import (
    CandidateApplyNotificationRequest,
    ListEventsRequest,
    SendEmailRequest,
)
from app.services.candidate_apply_notify import send_candidate_apply_email
from app.services.screening import get_gemini, run_in_thread
from app.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["email"])


@router.post("/api/candidate_apply_notification")
async def candidate_apply_notification(
    payload: CandidateApplyNotificationRequest,
    principal=Depends(get_current_principal),
):
    """Notify job HR once after the candidate submits their application."""
    from app.auth import Role

    if principal.role != Role.CANDIDATE:
        raise HTTPException(
            status_code=403,
            detail="Only candidates can trigger application notifications.",
        )

    candidate_id = str(getattr(principal, "candidate_id", None) or principal.subject or "")
    job_id = str(payload.job_id or "").strip()
    assert_candidate_access(principal, candidate_id, job_id)

    try:
        sent, message = send_candidate_apply_email(
            job_id=job_id,
            candidate_id=candidate_id,
            candidate_name=payload.candidate_name,
            job_role=payload.job_role,
            submission_date=payload.submission_date,
        )
        if not sent and "already sent" not in message.lower():
            return {
                "status": "warning",
                "email_sent": False,
                "message": message,
                "job_role": payload.job_role,
                "job_id": job_id,
            }
        return {
            "status": "success",
            "email_sent": True,
            "message": message,
            "job_role": payload.job_role,
            "job_id": job_id,
        }
    except Exception as e:
        logger.exception("candidate_apply_notification failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/send_email")
async def send_email_api(
    payload: SendEmailRequest, principal=Depends(require_hr)
):
    try:
        from email_utils import email_service

        data = payload.model_dump(exclude_unset=True)
        c_id = payload.candidate_id
        j_id = payload.job_id
        db = get_db()
        settings = get_settings()
        info: dict = {}

        if c_id:
            info = db["candidateInformation"].find_one({"candidate_id": c_id}) or {}
            if not j_id:
                j_id = info.get("job_id")
            if not data.get("candidate_name"):
                resolved = (
                    f"{info.get('first_name', '')} {info.get('last_name', '')}"
                ).strip()
                data["candidate_name"] = resolved or "Candidate"
            if not data.get("candidate_email"):
                data["candidate_email"] = info.get("email")
            if not data.get("position") or data.get("position") in {
                "Position",
                "[Position]",
                "N/A",
            }:
                data["position"] = info.get("applied_for")

        if j_id:
            assert_candidate_access(principal, c_id or "", str(j_id))

        if not data.get("position") or data.get("position") in {
            "Position",
            "[Position]",
            "N/A",
        }:
            data["position"] = (
                get_job_role_helper(j_id, c_id) if j_id else "Candidate"
            )

        if not data.get("interviewers"):
            data["interviewers"] = [
                {"name": "Hiring Team", "email": email}
                for email in settings.hr_email_list
            ]

        if not data.get("candidate_email"):
            raise HTTPException(
                status_code=400,
                detail="Candidate email could not be resolved from ID",
            )

        data.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
        data.setdefault("time", "10:00 AM")
        from app.services.calendar_identity import is_microsoft_meeting

        default_meet = settings.default_meeting_link
        if not is_microsoft_meeting(data) and (
            not data.get("meeting_link") or data.get("meeting_link") == default_meet
        ):
            data["meeting_link"] = default_meet

        email_text = await run_in_thread(get_gemini().generate_interview_email, data)
        ics_data = generate_ics_content(data)
        itv_emails = [i.get("email") for i in data.get("interviewers", []) if i.get("email")]
        recipients = list(
            set([data["candidate_email"]] + itv_emails + settings.hr_email_list)
        )
        html_content = re.sub(
            r"\*\*(.*?)\*\*", r"<b>\1</b>", email_text.replace("\n", "<br>")
        )
        email_service.send_email(
            subject=(
                f"Interview Invitation - {data.get('candidate_name')} - "
                f"{data.get('position')} - {data.get('interview_level')}"
            ),
            body_html=html_content,
            body_text=email_text,
            recipients=recipients,
            calendar_ics=ics_data,
        )

        try:
            from app.helpers import get_candidate_profile_picture
            from app.services.notifications import create_notification

            company_name = ""
            if j_id:
                job_doc = db["jobview"].find_one({"jobid": j_id}) or {}
                company_name = (
                    job_doc.get("companyName")
                    or job_doc.get("company")
                    or ""
                )
            company_label = company_name or "the hiring company"
            position = data.get("position") or "this role"
            candidate_picture = (
                get_candidate_profile_picture(c_id) if c_id else None
            )
            create_notification(
                audience="candidate",
                notification_type="interview_scheduled",
                message=(
                    f"Your interview has been scheduled by {company_label} "
                    f"for {position}. Please check your email for details."
                ),
                candidate_id=c_id,
                candidate_email=data.get("candidate_email"),
                candidate_picture=candidate_picture,
                job_id=j_id,
                job_role=position,
                company_name=company_name or None,
            )
        except Exception as notify_exc:
            logger.warning("Dashboard notification failed: %s", notify_exc)

        return {
            "role": "assistant",
            "parts": [{"text": email_text}],
            "resolved_details": {
                "candidate_name": data["candidate_name"],
                "candidate_email": data["candidate_email"],
                "position": data["position"],
                "candidate_id": c_id,
                "job_id": j_id,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("send_email_api failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/list-events")
async def list_events_api(
    payload: ListEventsRequest,
    principal=Depends(require_hr),
):
    """Calendar busy blocks for one email using the logged-in HR's Google connection."""
    from datetime import datetime

    from app.services.hr_calendar_oauth import CalendarNotConfiguredError
    from app.services.google_calendar import CalendarAccessError
    from app.services.calendar_availability import fetch_events_for_email
    from app.services.hr_calendar_oauth import get_hr_email_for_user_id

    hr_email = get_hr_email_for_user_id(principal.subject)
    if not hr_email:
        raise HTTPException(status_code=400, detail="HR email not found.")

    email_id = payload.email_id.lower().strip()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    if payload.start_date and payload.end_date:
        start = datetime.strptime(payload.start_date, "%Y-%m-%d")
        end = datetime.strptime(payload.end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )
    else:
        start = today
        end = today + timedelta(days=payload.days)

    try:
        events = fetch_events_for_email(email_id, start, end, hr_email=hr_email)
    except (CalendarNotConfiguredError, CalendarAccessError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "source": "google_freebusy",
        "calendar_mode": "google",
        "hr_email": hr_email,
        "total_events": len(events),
        "data": {"events": events},
    }
