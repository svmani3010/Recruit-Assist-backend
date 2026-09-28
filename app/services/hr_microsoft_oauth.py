"""HR Microsoft identity + Graph Calendar OAuth.

Login and calendar connect are separate OAuth grants:
- Login uses openid so we can create the HR session and store auth_provider.
- Calendar uses Calendars.ReadWrite + OnlineMeetings.ReadWrite for FreeBusy
  and Teams join links. Both grants require the same email as the HR login.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx
from bson import ObjectId
from fastapi import HTTPException

from app.database import get_db
from app.services.calendar_identity import (
    PROVIDER_MICROSOFT,
    emails_match,
    resolve_auth_provider,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)

LOGIN_SCOPES = ["openid", "profile", "email", "offline_access", "User.Read"]
CALENDAR_SCOPES = [
    "offline_access",
    "User.Read",
    "Calendars.ReadWrite",
    "OnlineMeetings.ReadWrite",
]
STATE_TTL_MINUTES = 12
LOGIN_CODE_TTL_MINUTES = 3
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftNotConfiguredError(RuntimeError):
    """Azure app settings are missing."""


def _hr_users():
    return get_db()["hr_users"]


def _oauth_states():
    return get_db()["microsoft_oauth_states"]


def _login_codes():
    return get_db()["hr_microsoft_login_codes"]


def microsoft_is_configured() -> bool:
    settings = get_settings()
    return bool(
        (settings.microsoft_client_id or "").strip()
        and (settings.microsoft_client_secret or "").strip()
        and not str(settings.microsoft_client_secret).startswith("CHANGE_ME")
    )


def require_microsoft_configured() -> None:
    if not microsoft_is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Microsoft sign-in is not configured. Set MICROSOFT_CLIENT_ID and "
                "MICROSOFT_CLIENT_SECRET, then restart the API."
            ),
        )


def _authority(tenant: Optional[str] = None) -> str:
    settings = get_settings()
    chosen = (
        (tenant or "").strip()
        or (settings.microsoft_tenant_id or "organizations").strip()
        or "organizations"
    )
    return f"https://login.microsoftonline.com/{chosen}"


def _login_redirect_uri() -> str:
    settings = get_settings()
    uri = (settings.microsoft_login_redirect_uri or "").strip()
    if uri:
        return uri
    return "http://localhost:8000/auth/hr/microsoft/callback"


def _candidate_login_redirect_uri() -> str:
    settings = get_settings()
    uri = (settings.microsoft_candidate_login_redirect_uri or "").strip()
    if uri:
        return uri
    return "http://localhost:8000/auth/candidate/microsoft/callback"


def _candidate_tenant() -> str:
    settings = get_settings()
    return (
        (settings.microsoft_candidate_tenant_id or "common").strip() or "common"
    )


def _calendar_redirect_uri() -> str:
    settings = get_settings()
    uri = (settings.microsoft_oauth_redirect_uri or "").strip()
    if uri:
        return uri
    return "http://localhost:8000/api/calendar/oauth/microsoft/callback"


def _pkce_pair() -> Tuple[str, str]:
    import base64

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _store_state(payload: Dict[str, Any]) -> str:
    state = secrets.token_urlsafe(32)
    doc = {
        **payload,
        "state": state,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=STATE_TTL_MINUTES),
        "created_at": datetime.now(timezone.utc),
    }
    _oauth_states().insert_one(doc)
    return state


def _pop_state(state: str, purpose: str) -> dict:
    if not state:
        raise HTTPException(status_code=400, detail="Microsoft sign-in session is missing.")
    now = datetime.now(timezone.utc)
    doc = _oauth_states().find_one_and_delete({"state": state, "purpose": purpose})
    if not doc:
        raise HTTPException(
            status_code=400,
            detail="Microsoft sign-in session expired or was already used. Please try again.",
        )
    expires = doc.get("expires_at")
    if expires:
        if getattr(expires, "tzinfo", None) is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < now:
            raise HTTPException(
                status_code=400,
                detail="Microsoft sign-in session expired. Please try again.",
            )
    return doc


def _authorize_url(
    *,
    scopes: list[str],
    redirect_uri: str,
    state: str,
    code_challenge: str,
    login_hint: str = "",
    tenant: Optional[str] = None,
) -> str:
    settings = get_settings()
    params = {
        "client_id": settings.microsoft_client_id.strip(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
        params["prompt"] = "login"
    return f"{_authority(tenant)}/oauth2/v2.0/authorize?{urlencode(params)}"


def _token_request(form: Dict[str, str], *, tenant: Optional[str] = None) -> dict:
    settings = get_settings()
    form = {
        **form,
        "client_id": settings.microsoft_client_id.strip(),
        "client_secret": settings.microsoft_client_secret.strip().strip('"').strip("'"),
    }
    with httpx.Client(timeout=25.0) as client:
        response = client.post(
            f"{_authority(tenant)}/oauth2/v2.0/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code >= 400:
        logger.warning("Microsoft token exchange failed: %s %s", response.status_code, response.text[:400])
        detail = "Microsoft could not complete sign-in. Please try again."
        try:
            err = response.json()
            desc = str(err.get("error_description") or err.get("error") or "").lower()
            if "redirect" in desc:
                detail = "Microsoft rejected the redirect URI. Check MICROSOFT_*_REDIRECT_URI matches Azure."
            elif "invalid_client" in desc:
                detail = "Microsoft rejected MICROSOFT_CLIENT_SECRET. Update it from Azure App registrations."
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=detail)
    return response.json()


def graph_get(access_token: str, path: str, params: Optional[dict] = None) -> dict:
    with httpx.Client(timeout=25.0) as client:
        response = client.get(
            f"{GRAPH_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail=f"Microsoft Graph request failed ({response.status_code}).",
        )
    return response.json() if response.content else {}


def graph_request(
    access_token: str,
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    with httpx.Client(timeout=30.0) as client:
        response = client.request(
            method,
            f"{GRAPH_BASE}{path}",
            params=params,
            json=json_body,
            headers=headers,
        )
    if response.status_code >= 400:
        logger.warning("Graph %s %s failed: %s %s", method, path, response.status_code, response.text[:500])
        raise RuntimeError(f"Microsoft Graph {method} {path} failed ({response.status_code})")
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def _profile_email(profile: dict) -> str:
    return (
        (profile.get("mail") or "").strip().lower()
        or (profile.get("userPrincipalName") or "").strip().lower()
    )


def build_login_authorization_url() -> Tuple[str, str]:
    require_microsoft_configured()
    verifier, challenge = _pkce_pair()
    redirect_uri = _login_redirect_uri()
    state = _store_state({"purpose": "login", "code_verifier": verifier})
    url = _authorize_url(
        scopes=LOGIN_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
    )
    return url, redirect_uri


def build_candidate_login_authorization_url() -> Tuple[str, str]:
    """Candidate portal Microsoft login (personal + work accounts via common)."""
    require_microsoft_configured()
    verifier, challenge = _pkce_pair()
    redirect_uri = _candidate_login_redirect_uri()
    tenant = _candidate_tenant()
    state = _store_state(
        {
            "purpose": "candidate_login",
            "code_verifier": verifier,
            "tenant": tenant,
        }
    )
    url = _authorize_url(
        scopes=LOGIN_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        tenant=tenant,
    )
    return url, redirect_uri


def exchange_login_code(code: str, state: str) -> dict:
    require_microsoft_configured()
    stored = _pop_state(state, "login")
    token_payload = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _login_redirect_uri(),
            "code_verifier": stored.get("code_verifier") or "",
            "scope": " ".join(LOGIN_SCOPES),
        }
    )
    access = token_payload.get("access_token") or ""
    profile = graph_get(access, "/me", params={"$select": "id,displayName,mail,userPrincipalName"})
    email = _profile_email(profile)
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Microsoft did not return a verified work email. Use your company Microsoft 365 account.",
        )
    return {
        "email": email,
        "name": (profile.get("displayName") or "").strip() or email.split("@")[0],
        "picture": None,
        "microsoft_oid": profile.get("id"),
        "microsoft_upn": (profile.get("userPrincipalName") or "").strip().lower(),
    }


def exchange_candidate_login_code(code: str, state: str) -> dict:
    require_microsoft_configured()
    stored = _pop_state(state, "candidate_login")
    tenant = stored.get("tenant") or _candidate_tenant()
    token_payload = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _candidate_login_redirect_uri(),
            "code_verifier": stored.get("code_verifier") or "",
            "scope": " ".join(LOGIN_SCOPES),
        },
        tenant=tenant,
    )
    access = token_payload.get("access_token") or ""
    profile = graph_get(
        access, "/me", params={"$select": "id,displayName,mail,userPrincipalName"}
    )
    email = _profile_email(profile)
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Microsoft did not return a verified email for this account.",
        )
    return {
        "email": email,
        "name": (profile.get("displayName") or "").strip() or email.split("@")[0],
        "picture": None,
        "microsoft_oid": profile.get("id"),
        "microsoft_upn": (profile.get("userPrincipalName") or "").strip().lower(),
    }


def issue_login_code(session_payload: dict) -> str:
    code = secrets.token_urlsafe(32)
    _login_codes().insert_one(
        {
            "code": code,
            **session_payload,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=LOGIN_CODE_TTL_MINUTES),
            "created_at": datetime.now(timezone.utc),
        }
    )
    return code


def consume_login_code(code: str) -> dict:
    if not code:
        raise HTTPException(status_code=400, detail="Missing Microsoft login code.")
    doc = _login_codes().find_one_and_delete({"code": code})
    if not doc:
        raise HTTPException(
            status_code=400,
            detail="Microsoft login expired or was already used. Please sign in again.",
        )
    expires = doc.get("expires_at")
    if expires:
        if getattr(expires, "tzinfo", None) is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="Microsoft login expired. Please sign in again.")
    return doc


def build_calendar_authorization_url(user_id: str) -> Tuple[str, str]:
    require_microsoft_configured()
    from app.services.hr_calendar_oauth import get_hr_user_by_id

    doc = get_hr_user_by_id(user_id)
    if not doc:
        raise HTTPException(status_code=404, detail="HR user not found")
    if resolve_auth_provider(doc) != PROVIDER_MICROSOFT:
        raise HTTPException(
            status_code=400,
            detail="This recruiter account signed in with Google. Connect Google Calendar instead.",
        )
    email = (doc.get("email") or "").strip().lower()
    verifier, challenge = _pkce_pair()
    redirect_uri = _calendar_redirect_uri()
    state = _store_state(
        {"purpose": "calendar", "user_id": user_id, "code_verifier": verifier}
    )
    url = _authorize_url(
        scopes=CALENDAR_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        login_hint=email,
    )
    return url, redirect_uri


def exchange_calendar_code(code: str, state: str) -> str:
    require_microsoft_configured()
    from app.services.hr_calendar_oauth import get_hr_user_by_id

    stored = _pop_state(state, "calendar")
    user_id = str(stored.get("user_id") or "")
    doc = get_hr_user_by_id(user_id)
    if not doc:
        raise HTTPException(status_code=400, detail="Invalid Microsoft calendar session.")
    expected_email = (doc.get("email") or "").strip().lower()

    token_payload = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _calendar_redirect_uri(),
            "code_verifier": stored.get("code_verifier") or "",
            "scope": " ".join(CALENDAR_SCOPES),
        }
    )
    access = token_payload.get("access_token") or ""
    refresh = token_payload.get("refresh_token")
    if not refresh:
        raise HTTPException(
            status_code=400,
            detail=(
                "Microsoft did not return a refresh token. Remove this app from your "
                "Microsoft account permissions and connect Calendar again."
            ),
        )
    profile = graph_get(access, "/me", params={"$select": "id,mail,userPrincipalName"})
    connected_email = _profile_email(profile)
    upn = (profile.get("userPrincipalName") or "").strip().lower()
    if not (emails_match(connected_email, expected_email) or emails_match(upn, expected_email)):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Connect Microsoft Calendar using the same email you signed in with "
                f"({expected_email}). You selected {connected_email or upn or 'a different account'}."
            ),
        )

    expires_in = int(token_payload.get("expires_in") or 3600)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=max(expires_in - 60, 30))
    result = _hr_users().update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "microsoft_calendar_connected": True,
                "microsoft_calendar_access_token": access,
                "microsoft_calendar_refresh_token": refresh,
                "microsoft_calendar_token_expiry": expiry,
                "microsoft_calendar_scopes": CALENDAR_SCOPES,
                "microsoft_calendar_connected_at": datetime.now(timezone.utc),
                "microsoft_oid": profile.get("id"),
            },
            "$unset": {
                "google_calendar_connected": "",
                "google_calendar_access_token": "",
                "google_calendar_refresh_token": "",
                "google_calendar_token_expiry": "",
                "google_calendar_scopes": "",
                "google_calendar_connected_at": "",
                "google_calendar_oauth_code_verifier": "",
            },
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="HR user not found")
    return expected_email


def get_microsoft_access_token(hr_email: str) -> str:
    from app.services.hr_calendar_oauth import CalendarNotConfiguredError, get_hr_user_by_email

    doc = get_hr_user_by_email(hr_email)
    if not doc or not doc.get("microsoft_calendar_refresh_token"):
        raise CalendarNotConfiguredError(
            f"Microsoft Calendar is not connected for {hr_email}. "
            "Connect Calendar in recruiter Settings."
        )

    expiry = doc.get("microsoft_calendar_token_expiry")
    access = doc.get("microsoft_calendar_access_token")
    now = datetime.now(timezone.utc)
    if expiry and access:
        try:
            if getattr(expiry, "tzinfo", None) is None:
                expiry_aware = expiry.replace(tzinfo=timezone.utc)
            else:
                expiry_aware = expiry
            if expiry_aware > now + timedelta(seconds=30):
                return access
        except Exception:
            pass

    token_payload = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": doc.get("microsoft_calendar_refresh_token"),
            "scope": " ".join(CALENDAR_SCOPES),
        }
    )
    access = token_payload.get("access_token") or ""
    refresh = token_payload.get("refresh_token") or doc.get("microsoft_calendar_refresh_token")
    expires_in = int(token_payload.get("expires_in") or 3600)
    expiry = now + timedelta(seconds=max(expires_in - 60, 30))
    _hr_users().update_one(
        {"_id": doc["_id"]},
        {
            "$set": {
                "microsoft_calendar_access_token": access,
                "microsoft_calendar_refresh_token": refresh,
                "microsoft_calendar_token_expiry": expiry,
                "microsoft_calendar_connected": True,
            }
        },
    )
    return access
