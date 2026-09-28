"""Health and auth token endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth import Role, create_access_token, require_hr
from app.schemas import TokenRequest
from app.settings import get_settings

router = APIRouter(tags=["system"])


@router.get("/health")
async def health():
    return {"status": "ok", "service": get_settings().app_name}


@router.get("/api/email/health")
async def email_health(_principal=Depends(require_hr)):
    """Delivery health for the notification subsystem (HR-only)."""
    from app.email.service import get_email_service

    return get_email_service().health()


@router.post("/api/prompts/reload")
async def reload_prompts(_principal=Depends(require_hr)):
    """Force-reload markdown system prompts from disk (HR-only)."""
    from app.services.screening import get_gemini

    client = get_gemini()
    loaded = client.prompt_manager.reload()
    return {"reloaded": True, "prompts": sorted(loaded.keys()), "count": len(loaded)}


@router.get("/api/prompts")
async def list_prompts(_principal=Depends(require_hr)):
    from app.services.screening import get_gemini

    return {"prompts": get_gemini().prompt_manager.list_prompts()}


@router.post("/api/auth/token")
async def issue_token(payload: TokenRequest):
    """Exchange a valid API key for a short-lived JWT."""
    settings = get_settings()
    if payload.api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    try:
        role = Role(payload.role.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role")
    token = create_access_token(
        subject=payload.subject,
        role=role,
        candidate_id=payload.candidate_id,
    )
    return {"access_token": token, "token_type": "bearer", "role": role.value}


@router.get("/api/auth/whoami")
async def whoami(principal=Depends(require_hr)):
    payload = principal.model_dump()
    from app.settings import get_settings

    if getattr(get_settings(), "rbac_org_enabled", False) and principal.org_id:
        from app.rbac import constants as C
        from app.rbac.permissions import can
        from app.rbac.roles import list_user_roles

        decision = can(
            user_id=principal.subject,
            org_id=principal.org_id,
            module=C.MODULE_RECRUITMENT,
            action=C.ACTION_WHOAMI,
            membership_status=principal.membership_status,
        )
        if not decision.allowed:
            from app.rbac.audit import audit_permission_refused

            audit_permission_refused(
                actor_id=principal.subject,
                org_id=principal.org_id,
                module=C.MODULE_RECRUITMENT,
                permission_action=C.ACTION_WHOAMI,
                detail={"reason": decision.reason},
            )
            raise HTTPException(status_code=403, detail=decision.reason)
        payload["roles"] = list_user_roles(principal.subject, principal.org_id)
        payload["rbac"] = True
    return payload
