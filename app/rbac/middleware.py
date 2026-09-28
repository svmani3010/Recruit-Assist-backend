"""FastAPI dependencies for opaque RBAC sessions and permission checks."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.auth import AuthPrincipal, Role, api_key_header, get_current_principal
from app.rbac import constants as C
from app.rbac.audit import audit_permission_refused
from app.rbac.permissions import PermissionDecision, can
from app.rbac.scope_filter import build_scope_filter
from app.rbac.session_store import get_session_store
from app.settings import get_settings

bearer_scheme = HTTPBearer(auto_error=False)


class RbacPrincipal(BaseModel):
    user_id: str
    org_id: Optional[str] = None
    membership_status: Optional[str] = None
    session_id: str
    email: Optional[str] = None


def rbac_enabled() -> bool:
    return bool(getattr(get_settings(), "rbac_org_enabled", False))


def looks_like_jwt(token: str) -> bool:
    """JWT has three base64 segments separated by dots."""
    parts = (token or "").split(".")
    return len(parts) == 3 and all(parts)


def resolve_opaque_session(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[RbacPrincipal]:
    if not credentials or not credentials.credentials:
        return None
    token = credentials.credentials.strip()
    if not token or looks_like_jwt(token):
        return None
    payload = get_session_store().get(token)
    if not payload or not payload.get("user_id"):
        return None
    return RbacPrincipal(
        user_id=str(payload["user_id"]),
        org_id=str(payload["org_id"]) if payload.get("org_id") else None,
        membership_status=payload.get("membership_status"),
        session_id=token,
        email=payload.get("email"),
    )


async def get_rbac_principal_optional(
    credentials: Annotated[
        Optional[HTTPAuthorizationCredentials], Security(bearer_scheme)
    ] = None,
) -> Optional[RbacPrincipal]:
    if not rbac_enabled():
        return None
    return resolve_opaque_session(credentials)


async def require_rbac_session(
    credentials: Annotated[
        Optional[HTTPAuthorizationCredentials], Security(bearer_scheme)
    ] = None,
) -> RbacPrincipal:
    if not rbac_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RBAC org mode is not enabled",
        )
    principal = resolve_opaque_session(credentials)
    if not principal:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid session",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def check_permission(
    rbac: RbacPrincipal,
    *,
    module: str,
    action: str,
) -> PermissionDecision:
    decision = can(
        user_id=rbac.user_id,
        org_id=rbac.org_id,
        module=module,
        action=action,
        membership_status=rbac.membership_status,
    )
    if not decision.allowed:
        audit_permission_refused(
            actor_id=rbac.user_id,
            org_id=rbac.org_id,
            module=module,
            permission_action=action,
            detail={"reason": decision.reason, "scope": decision.scope},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=decision.reason or C.DENY_CONTACT_MESSAGE,
        )
    return decision


def require_permission(module: str, action: str):
    """Dependency factory: enforce matrix permission when RBAC flag is on.

    When RBAC is off, falls through to legacy auth. When on, enforces the
    matrix for opaque sessions and for HR JWTs that carry (or resolve) org_id.
    """

    async def _dep(
        credentials: Annotated[
            Optional[HTTPAuthorizationCredentials], Security(bearer_scheme)
        ] = None,
        api_key: Annotated[Optional[str], Security(api_key_header)] = None,
        legacy: Annotated[AuthPrincipal, Depends(get_current_principal)] = None,
    ) -> tuple[AuthPrincipal, Optional[PermissionDecision], Optional[RbacPrincipal]]:
        if not rbac_enabled():
            return legacy, None, None

        rbac = resolve_opaque_session(credentials)
        if rbac is not None:
            principal = AuthPrincipal(
                subject=rbac.user_id,
                role=Role.HR,
                org_id=rbac.org_id,
                membership_status=rbac.membership_status,
                rbac_session_id=rbac.session_id,
            )
            decision = check_permission(rbac, module=module, action=action)
            return principal, decision, rbac

        # HR JWT dual-path: still enforce matrix using org attached on principal.
        if legacy and legacy.role == Role.HR:
            org_id = getattr(legacy, "org_id", None)
            if not org_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=C.DENY_CONTACT_MESSAGE,
                )
            decision = can(
                user_id=legacy.subject,
                org_id=org_id,
                module=module,
                action=action,
                membership_status=getattr(legacy, "membership_status", None),
            )
            if not decision.allowed:
                audit_permission_refused(
                    actor_id=legacy.subject,
                    org_id=org_id,
                    module=module,
                    permission_action=action,
                    detail={"reason": decision.reason, "scope": decision.scope},
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=decision.reason or C.DENY_CONTACT_MESSAGE,
                )
            return legacy, decision, None

        return legacy, None, None

    return _dep


def jobs_list_scope_filter(
    decision: Optional[PermissionDecision],
    principal: AuthPrincipal,
) -> dict:
    """Build jobview scope filter when RBAC decision present; else empty."""
    if not decision or not decision.allowed:
        return {}
    return build_scope_filter(
        scope=decision.scope,
        user_id=principal.subject,
        org_id=getattr(principal, "org_id", None),
        collection="jobview",
    )
