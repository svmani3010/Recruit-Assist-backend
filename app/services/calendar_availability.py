"""Calendar availability — Google Calendar FreeBusy via HR OAuth (no mock)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.database import get_db
from app.settings import get_settings
from app.timezone_utils import as_utc, resolve_timezone

logger = logging.getLogger(__name__)

INTERVITE_COLLECTION = "interviewInvites"


def _calendar_tz():
    return resolve_timezone(get_settings().google_calendar_timezone)


def _as_utc(dt: datetime) -> datetime:
    return as_utc(dt)


def _parse_event_dt(value, tz=None) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date") or ""
    text = str(value).replace("Z", "+00:00")
    try:
        # All-day events use YYYY-MM-DD — treat as local midnight.
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            local_tz = tz or _calendar_tz()
            day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=local_tz)
            return _as_utc(day)
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            local_tz = tz or _calendar_tz()
            parsed = parsed.replace(tzinfo=local_tz)
        return _as_utc(parsed)
    except ValueError:
        return None


def fetch_busy_blocks_batch(
    emails: List[str],
    start: datetime,
    end: datetime,
    hr_email: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """Google FreeBusy for HR + interviewers using HR's connected calendar."""
    from app.services.hr_calendar_oauth import (
        CalendarNotConfiguredError,
        is_hr_calendar_connected,
    )
    from app.services.calendar_gateway import CalendarAccessError, freebusy_for_emails

    hr_key = (hr_email or "").strip().lower()
    if not hr_key:
        raise CalendarNotConfiguredError("HR calendar email is required.")
    if not is_hr_calendar_connected(hr_key):
        raise CalendarNotConfiguredError(
            f"Calendar is not connected for {hr_key}. "
            "Connect Calendar in recruiter Settings."
        )

    try:
        return freebusy_for_emails(
            emails,
            _as_utc(start),
            _as_utc(end),
            hr_email=hr_key,
            timezone_name=get_settings().google_calendar_timezone,
        )
    except CalendarAccessError:
        raise
    except Exception as exc:
        logger.exception("FreeBusy failed")
        raise CalendarAccessError(str(exc)) from exc


def fetch_events_for_email(
    email: str,
    start: datetime,
    end: datetime,
    hr_email: str,
) -> List[Dict[str, Any]]:
    blocks = fetch_busy_blocks_batch([email], start, end, hr_email=hr_email)
    return blocks.get(email.lower().strip(), [])


