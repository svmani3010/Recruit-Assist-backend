"""Interview invite creation and candidate self-scheduling."""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.database import get_db
from app.helpers import get_job_role_helper, resolve_candidate_contact
from app.services.calendar_availability import (
    compute_available_slots,
    count_allowed_interview_days,
    is_allowed_interview_day,
    normalize_time_label,
    normalize_weekend_flags,
    parse_date_string,
    slots_for_date,
)
from app.timezone_utils import DEFAULT_TZ_NAME, as_utc, resolve_timezone
from app.settings import get_settings

logger = logging.getLogger(__name__)

COLLECTION = "interviewInvites"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _booking_token() -> str:
    return secrets.token_urlsafe(32)


def _weekend_flags_from_payload(payload: Optional[dict] = None, invite: Optional[dict] = None) -> tuple[bool, bool]:
    source = payload or {}
    fallback = invite or {}
    include_saturday = source.get("include_saturday")
    if include_saturday is None:
        include_saturday = fallback.get("include_saturday", False)
    include_sunday = source.get("include_sunday")
    if include_sunday is None:
        include_sunday = fallback.get("include_sunday", False)
    return normalize_weekend_flags(include_saturday, include_sunday)


def _assert_window_has_bookable_days(
    date_start: datetime,
    date_end: datetime,
    *,
    include_saturday: bool,
    include_sunday: bool,
) -> None:
    if count_allowed_interview_days(
        date_start,
        date_end,
        include_saturday=include_saturday,
        include_sunday=include_sunday,
    ) > 0:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "The selected date range has no bookable days. "
            "Extend the range or include Saturday/Sunday."
        ),
    )


def _extract_email(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, dict):
        return str(value.get("email") or "").strip().lower()
    return str(getattr(value, "email", "") or "").strip().lower()


def _unique_emails(*groups: Any) -> List[str]:
    emails: List[str] = []
    seen = set()
    for group in groups:
        values = group if isinstance(group, (list, tuple, set)) else [group]
        for value in values:
            email = _extract_email(value)
            if email and email not in seen:
                seen.add(email)
                emails.append(email)
    return emails


def _interviewer_emails(invite: dict) -> List[str]:
    return _unique_emails(
        invite.get("hr_calendar_email"),
        invite.get("interviewers") or [],
    )


def _share_emails(invite: dict) -> List[str]:
    return _unique_emails(invite.get("share_emails") or [])


def _normalize_interview_mode(value: Optional[str]) -> str:
    mode = str(value or "remote").strip().lower().replace("-", "_").replace(" ", "_")
    if mode in {"in_person", "inperson", "onsite", "on_site"}:
        return "in_person"
    return "remote"


def _event_description(invite: dict) -> str:
    mode = _normalize_interview_mode(invite.get("interview_mode"))
    lines = [
        f"Interview for {invite.get('position') or 'this role'} — "
        f"{invite.get('interview_level') or 'Technical L2'}"
    ]
    if mode == "in_person":
        address = (invite.get("location_address") or "").strip()
        maps_url = (invite.get("maps_url") or "").strip()
        if address:
            lines.append(f"In-person location: {address}")
        if maps_url:
            lines.append(f"Maps: {maps_url}")
        lines.append("This is an in-person interview. No video meeting link will be created.")
    return "\n".join(lines)


def _attendee_emails(invite: dict) -> List[str]:
    return _unique_emails(
        invite.get("candidate_email"),
        invite.get("hr_calendar_email"),
        invite.get("interviewers") or [],
    )


def _stakeholder_recipients(invite: dict) -> List[str]:
    """HR, interviewers, and tagged management — never the candidate."""
    from app.settings import get_settings

    candidate = _extract_email(invite.get("candidate_email"))
    return [
        email
        for email in _unique_emails(
            get_settings().hr_email_list or [],
            invite.get("hr_calendar_email"),
            invite.get("interviewers") or [],
            invite.get("share_emails") or [],
        )
        if email != candidate
    ]


def _hr_interviewer_recipients(invite: dict) -> List[str]:
    from app.services.l2_questions_pdf import l2_pdf_recipients

    return l2_pdf_recipients(
        candidate_email=invite.get("candidate_email"),
        hr_calendar_email=invite.get("hr_calendar_email"),
        interviewers=invite.get("interviewers") or [],
    )


def _confidential_l2_attachments(invite: dict) -> List[dict]:
    from app.services.l2_questions_pdf import l2_questions_pdf_attachment

    try:
        attachment = l2_questions_pdf_attachment(
            invite.get("candidate_id") or "",
            invite.get("job_id") or "",
            candidate_name=invite.get("candidate_name") or "Candidate",
            job_role=invite.get("position") or "this role",
        )
    except Exception as exc:
        logger.warning("L2 PDF attachment skipped: %s", exc)
        return []
    return [attachment] if attachment else []


def _interview_mail_payload(invite: dict, extra: Optional[dict] = None) -> dict:
    interviewers = invite.get("interviewers") or []
    interviewer_names = [
        (row.get("name") or "").strip()
        for row in interviewers
        if isinstance(row, dict) and (row.get("name") or "").strip()
    ]
    payload = {
        "candidate_id": invite.get("candidate_id"),
        "job_id": invite.get("job_id"),
        "candidate_name": invite.get("candidate_name") or "Candidate",
        "job_role": invite.get("position") or "this role",
        "scheduled_date": invite.get("scheduled_date"),
        "scheduled_time": invite.get("scheduled_time"),
        "timezone": invite.get("timezone"),
        "interview_mode": _normalize_interview_mode(invite.get("interview_mode")),
        "location_address": invite.get("location_address"),
        "maps_url": invite.get("maps_url"),
        "meeting_link": invite.get("meeting_link"),
        "meeting_provider": invite.get("meeting_provider"),
        "calendar_provider": invite.get("calendar_provider"),
        "interview_level": invite.get("interview_level") or "Level 2",
        "duration_minutes": int(invite.get("duration_minutes") or 60),
        "interviewer_name": ", ".join(interviewer_names) if interviewer_names else "HR Team",
    }
    if extra:
        payload.update(extra)
    return payload


