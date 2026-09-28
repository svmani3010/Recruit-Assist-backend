"""Candidate Google + Microsoft Sign-In: verifies identity, diverts personal
vs. corporate emails to the correct account type, and issues an app JWT.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.auth import Role, classify_email, create_access_token, is_hr_registered_email, verify_google_id_token
from app.database import get_db
from app.helpers import resolve_profile_picture
from app.schemas import GoogleAuthRequest
from app.services.calendar_identity import PROVIDER_MICROSOFT
from app.settings import get_settings

router = APIRouter(tags=["auth"])


def _candidates_collection():
    return get_db()["candidates"]


def find_candidate_by_email(email: str) -> Optional[dict]:
    doc = _candidates_collection().find_one({"email": email})
    if doc is None:
        return None
    onboarding_completed = bool(doc.get("onboardingCompleted", False))
    return {
        "id": str(doc["_id"]),
        "onboardingCompleted": onboarding_completed,
        "profileState": (
            "PROFILE_COMPLETE"
            if onboarding_completed
            else (doc.get("profileState") or "PROFILE_INCOMPLETE")
        ),
        "profileSetupStep": doc.get("profileSetupStep") or 1,
        "profile_picture": resolve_profile_picture(doc),
        "name": doc.get("name"),
    }


def create_candidate_user(
    email: str,
    name: Optional[str],
    picture: Optional[str],
    *,
    auth_provider: str = "google",
    extra: Optional[dict[str, Any]] = None,
) -> str:
    doc: dict[str, Any] = {
        "email": email,
        "name": name,
        "picture": picture,
        "onboardingCompleted": False,
        "profileState": "PROFILE_INCOMPLETE",
        "profileSetupStep": 1,
        "mobile_verified": False,
        "whatsappOptIn": False,
        "auth_provider": auth_provider,
    }
    if extra:
        doc.update(extra)
    result = _candidates_collection().insert_one(doc)
    return str(result.inserted_id)


def _assert_candidate_portal_email(email: str) -> None:
    if classify_email(email) != "candidate":
        raise HTTPException(
            status_code=400,
            detail={
                "message": "That looks like a company email. Please use Recruiters Login instead.",
                "redirect": "/recruiters/signup",
            },
        )

    if is_hr_registered_email(email):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "This email is registered as a recruiter account. Please use Recruiters Login instead.",
                "redirect": "/recruiters/signup",
            },
        )


def _complete_candidate_login(
    *,
    email: str,
    name: Optional[str],
    picture: Optional[str],
    auth_provider: str,
    extra: Optional[dict[str, Any]] = None,
) -> dict:
    _assert_candidate_portal_email(email)

    existing_user = find_candidate_by_email(email)
    is_new_user = existing_user is None
    stored_picture = None
    if is_new_user:
        user_id = create_candidate_user(
            email=email,
            name=name,
            picture=picture,
            auth_provider=auth_provider,
            extra=extra,
        )
        onboarding_completed = False
        profile_state = "PROFILE_INCOMPLETE"
        profile_setup_step = 1
        display_name = name
    else:
        user_id = existing_user["id"]
        onboarding_completed = existing_user["onboardingCompleted"]
        profile_state = existing_user.get("profileState") or (
            "PROFILE_COMPLETE" if onboarding_completed else "PROFILE_INCOMPLETE"
        )
        profile_setup_step = int(existing_user.get("profileSetupStep") or 1)
        stored_picture = existing_user.get("profile_picture")
        display_name = name or existing_user.get("name")
        updates: dict[str, Any] = {"auth_provider": auth_provider}
        if extra:
            updates.update({k: v for k, v in extra.items() if v is not None})
        if name:
            updates["name"] = name
        _candidates_collection().update_one({"email": email}, {"$set": updates})

    app_token = create_access_token(subject=user_id, role=Role.CANDIDATE, candidate_id=user_id)
    profile_picture = stored_picture

    return {
        "token": app_token,
        "is_new_user": is_new_user,
        "user": {
            "id": user_id,
            "name": display_name,
            "email": email,
            "picture": picture,
            "profilePicture": profile_picture,
            "profile_picture": profile_picture,
            "onboardingCompleted": onboarding_completed,
            "profileState": profile_state,
            "profileSetupStep": profile_setup_step,
            "auth_provider": auth_provider,
        },
    }


@router.post("/auth/candidate/google")
async def candidate_google_login(payload: GoogleAuthRequest):
    try:
        decoded = verify_google_id_token(payload.credential)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token.")

    email = decoded.get("email")
    email_verified = decoded.get("email_verified", False)
    if not email or not email_verified:
        raise HTTPException(status_code=401, detail="Google email not verified.")

    return _complete_candidate_login(
        email=email,
        name=decoded.get("name"),
        picture=decoded.get("picture"),
        auth_provider="google",
    )


class MicrosoftLoginCompleteRequest(BaseModel):
    code: str = Field(min_length=8)


@router.get("/auth/candidate/microsoft/url")
async def candidate_microsoft_login_url():
    from app.services.hr_microsoft_oauth import build_candidate_login_authorization_url

    url, redirect_uri = build_candidate_login_authorization_url()
    return {"url": url, "redirect_uri": redirect_uri, "provider": PROVIDER_MICROSOFT}


@router.get("/auth/candidate/microsoft/callback")
async def candidate_microsoft_login_callback(
    code: str = "", state: str = "", error: str = ""
):
    settings = get_settings()
    frontend = (settings.frontend_url or "").rstrip("/")
    login_path = f"{frontend}/login"
    if error or not code:
        return RedirectResponse(url=f"{login_path}?ms_login=error")
    try:
        from app.services.hr_microsoft_oauth import (
            exchange_candidate_login_code,
            issue_login_code,
        )

        profile = exchange_candidate_login_code(code, state)
        session = _complete_candidate_login(
            email=profile["email"],
            name=profile.get("name"),
            picture=profile.get("picture"),
            auth_provider=PROVIDER_MICROSOFT,
            extra={
                "microsoft_oid": profile.get("microsoft_oid"),
                "microsoft_upn": profile.get("microsoft_upn"),
            },
        )
        one_time = issue_login_code(session)
    except HTTPException as exc:
        message = exc.detail
        if isinstance(message, dict):
            message = message.get("message") or "Microsoft sign-in failed."
        return RedirectResponse(
            url=f"{login_path}?ms_login=error&detail={quote(str(message)[:180])}"
        )
    except Exception:
        return RedirectResponse(url=f"{login_path}?ms_login=error")
    return RedirectResponse(url=f"{frontend}/auth/callback/microsoft?code={one_time}")


@router.post("/auth/candidate/microsoft/complete")
async def candidate_microsoft_login_complete(payload: MicrosoftLoginCompleteRequest):
    from app.services.hr_microsoft_oauth import consume_login_code

    session = consume_login_code(payload.code)
    return {
        "token": session.get("token"),
        "is_new_user": bool(session.get("is_new_user")),
        "user": session.get("user") or {},
    }
