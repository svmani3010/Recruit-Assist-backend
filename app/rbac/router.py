"""RBAC HTTP routes: can-I, logout, org create request, role assign."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.rbac import constants as C
from app.rbac.audit import audit_role_admin_change, write_audit
from app.rbac.middleware import (
    RbacPrincipal,
    rbac_enabled,
    require_rbac_session,
)
from app.rbac.org_service import (
    get_membership,
    queue_org_create_request,
    upsert_membership,
)
from app.rbac.permissions import can
from app.rbac.roles import get_module_role, list_user_roles, set_module_role
from app.rbac.session_store import get_session_store

router = APIRouter(tags=["rbac"])


class OrgCreateRequestBody(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=200)
    company_domain: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=2000)


class AssignRoleBody(BaseModel):
    user_id: str
    module: str = C.MODULE_RECRUITMENT
    role_id: str
    activate_membership: bool = True


class InviteByEmailBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    role_id: str
    module: str = C.MODULE_RECRUITMENT
    name: str = Field(default="", max_length=200)


@router.get("/api/rbac/export")
async def export_organisation_data(
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    """Export org data — matrix ``data.export`` (Super Admin yes, others no)."""
    from app.rbac.matrix.loader import matrix_denied_message
    from app.rbac.org_service import get_org

    decision = can(
        user_id=rbac.user_id,
        org_id=rbac.org_id,
        module=C.MODULE_RECRUITMENT,
        action=C.ACTION_DATA_EXPORT,
        membership_status=rbac.membership_status,
    )
    if not decision.allowed:
        from app.rbac.audit import audit_permission_refused

        audit_permission_refused(
            actor_id=rbac.user_id,
            org_id=rbac.org_id,
            module=C.MODULE_RECRUITMENT,
            permission_action=C.ACTION_DATA_EXPORT,
            detail={"reason": decision.reason},
        )
        raise HTTPException(
            status_code=403,
            detail=decision.reason
            or matrix_denied_message()
            or C.DENY_CONTACT_MESSAGE,
        )

    org = get_org(rbac.org_id) if rbac.org_id else None
    if org and org.get("_id") is not None:
        org = {**org, "_id": str(org["_id"])}
    return {
        "exported": True,
        "org": org,
        "role_id": decision.role_id,
        "message": "Organisation export allowed by permission matrix",
    }


@router.get("/api/rbac/can")
async def rbac_can(
    module: str,
    action: str,
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    decision = can(
        user_id=rbac.user_id,
        org_id=rbac.org_id,
        module=module,
        action=action,
        membership_status=rbac.membership_status,
    )
    return {
        "allowed": decision.allowed,
        "scope": decision.scope,
        "role_id": decision.role_id,
        "reason": decision.reason,
        "module": module,
        "action": action,
    }


# Actions the Settings / nav UI needs in one round-trip.
_CAPABILITY_ACTIONS = (
    C.ACTION_JOBS_LIST,
    C.ACTION_JOBS_READ,
    C.ACTION_JOBS_CREATE,
    C.ACTION_JOBS_UPDATE,
    C.ACTION_JOBS_DELETE,
    C.ACTION_JOBS_PUBLISH,
    C.ACTION_MEMBERS_INVITE,
    C.ACTION_MEMBERS_ASSIGN_ROLE,
    C.ACTION_ORG_SETTINGS,
    C.ACTION_WHOAMI,
    C.ACTION_DATA_EXPORT,
    C.ACTION_AUDIT_EXPORT,
    C.ACTION_DASHBOARD_VIEW,
    C.ACTION_REPORTS_VIEW,
    C.ACTION_ANALYTICS_VIEW,
    C.ACTION_OFFERS_MANAGE,
    C.ACTION_BGV_CONTROL,
    C.ACTION_BGV_ASSIGNED,
    C.ACTION_REQUESTS_RAISE,
    C.ACTION_PROVISIONING_RUN,
)


@router.get("/api/rbac/capabilities")
async def rbac_capabilities(
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
    module: str = C.MODULE_RECRUITMENT,
):
    """Return allow/deny for every wired Recruitment action for this session."""
    from app.rbac.roles import get_module_role

    role_id = get_module_role(rbac.user_id, rbac.org_id, module) if rbac.org_id else None
    actions: dict = {}
    for action in _CAPABILITY_ACTIONS:
        decision = can(
            user_id=rbac.user_id,
            org_id=rbac.org_id,
            module=module,
            action=action,
            membership_status=rbac.membership_status,
        )
        actions[action] = {
            "allowed": decision.allowed,
            "scope": decision.scope,
            "reason": decision.reason
            or (
                None
                if decision.allowed
                else "This action is not permitted for your role."
            ),
        }
    return {
        "module": module,
        "role_id": role_id,
        "org_id": rbac.org_id,
        "membership_status": rbac.membership_status,
        "actions": actions,
    }


@router.post("/api/rbac/logout")
async def rbac_logout(
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    get_session_store().delete(rbac.session_id)
    write_audit(
        action=C.AUDIT_SESSION_REVOKED,
        actor_id=rbac.user_id,
        org_id=rbac.org_id,
        target={"session_id": rbac.session_id[:8] + "…"},
    )
    return {"revoked": True}


@router.post("/api/rbac/org-create-request")
async def submit_org_create_request(
    body: OrgCreateRequestBody,
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    """Manual org form for consumer-domain users (queued for review)."""
    doc = queue_org_create_request(
        requester_user_id=rbac.user_id,
        email=rbac.email or "",
        company_name=body.company_name,
        company_domain=body.company_domain,
        notes=body.notes,
    )
    write_audit(
        action=C.AUDIT_ORG_CREATE_REQUESTED,
        actor_id=rbac.user_id,
        target={
            "request_id": str(doc["_id"]),
            "company_name": body.company_name,
        },
    )
    return {
        "status": C.ORG_STATUS_PENDING_REVIEW,
        "request_id": str(doc["_id"]),
        "message": "Organization create request queued for review",
    }


@router.get("/api/rbac/members")
async def list_org_members(
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
    q: str = "",
):
    """List organisation memberships (search by name/email). Admins only."""
    if not rbac.org_id:
        raise HTTPException(status_code=400, detail="No organization on session")

    decision = can(
        user_id=rbac.user_id,
        org_id=rbac.org_id,
        module=C.MODULE_RECRUITMENT,
        action=C.ACTION_MEMBERS_ASSIGN_ROLE,
        membership_status=rbac.membership_status,
    )
    if not decision.allowed:
        # Allow Super/Content style read via invite permission too
        invite = can(
            user_id=rbac.user_id,
            org_id=rbac.org_id,
            module=C.MODULE_RECRUITMENT,
            action=C.ACTION_MEMBERS_INVITE,
            membership_status=rbac.membership_status,
        )
        if not invite.allowed:
            from app.rbac.audit import audit_permission_refused

            audit_permission_refused(
                actor_id=rbac.user_id,
                org_id=rbac.org_id,
                module=C.MODULE_RECRUITMENT,
                permission_action="members.list",
                detail={"reason": decision.reason},
            )
            raise HTTPException(status_code=403, detail=decision.reason)

    from app.database import get_db
    from bson import ObjectId

    db = get_db()
    memberships = list(
        db[C.COL_MEMBERSHIPS].find({"org_id": str(rbac.org_id)})
    )
    needle = (q or "").strip().lower()
    results = []
    for m in memberships:
        uid = str(m.get("user_id") or "")
        email = (m.get("email") or "").strip().lower()
        name = ""
        try:
            hr = db["hr_users"].find_one({"_id": ObjectId(uid)})
            if hr:
                name = (hr.get("name") or "").strip()
                if not email:
                    email = (hr.get("email") or "").strip().lower()
        except Exception:
            pass
        if needle and needle not in email and needle not in name.lower():
            continue
        role_id = get_module_role(uid, rbac.org_id, C.MODULE_RECRUITMENT)
        results.append(
            {
                "user_id": uid,
                "email": email,
                "name": name,
                "membership_status": m.get("status"),
                "role_id": role_id,
            }
        )
    results.sort(key=lambda r: (r.get("name") or r.get("email") or "").lower())
    return {"members": results, "org_id": rbac.org_id}


@router.get("/api/rbac/notifications")
async def list_rbac_notifications(
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    if not rbac.org_id:
        return {"notifications": []}
    from app.database import get_db

    rows = list(
        get_db()[C.COL_RBAC_NOTIFICATIONS]
        .find(
            {
                "org_id": str(rbac.org_id),
                "recipient_user_id": str(rbac.user_id),
            }
        )
        .sort("created_at", -1)
        .limit(50)
    )
    out = []
    for r in rows:
        out.append(
            {
                "id": str(r.get("_id")),
                "type": r.get("type"),
                "message": r.get("message"),
                "payload": r.get("payload") or {},
                "read": bool(r.get("read")),
                "created_at": r.get("created_at").isoformat()
                if r.get("created_at")
                else None,
            }
        )
    return {"notifications": out}


@router.get("/api/rbac/matrix")
async def get_role_matrix(
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
    role_id: str = "",
    module: str = C.MODULE_RECRUITMENT,
):
    """Return permission-matrix rows for a role (what that role can do)."""
    from app.database import get_db

    query: dict = {"module": module}
    if role_id:
        query["role_id"] = role_id
    rows = list(get_db()[C.COL_PERMISSION_MATRIX].find(query, {"_id": 0}))
    return {"module": module, "role_id": role_id or None, "rows": rows}


@router.post("/api/rbac/members/invite")
async def invite_member_by_email(
    body: InviteByEmailBody,
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    """Save an HR email into a matrix role before (or after) they first log in.

    Domain must match the organisation domain. Creates an hr_users stub if needed,
    activates membership, and maps the Recruitment role from the permission matrix.
    """
    if not rbac.org_id:
        raise HTTPException(status_code=400, detail="No organization on session")

    decision = can(
        user_id=rbac.user_id,
        org_id=rbac.org_id,
        module=C.MODULE_RECRUITMENT,
        action=C.ACTION_MEMBERS_ASSIGN_ROLE,
        membership_status=rbac.membership_status,
    )
    if not decision.allowed:
        from app.rbac.audit import audit_permission_refused

        audit_permission_refused(
            actor_id=rbac.user_id,
            org_id=rbac.org_id,
            module=C.MODULE_RECRUITMENT,
            permission_action=C.ACTION_MEMBERS_ASSIGN_ROLE,
            detail={"reason": decision.reason},
        )
        raise HTTPException(status_code=403, detail=decision.reason)

    email = (body.email or "").strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")

    from app.rbac.ladder import assert_can_assign_role
    from app.rbac.org_service import (
        assert_email_matches_org_domain,
        ensure_hr_user_stub,
        get_org,
    )

    org = get_org(rbac.org_id) or {}
    try:
        org_domain = assert_email_matches_org_domain(email=email, org=org)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Persist domain if it was only inferred from Super Admin email.
    if org and not (org.get("domain") or "").strip() and org_domain:
        from app.database import get_db
        from bson import ObjectId
        from app.rbac.models import utcnow

        get_db()[C.COL_ORGANIZATIONS].update_one(
            {"_id": ObjectId(str(rbac.org_id))},
            {"$set": {"domain": org_domain, "updated_at": utcnow()}},
        )

    actor_role = get_module_role(
        rbac.user_id, rbac.org_id, C.MODULE_RECRUITMENT
    )
    target_user_id = ensure_hr_user_stub(
        email=email,
        org_id=rbac.org_id,
        name=(body.name or "").strip() or None,
    )
    if str(target_user_id) == str(rbac.user_id):
        raise HTTPException(
            status_code=403, detail="You cannot change your own role"
        )

    before = get_module_role(target_user_id, rbac.org_id, body.module)
    assert_can_assign_role(
        actor_role=actor_role,
        target_role=body.role_id,
        target_current_role=before,
        actor_user_id=rbac.user_id,
        target_user_id=target_user_id,
    )

    upsert_membership(
        user_id=target_user_id,
        org_id=rbac.org_id,
        status=C.MEMBERSHIP_ACTIVE,
        email=email,
    )
    set_module_role(
        user_id=target_user_id,
        org_id=rbac.org_id,
        module=body.module,
        role_id=body.role_id,
    )
    audit_role_admin_change(
        actor_id=rbac.user_id,
        org_id=rbac.org_id,
        target_user_id=target_user_id,
        module=body.module,
        before_role=before,
        after_role=body.role_id,
    )

    # Matrix preview for the UI confirmation
    from app.database import get_db

    matrix_rows = list(
        get_db()[C.COL_PERMISSION_MATRIX].find(
            {"role_id": body.role_id, "module": body.module},
            {"_id": 0, "action": 1, "scope": 1, "notes": 1},
        )
    )

    return {
        "user_id": target_user_id,
        "email": email,
        "org_id": rbac.org_id,
        "module": body.module,
        "role_id": body.role_id,
        "membership_status": C.MEMBERSHIP_ACTIVE,
        "matrix": matrix_rows,
        "message": (
            f"{email} will receive the {body.role_id} permission set "
            "from the matrix when they sign in."
        ),
    }


@router.post("/api/rbac/members/assign-role")
async def assign_member_role(
    body: AssignRoleBody,
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    if not rbac.org_id:
        raise HTTPException(status_code=400, detail="No organization on session")

    decision = can(
        user_id=rbac.user_id,
        org_id=rbac.org_id,
        module=C.MODULE_RECRUITMENT,
        action=C.ACTION_MEMBERS_ASSIGN_ROLE,
        membership_status=rbac.membership_status,
    )
    if not decision.allowed:
        from app.rbac.audit import audit_permission_refused

        audit_permission_refused(
            actor_id=rbac.user_id,
            org_id=rbac.org_id,
            module=C.MODULE_RECRUITMENT,
            permission_action=C.ACTION_MEMBERS_ASSIGN_ROLE,
            detail={"reason": decision.reason},
        )
        raise HTTPException(status_code=403, detail=decision.reason)

    from app.rbac.ladder import assert_can_assign_role
    from app.rbac.org_service import assert_email_matches_org_domain, get_org

    org = get_org(rbac.org_id) or {}
    membership = get_membership(body.user_id, rbac.org_id)
    target_email = (membership or {}).get("email") or ""
    if not target_email:
        from bson import ObjectId

        from app.database import get_db

        hr = get_db()["hr_users"].find_one(
            {"_id": ObjectId(str(body.user_id))},
            {"email": 1},
        )
        target_email = ((hr or {}).get("email") or "").strip().lower()
    if target_email:
        try:
            assert_email_matches_org_domain(email=target_email, org=org)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor_role = get_module_role(
        rbac.user_id, rbac.org_id, C.MODULE_RECRUITMENT
    )
    before = get_module_role(body.user_id, rbac.org_id, body.module)
    assert_can_assign_role(
        actor_role=actor_role,
        target_role=body.role_id,
        target_current_role=before,
        actor_user_id=rbac.user_id,
        target_user_id=body.user_id,
    )

    set_module_role(
        user_id=body.user_id,
        org_id=rbac.org_id,
        module=body.module,
        role_id=body.role_id,
    )
    if body.activate_membership:
        membership = get_membership(body.user_id, rbac.org_id)
        email = (membership or {}).get("email") or target_email or ""
        upsert_membership(
            user_id=body.user_id,
            org_id=rbac.org_id,
            status=C.MEMBERSHIP_ACTIVE,
            email=email,
        )

    audit_role_admin_change(
        actor_id=rbac.user_id,
        org_id=rbac.org_id,
        target_user_id=body.user_id,
        module=body.module,
        before_role=before,
        after_role=body.role_id,
    )
    return {
        "user_id": body.user_id,
        "org_id": rbac.org_id,
        "module": body.module,
        "role_id": body.role_id,
        "membership_status": C.MEMBERSHIP_ACTIVE
        if body.activate_membership
        else None,
    }


@router.get("/api/rbac/orgs/current")
async def get_current_organization(
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    """Return the caller's organization document (for debugging / welcome UI)."""
    from app.database import get_db
    from app.rbac.org_service import get_org

    if not rbac.org_id:
        return {
            "org": None,
            "membership_status": rbac.membership_status,
            "message": "No organization on session",
        }
    org = get_org(rbac.org_id)
    if org and org.get("_id") is not None:
        org = {**org, "_id": str(org["_id"])}
    membership = get_membership(rbac.user_id, rbac.org_id)
    role_id = get_module_role(
        rbac.user_id, rbac.org_id, C.MODULE_RECRUITMENT
    )
    return {
        "org": org,
        "membership_status": (membership or {}).get("status")
        or rbac.membership_status,
        "role_id": role_id,
        "collection": C.COL_ORGANIZATIONS,
        "db_hint": "Look in MongoDB collection 'organizations' (lowercase)",
    }