def _notify_interview_parties(action: str, invite: dict, extra: Optional[dict] = None) -> None:
    from email_utils import notify_candidate, notify_hr

    extra_fields = {**(extra or {})}
    candidate_email = invite.get("candidate_email")
    candidate = _extract_email(candidate_email)
    interviewer_set = set(
        _unique_emails(
            invite.get("hr_calendar_email"),
            invite.get("interviewers") or [],
        )
    )
    attachments = _confidential_l2_attachments(invite) if action != "interview_cancelled" else []
    hr_payload = _interview_mail_payload(
        invite, {**extra_fields, "l2_pdf_attached": bool(attachments)}
    )
    candidate_payload = _interview_mail_payload(invite, extra_fields)
    if candidate_email:
        try:
            candidate_attachments = None
            if action == "interview_confirmed":
                ics = _ics_attachment_for_invite(invite)
                candidate_attachments = [ics] if ics else None
            notify_candidate(
                action,
                candidate_payload,
                candidate_email,
                attachments=candidate_attachments,
            )
        except Exception as exc:
            logger.warning("Candidate %s email failed: %s", action, exc)

    hr_interviewers = [
        email
        for email in _hr_interviewer_recipients(invite)
        if email != candidate
    ]
    share_only = [
        email
        for email in _share_emails(invite)
        if email != candidate and email not in interviewer_set
    ]
    # Tagged managers get FYI mail only — never L2 questions or the candidate booking link.
    hr_interviewers = [email for email in hr_interviewers if email not in set(share_only)]

    if hr_interviewers:
        try:
            notify_hr(
                action,
                hr_payload,
                recipients=hr_interviewers,
                attachments=attachments or None,
            )
        except Exception as exc:
            logger.warning("HR/interviewer %s email failed: %s", action, exc)

    if share_only:
        fyi_payload = {
            **_interview_mail_payload(invite),
            "is_fyi": True,
            "l2_pdf_attached": False,
        }
        fyi_payload.pop("booking_url", None)
        try:
            notify_hr(action, fyi_payload, recipients=share_only)
        except Exception as exc:
            logger.warning("Tagged management %s email failed: %s", action, exc)

    logger.info(
        "Interview %s emails targeted has_candidate=%s hr_n=%s managers_n=%s has_meet=%s",
        action,
        bool(candidate_email),
        len(hr_interviewers),
        len(share_only),
        bool(invite.get("meeting_link") or invite.get("maps_url")),
    )


def _create_booked_calendar_event(invite: dict, date: str, time_label: str) -> dict:
    from app.services.calendar_gateway import CalendarAccessError, create_interview_event

    tz_name = invite.get("timezone") or DEFAULT_TZ_NAME
    tz = resolve_timezone(tz_name)
    start_dt = datetime.strptime(
        f"{date} {time_label}",
        "%Y-%m-%d %I:%M %p",
    ).replace(tzinfo=tz)
    duration = int(invite.get("duration_minutes") or 60)
    end_dt = start_dt + timedelta(minutes=duration)
    mode = _normalize_interview_mode(invite.get("interview_mode"))
    location = ""
    if mode == "in_person":
        location = (invite.get("location_address") or "").strip()
        if invite.get("maps_url"):
            location = f"{location} ({invite.get('maps_url')})" if location else invite.get("maps_url")

    try:
        event = create_interview_event(
            hr_email=invite.get("hr_calendar_email"),
            summary=(
                f"Interview: {invite.get('interview_level')} - "
                f"{invite.get('candidate_name')} ({invite.get('position')})"
            ),
            start_dt=start_dt,
            end_dt=end_dt,
            timezone=tz_name,
            attendee_emails=_attendee_emails(invite),
            description=_event_description(invite),
            location=location,
            with_meet=(mode != "in_person"),
        )
    except CalendarAccessError:
        raise
    return event


def create_interview_invite(payload: dict, hr_principal_user_id: Optional[str] = None) -> dict:
    db = get_db()
    settings = get_settings()

    from app.services.calendar_gateway import (
        calendar_connect_required_detail,
        get_auth_provider_for_email,
        is_hr_calendar_connected,
    )
    from app.services.hr_calendar_oauth import get_hr_email_for_user_id

    candidate_id = payload.get("candidate_id")
    job_id = payload.get("job_id")
    if not candidate_id or not job_id:
        raise HTTPException(status_code=400, detail="candidate_id and job_id are required")

    hr_calendar_email = (
        payload.get("hr_calendar_email")
        or get_hr_email_for_user_id(hr_principal_user_id or "")
        or (settings.hr_email_list[0] if settings.hr_email_list else None)
    )
    hr_calendar_email = (hr_calendar_email or "").strip().lower()
    if not hr_calendar_email:
        raise HTTPException(status_code=400, detail="HR calendar email could not be resolved.")
    if not is_hr_calendar_connected(hr_calendar_email):
        raise HTTPException(
            status_code=403,
            detail=calendar_connect_required_detail(hr_calendar_email),
        )

    date_start = parse_date_string(payload["date_range_start"])
    date_end = parse_date_string(payload["date_range_end"])
    if date_end < date_start:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")

    include_saturday, include_sunday = _weekend_flags_from_payload(payload)
    _assert_window_has_bookable_days(
        date_start,
        date_end,
        include_saturday=include_saturday,
        include_sunday=include_sunday,
    )

    mode = _normalize_interview_mode(payload.get("interview_mode"))
    location_address = (payload.get("location_address") or "").strip()
    maps_url = (payload.get("maps_url") or "").strip()
    if mode == "in_person" and not location_address:
        raise HTTPException(
            status_code=400,
            detail="Office address is required for an in-person interview.",
        )

    interviewers = payload.get("interviewers") or []
    if not interviewers:
        interviewers = [
            {"name": "Hiring Team", "email": email}
            for email in settings.hr_email_list
        ]

    share_emails = [
        e.strip().lower()
        for e in (payload.get("share_emails") or [])
        if isinstance(e, str) and e.strip()
    ]

    token = _booking_token()
    invite_id = uuid.uuid4().hex[:16]

    duration_minutes = int(payload.get("duration_minutes") or 60)
    slot_increment_minutes = int(
        payload.get("slot_increment_minutes") or duration_minutes or 30
    )

    doc = {
        "invite_id": invite_id,
        "token": token,
        "candidate_id": candidate_id,
        "job_id": job_id,
        "candidate_name": payload.get("candidate_name") or "Candidate",
        "candidate_email": payload.get("candidate_email"),
        "position": payload.get("position"),
        "interview_level": payload.get("interview_level") or "Technical L2",
        "duration_minutes": duration_minutes,
        "timezone": payload.get("timezone") or DEFAULT_TZ_NAME,
        "date_range_start": payload["date_range_start"],
        "date_range_end": payload["date_range_end"],
        "include_saturday": include_saturday,
        "include_sunday": include_sunday,
        "working_hours_start": int(payload.get("working_hours_start") or 9),
        "working_hours_end": int(payload.get("working_hours_end") or 21),
        "slot_increment_minutes": slot_increment_minutes,
        "interviewers": interviewers,
        "share_emails": share_emails,
        "hr_calendar_email": hr_calendar_email,
        "calendar_provider": get_auth_provider_for_email(hr_calendar_email),
        "interview_mode": mode,
        "location_address": location_address,
        "maps_url": maps_url,
        "status": "pending",
        "scheduled_date": None,
        "scheduled_time": None,
        "meeting_link": None,
        "created_at": _utcnow(),
        "expires_at": date_end + timedelta(days=1),
        "booked_at": None,
    }

    if not doc["candidate_email"]:
        info = db["candidateInformation"].find_one({"candidate_id": candidate_id}) or {}
        contact = resolve_candidate_contact(candidate_id, info, job_id)
        doc["candidate_email"] = contact.get("email")
    if not doc["position"]:
        doc["position"] = get_job_role_helper(job_id, candidate_id)

    if not doc["candidate_email"]:
        raise HTTPException(status_code=400, detail="Candidate email is required")

    db[COLLECTION].insert_one(doc)

    frontend_base = _safe_frontend_base(payload.get("frontend_base_url"))
    booking_url = f"{frontend_base}/calendar?token={token}"

    return {
        "invite_id": invite_id,
        "token": token,
        "booking_url": booking_url,
        "status": "pending",
        "candidate_email": doc["candidate_email"],
        "date_range_start": doc["date_range_start"],
        "date_range_end": doc["date_range_end"],
        "include_saturday": doc["include_saturday"],
        "include_sunday": doc["include_sunday"],
    }


