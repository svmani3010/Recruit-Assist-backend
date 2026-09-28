"""HR Google Sign-In plus company-onboarding follow-up endpoints.

Mirrors `auth_candidate.py`'s verification flow but diverts corporate emails
into the `hr_users` collection and gates onboarding/status behind a strict
HR-account JWT (see `app.auth.require_hr_account`).
"""

from __future__ import annotations

from typing import Annotated, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth import AuthPrincipal, Role, classify_email, create_access_token, is_candidate_registered_email, require_hr_account, verify_google_id_token
from app.company_branding import propagate_hr_branding_to_jobs
from app.database import get_db
from app.hr_tenant import backfill_hr_job_ownership, propagate_company_name_to_jobs
from app.schemas import GoogleAuthRequest, HrOnboardingRequest
from app.services.calendar_identity import (
    PROVIDER_GOOGLE,
    PROVIDER_MICROSOFT,
    normalize_auth_provider,
    provider_label,
    resolve_auth_provider,
)
from app.settings import get_settings

router = APIRouter(tags=["auth"])


def _hr_users_collection():
    return get_db()["hr_users"]


def find_hr_user_by_email(email: str) -> Optional[dict]:
    doc = _hr_users_collection().find_one({"email": email})
    if doc is None:
        return None
    return {"id": str(doc["_id"])}


def create_hr_user(
    email: str,
    name: Optional[str],
    picture: Optional[str],
    company_domain: str,
    auth_provider: str = PROVIDER_GOOGLE,
    extra: Optional[dict] = None,
) -> str:
    doc = {
        "email": email,
        "name": name,
        "picture": picture,
        "company_domain": company_domain,
        "onboarding_complete": False,
        "auth_provider": auth_provider,
    }
    if extra:
        doc.update(extra)
    result = _hr_users_collection().insert_one(doc)
    return str(result.inserted_id)


def _assert_provider_allowed(existing: Optional[dict], provider: str) -> None:
    if not existing:
        return
    raw = (existing.get("auth_provider") or "").strip().lower()
    if not raw:
        return
    locked = normalize_auth_provider(raw)
    if locked != provider:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    f"This recruiter email is registered with {provider_label(locked)}. "
                    f"Please sign in with {provider_label(locked)}."
                ),
                "redirect": "/recruiters/signup",
            },
        )


def _complete_hr_login(
    *,
    email: str,
    name: Optional[str],
    picture: Optional[str],
    auth_provider: str,
    extra: Optional[dict] = None,
) -> dict:
    email = (email or "").strip().lower()
    if classify_email(email) != "hr":
        raise HTTPException(
            status_code=400,
            detail={
                "message": "That looks like a personal email. Please use the candidate login instead.",
                "redirect": "/login",
            },
        )
    if is_candidate_registered_email(email):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "This email is registered as a candidate account. Please use Candidate Login instead.",
                "redirect": "/login",
            },
        )

    collection = _hr_users_collection()
    existing = collection.find_one({"email": {"$regex": f"^{email}$", "$options": "i"}})
    _assert_provider_allowed(existing, auth_provider)
    is_new_user = existing is None
    if is_new_user:
        user_id = create_hr_user(
            email=email,
            name=name,
            picture=picture,
            company_domain=email.split("@")[1],
            auth_provider=auth_provider,
            extra=extra,
        )
    else:
        user_id = str(existing["_id"])
        updates = {
            "name": name or existing.get("name"),
            "email": email,
        }
        if not (existing.get("auth_provider") or "").strip():
            updates["auth_provider"] = auth_provider
        if picture:
            updates["picture"] = picture
        if extra:
            updates.update(extra)
        collection.update_one({"_id": existing["_id"]}, {"$set": updates})

    app_token = create_access_token(subject=user_id, role=Role.HR)
    stored_provider = auth_provider
    if existing and (existing.get("auth_provider") or "").strip():
        stored_provider = normalize_auth_provider(existing.get("auth_provider"))

    rbac_meta: dict = {}
    from app.settings import get_settings as _get_settings
    import logging

    _log = logging.getLogger(__name__)

    if getattr(_get_settings(), "rbac_org_enabled", False):
        from app.rbac.bootstrap import resolve_org_on_login
        from app.rbac.indexes import ensure_rbac_indexes
        from app.rbac.seed_matrix import seed_permission_matrix
        from app.rbac.session_store import get_session_store

        try:
            ensure_rbac_indexes()
            seed_permission_matrix()
        except Exception:
            _log.exception("RBAC index/seed failed during HR login")

        try:
            boot = resolve_org_on_login(
                user_id=user_id,
                email=email,
                display_name=name,
            )
            _log.info(
                "RBAC login bootstrap email=%s org_id=%s role=%s new_org=%s pending=%s review=%s msg=%s",
                email,
                boot.org_id,
                boot.role_id,
                boot.is_new_org,
                boot.membership_status,
                boot.requires_org_review,
                boot.message,
            )
        except Exception as exc:
            _log.exception("RBAC resolve_org_on_login failed for %s", email)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create/join organization: {exc}",
            ) from exc

        # Stamp organization_id on hr_users for dual-path convenience.
        if boot.org_id:
            collection.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"organization_id": boot.org_id}},
            )
        app_token = get_session_store().create(
            user_id=user_id,
            org_id=boot.org_id,
            membership_status=boot.membership_status,
            email=email,
        )
        rbac_meta = {
            "org_id": boot.org_id,
            "membership_status": boot.membership_status,
            "role_id": boot.role_id,
            "is_new_org": boot.is_new_org,
            "requires_org_review": boot.requires_org_review,
            "domain": boot.domain,
            "rbac_message": boot.message,
            "organization_created": bool(boot.org_id and boot.is_new_org),
        }

    user_payload = {
        "name": name,
        "email": email,
        "picture": picture,
        "auth_provider": stored_provider,
    }
    if rbac_meta:
        user_payload.update(rbac_meta)

    return {
        "token": app_token,
        "is_new_user": is_new_user,
        "user": user_payload,
    }


