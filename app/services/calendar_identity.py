"""HR identity provider (Google vs Microsoft) used for calendar routing."""

from __future__ import annotations

from typing import Optional

PROVIDER_GOOGLE = "google"
PROVIDER_MICROSOFT = "microsoft"

_MICROSOFT_ALIASES = {
    "microsoft",
    "azure",
    "ms",
    "office365",
    "outlook",
    "teams",
    "teamsforbusiness",
}


def normalize_auth_provider(value: Optional[str]) -> str:
    raw = str(value or "").strip().lower()
    if raw in _MICROSOFT_ALIASES:
        return PROVIDER_MICROSOFT
    return PROVIDER_GOOGLE


def resolve_auth_provider(doc: Optional[dict]) -> str:
    """Use the login provider stored on hr_users — never email domain or leftover tokens."""
    if not doc:
        return PROVIDER_GOOGLE
    raw = (doc.get("auth_provider") or "").strip().lower()
    if raw:
        return normalize_auth_provider(raw)
    return PROVIDER_GOOGLE


def provider_label(provider: Optional[str]) -> str:
    return "Microsoft" if normalize_auth_provider(provider) == PROVIDER_MICROSOFT else "Google"


def meeting_provider_for(auth_provider: Optional[str], *, online: bool) -> Optional[str]:
    if not online:
        return None
    if normalize_auth_provider(auth_provider) == PROVIDER_MICROSOFT:
        return "teams"
    return "google_meet"


def is_teams_link(link: Optional[str]) -> bool:
    text = (link or "").lower()
    return "teams.microsoft.com" in text or "teams.live.com" in text


def is_microsoft_meeting(
    data: Optional[dict] = None,
    *,
    meeting_link: str = "",
    meeting_provider: str = "",
) -> bool:
    payload = data or {}
    provider = (
        meeting_provider
        or payload.get("meeting_provider")
        or payload.get("calendar_provider")
        or payload.get("auth_provider")
        or ""
    )
    link = meeting_link or payload.get("meeting_link") or ""
    return is_teams_link(link) or normalize_auth_provider(provider) == PROVIDER_MICROSOFT


def video_call_label(
    data: Optional[dict] = None,
    *,
    meeting_link: str = "",
    meeting_provider: str = "",
) -> str:
    payload = data or {}
    link = meeting_link or payload.get("meeting_link") or ""
    provider = (
        meeting_provider
        or payload.get("meeting_provider")
        or payload.get("calendar_provider")
        or payload.get("auth_provider")
        or ""
    )
    if is_microsoft_meeting(payload, meeting_link=link, meeting_provider=provider):
        return "Microsoft Teams"
    if link or provider:
        return "Google Meet"
    return "Online"


def emails_match(left: Optional[str], right: Optional[str]) -> bool:
    return (left or "").strip().lower() == (right or "").strip().lower()