@router.post("/api/rbac/orgs/ensure")
async def ensure_my_organization(
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    """Repair path: create/join org bucket for the logged-in HR user now."""
    from app.database import get_db
    from app.rbac.bootstrap import ensure_company_org_bucket
    from bson import ObjectId

    hr = get_db()["hr_users"].find_one({"_id": ObjectId(rbac.user_id)})
    if not hr:
        raise HTTPException(status_code=404, detail="HR user not found")
    email = (hr.get("email") or rbac.email or "").strip().lower()
    company = str(hr.get("companyName") or "").strip()
    boot = ensure_company_org_bucket(
        user_id=rbac.user_id,
        email=email,
        company_name=company or email.split("@")[-1],
    )
    from app.rbac.session_store import get_session_store

    get_session_store().delete(rbac.session_id)
    token = get_session_store().create(
        user_id=rbac.user_id,
        org_id=boot.org_id,
        membership_status=boot.membership_status,
        email=email,
    )
    return {
        "org_id": boot.org_id,
        "role_id": boot.role_id,
        "is_new_org": boot.is_new_org,
        "membership_status": boot.membership_status,
        "domain": boot.domain,
        "message": boot.message,
        "session_token": token,
        "collection": C.COL_ORGANIZATIONS,
    }


@router.get("/api/rbac/me")
async def rbac_me(
    rbac: Annotated[RbacPrincipal, Depends(require_rbac_session)],
):
    """Return live membership + roles (not a stale session snapshot)."""
    from app.rbac.org_service import get_org, resolve_org_domain

    roles = (
        list_user_roles(rbac.user_id, rbac.org_id) if rbac.org_id else []
    )
    live_status = rbac.membership_status
    org_domain = None
    if rbac.org_id:
        membership = get_membership(rbac.user_id, rbac.org_id)
        if membership:
            live_status = membership.get("status") or live_status
        org_domain = resolve_org_domain(get_org(rbac.org_id))
    return {
        "user_id": rbac.user_id,
        "org_id": rbac.org_id,
        "membership_status": live_status,
        "email": rbac.email,
        "roles": roles,
        "rbac_enabled": rbac_enabled(),
        "org_domain": org_domain or None,
    }