@router.post("/auth/hr/google")
async def hr_google_login(payload: GoogleAuthRequest):
    try:
        decoded = verify_google_id_token(payload.credential)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token.")

    email = decoded.get("email")
    email_verified = decoded.get("email_verified", False)
    if not email or not email_verified:
        raise HTTPException(status_code=401, detail="Google email not verified.")

    if classify_email(email) != "hr":
        raise HTTPException(
            status_code=400,
            detail={
                "message": "That looks like a personal email. Please use the candidate login instead.",
                "redirect": "/login",
            },
        )

    if is_candidate_registered_email(email):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "This email is registered as a candidate account. Please use Candidate Login instead.",
                "redirect": "/login",
            },
        )

    existing_user = find_hr_user_by_email(email)
    existing_doc = None
    if existing_user:
        existing_doc = _hr_users_collection().find_one({"_id": ObjectId(existing_user["id"])})
    _assert_provider_allowed(existing_doc, PROVIDER_GOOGLE)

    session = _complete_hr_login(
        email=email,
        name=decoded.get("name"),
        picture=decoded.get("picture"),
        auth_provider=PROVIDER_GOOGLE,
    )

    return {
        "token": session["token"],
        "is_new_user": session["is_new_user"],
        "user": session["user"],
    }


class MicrosoftLoginCompleteRequest(BaseModel):
    code: str


@router.get("/auth/hr/microsoft/url")
async def hr_microsoft_login_url():
    from app.services.hr_microsoft_oauth import build_login_authorization_url

    url, redirect_uri = build_login_authorization_url()
    return {"url": url, "redirect_uri": redirect_uri, "provider": PROVIDER_MICROSOFT}


