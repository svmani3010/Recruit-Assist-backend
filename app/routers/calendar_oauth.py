"""Calendar OAuth connect flow for HR recruiters (Google or Microsoft)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from app.auth import AuthPrincipal, require_hr, require_hr_account
from app.services.calendar_identity import (
    PROVIDER_MICROSOFT,
    provider_label,
    resolve_auth_provider,
)
from app.services.hr_calendar_oauth import (
    build_oauth_authorization_url,
    exchange_oauth_code,
    get_hr_email_for_user_id,
    get_hr_user_by_id,
    is_hr_calendar_connected,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["calendar"])


def _hr_provider(user_id: str) -> str:
    return resolve_auth_provider(get_hr_user_by_id(user_id))


@router.get("/api/calendar/oauth/url")
async def calendar_oauth_url(principal: AuthPrincipal = Depends(require_hr_account)):
    """Return the calendar OAuth URL for the HR login provider."""
    provider = _hr_provider(principal.subject)
    email = get_hr_email_for_user_id(principal.subject)
    if provider == PROVIDER_MICROSOFT:
        from app.services.hr_microsoft_oauth import build_calendar_authorization_url

        url, redirect_uri = build_calendar_authorization_url(principal.subject)
    else:
        url, redirect_uri = build_oauth_authorization_url(principal.subject)
    return {
        "url": url,
        "hr_email": email,
        "redirect_uri": redirect_uri,
        "calendar_mode": provider,
        "provider": provider,
        "login_hint": email,
    }


@router.get("/api/calendar/oauth/callback")
async def calendar_oauth_callback(code: str, state: str):
    """Google redirects here after consent; tokens are stored on the HR user."""
    settings = get_settings()
    try:
        hr_email = exchange_oauth_code(code, state)
    except Exception as exc:
        logger.exception("Calendar OAuth callback failed")
        reason = "error"
        detail = str(getattr(exc, "detail", exc)).lower()
        if "client secret" in detail or "invalid_client" in detail:
            reason = "invalid_secret"
        elif "same email" in detail:
            reason = "wrong_account"
        target = f"{settings.frontend_url}/recruiters/settings?calendar={reason}"
        return RedirectResponse(url=target)

    target = (
        f"{settings.frontend_url}/recruiters/settings"
        f"?calendar=connected&email={hr_email}"
    )
    return RedirectResponse(url=target)


@router.get("/api/calendar/oauth/microsoft/callback")
async def microsoft_calendar_oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Microsoft redirects here after calendar consent."""
    settings = get_settings()
    if error or not code:
        target = f"{settings.frontend_url}/recruiters/settings?calendar=error"
        return RedirectResponse(url=target)
    try:
        from app.services.hr_microsoft_oauth import exchange_calendar_code

        hr_email = exchange_calendar_code(code, state)
    except Exception as exc:
        logger.exception("Microsoft calendar OAuth callback failed")
        reason = "error"
        detail = str(getattr(exc, "detail", exc)).lower()
        if "same email" in detail:
            reason = "wrong_account"
        target = f"{settings.frontend_url}/recruiters/settings?calendar={reason}"
        return RedirectResponse(url=target)

    target = (
        f"{settings.frontend_url}/recruiters/settings"
        f"?calendar=connected&email={hr_email}"
    )
    return RedirectResponse(url=target)


@router.get("/api/calendar/status")
async def calendar_status_api(principal: AuthPrincipal = Depends(require_hr)):
    """Whether the logged-in HR user's calendar is connected."""
    settings = get_settings()
    doc = get_hr_user_by_id(principal.subject)
    email = (doc.get("email") or "").strip().lower() if doc else None
    provider = resolve_auth_provider(doc)
    connected = is_hr_calendar_connected(email) if email else False
    label = provider_label(provider)

    return {
        "calendar_mode": provider,
        "provider": provider,
        "provider_label": label,
        "connected": connected,
        "hr_email": email,
        "timezone": settings.google_calendar_timezone,
        "requires_connect": not connected,
        "meeting_label": "Microsoft Teams" if provider == PROVIDER_MICROSOFT else "Google Meet",
    }