def _safe_frontend_base(frontend_base_url: Optional[str] = None) -> str:
    """Only allow booking links on configured frontend / CORS origins."""
    settings = get_settings()
    configured = (settings.frontend_url or "").strip().rstrip("/")
    allowed = {configured.lower()} if configured else set()
    for origin in settings.cors_origin_list:
        cleaned = (origin or "").strip().rstrip("/")
        if cleaned:
            allowed.add(cleaned.lower())
    requested = (frontend_base_url or "").strip().rstrip("/")
    if requested and requested.lower() in allowed:
        return requested
    return configured


def _booking_url_for_token(token: str, frontend_base_url: Optional[str] = None) -> str:
    frontend_base = _safe_frontend_base(frontend_base_url)
    return f"{frontend_base}/calendar?token={token}"


def _find_reschedule_invite(
    db,
    candidate_id: str,
    job_id: str,
    invite_id: Optional[str] = None,
    token: Optional[str] = None,
) -> dict:
    query: Dict[str, Any] = {}
    if invite_id:
        query["invite_id"] = invite_id
    elif token:
        query["token"] = token
    else:
        query = {"candidate_id": candidate_id, "job_id": job_id}

    docs = list(db[COLLECTION].find(query).sort("created_at", -1))
    if not docs:
        raise HTTPException(
            status_code=404,
            detail="No interview invitation found to reschedule. Send an invite first.",
        )

    for preferred in ("scheduled", "pending", "booking_in_progress"):
        for doc in docs:
            if doc.get("status") == preferred:
                _recover_stale_booking_lock(doc)
                return doc
    return docs[0]


def reschedule_interview_invite(
    payload: dict, hr_principal_user_id: Optional[str] = None
) -> dict:
    """Reset an existing invite so HR can pick a new window and the candidate books again."""
    db = get_db()
    settings = get_settings()

    from app.services.calendar_gateway import (
        calendar_connect_required_detail,
        cancel_interview_event,
        is_hr_calendar_connected,
    )
    from app.services.hr_calendar_oauth import get_hr_email_for_user_id

    candidate_id = payload.get("candidate_id")
    job_id = payload.get("job_id")
    if not candidate_id or not job_id:
        raise HTTPException(status_code=400, detail="candidate_id and job_id are required")

    invite = _find_reschedule_invite(
        db,
        candidate_id,
        job_id,
        invite_id=payload.get("invite_id"),
        token=payload.get("token"),
    )

    hr_calendar_email = (
        payload.get("hr_calendar_email")
        or invite.get("hr_calendar_email")
        or get_hr_email_for_user_id(hr_principal_user_id or "")
        or (settings.hr_email_list[0] if settings.hr_email_list else None)
    )
    hr_calendar_email = (hr_calendar_email or "").strip().lower()
    if not hr_calendar_email:
        raise HTTPException(status_code=400, detail="HR calendar email could not be resolved.")
    if not is_hr_calendar_connected(hr_calendar_email):
        raise HTTPException(
            status_code=403,
            detail=calendar_connect_required_detail(hr_calendar_email),
        )

    date_start = parse_date_string(payload["date_range_start"])
    date_end = parse_date_string(payload["date_range_end"])
    if date_end < date_start:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")

    include_saturday, include_sunday = _weekend_flags_from_payload(payload, invite)
    _assert_window_has_bookable_days(
        date_start,
        date_end,
        include_saturday=include_saturday,
        include_sunday=include_sunday,
    )

    previous_date = invite.get("scheduled_date")
    previous_time = invite.get("scheduled_time")
    previous_link = invite.get("meeting_link")

    if invite.get("google_event_id"):
        cancel_interview_event(
            invite.get("hr_calendar_email") or hr_calendar_email,
            invite["google_event_id"],
            calendar_provider=invite.get("calendar_provider"),
        )

    interviewers = payload.get("interviewers") or invite.get("interviewers") or []
    if not interviewers:
        interviewers = [
            {"name": "Hiring Team", "email": email}
            for email in settings.hr_email_list
        ]

    share_emails = [
        e.strip().lower()
        for e in (payload.get("share_emails") or invite.get("share_emails") or [])
        if isinstance(e, str) and e.strip()
    ]

    new_token = _booking_token()
    updates = {
        "token": new_token,
        "candidate_name": payload.get("candidate_name") or invite.get("candidate_name") or "Candidate",
        "candidate_email": payload.get("candidate_email") or invite.get("candidate_email"),
        "position": payload.get("position") or invite.get("position"),
        "interview_level": payload.get("interview_level") or invite.get("interview_level") or "Technical L2",
        "duration_minutes": int(payload.get("duration_minutes") or invite.get("duration_minutes") or 60),
        "timezone": payload.get("timezone") or invite.get("timezone") or DEFAULT_TZ_NAME,
        "date_range_start": payload["date_range_start"],
        "date_range_end": payload["date_range_end"],
        "include_saturday": include_saturday,
        "include_sunday": include_sunday,
        "working_hours_start": int(
            payload.get("working_hours_start") or invite.get("working_hours_start") or 9
        ),
        "working_hours_end": int(
            payload.get("working_hours_end") or invite.get("working_hours_end") or 21
        ),
        "slot_increment_minutes": int(
            payload.get("slot_increment_minutes") or invite.get("slot_increment_minutes") or 30
        ),
        "interviewers": interviewers,
        "share_emails": share_emails,
        "hr_calendar_email": hr_calendar_email,
        "interview_mode": _normalize_interview_mode(
            payload.get("interview_mode") or invite.get("interview_mode")
        ),
        "location_address": (
            payload.get("location_address")
            if payload.get("location_address") is not None
            else invite.get("location_address") or ""
        ),
        "maps_url": (
            payload.get("maps_url")
            if payload.get("maps_url") is not None
            else invite.get("maps_url") or ""
        ),
        "status": "pending",
        "scheduled_date": None,
        "scheduled_time": None,
        "meeting_link": None,
        "google_event_id": None,
        "booked_at": None,
        "expires_at": date_end + timedelta(days=1),
        "rescheduled_at": _utcnow(),
        "previous_scheduled_date": previous_date,
        "previous_scheduled_time": previous_time,
        "previous_meeting_link": previous_link,
    }
    if not updates["position"]:
        updates["position"] = get_job_role_helper(job_id, candidate_id)
    if not updates["candidate_email"]:
        info = db["candidateInformation"].find_one({"candidate_id": candidate_id}) or {}
        contact = resolve_candidate_contact(candidate_id, info, job_id)
        updates["candidate_email"] = contact.get("email")
    if not updates["candidate_email"]:
        raise HTTPException(status_code=400, detail="Candidate email is required")

    db[COLLECTION].update_one({"_id": invite["_id"]}, {"$set": updates, "$unset": {"booking_started_at": ""}})
    invite.update(updates)

    booking_url = _booking_url_for_token(new_token, payload.get("frontend_base_url"))
    slots_payload: dict = {}
    try:
        slots_payload = get_available_slots_for_invite(new_token)
    except HTTPException as exc:
        logger.warning("Could not prefetch reschedule slots: %s", exc.detail)
    except Exception:
        logger.exception("Could not prefetch reschedule slots")

    company_name = _resolve_company_name_for_job(job_id)

    try:
        _notify_interview_parties(
            "interview_reschedule",
            invite,
            extra={
                "booking_url": booking_url,
                "date_range_start": updates["date_range_start"],
                "date_range_end": updates["date_range_end"],
                "previous_scheduled_date": previous_date,
                "previous_scheduled_time": previous_time,
                "company_name": company_name or "Company",
            },
        )
    except Exception as exc:
        logger.warning("Interview reschedule emails failed: %s", exc)

    return {
        "invite_id": invite.get("invite_id"),
        "token": new_token,
        "booking_url": booking_url,
        "status": "pending",
        "rescheduled": True,
        "candidate_email": updates["candidate_email"],
        "date_range_start": updates["date_range_start"],
        "date_range_end": updates["date_range_end"],
        "previous_scheduled_date": previous_date,
        "previous_scheduled_time": previous_time,
        "slots_by_date": slots_payload.get("slots_by_date") or {},
    }


