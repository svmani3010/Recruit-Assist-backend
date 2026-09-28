"""Microsoft Graph Calendar + Teams meeting creation."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.google_calendar import CalendarAccessError
from app.services.hr_microsoft_oauth import get_microsoft_access_token, graph_request
from app.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)


def _to_graph_dt(dt: datetime, tz_name: str) -> dict:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=resolve_timezone(tz_name))
    return {
        "dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "timeZone": tz_name or "Asia/Kolkata",
    }


def _item_iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        raw = value.get("dateTime") or value.get("date") or ""
        tz = value.get("timeZone") or "UTC"
        if not raw:
            return None
        text = str(raw).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text[:26])
        except ValueError:
            return str(raw)
        if parsed.tzinfo is None:
            try:
                parsed = parsed.replace(tzinfo=resolve_timezone(tz))
            except Exception:
                pass
        return parsed.isoformat()
    return str(value)


def freebusy_for_emails(
    emails: List[str],
    time_min: datetime,
    time_max: datetime,
    hr_email: str,
    timezone_name: str = "Asia/Kolkata",
) -> Dict[str, List[Dict[str, Any]]]:
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

    token = get_microsoft_access_token(hr_key)
    body = {
        "schedules": unique,
        "startTime": _to_graph_dt(time_min, timezone_name),
        "endTime": _to_graph_dt(time_max, timezone_name),
        "availabilityViewInterval": 30,
    }
    try:
        response = graph_request(token, "POST", "/me/calendar/getSchedule", json_body=body)
    except Exception as exc:
        logger.exception("Microsoft getSchedule failed for HR %s", hr_email)
        raise CalendarAccessError(f"Microsoft Calendar availability failed: {exc}") from exc

    result: Dict[str, List[Dict[str, Any]]] = {email: [] for email in unique}
    for row in response.get("value") or []:
        schedule_id = (row.get("scheduleId") or "").strip().lower()
        if schedule_id not in result:
            continue
        blocks: List[Dict[str, Any]] = []
        for item in row.get("scheduleItems") or []:
            status = str(item.get("status") or "").lower()
            if status in {"free", "workingelsewhere"}:
                continue
            start = _item_iso(item.get("start"))
            end = _item_iso(item.get("end"))
            if start and end:
                blocks.append({"summary": status or "Busy", "start": start, "end": end})
        result[schedule_id] = blocks
    return result


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
    token = get_microsoft_access_token(hr_email)
    attendees = []
    seen = set()
    for raw in attendee_emails:
        email = (raw or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            attendees.append(
                {
                    "emailAddress": {"address": email},
                    "type": "required",
                }
            )

    event_body: Dict[str, Any] = {
        "subject": summary,
        "body": {
            "contentType": "HTML",
            "content": (description or "").replace("\n", "<br>"),
        },
        "start": _to_graph_dt(start_dt, timezone),
        "end": _to_graph_dt(end_dt, timezone),
        "attendees": attendees,
        "allowNewTimeProposals": False,
    }
    if location:
        event_body["location"] = {"displayName": location}
    if with_meet:
        event_body["isOnlineMeeting"] = True
        event_body["onlineMeetingProvider"] = "teamsForBusiness"

    try:
        created = graph_request(token, "POST", "/me/events", json_body=event_body)
    except Exception as exc:
        logger.exception("Failed to create Microsoft Calendar event for HR %s", hr_email)
        raise CalendarAccessError(f"Could not create Microsoft Calendar event: {exc}") from exc

    teams_link = ((created.get("onlineMeeting") or {}).get("joinUrl") or "").strip()
    if not teams_link:
        teams_link = (created.get("onlineMeetingUrl") or "").strip()
    event_id = created.get("id")
    if with_meet and not teams_link and event_id:
        try:
            refreshed = graph_request(token, "GET", f"/me/events/{event_id}")
            teams_link = ((refreshed.get("onlineMeeting") or {}).get("joinUrl") or "").strip()
            if not teams_link:
                teams_link = (refreshed.get("onlineMeetingUrl") or "").strip()
        except Exception:
            logger.warning("Could not re-read Teams join URL for event %s", event_id)
    if with_meet and not teams_link:
        raise CalendarAccessError(
            "Microsoft Calendar did not return a Teams join link. "
            "Confirm this Microsoft 365 account has Teams enabled, then reconnect Calendar."
        )

    return {
        "event_id": created.get("id"),
        "meeting_link": teams_link or None,
        "html_link": (created.get("webLink") or "").strip() or None,
        "location": location,
        "calendar_provider": "microsoft",
        "meeting_provider": "teams" if with_meet else None,
    }


def cancel_interview_event(hr_email: str, event_id: str) -> None:
    if not hr_email or not event_id:
        return
    try:
        token = get_microsoft_access_token(hr_email)
        graph_request(token, "DELETE", f"/me/events/{event_id}")
    except Exception as exc:
        logger.warning(
            "Could not cancel Microsoft Calendar event %s for HR %s: %s",
            event_id,
            hr_email,
            exc,
        )
