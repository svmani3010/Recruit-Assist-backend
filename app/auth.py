"""API key and JWT authentication / authorization."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from jose import JWTError, jwt
from pydantic import BaseModel

from app.database import get_db
from app.settings import get_settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)

# Domains treated as personal (candidate) email providers; anything else is
# classified as a corporate/HR domain. Used to route Google Sign-In users to
# the correct account type (candidate vs. HR) at login time.
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "protonmail.com", "aol.com", "live.com", "yandex.com", "rediffmail.com",
}


def _email_query(email: str) -> dict:
    normalized = email.strip()
    return {"email": {"$regex": f"^{re.escape(normalized)}$", "$options": "i"}}


def is_hr_registered_email(email: str) -> bool:
    """True when this email already belongs to a recruiter/HR account."""
    if not email or not email.strip():
        return False
    return get_db()["hr_users"].find_one(_email_query(email)) is not None


def is_candidate_registered_email(email: str) -> bool:
    """True when this email already belongs to a candidate account."""
    if not email or not email.strip():
        return False
    return get_db()["candidates"].find_one(_email_query(email)) is not None


def classify_email(email: str) -> str:
    """Returns "candidate" for personal email domains, "hr" otherwise."""
    domain = email.strip().lower().split("@")[-1]
    return "candidate" if domain in PERSONAL_EMAIL_DOMAINS else "hr"


def verify_google_id_token(credential: str) -> dict:
    """Verify a Google Identity Services ID token against configured audiences.

    GIS issues tokens for VITE_GOOGLE_CLIENT_ID, which can differ from the
    Calendar OAuth web client in GOOGLE_CLIENT_ID. Accept either.
    """
    if not credential or not str(credential).strip():
        raise ValueError("Missing Google credential.")

    audiences = get_settings().google_id_token_audiences
    if not audiences:
        raise ValueError("No Google client IDs configured.")

    last_error: Exception | None = None
    for audience in audiences:
        try:
            return id_token.verify_oauth2_token(
                credential,
                google_requests.Request(),
                audience,
                clock_skew_in_seconds=10,
            )
        except ValueError as exc:
            last_error = exc
    raise ValueError(str(last_error) if last_error else "Invalid Google token.")


class Role(str, Enum):
    HR = "hr"
    CANDIDATE = "candidate"
    SERVICE = "service"


class AuthPrincipal(BaseModel):
    subject: str
    role: Role
    candidate_id: Optional[str] = None
    job_id: Optional[str] = None
    # Populated when RBAC_ORG_ENABLED and bearer is an opaque session.
    org_id: Optional[str] = None
    membership_status: Optional[str] = None
    rbac_session_id: Optional[str] = None


def create_access_token(
    subject: str,
    role: Role,
    candidate_id: Optional[str] = None,
    job_id: Optional[str] = None,
    expires_minutes: Optional[int] = None,
) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.jwt_expire_minutes
    )
    payload = {
        "sub": subject,
        "role": role.value,
        "exp": expire,
    }
    if candidate_id:
        payload["candidate_id"] = candidate_id
    if job_id:
        payload["job_id"] = job_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_candidate_token(candidate_id: str, job_id: str) -> str:
    """Short-lived token issued after resume upload for candidate-scoped APIs."""
    settings = get_settings()
    return create_access_token(
        subject=f"candidate:{candidate_id}",
        role=Role.CANDIDATE,
        candidate_id=candidate_id,
        job_id=job_id,
        expires_minutes=min(settings.jwt_expire_minutes, 720),  # max 12h
    )


def _principal_from_api_key(api_key: Optional[str]) -> Optional[AuthPrincipal]:
    settings = get_settings()
    if api_key and api_key == settings.api_key:
        return AuthPrincipal(subject="api-key", role=Role.SERVICE)
    return None


def _principal_from_jwt(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[AuthPrincipal]:
    if not credentials:
        return None
    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        role = Role(payload.get("role", Role.CANDIDATE.value))
        principal = AuthPrincipal(
            subject=str(payload.get("sub", "")),
            role=role,
            candidate_id=payload.get("candidate_id"),
            job_id=payload.get("job_id"),
        )
        # When RBAC is on, attach live org/membership so handlers + matrix
        # checks cannot be bypassed by a bare JWT without opaque session.
        if (
            role == Role.HR
            and getattr(settings, "rbac_org_enabled", False)
            and principal.subject
        ):
            try:
                from bson import ObjectId

                from app.database import get_db
                from app.rbac.org_service import get_membership

                doc = get_db()["hr_users"].find_one(
                    {"_id": ObjectId(principal.subject)},
                    {"organization_id": 1, "org_id": 1},
                )
                org_id = (doc or {}).get("organization_id") or (doc or {}).get(
                    "org_id"
                )
                if org_id:
                    principal.org_id = str(org_id)
                    membership = get_membership(principal.subject, str(org_id))
                    if membership:
                        principal.membership_status = membership.get("status")
            except Exception:
                pass
        return principal
    except (JWTError, ValueError):
        return None


def _principal_from_rbac_session(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[AuthPrincipal]:
    """Resolve opaque session when RBAC_ORG_ENABLED (dual-path)."""
    settings = get_settings()
    if not getattr(settings, "rbac_org_enabled", False):
        return None
    if not credentials or not credentials.credentials:
        return None
    token = credentials.credentials.strip()
    # Skip JWT-shaped tokens — leave those to _principal_from_jwt.
    if token.count(".") == 2:
        return None
    try:
        from app.rbac.session_store import get_session_store

        payload = get_session_store().get(token)
    except Exception:
        return None
    if not payload or not payload.get("user_id"):
        return None
    return AuthPrincipal(
        subject=str(payload["user_id"]),
        role=Role.HR,
        org_id=str(payload["org_id"]) if payload.get("org_id") else None,
        membership_status=payload.get("membership_status"),
        rbac_session_id=token,
    )


async def get_current_principal(
    api_key: Annotated[Optional[str], Security(api_key_header)] = None,
    credentials: Annotated[
        Optional[HTTPAuthorizationCredentials], Security(bearer_scheme)
    ] = None,
) -> AuthPrincipal:
    principal = (
        _principal_from_api_key(api_key)
        or _principal_from_rbac_session(credentials)
        or _principal_from_jwt(credentials)
    )
    if not principal:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def get_current_principal_optional(
    api_key: Annotated[Optional[str], Security(api_key_header)] = None,
    credentials: Annotated[
        Optional[HTTPAuthorizationCredentials], Security(bearer_scheme)
    ] = None,
) -> Optional[AuthPrincipal]:
    """Returns principal if valid credentials exist, otherwise returns None."""
    return (
        _principal_from_api_key(api_key)
        or _principal_from_rbac_session(credentials)
        or _principal_from_jwt(credentials)
    )


# =====================================================================
# ADDED HELPER DEPENDENCIES FOR ROUTERS (e.g. jobs.py, settings_hr.py)
# =====================================================================

async def get_current_user(
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> AuthPrincipal:
    """Alias for get_current_principal to support routes requesting get_current_user."""
    return principal


async def get_current_user_optional(
    principal: Annotated[Optional[AuthPrincipal], Depends(get_current_principal_optional)],
) -> Optional[AuthPrincipal]:
    """Alias for get_current_principal_optional."""
    return principal


async def optional_hr(
    principal: Annotated[Optional[AuthPrincipal], Depends(get_current_principal_optional)],
) -> Optional[AuthPrincipal]:
    """Returns AuthPrincipal if caller is HR/Service, otherwise returns None without throwing 401/403."""
    if principal and principal.role in {Role.HR, Role.SERVICE}:
        return principal
    return None


async def require_hr(
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> AuthPrincipal:
    if principal.role not in {Role.HR, Role.SERVICE}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="HR or service credentials required",
        )
    return principal


async def require_authenticated(
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> AuthPrincipal:
    return principal


async def require_hr_account(
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> AuthPrincipal:
    """Strict HR-account guard (excludes the service API-key principal).

    Used by Google-auth HR account endpoints (onboarding/status) where the
    caller must be a real HR user record, not a service-to-service caller.
    """
    if principal.role != Role.HR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires a recruiter/HR account.",
        )
    return principal


async def require_candidate(
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> AuthPrincipal:
    if principal.role != Role.CANDIDATE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires a candidate account.",
        )
    return principal


def assert_candidate_access(
    principal: AuthPrincipal,
    candidate_id: str,
    job_id: Optional[str] = None,
) -> None:
    """Candidates only their own scope; HR only jobs in their tenant bucket."""
    if principal.role == Role.SERVICE:
        return
    if principal.role == Role.HR:
        if job_id:
            from app.database import get_db
            from app.hr_tenant import require_hr_job

            require_hr_job(principal, job_id, get_db())
        return
    if principal.role != Role.CANDIDATE:
        raise HTTPException(status_code=403, detail="Insufficient role")
    if principal.candidate_id != candidate_id:
        raise HTTPException(
            status_code=403, detail="Token is not authorized for this candidate"
        )
    if job_id and principal.job_id and principal.job_id != job_id:
        raise HTTPException(
            status_code=403, detail="Token is not authorized for this job"
        )