@router.get("/auth/hr/microsoft/callback")
async def hr_microsoft_login_callback(code: str = "", state: str = "", error: str = ""):
    settings = get_settings()
    frontend = (settings.frontend_url or "").rstrip("/")
    if error or not code:
        return RedirectResponse(url=f"{frontend}/recruiters/signup?ms_login=error")
    try:
        from app.services.hr_microsoft_oauth import exchange_login_code, issue_login_code

        profile = exchange_login_code(code, state)
        session = _complete_hr_login(
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
        from urllib.parse import quote

        return RedirectResponse(
            url=f"{frontend}/recruiters/signup?ms_login=error&detail={quote(str(message)[:180])}"
        )
    except Exception:
        return RedirectResponse(url=f"{frontend}/recruiters/signup?ms_login=error")
    return RedirectResponse(url=f"{frontend}/recruiters/auth/callback?code={one_time}")


@router.post("/auth/hr/microsoft/complete")
async def hr_microsoft_login_complete(payload: MicrosoftLoginCompleteRequest):
    from app.services.hr_microsoft_oauth import consume_login_code

    session = consume_login_code(payload.code)
    return {
        "token": session.get("token"),
        "is_new_user": bool(session.get("is_new_user")),
        "user": session.get("user") or {},
    }


@router.post("/auth/hr/onboarding")
async def submit_onboarding(
    payload: HrOnboardingRequest,
    principal: Annotated[AuthPrincipal, Depends(require_hr_account)],
):
    existing = _hr_users_collection().find_one({"_id": ObjectId(principal.subject)})
    if existing and str(existing.get("companyName") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Company profile is already set and cannot be submitted again.",
        )

    company_name = str(payload.companyName).strip()
    _hr_users_collection().update_one(
        {"_id": ObjectId(principal.subject)},
        {
            "$set": {
                **payload.model_dump(),
                "onboarding_complete": True,
                "company_name_locked": True,
                "companyName": company_name,
            }
        },
    )
    db = get_db()
    backfill_hr_job_ownership(principal.subject, db)
    propagate_company_name_to_jobs(principal.subject, company_name, db)
    propagate_hr_branding_to_jobs(
        principal.subject,
        db,
        logo=payload.companyLogo,
        banner=payload.companyBanner,
    )

    org_payload: dict = {}
    from app.settings import get_settings as _get_settings
    import logging

    _log = logging.getLogger(__name__)

    if getattr(_get_settings(), "rbac_org_enabled", False):
        from app.rbac.bootstrap import ensure_company_org_bucket
        from app.rbac.indexes import ensure_rbac_indexes
        from app.rbac.seed_matrix import seed_permission_matrix
        from app.rbac.org_service import is_consumer_domain, extract_domain

        try:
            ensure_rbac_indexes()
            seed_permission_matrix()
        except Exception:
            _log.exception("RBAC index/seed failed during onboarding")

        email = ""
        if existing:
            email = (existing.get("email") or "").strip().lower()
        if not email:
            doc = _hr_users_collection().find_one({"_id": ObjectId(principal.subject)})
            email = ((doc or {}).get("email") or "").strip().lower()
        if not email:
            raise HTTPException(
                status_code=400,
                detail="HR account has no email; cannot create organization bucket.",
            )

        try:
            boot = ensure_company_org_bucket(
                user_id=principal.subject,
                email=email,
                company_name=company_name,
            )
        except Exception as exc:
            _log.exception("ensure_company_org_bucket failed for %s", email)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create organization bucket: {exc}",
            ) from exc

        _log.info(
            "RBAC onboarding org email=%s org_id=%s role=%s new_org=%s",
            email,
            boot.org_id,
            boot.role_id,
            boot.is_new_org,
        )

        if not boot.org_id and not is_consumer_domain(extract_domain(email)):
            raise HTTPException(
                status_code=500,
                detail=(
                    "Organization bucket was not created. "
                    f"Bootstrap message: {boot.message}"
                ),
            )

        from app.rbac.session_store import get_session_store

        if getattr(principal, "rbac_session_id", None):
            get_session_store().delete(principal.rbac_session_id)
        new_sid = get_session_store().create(
            user_id=principal.subject,
            org_id=boot.org_id,
            membership_status=boot.membership_status,
            email=email,
        )
        org_payload["session_token"] = new_sid

        org_payload.update(
            {
                "org_id": boot.org_id,
                "membership_status": boot.membership_status,
                "role_id": boot.role_id,
                "is_new_org": boot.is_new_org,
                "organization_created": bool(boot.org_id),
                "rbac_message": boot.message,
                "domain": boot.domain,
            }
        )

    return {"success": True, **org_payload}