def _recover_stale_booking_lock(invite: dict) -> None:
    """Revert invites stuck in booking_in_progress after a failed/crashed booking."""
    if invite.get("status") != "booking_in_progress":
        return
    started = invite.get("booking_started_at")
    if not started:
        return
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    started = as_utc(started)
    if as_utc(_utcnow()) - started > timedelta(minutes=10):
        get_db()[COLLECTION].update_one(
            {"_id": invite["_id"], "status": "booking_in_progress"},
            {"$set": {"status": "pending"}, "$unset": {"booking_started_at": ""}},
        )
        invite["status"] = "pending"


def get_invite_by_token(token: str) -> dict:
    db = get_db()
    invite = db[COLLECTION].find_one({"token": token})
    if not invite:
        raise HTTPException(status_code=404, detail="Interview scheduling link not found")
    _recover_stale_booking_lock(invite)
    if invite.get("status") == "pending" and _invite_expired(invite):
        raise HTTPException(
            status_code=400,
            detail="This scheduling link has expired. Please contact the recruiter for a new link.",
        )
    return invite


def _mask_email(email: str) -> str:
    raw = (email or "").strip()
    if "@" not in raw:
        return "the email this invite was sent to"
    local, domain = raw.split("@", 1)
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def _session_candidate_email(principal) -> str:
    from bson import ObjectId

    cid = str(
        getattr(principal, "candidate_id", None) or getattr(principal, "subject", None) or ""
    )
    db = get_db()
    if not cid:
        return ""
    try:
        user = db["candidates"].find_one({"_id": ObjectId(cid)})
    except Exception:
        user = db["candidates"].find_one({"_id": cid})
    if user and user.get("email"):
        return str(user.get("email")).strip().lower()
    info = db["candidateInformation"].find_one({"candidate_id": cid}) or {}
    contact = resolve_candidate_contact(cid, info, getattr(principal, "job_id", None))
    return str(contact.get("email") or "").strip().lower()


def assert_invite_owned_by_candidate(invite: dict, principal) -> None:
    """Booking links are for the invited candidate only — never an HR session."""
    from app.auth import Role

    if principal.role == Role.HR:
        raise HTTPException(
            status_code=403,
            detail=(
                "This scheduling link is for the candidate. Sign out of the recruiter "
                "account and sign in with the same email this interview invite was sent to."
            ),
        )
    if principal.role != Role.CANDIDATE:
        raise HTTPException(
            status_code=403,
            detail="Sign in with the candidate account that received this interview invite.",
        )

    invite_email = str(invite.get("candidate_email") or "").strip().lower()
    session_email = _session_candidate_email(principal)
    invite_cid = str(invite.get("candidate_id") or "").strip()
    session_cid = str(
        getattr(principal, "candidate_id", None) or getattr(principal, "subject", None) or ""
    ).strip()

    if invite_email and session_email and invite_email == session_email:
        return
    if invite_cid and session_cid and invite_cid == session_cid:
        return

    hint = f" ({_mask_email(invite_email)})" if invite_email else ""
    raise HTTPException(
        status_code=403,
        detail=(
            "This scheduling link belongs to another candidate. "
            f"Please sign in with the same email used for the application{hint}."
        ),
    )


def _invite_expired(invite: dict) -> bool:
    expires_at = invite.get("expires_at")
    if not expires_at:
        return False
    now = as_utc(_utcnow())
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now > as_utc(expires_at)


def _serialize_invite_public(invite: dict) -> dict:
    interviewers = [
        {"name": (row.get("name") or "").strip()}
        for row in (invite.get("interviewers") or [])
        if isinstance(row, dict) and (row.get("name") or "").strip()
    ]
    return {
        "invite_id": invite.get("invite_id"),
        "status": invite.get("status"),
        "candidate_name": invite.get("candidate_name"),
        "position": invite.get("position"),
        "interview_level": invite.get("interview_level"),
        "duration_minutes": invite.get("duration_minutes") or 60,
        "timezone": invite.get("timezone") or DEFAULT_TZ_NAME,
        "date_range_start": invite.get("date_range_start"),
        "date_range_end": invite.get("date_range_end"),
        "include_saturday": bool(invite.get("include_saturday", False)),
        "include_sunday": bool(invite.get("include_sunday", False)),
        "working_hours_start": invite.get("working_hours_start"),
        "working_hours_end": invite.get("working_hours_end"),
        "scheduled_date": invite.get("scheduled_date"),
        "scheduled_time": invite.get("scheduled_time"),
        "meeting_link": invite.get("meeting_link"),
        "meeting_provider": invite.get("meeting_provider"),
        "calendar_provider": invite.get("calendar_provider"),
        "interview_mode": _normalize_interview_mode(invite.get("interview_mode")),
        "location_address": invite.get("location_address") or "",
        "maps_url": invite.get("maps_url") or "",
        "interviewers": interviewers,
        "expires_at": _iso(invite.get("expires_at")),
    }