def _load_scheduled_interview_blocks(
    emails: List[str],
    start: datetime,
    end: datetime,
    timezone_name: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return busy blocks per email for already-booked interviews in our DB."""
    db = get_db()
    email_set = {e.lower() for e in emails}
    start_key = start.strftime("%Y-%m-%d")
    end_key = end.strftime("%Y-%m-%d")

    cursor = db[INTERVITE_COLLECTION].find(
        {
            "status": "scheduled",
            "scheduled_date": {"$gte": start_key, "$lte": end_key},
        }
    )

    blocks_by_email: Dict[str, List[Dict[str, Any]]] = {e: [] for e in email_set}
    tz = resolve_timezone(timezone_name) if timezone_name else _calendar_tz()

    for doc in cursor:
        involved = set()
        hr = (doc.get("hr_calendar_email") or "").strip().lower()
        if hr:
            involved.add(hr)
        for interviewer in doc.get("interviewers") or []:
            ie = (interviewer.get("email") or "").strip().lower()
            if ie:
                involved.add(ie)
        overlap = involved.intersection(email_set)
        if not overlap:
            continue

        scheduled_date = doc.get("scheduled_date")
        scheduled_time = doc.get("scheduled_time")
        if not scheduled_date or not scheduled_time:
            continue

        invite_tz = resolve_timezone(doc.get("timezone") or timezone_name)
        try:
            start_dt = datetime.strptime(
                f"{scheduled_date} {scheduled_time}",
                "%Y-%m-%d %I:%M %p",
            ).replace(tzinfo=invite_tz)
        except ValueError:
            try:
                start_dt = datetime.strptime(
                    f"{scheduled_date} {scheduled_time}",
                    "%Y-%m-%d %H:%M",
                ).replace(tzinfo=invite_tz)
            except ValueError:
                continue

        duration = _coerce_minutes(doc.get("duration_minutes"), 60)
        end_dt = start_dt + timedelta(minutes=duration)
        block = {
            "summary": "Scheduled interview",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
        }
        for email in overlap:
            blocks_by_email[email].append(block)

    return blocks_by_email


def _slot_overlaps_busy(
    slot_start: datetime,
    slot_end: datetime,
    busy_events: List[Dict[str, Any]],
    tz=None,
) -> bool:
    """True when [slot_start, slot_end) intersects any busy [start, end)."""
    slot_start = _as_utc(slot_start)
    slot_end = _as_utc(slot_end)
    if slot_end <= slot_start:
        return True

    for event in busy_events:
        event_start = _parse_event_dt(event.get("start", ""), tz=tz)
        event_end = _parse_event_dt(event.get("end", ""), tz=tz)
        if not event_start or not event_end:
            continue
        if slot_start < event_end and slot_end > event_start:
            return True
    return False


def _format_slot_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")


def _coerce_minutes(value: Any, default: int) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return default
    return minutes if minutes > 0 else default


def compute_available_slots(
    interviewer_emails: List[str],
    date_start: datetime,
    date_end: datetime,
    duration_minutes: int = 60,
    working_hours_start: int = 9,
    working_hours_end: int = 21,
    slot_increment_minutes: int = 30,
    hr_email: Optional[str] = None,
    timezone_name: Optional[str] = None,
    include_saturday: bool = False,
    include_sunday: bool = False,
) -> Dict[str, List[str]]:
    duration_minutes = _coerce_minutes(duration_minutes, 60)
    slot_increment_minutes = _coerce_minutes(slot_increment_minutes, duration_minutes)
    # Keep starts on a grid that fits the interview length (60 → hourly when matched).
    if slot_increment_minutes > duration_minutes:
        slot_increment_minutes = duration_minutes

    emails = [e.strip().lower() for e in interviewer_emails if e and e.strip()]
    if not emails or not hr_email:
        return {}

    include_saturday, include_sunday = normalize_weekend_flags(
        include_saturday, include_sunday
    )

    tz = resolve_timezone(timezone_name) if timezone_name else _calendar_tz()
    range_start = date_start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    range_end = date_end.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=None)
    range_start = datetime.combine(range_start.date(), datetime.min.time(), tzinfo=tz)
    range_end = datetime.combine(range_end.date(), datetime.max.time(), tzinfo=tz)

    busy_by_email = fetch_busy_blocks_batch(
        emails,
        range_start,
        range_end,
        hr_email=hr_email,
    )

    scheduled_by_email = _load_scheduled_interview_blocks(
        emails, range_start, range_end, timezone_name=timezone_name
    )
    for email in emails:
        busy_by_email.setdefault(email, []).extend(scheduled_by_email.get(email, []))

    slots_by_date: Dict[str, List[str]] = {}
    day = range_start
    while day <= range_end:
        if not is_allowed_interview_day(
            day,
            include_saturday=include_saturday,
            include_sunday=include_sunday,
        ):
            day += timedelta(days=1)
            continue

        date_key = day.strftime("%Y-%m-%d")
        day_slots: List[str] = []

        slot_cursor = day.replace(
            hour=working_hours_start, minute=0, second=0, microsecond=0
        )
        day_end = day.replace(
            hour=working_hours_end, minute=0, second=0, microsecond=0
        )

        while slot_cursor + timedelta(minutes=duration_minutes) <= day_end:
            slot_end = slot_cursor + timedelta(minutes=duration_minutes)
            is_free = True
            for email in emails:
                if _slot_overlaps_busy(
                    slot_cursor,
                    slot_end,
                    busy_by_email.get(email, []),
                    tz=tz,
                ):
                    is_free = False
                    break
            if is_free:
                day_slots.append(_format_slot_time(slot_cursor))
            slot_cursor += timedelta(minutes=slot_increment_minutes)

        if day_slots:
            slots_by_date[date_key] = day_slots
        day += timedelta(days=1)

    return slots_by_date


def slots_for_date(
    interviewer_emails: List[str],
    target_date: datetime,
    duration_minutes: int = 60,
    working_hours_start: int = 9,
    working_hours_end: int = 21,
    slot_increment_minutes: int = 30,
    hr_email: Optional[str] = None,
    timezone_name: Optional[str] = None,
    include_saturday: bool = False,
    include_sunday: bool = False,
) -> List[str]:
    grouped = compute_available_slots(
        interviewer_emails=interviewer_emails,
        date_start=target_date,
        date_end=target_date,
        duration_minutes=duration_minutes,
        working_hours_start=working_hours_start,
        working_hours_end=working_hours_end,
        slot_increment_minutes=slot_increment_minutes,
        hr_email=hr_email,
        timezone_name=timezone_name,
        include_saturday=include_saturday,
        include_sunday=include_sunday,
    )
    return grouped.get(target_date.strftime("%Y-%m-%d"), [])


def parse_date_string(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def normalize_weekend_flags(
    include_saturday: Optional[bool] = None,
    include_sunday: Optional[bool] = None,
) -> tuple[bool, bool]:
    """Default: weekends excluded unless HR explicitly opts in."""
    return bool(include_saturday), bool(include_sunday)


def is_allowed_interview_day(
    day: datetime,
    *,
    include_saturday: bool = False,
    include_sunday: bool = False,
) -> bool:
    """Mon–Fri always allowed; Sat/Sun only when opted in (Python weekday: Mon=0 … Sun=6)."""
    weekday = day.weekday()
    if weekday == 5:
        return include_saturday
    if weekday == 6:
        return include_sunday
    return True


def count_allowed_interview_days(
    date_start: datetime,
    date_end: datetime,
    *,
    include_saturday: bool = False,
    include_sunday: bool = False,
) -> int:
    day = date_start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = date_end.replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    while day <= end:
        if is_allowed_interview_day(
            day,
            include_saturday=include_saturday,
            include_sunday=include_sunday,
        ):
            count += 1
        day += timedelta(days=1)
    return count


def normalize_time_label(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("Time is required")

    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            parsed = datetime.strptime(text.upper(), fmt)
            return parsed.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
        except ValueError:
            continue
    raise ValueError(f"Invalid time format: {value}")
