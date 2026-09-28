"""Google Calendar via HR OAuth — FreeBusy + Meet event creation."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.hr_calendar_oauth import get_hr_calendar_credentials
from app.settings import get_settings
from app.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)


class CalendarNotConfiguredError(RuntimeError):
    """HR has not connected Google Calendar."""


class CalendarAccessError(RuntimeError):
    """Google Calendar API error."""


def _calendar_tz():
    return resolve_timezone(get_settings().google_calendar_timezone)


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_calendar_tz())
    return dt.isoformat()


def _calendar_service(hr_email: str):
    from googleapiclient.discovery import build

    credentials = get_hr_calendar_credentials(hr_email)
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _is_overlay_calendar(item: Dict[str, Any]) -> bool:
    summary = (item.get("summary") or "").lower()
    skip = ("holiday", "holidays", "birthday", "birthdays", "weather")
    return any(word in summary for word in skip)


def list_hr_busy_calendar_ids(hr_email: str) -> List[str]:
    """Calendars visible in the HR Google Calendar UI (selected overlays included)."""
    service = _calendar_service(hr_email)
    calendar_ids: List[str] = []
    seen = set()
    page_token = None
    try:
        while True:
            response = service.calendarList().list(
                maxResults=250,
                pageToken=page_token,
            ).execute()
            for item in response.get("items") or []:
                if item.get("deleted") or item.get("selected") is False:
                    continue
                if _is_overlay_calendar(item):
                    continue
                calendar_id = (item.get("id") or "").strip()
                key = calendar_id.lower()
                if not calendar_id or key in seen:
                    continue
                seen.add(key)
                calendar_ids.append(calendar_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        logger.warning("Could not list calendars for %s: %s", hr_email, exc)

    hr_key = (hr_email or "").strip().lower()
    if hr_key and hr_key not in seen:
        calendar_ids.insert(0, hr_key)
    if "primary" not in seen:
        calendar_ids.insert(0, "primary")
    return calendar_ids


def _busy_blocks_from_calendar_payload(cal_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if cal_data.get("errors"):
        # Returning [] here used to mark the whole calendar as free and caused
        # overlapping bookings. Callers must treat this as a hard failure.
        raise CalendarAccessError(
            f"Google FreeBusy calendar error: {cal_data.get('errors')}"
        )
    blocks: List[Dict[str, Any]] = []
    for busy in cal_data.get("busy") or []:
        start = busy.get("start")
        end = busy.get("end")
        if start and end:
            blocks.append({"summary": "Busy", "start": start, "end": end})
    return blocks


def list_opaque_event_busy_blocks(
    hr_email: str,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> List[Dict[str, Any]]:
    """Fallback busy blocks from events.list (catches FreeBusy gaps)."""
    service = _calendar_service(hr_email)
    blocks: List[Dict[str, Any]] = []
    page_token = None
    try:
        while True:
            response = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=_to_rfc3339(time_min),
                    timeMax=_to_rfc3339(time_max),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page_token,
                )
                .execute()
            )
            for item in response.get("items") or []:
                if item.get("status") == "cancelled":
                    continue
                # "transparent" means Show as Free — do not block scheduling.
                if str(item.get("transparency") or "").lower() == "transparent":
                    continue
                start = item.get("start") or {}
                end = item.get("end") or {}
                start_raw = start.get("dateTime") or start.get("date")
                end_raw = end.get("dateTime") or end.get("date")
                if start_raw and end_raw:
                    blocks.append(
                        {
                            "summary": item.get("summary") or "Busy",
                            "start": start_raw,
                            "end": end_raw,
                        }
                    )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        logger.warning(
            "events.list busy fallback failed for %s/%s: %s",
            hr_email,
            calendar_id,
            exc,
        )
    return blocks


def freebusy_for_emails(
    emails: List[str],
    time_min: datetime,
    time_max: datetime,
    hr_email: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Batch FreeBusy for HR + interviewers using the HR's connected Google account.

    HR busy time includes every selected calendar on that account (team calendars
    shown in the Google Calendar UI), not only the primary inbox calendar.
    """
    unique: List[str] = []
    seen = set()
    hr_key = (hr_email or "").strip().lower()
    if hr_key:
        seen.add(hr_key)
        unique.append(hr_key)

    for email in emails:
        key = (email or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)

    if not unique:
        return {}

    query_ids = list(unique)
    hr_extra_ids: List[str] = []
    if hr_key:
        for calendar_id in list_hr_busy_calendar_ids(hr_key):
            key = calendar_id.lower()
            if key in seen:
                continue
            seen.add(key)
            query_ids.append(calendar_id)
            hr_extra_ids.append(calendar_id)

    service = _calendar_service(hr_key)

    body = {
        "timeMin": _to_rfc3339(time_min),
        "timeMax": _to_rfc3339(time_max),
        "items": [{"id": calendar_id} for calendar_id in query_ids],
    }

    try:
        response = service.freebusy().query(body=body).execute()
    except Exception as exc:
        logger.exception("Google Calendar FreeBusy failed for HR %s", hr_email)
        raise CalendarAccessError(f"Google Calendar FreeBusy failed: {exc}") from exc

    calendars = response.get("calendars") or {}
    result: Dict[str, List[Dict[str, Any]]] = {email: [] for email in unique}

    # Google may return calendar keys with different casing than requested ids.
    normalized_response: Dict[str, Any] = {
        (key or "").lower(): value for key, value in calendars.items()
    }

    for email in unique:
        cal_data = normalized_response.get(email.lower()) or {}
        try:
            result[email] = _busy_blocks_from_calendar_payload(cal_data)
        except CalendarAccessError as exc:
            logger.warning(
                "FreeBusy failed for %s (%s); falling back to events.list",
                email,
                exc,
            )
            result[email] = list_opaque_event_busy_blocks(
                hr_key, email, time_min, time_max
            )

    if hr_key:
        hr_blocks = list(result.get(hr_key) or [])
        primary_data = normalized_response.get("primary") or {}
        try:
            hr_blocks.extend(_busy_blocks_from_calendar_payload(primary_data))
        except CalendarAccessError as exc:
            logger.warning("FreeBusy primary failed (%s); using events.list", exc)
            hr_blocks.extend(
                list_opaque_event_busy_blocks(hr_key, "primary", time_min, time_max)
            )
        for calendar_id in hr_extra_ids:
            extra = normalized_response.get(calendar_id.lower()) or {}
            try:
                hr_blocks.extend(_busy_blocks_from_calendar_payload(extra))
            except CalendarAccessError as exc:
                logger.warning(
                    "FreeBusy failed for overlay %s (%s); using events.list",
                    calendar_id,
                    exc,
                )
                hr_blocks.extend(
                    list_opaque_event_busy_blocks(
                        hr_key, calendar_id, time_min, time_max
                    )
                )
        # Always merge opaque events from primary as a second source so meetings
        # FreeBusy misses still block 30/60-minute interview slots.
        hr_blocks.extend(
            list_opaque_event_busy_blocks(hr_key, "primary", time_min, time_max)
        )
        if hr_key != "primary":
            hr_blocks.extend(
                list_opaque_event_busy_blocks(hr_key, hr_key, time_min, time_max)
            )
        result[hr_key] = hr_blocks

    return result