def _iso(value: Any) -> Optional[str]:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _serialize_invite_hr(invite: dict) -> dict:
    return {
        "invite_id": invite.get("invite_id"),
        "candidate_id": invite.get("candidate_id"),
        "job_id": invite.get("job_id"),
        "candidate_name": invite.get("candidate_name"),
        "candidate_email": invite.get("candidate_email"),
        "position": invite.get("position"),
        "interview_level": invite.get("interview_level"),
        "duration_minutes": invite.get("duration_minutes") or 60,
        "timezone": invite.get("timezone"),
        "status": invite.get("status"),
        "scheduled_date": invite.get("scheduled_date"),
        "scheduled_time": invite.get("scheduled_time"),
        "meeting_link": invite.get("meeting_link"),
        "date_range_start": invite.get("date_range_start"),
        "date_range_end": invite.get("date_range_end"),
        "include_saturday": bool(invite.get("include_saturday", False)),
        "include_sunday": bool(invite.get("include_sunday", False)),
        "interviewers": invite.get("interviewers") or [],
        "interview_mode": _normalize_interview_mode(invite.get("interview_mode")),
        "location_address": invite.get("location_address") or "",
        "maps_url": invite.get("maps_url") or "",
        "rescheduled_at": _iso(invite.get("rescheduled_at")),
        "previous_scheduled_date": invite.get("previous_scheduled_date"),
        "previous_scheduled_time": invite.get("previous_scheduled_time"),
        "created_at": _iso(invite.get("created_at")),
    }


def list_invites_for_job(job_id: str) -> List[dict]:
    db = get_db()
    job_ids: List[Any] = [job_id]
    try:
        as_int = int(job_id)
        if as_int != job_id:
            job_ids.append(as_int)
    except (TypeError, ValueError):
        pass
    docs = list(
        db[COLLECTION].find({"job_id": {"$in": job_ids}}).sort("created_at", -1)
    )
    return [_serialize_invite_hr(doc) for doc in docs]


def get_available_slots_for_invite(token: str, date: Optional[str] = None) -> dict:
    invite = get_invite_by_token(token)
    if invite.get("status") == "scheduled":
        raise HTTPException(status_code=400, detail="This interview has already been scheduled")
    if invite.get("status") == "booking_in_progress":
        raise HTTPException(
            status_code=400,
            detail="This interview is being finalized. Please wait a moment and refresh.",
        )

    emails = _interviewer_emails(invite)
    if not emails:
        raise HTTPException(status_code=400, detail="No interviewer calendars configured")

    start = parse_date_string(invite["date_range_start"])
    end = parse_date_string(invite["date_range_end"])
    include_saturday, include_sunday = _weekend_flags_from_payload(invite=invite)

    delegate = invite.get("hr_calendar_email")
    hr_email = delegate

    try:
        if date:
            target = parse_date_string(date)
            if target < start or target > end:
                raise HTTPException(status_code=400, detail="Date is outside the allowed scheduling window")
            if not is_allowed_interview_day(
                target,
                include_saturday=include_saturday,
                include_sunday=include_sunday,
            ):
                raise HTTPException(
                    status_code=400,
                    detail="This day is excluded from the scheduling window (weekend).",
                )
            slots = slots_for_date(
                emails,
                target,
                duration_minutes=int(invite.get("duration_minutes") or 60),
                working_hours_start=int(invite.get("working_hours_start") or 9),
                working_hours_end=int(invite.get("working_hours_end") or 21),
                slot_increment_minutes=int(
                    invite.get("slot_increment_minutes")
                    or invite.get("duration_minutes")
                    or 30
                ),
                hr_email=hr_email,
                timezone_name=invite.get("timezone"),
                include_saturday=include_saturday,
                include_sunday=include_sunday,
            )
            return {"date": date, "slots": slots, "duration_minutes": int(invite.get("duration_minutes") or 60)}

        grouped = compute_available_slots(
            emails,
            start,
            end,
            duration_minutes=int(invite.get("duration_minutes") or 60),
            working_hours_start=int(invite.get("working_hours_start") or 9),
            working_hours_end=int(invite.get("working_hours_end") or 21),
            slot_increment_minutes=int(
                invite.get("slot_increment_minutes")
                or invite.get("duration_minutes")
                or 30
            ),
            hr_email=hr_email,
            timezone_name=invite.get("timezone"),
            include_saturday=include_saturday,
            include_sunday=include_sunday,
        )
        return {
            "slots_by_date": grouped,
            "duration_minutes": int(invite.get("duration_minutes") or 60),
            "include_saturday": include_saturday,
            "include_sunday": include_sunday,
        }
    except Exception as exc:
        from app.services.calendar_gateway import CalendarAccessError
        from app.services.hr_calendar_oauth import CalendarNotConfiguredError

        if isinstance(exc, (CalendarNotConfiguredError, CalendarAccessError)):
            logger.warning("Candidate booking availability failed: %s", exc)
            detail = str(exc) or "Interview availability is temporarily unavailable."
            # Surface actionable OAuth config problems instead of a vague 503.
            lower = detail.lower()
            if "invalid_client" in lower or "client secret is invalid" in lower:
                detail = (
                    "Google Calendar could not refresh access (invalid client secret). "
                    "Update GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in the API .env to "
                    "match the Google Cloud OAuth client, reconnect Calendar in Settings, "
                    "then try again."
                )
            elif "unauthorized_client" in lower:
                detail = (
                    "Google Calendar refresh failed (unauthorized_client): the saved "
                    "refresh token does not belong to the current GOOGLE_CLIENT_ID. "
                    "Confirm .env client ID/secret, then reconnect Calendar in Settings."
                )
            elif "not connected" in lower or isinstance(exc, CalendarNotConfiguredError):
                detail = (
                    "Google Calendar is not connected for the scheduling account. "
                    "Reconnect Calendar in recruiter Settings, then try again."
                )
            else:
                detail = (
                    "Interview availability is temporarily unavailable. Please try again shortly."
                )
            raise HTTPException(status_code=503, detail=detail) from exc
        if isinstance(exc, HTTPException):
            raise
        logger.exception("Failed to compute availability")
        raise HTTPException(status_code=500, detail="Could not load calendar availability") from exc


