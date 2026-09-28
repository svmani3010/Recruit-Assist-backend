"""HR Google Calendar OAuth — store refresh tokens on hr_users."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.database import get_db
from app.settings import get_settings
from app.timezone_utils import as_naive_utc

logger = logging.getLogger(__name__)

CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
]


def _prepare_oauthlib_env() -> None:
    """Local HTTP callbacks + extra Google scopes (openid/email/profile) are expected."""
    import os

    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def _oauth_client_config():
    settings = get_settings()
    client_id = (settings.google_client_id or "").strip()
    client_secret = (settings.google_client_secret or "").strip().strip('"').strip("'")
    redirect_uri = (settings.google_oauth_redirect_uri or "").strip().rstrip("/")
    if not client_secret or client_secret.startswith("CHANGE_ME"):
        raise HTTPException(
            status_code=503,
            detail=(
                "GOOGLE_CLIENT_SECRET is missing or invalid. Copy the Client secret "
                "from the same Web OAuth client in Google Cloud Console into .env."
            ),
        )
    if not redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_OAUTH_REDIRECT_URI is not configured on the server.",
        )
    # Production must never advertise a localhost callback — Google returns
    # Error 400: redirect_uri_mismatch when the URI is not an authorized
    # https callback for this Web client.
    env = (settings.environment or "").strip().lower()
    if env not in {"development", "dev", "local", "test"} and (
        "localhost" in redirect_uri or "127.0.0.1" in redirect_uri
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "GOOGLE_OAUTH_REDIRECT_URI points at localhost but ENVIRONMENT="
                f"{settings.environment}. Set it to the public backend callback "
                "(e.g. https://<backend>/api/calendar/oauth/callback) and register "
                "that exact URI under Authorized redirect URIs in Google Cloud Console."
            ),
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }, redirect_uri


class CalendarNotConfiguredError(RuntimeError):
    """HR has not connected Google Calendar."""


def _hr_users():
    return get_db()["hr_users"]


def get_hr_user_by_id(user_id: str) -> Optional[dict]:
    try:
        return _hr_users().find_one({"_id": ObjectId(user_id)})
    except Exception:
        return None


def get_hr_email_for_user_id(user_id: str) -> Optional[str]:
    doc = get_hr_user_by_id(user_id)
    if not doc:
        return None
    return (doc.get("email") or "").strip().lower() or None


def get_hr_user_by_email(email: str) -> Optional[dict]:
    if not email:
        return None
    return _hr_users().find_one({"email": {"$regex": f"^{email}$", "$options": "i"}})


def _store_pkce_verifier(user_id: str, code_verifier: str) -> None:
    """Stash the PKCE code_verifier generated at authorization-url time so the
    callback (a separate request/Flow instance) can retrieve it before
    exchanging the code. Cleared immediately after a successful exchange."""
    _hr_users().update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"google_calendar_oauth_code_verifier": code_verifier}},
    )


def _pop_pkce_verifier(user_id: str) -> Optional[str]:
    doc = get_hr_user_by_id(user_id) or {}
    code_verifier = doc.get("google_calendar_oauth_code_verifier")
    if code_verifier:
        _hr_users().update_one(
            {"_id": ObjectId(user_id)},
            {"$unset": {"google_calendar_oauth_code_verifier": ""}},
        )
    return code_verifier


def is_hr_calendar_connected(hr_email: str) -> bool:
    doc = get_hr_user_by_email(hr_email)
    if not doc:
        return False
    from app.services.calendar_identity import PROVIDER_MICROSOFT, resolve_auth_provider

    if resolve_auth_provider(doc) == PROVIDER_MICROSOFT:
        return bool(doc.get("microsoft_calendar_refresh_token"))
    return bool(doc.get("google_calendar_refresh_token"))


def store_hr_calendar_tokens(user_id: str, token_payload: Dict[str, Any]) -> str:
    refresh = token_payload.get("refresh_token")
    if not refresh:
        logger.warning("Google OAuth returned no refresh_token for HR user %s", user_id)

    existing = get_hr_user_by_id(user_id) or {}
    preserved_refresh = existing.get("google_calendar_refresh_token")

    update = {
        "google_calendar_connected": True,
        "google_calendar_access_token": token_payload.get("access_token"),
        "google_calendar_token_expiry": token_payload.get("expiry"),
        "google_calendar_scopes": token_payload.get("scopes") or CALENDAR_SCOPES,
        "google_calendar_connected_at": datetime.now(timezone.utc),
        "google_calendar_refresh_token": refresh or preserved_refresh,
    }
    if not update["google_calendar_refresh_token"]:
        raise HTTPException(
            status_code=400,
            detail="Google did not return a refresh token. Remove app access in Google Account and reconnect.",
        )

    result = _hr_users().update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": update,
            "$unset": {
                "microsoft_calendar_connected": "",
                "microsoft_calendar_access_token": "",
                "microsoft_calendar_refresh_token": "",
                "microsoft_calendar_token_expiry": "",
                "microsoft_calendar_scopes": "",
                "microsoft_calendar_connected_at": "",
            },
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="HR user not found")

    doc = get_hr_user_by_id(user_id)
    return (doc.get("email") or "").strip().lower()


def build_oauth_authorization_url(user_id: str) -> str:
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Calendar connect is unavailable because google-auth-oauthlib "
                "is not installed on the server. Run: pip install google-auth-oauthlib"
            ),
        ) from exc

    from app.services.calendar_identity import PROVIDER_GOOGLE, resolve_auth_provider

    doc = get_hr_user_by_id(user_id)
    if not doc:
        raise HTTPException(status_code=404, detail="HR user not found")
    if resolve_auth_provider(doc) != PROVIDER_GOOGLE:
        raise HTTPException(
            status_code=400,
            detail="This recruiter account signed in with Microsoft. Connect Microsoft Calendar instead.",
        )

    _prepare_oauthlib_env()
    client_config, redirect_uri = _oauth_client_config()

    flow = Flow.from_client_config(
        client_config,
        scopes=CALENDAR_SCOPES,
        redirect_uri=redirect_uri,
    )
    hr_email = (doc.get("email") or "").strip().lower()
    auth_kwargs = {
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "select_account consent",
        "state": user_id,
    }
    if hr_email:
        auth_kwargs["login_hint"] = hr_email
    auth_url, _ = flow.authorization_url(**auth_kwargs)
    # google-auth-oauthlib auto-generates a PKCE code_verifier on this Flow
    # instance; it must be replayed on the *next* request's Flow instance
    # (the callback) or fetch_token() will fail with invalid_grant.
    if getattr(flow, "code_verifier", None):
        _store_pkce_verifier(user_id, flow.code_verifier)
    logger.info("Calendar OAuth URL built with redirect_uri=%s", redirect_uri)
    return auth_url, redirect_uri


def exchange_oauth_code(code: str, user_id: str) -> str:
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Calendar connect is unavailable because google-auth-oauthlib "
                "is not installed on the server. Run: pip install google-auth-oauthlib"
            ),
        ) from exc

    try:
        ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    existing = get_hr_user_by_id(user_id)
    if not existing:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")
    from app.services.calendar_identity import PROVIDER_GOOGLE, resolve_auth_provider

    if resolve_auth_provider(existing) != PROVIDER_GOOGLE:
        raise HTTPException(
            status_code=400,
            detail="This recruiter account signed in with Microsoft. Connect Microsoft Calendar instead.",
        )

    _prepare_oauthlib_env()
    client_config, redirect_uri = _oauth_client_config()

    flow = Flow.from_client_config(
        client_config,
        scopes=CALENDAR_SCOPES,
        redirect_uri=redirect_uri,
        state=user_id,
    )

    code_verifier = _pop_pkce_verifier(user_id)
    if not code_verifier:
        raise HTTPException(
            status_code=400,
            detail=(
                "OAuth session expired or was already used (no matching PKCE "
                "code_verifier for this state). Restart the Connect Calendar flow."
            ),
        )
    flow.code_verifier = code_verifier

    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        message = str(exc).lower()
        if "invalid_client" in message or "client secret" in message:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Google rejected GOOGLE_CLIENT_SECRET. Open the same Web client "
                    "in Google Cloud Console, copy the current Client secret into "
                    ".env as GOOGLE_CLIENT_SECRET, then restart the API."
                ),
            ) from exc
        raise

    credentials = flow.credentials
    expected_email = ((get_hr_user_by_id(user_id) or {}).get("email") or "").strip().lower()
    connected_email = _google_oauth_email(credentials.token)
    from app.services.calendar_identity import emails_match

    if expected_email and connected_email and not emails_match(expected_email, connected_email):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Connect Google Calendar using the same email you signed in with "
                f"({expected_email}). You selected {connected_email}."
            ),
        )

    payload = {
        "refresh_token": credentials.refresh_token,
        "access_token": credentials.token,
        "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
        "scopes": list(credentials.scopes or CALENDAR_SCOPES),
    }
    return store_hr_calendar_tokens(user_id, payload)


def _google_oauth_email(access_token: str) -> str:
    if not access_token:
        return ""
    try:
        import httpx

        response = httpx.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )
        if response.status_code >= 400:
            return ""
        data = response.json()
        return (data.get("email") or "").strip().lower()
    except Exception:
        logger.warning("Could not read Google account email after calendar OAuth")
        return ""


def get_hr_calendar_credentials(hr_email: str):
    """Return refreshed Google Credentials for an HR user's connected calendar."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    settings = get_settings()
    doc = get_hr_user_by_email(hr_email)
    if not doc or not doc.get("google_calendar_refresh_token"):
        raise CalendarNotConfiguredError(
            f"Google Calendar is not connected for {hr_email}. "
            "Connect Calendar in recruiter Settings."
        )

    expiry = None
    expiry_raw = doc.get("google_calendar_token_expiry")
    if expiry_raw:
        try:
            expiry = datetime.fromisoformat(str(expiry_raw))
            # google-auth compares expiry to a naive UTC timestamp
            expiry = as_naive_utc(expiry)
        except ValueError:
            expiry = None

    creds = Credentials(
        token=doc.get("google_calendar_access_token"),
        refresh_token=doc.get("google_calendar_refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=doc.get("google_calendar_scopes") or CALENDAR_SCOPES,
        expiry=expiry,
    )

    if creds.expired or not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as exc:
            from app.services.google_calendar import CalendarAccessError

            message = str(exc).lower()
            if "invalid_client" in message or "client secret" in message:
                raise CalendarAccessError(
                    "invalid_client: The provided client secret is invalid. "
                    "Update GOOGLE_CLIENT_SECRET in .env to match the Google Cloud "
                    "OAuth Web client, restart the API, and reconnect Calendar in Settings."
                ) from exc
            if "unauthorized_client" in message:
                raise CalendarAccessError(
                    "unauthorized_client: Stored Google refresh token was not issued "
                    "for the current GOOGLE_CLIENT_ID. Reconnect Calendar in Settings "
                    "after confirming .env matches the same OAuth Web client."
                ) from exc
            if "invalid_grant" in message:
                raise CalendarNotConfiguredError(
                    f"Google Calendar access expired for {hr_email}. "
                    "Reconnect Calendar in recruiter Settings."
                ) from exc
            raise CalendarAccessError(f"Google Calendar token refresh failed: {exc}") from exc
        _hr_users().update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "google_calendar_access_token": creds.token,
                    "google_calendar_token_expiry": (
                        creds.expiry.isoformat() if creds.expiry else None
                    ),
                    "google_calendar_connected": True,
                }
            },
        )

    return creds