def freebusy_events_for_email(
    email: str,
    time_min: datetime,
    time_max: datetime,
    hr_email: str,
) -> List[Dict[str, Any]]:
    grouped = freebusy_for_emails([email], time_min, time_max, hr_email=hr_email)
    return grouped.get(email.lower().strip(), [])


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
    """Create an event on HR's calendar. Remote interviews get a Meet link."""
    service = _calendar_service(hr_email)
    request_id = uuid.uuid4().hex

    attendees = []
    seen = set()
    for raw in attendee_emails:
        email = (raw or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            attendees.append({"email": email})

    event_body: Dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": timezone,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": timezone,
        },
        "attendees": attendees,
    }
    if location:
        event_body["location"] = location
    conference_version = 0
    if with_meet:
        event_body["conferenceData"] = {
            "createRequest": {
                "requestId": request_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        conference_version = 1

    try:
        created = service.events().insert(
            calendarId="primary",
            body=event_body,
            conferenceDataVersion=conference_version,
            sendUpdates="all",
        ).execute()
    except Exception as exc:
        logger.exception("Failed to create Google Calendar event for HR %s", hr_email)
        raise CalendarAccessError(f"Could not create calendar event: {exc}") from exc

    meet_link = created.get("hangoutLink")
    if not meet_link:
        entry_points = (created.get("conferenceData") or {}).get("entryPoints") or []
        for entry in entry_points:
            if entry.get("entryPointType") == "video":
                meet_link = entry.get("uri")
                break

    if with_meet and not meet_link:
        raise CalendarAccessError("Google Calendar did not return a Meet link.")

    return {
        "event_id": created.get("id"),
        "meeting_link": meet_link,
        "html_link": created.get("htmlLink"),
        "location": created.get("location") or location,
    }


def create_interview_event_with_meet(
    hr_email: str,
    summary: str,
    start_dt: datetime,
    end_dt: datetime,
    timezone: str,
    attendee_emails: List[str],
    description: str = "",
) -> Dict[str, Any]:
    return create_interview_event(
        hr_email=hr_email,
        summary=summary,
        start_dt=start_dt,
        end_dt=end_dt,
        timezone=timezone,
        attendee_emails=attendee_emails,
        description=description,
        with_meet=True,
    )


def cancel_interview_event(hr_email: str, event_id: str) -> None:
    """Cancel a previously created interview on HR's Google Calendar."""
    if not hr_email or not event_id:
        return
    service = _calendar_service(hr_email)
    try:
        service.events().delete(
            calendarId="primary",
            eventId=event_id,
            sendUpdates="all",
        ).execute()
    except Exception as exc:
        logger.warning(
            "Could not cancel Google Calendar event %s for HR %s: %s",
            event_id,
            hr_email,
            exc,
        )