def confirm_interview_booking(token: str, date: str, time: str) -> dict:
    db = get_db()
    invite = get_invite_by_token(token)

    if invite.get("status") == "scheduled":
        raise HTTPException(status_code=400, detail="This interview has already been scheduled")
    if invite.get("status") == "booking_in_progress":
        raise HTTPException(
            status_code=400,
            detail="This interview is being finalized. Please wait a moment and try again.",
        )

    start = parse_date_string(invite["date_range_start"])
    end = parse_date_string(invite["date_range_end"])
    target = parse_date_string(date)
    if target < start or target > end:
        raise HTTPException(status_code=400, detail="Selected date is outside the allowed window")

    include_saturday, include_sunday = _weekend_flags_from_payload(invite=invite)
    if not is_allowed_interview_day(
        target,
        include_saturday=include_saturday,
        include_sunday=include_sunday,
    ):
        raise HTTPException(
            status_code=400,
            detail="Selected date is a weekend day excluded from this scheduling window.",
        )

    normalized_time = normalize_time_label(time)
    emails = _interviewer_emails(invite)
    hr_email = invite.get("hr_calendar_email")
    available = slots_for_date(
        emails,
        target,
        duration_minutes=int(invite.get("duration_minutes") or 60),
        working_hours_start=int(invite.get("working_hours_start") or 9),
        working_hours_end=int(invite.get("working_hours_end") or 21),
        slot_increment_minutes=int(
            invite.get("slot_increment_minutes")
            or invite.get("duration_minutes")
            or 30
        ),
        hr_email=hr_email,
        timezone_name=invite.get("timezone"),
        include_saturday=include_saturday,
        include_sunday=include_sunday,
    )

    available_normalized = {normalize_time_label(s) for s in available}
    if normalized_time not in available_normalized:
        raise HTTPException(
            status_code=400,
            detail="Selected time is no longer available. Please choose another slot.",
        )

    from app.services.google_calendar import CalendarAccessError

    # Lock invite before creating calendar event (prevents double-booking same link).
    lock_result = db[COLLECTION].update_one(
        {"token": token, "status": "pending"},
        {"$set": {"status": "booking_in_progress", "booking_started_at": _utcnow()}},
    )
    if lock_result.modified_count == 0:
        raise HTTPException(
            status_code=400,
            detail="This interview has already been scheduled or is being processed.",
        )

    try:
        event = _create_booked_calendar_event(invite, date, normalized_time)
    except CalendarAccessError as exc:
        db[COLLECTION].update_one(
            {"token": token, "status": "booking_in_progress"},
            {"$set": {"status": "pending"}, "$unset": {"booking_started_at": ""}},
        )
        logger.warning("Calendar event creation failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Could not create the interview calendar event. Please try another slot.",
        ) from exc
    except Exception as exc:
        db[COLLECTION].update_one(
            {"token": token, "status": "booking_in_progress"},
            {"$set": {"status": "pending"}, "$unset": {"booking_started_at": ""}},
        )
        logger.exception("Calendar event creation failed")
        raise HTTPException(
            status_code=500,
            detail="Could not create the calendar event. Please try another slot.",
        ) from exc

    meeting_link = event.get("meeting_link")
    if not meeting_link and _normalize_interview_mode(invite.get("interview_mode")) == "in_person":
        meeting_link = invite.get("maps_url") or None
    google_event_id = event.get("event_id")
    booked_at = _utcnow()

    finalize = db[COLLECTION].update_one(
        {"token": token, "status": "booking_in_progress"},
        {
            "$set": {
                "status": "scheduled",
                "scheduled_date": date,
                "scheduled_time": normalized_time,
                "meeting_link": meeting_link,
                "google_event_id": google_event_id,
                "calendar_provider": event.get("calendar_provider") or invite.get("calendar_provider"),
                "meeting_provider": event.get("meeting_provider"),
                "booked_at": booked_at,
            },
            "$unset": {"booking_started_at": ""},
        },
    )
    if finalize.modified_count == 0:
        logger.error(
            "Calendar event created but invite finalize failed for token=%s event=%s",
            token,
            google_event_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Interview was booked on calendar but could not be finalized. Contact support.",
        )

    invite.update(
        {
            "status": "scheduled",
            "scheduled_date": date,
            "scheduled_time": normalized_time,
            "meeting_link": meeting_link,
            "google_event_id": google_event_id,
            "calendar_provider": event.get("calendar_provider") or invite.get("calendar_provider"),
            "meeting_provider": event.get("meeting_provider"),
            "booked_at": booked_at,
        }
    )

    _send_booking_emails(invite)

    return {
        **_serialize_invite_public(invite),
        "status": "scheduled",
        "scheduled_date": date,
        "scheduled_time": normalized_time,
        "meeting_link": meeting_link,
    }


def _send_booking_emails(invite: dict) -> None:
    """Candidate + HR/interviewers/management emails after a slot is booked."""
    try:
        _notify_interview_parties("interview_confirmed", invite)

        try:
            from app.helpers import get_candidate_profile_picture
            from app.services.notifications import create_notification

            candidate_picture = get_candidate_profile_picture(invite.get("candidate_id"))
            mode = _normalize_interview_mode(invite.get("interview_mode"))
            create_notification(
                audience="candidate",
                notification_type="interview_scheduled",
                message=(
                    f"Your interview for {invite.get('position')} is confirmed on "
                    f"{invite.get('scheduled_date')} at {invite.get('scheduled_time')}. "
                    f"{'Venue details are in your email.' if mode == 'in_person' else 'Check your email for the calendar invite.'}"
                ),
                candidate_id=invite.get("candidate_id"),
                candidate_email=invite.get("candidate_email"),
                candidate_picture=candidate_picture,
                job_id=invite.get("job_id"),
                job_role=invite.get("position"),
            )
        except Exception as notify_exc:
            logger.warning("Booking notification failed: %s", notify_exc)

    except Exception as exc:
        logger.exception("Failed to send booking emails: %s", exc)


def _resolve_company_name_for_job(job_id) -> str:
    try:
        db = get_db()
        job_doc = {}
        for jid in (job_id,):
            job_doc = db["jobview"].find_one({"jobid": jid}) or {}
            if job_doc:
                break
        if not job_doc:
            try:
                job_doc = db["jobview"].find_one({"jobid": int(job_id)}) or {}
            except (TypeError, ValueError):
                job_doc = {}
        return (
            job_doc.get("companyName")
            or job_doc.get("company")
            or job_doc.get("company_name")
            or ""
        )
    except Exception:
        return ""


