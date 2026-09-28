"""ASGI middleware: check org + role Yes/No matrix on mapped HR API routes.

Enforces for both opaque RBAC sessions and legacy HR JWTs when RBAC is on.
Unmapped routes (public careers, candidate booking) pass through.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Tuple

from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.rbac import constants as C
from app.rbac.matrix.loader import matrix_denied_message
from app.rbac.route_actions import resolve_action

logger = logging.getLogger(__name__)

# Candidate / public paths must never enter the recruiter permission matrix,
# even if a regex in route_actions accidentally overlaps.
_CANDIDATE_PATH_PREFIXES = (
    "/auth/candidate",
    "/api/candidate/profile-setup",
    "/api/candidate/region",
    "/api/candidate_apply",
    "/api/jobviewmain",
    "/api/notifications/candidate",
    "/api/interviews/booking",
    "/api/store_candidate_info",
    "/api/profile/",
    "/api/resume_json_personal",
    "/api/personal_info_status",
)


def _is_candidate_path(path: str) -> bool:
    p = path or ""
    if p.startswith(_CANDIDATE_PATH_PREFIXES):
        return True
    # Candidate-owned actions: /api/candidate/{id}/submit-application, etc.
    # HR detail is GET /api/candidate/{id} (exactly 3 slashes).
    if p.startswith("/api/candidate/") and p.count("/") >= 4:
        return True
    if "/events/" in p and p.startswith("/api/candidates/"):
        return True
    return False


def _deny_response(detail: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": detail})


def _resolve_hr_actor(token: str) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """Return (user_id, org_id, membership_status) for matrix checks, or None."""
    from app.settings import get_settings

    settings = get_settings()
    token = (token or "").strip()
    if not token:
        return None

    # Opaque session
    if token.count(".") != 2:
        from app.rbac.session_store import get_session_store

        payload = get_session_store().get(token)
        if not payload or not payload.get("user_id"):
            return None
        return (
            str(payload["user_id"]),
            str(payload["org_id"]) if payload.get("org_id") else None,
            payload.get("membership_status"),
        )

    # Legacy JWT — still enforce matrix for HR subjects when RBAC is on
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except (JWTError, ValueError):
        return None

    role = str(claims.get("role") or "").lower()
    if role != "hr":
        # Candidate / other JWT on a mapped HR path → do not auto-allow;
        # let the route's own auth decide (usually 401/403).
        return None

    user_id = str(claims.get("sub") or "")
    if not user_id:
        return None

    org_id = claims.get("org_id")
    membership_status = claims.get("membership_status")
    try:
        from bson import ObjectId

        from app.database import get_db
        from app.rbac.org_service import get_membership

        doc = get_db()["hr_users"].find_one(
            {"_id": ObjectId(user_id)},
            {"organization_id": 1, "org_id": 1},
        )
        if doc:
            org_id = org_id or doc.get("organization_id") or doc.get("org_id")
        if org_id:
            membership = get_membership(user_id, str(org_id))
            if membership and membership.get("status"):
                membership_status = membership.get("status")
    except Exception:
        logger.exception("RBAC middleware JWT org lookup failed for %s", user_id)

    return (
        user_id,
        str(org_id) if org_id else None,
        membership_status,
    )


class RbacMatrixMiddleware(BaseHTTPMiddleware):
    """Enforce Yes/No matrix on every mapped HR API for authenticated HR callers."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        from app.settings import get_settings

        if not getattr(get_settings(), "rbac_org_enabled", False):
            return await call_next(request)

        path = request.url.path or ""
        if _is_candidate_path(path):
            return await call_next(request)

        action = resolve_action(request.method, path)
        if not action:
            return await call_next(request)

        auth = request.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return await call_next(request)
        token = auth.split(" ", 1)[1].strip()

        try:
            actor = _resolve_hr_actor(token)
        except Exception:
            logger.exception("RBAC middleware actor resolve failed")
            return _deny_response(
                matrix_denied_message() or C.DENY_CONTACT_MESSAGE
            )

        if actor is None:
            # Non-HR bearer on a mapped route (e.g. candidate JWT) — leave to route auth.
            return await call_next(request)

        user_id, org_id, membership_status = actor

        try:
            from app.rbac.audit import audit_permission_refused
            from app.rbac.permissions import can

            decision = can(
                user_id=user_id,
                org_id=org_id,
                module=C.MODULE_RECRUITMENT,
                action=action,
                membership_status=membership_status,
            )
        except Exception:
            logger.exception("RBAC middleware permission check failed")
            return _deny_response(
                matrix_denied_message() or C.DENY_CONTACT_MESSAGE
            )

        if decision.allowed:
            request.state.rbac_action = action
            request.state.rbac_role_id = decision.role_id
            return await call_next(request)

        detail = decision.reason or matrix_denied_message() or C.DENY_CONTACT_MESSAGE
        try:
            audit_permission_refused(
                actor_id=user_id,
                org_id=org_id,
                module=C.MODULE_RECRUITMENT,
                permission_action=action,
                detail={"reason": detail, "path": request.url.path},
            )
        except Exception:
            logger.exception("Failed to audit RBAC deny")

        return _deny_response(detail)
