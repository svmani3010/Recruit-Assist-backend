"""Provider-aware calendar operations for HR interview scheduling."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.calendar_identity import (
    PROVIDER_MICROSOFT,
    meeting_provider_for,
    provider_label,
    resolve_auth_provider,
)
from app.services.google_calendar import CalendarAccessError
from app.services.hr_calendar_oauth import (
    CalendarNotConfiguredError,
    get_hr_user_by_email,
)

__all__ = [
    "CalendarAccessError",
    "CalendarNotConfiguredError",
    "calendar_connect_required_detail",
    "cancel_interview_event",
    "create_interview_event",
    "freebusy_for_emails",
    "get_auth_provider_for_email",
    "is_hr_calendar_connected",
]


def get_auth_provider_for_email(hr_email: str) -> str:
    return resolve_auth_provider(get_hr_user_by_email(hr_email))


def is_hr_calendar_connected(hr_email: str) -> bool:
    doc = get_hr_user_by_email(hr_email)
    if not doc:
        return False
    if resolve_auth_provider(doc) == PROVIDER_MICROSOFT:
        return bool(doc.get("microsoft_calendar_refresh_token"))
    return bool(doc.get("google_calendar_refresh_token"))


def calendar_connect_required_detail(hr_email: str = "") -> str:
    label = provider_label(get_auth_provider_for_email(hr_email) if hr_email else "google")
    return (
        f"Connect your company {label} Calendar in Settings before scheduling interviews."
    )


def freebusy_for_emails(
    emails: List[str],
    time_min: datetime,
    time_max: datetime,
    hr_email: str,
    timezone_name: str = "Asia/Kolkata",
) -> Dict[str, List[Dict[str, Any]]]:
    if get_auth_provider_for_email(hr_email) == PROVIDER_MICROSOFT:
        from app.services.microsoft_calendar import freebusy_for_emails as ms_freebusy

        return ms_freebusy(
            emails,
            time_min,
            time_max,
            hr_email,
            timezone_name=timezone_name,
        )
    from app.services.google_calendar import freebusy_for_emails as google_freebusy

    return google_freebusy(emails, time_min, time_max, hr_email=hr_email)


def create_interview_event(
    hr_email: str,
    summary: str,
    start_dt: datetime,
    end_dt: datetime,
    timezone: str,
    attendee_emails: List[str],
    description: str = "",
    location: str = "",
    with_meet: bool = True,
) -> Dict[str, Any]:
    provider = get_auth_provider_for_email(hr_email)
    if provider == PROVIDER_MICROSOFT:
        from app.services.microsoft_calendar import create_interview_event as ms_create

        event = ms_create(
            hr_email=hr_email,
            summary=summary,
            start_dt=start_dt,
            end_dt=end_dt,
            timezone=timezone,
            attendee_emails=attendee_emails,
            description=description,
            location=location,
            with_meet=with_meet,
        )
    else:
        from app.services.google_calendar import create_interview_event as google_create

        event = google_create(
            hr_email=hr_email,
            summary=summary,
            start_dt=start_dt,
            end_dt=end_dt,
            timezone=timezone,
            attendee_emails=attendee_emails,
            description=description,
            location=location,
            with_meet=with_meet,
        )
        event["calendar_provider"] = "google"
        event["meeting_provider"] = meeting_provider_for("google", online=with_meet)
    return event


def cancel_interview_event(
    hr_email: str,
    event_id: str,
    calendar_provider: Optional[str] = None,
) -> None:
    provider = (calendar_provider or "").strip().lower() or get_auth_provider_for_email(hr_email)
    if provider == PROVIDER_MICROSOFT:
        from app.services.microsoft_calendar import cancel_interview_event as ms_cancel

        return ms_cancel(hr_email, event_id)
    from app.services.google_calendar import cancel_interview_event as google_cancel

    return google_cancel(hr_email, event_id)