def _ics_attachment_for_invite(invite: dict) -> Optional[dict]:
    from app.helpers import generate_ics_content

    if not invite.get("scheduled_date") or not invite.get("scheduled_time"):
        return None
    mode = _normalize_interview_mode(invite.get("interview_mode"))
    meeting_link = invite.get("meeting_link") or ""
    location = meeting_link
    if mode == "in_person":
        location = invite.get("location_address") or invite.get("maps_url") or meeting_link or "In-person"
    ics = generate_ics_content(
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
    if not ics:
        return None
    return {
        "filename": "interview.ics",
        "content": ics,
        "mime_type": "text/calendar",
    }


def _send_hr_direct_reschedule_emails(invite: dict, *, previous_date=None, previous_time=None) -> None:
    """
    HR picked a new date/time from the website.
    Candidate gets the reschedule-update card; HR gets the HR reschedule card.
    """
    from email_utils import notify_candidate, notify_hr

    company_name = _resolve_company_name_for_job(invite.get("job_id"))
    extra = {
        "reschedule_complete": True,
        "previous_scheduled_date": previous_date,
        "previous_scheduled_time": previous_time,
        "company_name": company_name or "Company",
    }
    base = _interview_mail_payload(invite, extra)
    candidate_email = invite.get("candidate_email")
    candidate = _extract_email(candidate_email)
    ics_attachment = _ics_attachment_for_invite(invite)

    if candidate_email:
        try:
            notify_candidate(
                "interview_reschedule",
                base,
                candidate_email,
                attachments=[ics_attachment] if ics_attachment else None,
            )
        except Exception as exc:
            logger.warning("Candidate reschedule-update email failed: %s", exc)

    interviewer_set = set(
        _unique_emails(
            invite.get("hr_calendar_email"),
            invite.get("interviewers") or [],
        )
    )
    hr_interviewers = [
        email
        for email in _hr_interviewer_recipients(invite)
        if email != candidate
    ]
    share_only = [
        email
        for email in _share_emails(invite)
        if email != candidate and email not in interviewer_set
    ]
    hr_interviewers = [email for email in hr_interviewers if email not in set(share_only)]

    if hr_interviewers:
        try:
            notify_hr("interview_reschedule", base, recipients=hr_interviewers)
        except Exception as exc:
            logger.warning("HR reschedule-update email failed: %s", exc)

    if share_only:
        try:
            notify_hr(
                "interview_reschedule",
                {**base, "is_fyi": True},
                recipients=share_only,
            )
        except Exception as exc:
            logger.warning("FYI reschedule-update email failed: %s", exc)

    try:
        from app.helpers import get_candidate_profile_picture
        from app.services.notifications import create_notification

        create_notification(
            audience="candidate",
            notification_type="interview_rescheduled",
            message=(
                f"Your interview for {invite.get('position') or 'this role'} was rescheduled to "
                f"{invite.get('scheduled_date')} at {invite.get('scheduled_time')}."
            ),
            candidate_id=invite.get("candidate_id"),
            candidate_email=invite.get("candidate_email"),
            candidate_picture=get_candidate_profile_picture(invite.get("candidate_id")),
            job_id=invite.get("job_id"),
            job_role=invite.get("position"),
        )
    except Exception as notify_exc:
        logger.warning("Reschedule in-app notification failed: %s", notify_exc)


def send_share_invitations(token: str, extra_emails: Optional[List[str]] = None) -> dict:
    """
    Notify share/tag emails about a scheduled interview (no availability check).
  Only works after the interview is booked.
    """
    invite = get_invite_by_token(token)
    if invite.get("status") != "scheduled":
        raise HTTPException(
            status_code=400,
            detail="Interview must be scheduled before sharing invitations",
        )

    from email_utils import email_service

    emails = list(invite.get("share_emails") or [])
    if extra_emails:
        emails.extend([e.strip().lower() for e in extra_emails if e and e.strip()])
    emails = list(dict.fromkeys(emails))

    if not emails:
        return {"sent": 0, "message": "No share emails configured"}

    subject = (
        f"Interview Scheduled (FYI) - {invite.get('candidate_name')} - "
        f"{invite.get('position')}"
    )
    body_html = (
        f"<p>This is for your information. An interview has been scheduled.</p>"
        f"<p><strong>Candidate:</strong> {invite.get('candidate_name')}<br>"
        f"<strong>Position:</strong> {invite.get('position')}<br>"
        f"<strong>Date:</strong> {invite.get('scheduled_date')}<br>"
        f"<strong>Time:</strong> {invite.get('scheduled_time')} "
        f"({invite.get('timezone')})<br>"
        f"<strong>Meeting link:</strong> "
        f"<a href=\"{invite.get('meeting_link')}\">{invite.get('meeting_link')}</a></p>"
        f"<p>You are not required to attend. If you would like to join, use the meeting link above.</p>"
        f"<p><em>No action is required unless you choose to join.</em></p>"
    )
    body_text = (
        f"Interview scheduled for {invite.get('candidate_name')} "
        f"({invite.get('position')}) on {invite.get('scheduled_date')} at "
        f"{invite.get('scheduled_time')}.\nMeeting: {invite.get('meeting_link')}"
    )

    email_service.send_email(
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        recipients=emails,
    )
    return {"sent": len(emails), "recipients": emails}


def hr_book_interview_slot(payload: dict, hr_principal_user_id: Optional[str] = None) -> dict:
    """HR picks an exact date/time (reschedule). Does not require Google FreeBusy."""
    from app.services.calendar_gateway import (
        CalendarAccessError,
        calendar_connect_required_detail,
        cancel_interview_event,
        is_hr_calendar_connected,
    )
    from app.services.hr_calendar_oauth import get_hr_email_for_user_id

    db = get_db()
    settings = get_settings()
    candidate_id = payload.get("candidate_id")
    job_id = payload.get("job_id")
    if not candidate_id or not job_id:
        raise HTTPException(status_code=400, detail="candidate_id and job_id are required")

    invite = _find_reschedule_invite(
        db,
        candidate_id,
        job_id,
        invite_id=payload.get("invite_id"),
        token=payload.get("token"),
    )
    if invite.get("status") in {"cancelled", "completed"}:
        raise HTTPException(status_code=400, detail="This interview can no longer be rescheduled.")

    date = payload.get("scheduled_date")
    time_raw = payload.get("scheduled_time")
    if not date or not time_raw:
        raise HTTPException(status_code=400, detail="Date and time are required")
    try:
        parse_date_string(date)
        time_label = normalize_time_label(time_raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    hr_calendar_email = (
        payload.get("hr_calendar_email")
        or invite.get("hr_calendar_email")
        or get_hr_email_for_user_id(hr_principal_user_id or "")
        or (settings.hr_email_list[0] if settings.hr_email_list else None)
    )
    hr_calendar_email = (hr_calendar_email or "").strip().lower()
    if not hr_calendar_email:
        raise HTTPException(status_code=400, detail="HR calendar email could not be resolved.")
    if not is_hr_calendar_connected(hr_calendar_email):
        raise HTTPException(
            status_code=403,
            detail=calendar_connect_required_detail(hr_calendar_email),
        )

    mode = _normalize_interview_mode(
        payload.get("interview_mode") or invite.get("interview_mode")
    )
    location_address = (
        payload.get("location_address")
        if payload.get("location_address") is not None
        else invite.get("location_address") or ""
    )
    maps_url = (
        payload.get("maps_url")
        if payload.get("maps_url") is not None
        else invite.get("maps_url") or ""
    )
    if mode == "in_person" and not str(location_address).strip():
        raise HTTPException(
            status_code=400,
            detail="Office address is required for an in-person interview.",
        )

    previous_date = invite.get("scheduled_date")
    previous_time = invite.get("scheduled_time")
    if invite.get("google_event_id"):
        cancel_interview_event(
            invite.get("hr_calendar_email") or hr_calendar_email,
            invite["google_event_id"],
            calendar_provider=invite.get("calendar_provider"),
        )

    interviewers = payload.get("interviewers") or invite.get("interviewers") or []
    invite.update(
        {
            "hr_calendar_email": hr_calendar_email,
            "interview_mode": mode,
            "location_address": str(location_address).strip(),
            "maps_url": str(maps_url).strip(),
            "interviewers": interviewers,
            "candidate_name": payload.get("candidate_name") or invite.get("candidate_name"),
            "candidate_email": payload.get("candidate_email") or invite.get("candidate_email"),
            "position": payload.get("position") or invite.get("position"),
            "interview_level": payload.get("interview_level") or invite.get("interview_level"),
            "duration_minutes": int(
                payload.get("duration_minutes") or invite.get("duration_minutes") or 60
            ),
            "timezone": payload.get("timezone") or invite.get("timezone") or DEFAULT_TZ_NAME,
        }
    )

    try:
        event = _create_booked_calendar_event(invite, date, time_label)
    except CalendarAccessError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    meeting_link = event.get("meeting_link")
    if mode == "in_person":
        meeting_link = maps_url or meeting_link
    booked_at = _utcnow()
    updates = {
        "status": "scheduled",
        "scheduled_date": date,
        "scheduled_time": time_label,
        "meeting_link": meeting_link,
        "google_event_id": event.get("event_id"),
        "calendar_provider": event.get("calendar_provider") or invite.get("calendar_provider"),
        "meeting_provider": event.get("meeting_provider"),
        "booked_at": booked_at,
        "rescheduled_at": booked_at,
        "previous_scheduled_date": previous_date,
        "previous_scheduled_time": previous_time,
        "hr_calendar_email": hr_calendar_email,
        "interview_mode": mode,
        "location_address": str(location_address).strip(),
        "maps_url": str(maps_url).strip(),
        "date_range_start": date,
        "date_range_end": date,
    }
    db[COLLECTION].update_one({"invite_id": invite["invite_id"]}, {"$set": updates})
    invite.update(updates)
    _send_hr_direct_reschedule_emails(
        invite,
        previous_date=previous_date,
        previous_time=previous_time,
    )
    return {
        "invite_id": invite.get("invite_id"),
        "status": "scheduled",
        "scheduled_date": date,
        "scheduled_time": time_label,
        "meeting_link": meeting_link,
        "interview_mode": mode,
        "location_address": str(location_address).strip(),
        "maps_url": str(maps_url).strip(),
        "candidate_email": invite.get("candidate_email"),
        "previous_scheduled_date": previous_date,
        "previous_scheduled_time": previous_time,
        "direct_booked": True,
    }


def _normalize_outcome_status(status: str) -> str:
    wanted = str(status or "").strip().lower()
    if wanted == "canceled":
        return "cancelled"
    return wanted


def set_interview_outcome(invite_id: str, status: str, job_id: Optional[str] = None) -> dict:
    """Mark a booked interview completed or cancelled."""
    from app.services.calendar_gateway import cancel_interview_event

    wanted = _normalize_outcome_status(status)
    if wanted not in {"completed", "cancelled"}:
        raise HTTPException(status_code=400, detail="Status must be completed or cancelled")

    db = get_db()
    query: Dict[str, Any] = {"invite_id": invite_id}
    if job_id:
        job_ids: List[Any] = [job_id]
        try:
            as_int = int(job_id)
            if as_int != job_id:
                job_ids.append(as_int)
        except (TypeError, ValueError):
            pass
        query["job_id"] = {"$in": job_ids}

    invite = db[COLLECTION].find_one(query) or db[COLLECTION].find_one({"invite_id": invite_id})
    if not invite:
        raise HTTPException(status_code=404, detail="Interview not found")
    current = _normalize_outcome_status(invite.get("status") or "")
    if current == wanted:
        return _serialize_invite_hr(invite)
    if current in {"completed", "cancelled"}:
        raise HTTPException(status_code=400, detail=f"Interview is already {current}")

    updates = {"status": wanted, "outcome_at": _utcnow()}
    if wanted == "cancelled" and invite.get("google_event_id"):
        try:
            cancel_interview_event(
                invite.get("hr_calendar_email"),
                invite["google_event_id"],
                calendar_provider=invite.get("calendar_provider"),
            )
        except Exception as exc:
            logger.warning("Could not cancel calendar event: %s", exc)
        updates["meeting_link"] = None
        updates["google_event_id"] = None

    db[COLLECTION].update_one({"_id": invite["_id"]}, {"$set": updates})
    invite.update(updates)

    if wanted == "cancelled":
        try:
            from email_utils import notify_candidate, notify_hr

            notify_candidate(
                "interview_cancelled",
                {
                    "candidate_id": invite.get("candidate_id"),
                    "job_id": invite.get("job_id"),
                    "candidate_name": invite.get("candidate_name") or "Candidate",
                    "job_role": invite.get("position") or "this role",
                    "scheduled_date": invite.get("scheduled_date"),
                    "scheduled_time": invite.get("scheduled_time"),
                },
                invite.get("candidate_email"),
            )
            notify_hr(
                "interview_cancelled",
                {
                    "candidate_id": invite.get("candidate_id"),
                    "job_id": invite.get("job_id"),
                    "candidate_name": invite.get("candidate_name") or "Candidate",
                    "job_role": invite.get("position") or "this role",
                    "scheduled_date": invite.get("scheduled_date"),
                    "scheduled_time": invite.get("scheduled_time"),
                },
                recipients=_stakeholder_recipients(invite) or None,
            )
        except Exception as exc:
            logger.warning("Interview cancelled emails failed: %s", exc)

    return _serialize_invite_hr(invite)
