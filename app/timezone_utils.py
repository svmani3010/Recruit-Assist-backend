"""IANA timezone lookup that works on Windows without a system zoneinfo DB."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_TZ_NAME = "Asia/Kolkata"
_IST_OFFSET = timezone(timedelta(hours=5, minutes=30), name=DEFAULT_TZ_NAME)

_ALIASES = {
    "ist": DEFAULT_TZ_NAME,
    "india": DEFAULT_TZ_NAME,
    "asia/calcutta": DEFAULT_TZ_NAME,
    "kolkata": DEFAULT_TZ_NAME,
    "calcutta": DEFAULT_TZ_NAME,
}


def _ensure_tzdata() -> None:
    try:
        import tzdata  # noqa: F401
    except ImportError:
        pass


_ensure_tzdata()


def normalize_tz_name(name: Optional[str]) -> str:
    raw = str(name or "").strip()
    if not raw:
        return DEFAULT_TZ_NAME
    return _ALIASES.get(raw.lower(), raw)


def resolve_timezone(name: Optional[str] = None):
    """Return a tzinfo for the given IANA key; never fail for Asia/Kolkata."""
    requested = normalize_tz_name(name)
    for candidate in (requested, DEFAULT_TZ_NAME):
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, KeyError, OSError):
            continue
    logger.warning(
        "IANA timezone data missing for %r; using fixed UTC+05:30. "
        "Install the tzdata package for full timezone support.",
        requested,
    )
    return _IST_OFFSET


def localize(dt: datetime, name: Optional[str] = None) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(resolve_timezone(name))
    return dt.replace(tzinfo=resolve_timezone(name))


def as_utc(dt: datetime) -> datetime:
    """Normalize any datetime to timezone-aware UTC for safe comparisons."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_naive_utc(dt: datetime) -> datetime:
    """UTC datetime with tzinfo stripped (google-auth Credentials.expiry)."""
    return as_utc(dt).replace(tzinfo=None)