@router.get("/auth/hr/status")
async def hr_status(principal: Annotated[AuthPrincipal, Depends(require_hr_account)]):
    """Called on HR page load/refresh so onboarding state is never trusted
    to a client-only flag.

    When RBAC is on, also backfills the organization bucket for users who
    onboarded before org bootstrap existed (or who still hold a legacy JWT),
    and returns ``session_token`` so the client can upgrade to an opaque
    RBAC session.
    """
    import logging

    from app.settings import get_settings as _get_settings

    _log = logging.getLogger(__name__)
    doc = _hr_users_collection().find_one({"_id": ObjectId(principal.subject)})
    if doc is None:
        raise HTTPException(status_code=404, detail="User not found.")

    email = (doc.get("email") or "").strip().lower()
    response: dict = {
        "onboarding_complete": doc.get("onboarding_complete", False),
        "auth_provider": resolve_auth_provider(doc),
        "email": email,
        "organization_id": doc.get("organization_id"),
        "companyName": doc.get("companyName"),
    }

    if not getattr(_get_settings(), "rbac_org_enabled", False):
        return response

    from app.rbac.bootstrap import ensure_company_org_bucket
    from app.rbac.org_service import get_membership
    from app.rbac.roles import get_module_role
    from app.rbac import constants as C
    from app.rbac.session_store import get_session_store

    company_name = str(doc.get("companyName") or "").strip()
    needs_org = not (doc.get("organization_id") or principal.org_id)
    boot = None
    if needs_org and email:
        try:
            boot = ensure_company_org_bucket(
                user_id=principal.subject,
                email=email,
                company_name=company_name or email.split("@")[-1],
            )
            _log.info(
                "RBAC status backfill email=%s org_id=%s role=%s new_org=%s msg=%s",
                email,
                boot.org_id,
                boot.role_id,
                boot.is_new_org,
                boot.message,
            )
        except Exception:
            _log.exception("RBAC status org backfill failed for %s", email)
            raise HTTPException(
                status_code=500,
                detail="Failed to create organization for this account.",
            )

    org_id = (
        (boot.org_id if boot else None)
        or doc.get("organization_id")
        or principal.org_id
    )
    # Always prefer live membership/role so Super Admin grants unlock without
    # waiting for a full re-login (session snapshot can stay "pending").
    membership_status = boot.membership_status if boot else None
    role_id = boot.role_id if boot else None
    if org_id:
        membership = get_membership(principal.subject, str(org_id))
        if membership and membership.get("status"):
            membership_status = membership.get("status")
        if not membership_status:
            membership_status = principal.membership_status
        live_role = get_module_role(
            principal.subject, str(org_id), C.MODULE_RECRUITMENT
        )
        if live_role:
            role_id = live_role
        # Pre-assigned role ⇒ treat as active even if membership row lagged.
        if role_id and membership_status == C.MEMBERSHIP_PENDING:
            from app.rbac.org_service import upsert_membership

            upsert_membership(
                user_id=principal.subject,
                org_id=str(org_id),
                email=email,
                status=C.MEMBERSHIP_ACTIVE,
            )
            membership_status = C.MEMBERSHIP_ACTIVE

    response.update(
        {
            "organization_id": org_id,
            "org_id": org_id,
            "membership_status": membership_status,
            "role_id": role_id,
            "is_new_org": bool(boot.is_new_org) if boot else False,
            "requires_org_review": bool(boot.requires_org_review)
            if boot
            else False,
            "domain": (boot.domain if boot else None)
            or (email.split("@")[-1] if email else None),
            "rbac_message": boot.message if boot else None,
            "organization_created": bool(boot and boot.org_id and boot.is_new_org),
            "rbac_enabled": True,
        }
    )

    # Upgrade legacy JWT → opaque session, or refresh only when org changes.
    # Do not re-issue on every membership string drift — that caused the
    # frontend session guard to loop on "Checking your session…".
    store = get_session_store()
    org_drift = (
        boot is not None
        and boot.org_id
        and str(boot.org_id) != str(principal.org_id or "")
    )
    status_drift = (
        principal.rbac_session_id
        and membership_status
        and str(membership_status) != str(principal.membership_status or "")
        and (
            # Only rotate when moving out of pending (role grant unlock).
            str(principal.membership_status or "") == C.MEMBERSHIP_PENDING
            or str(membership_status) == C.MEMBERSHIP_PENDING
        )
    )
    must_issue = (not principal.rbac_session_id) or org_drift or status_drift
    if must_issue:
        if principal.rbac_session_id:
            store.delete(principal.rbac_session_id)
        response["session_token"] = store.create(
            user_id=principal.subject,
            org_id=org_id,
            membership_status=membership_status,
            email=email,
        )

    return response
