"""Phase 0 — domain-keyed org bootstrap on first HR login."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.rbac import constants as C
from app.rbac.audit import write_audit
from app.rbac.notifications import notify_admins_pending_member
from app.rbac.org_service import (
    create_organization_atomic,
    extract_domain,
    find_membership_by_email,
    find_org_by_domain,
    get_membership,
    is_consumer_domain,
    queue_org_create_request,
    upsert_membership,
)
from app.rbac.roles import get_module_role, set_module_role


@dataclass
class BootstrapResult:
    org_id: Optional[str]
    membership_status: Optional[str]
    role_id: Optional[str]
    is_new_org: bool
    requires_org_review: bool
    domain: str
    message: str = ""


def resolve_org_on_login(
    *,
    user_id: str,
    email: str,
    display_name: Optional[str] = None,
) -> BootstrapResult:
    """Resolve or create Organization + Membership for an HR login.

    First person from a corporate domain → company org (provisional) + Super Admin.
    Later peers → pending until an admin assigns a matrix role (or pre-invited).
    Pre-invited emails (role already saved) → active with that role on first login.
    Consumer domains → never auto Super Admin.
    """
    email = (email or "").strip().lower()
    domain = extract_domain(email)
    user_id = str(user_id)

    if is_consumer_domain(domain):
        queue_org_create_request(
            requester_user_id=user_id,
            email=email,
            company_name="",
            company_domain="",
            notes="Auto-queued: consumer email domain cannot auto-create org",
        )
        write_audit(
            action=C.AUDIT_ORG_CREATE_REQUESTED,
            actor_id=user_id,
            target={"email": email, "domain": domain},
        )
        return BootstrapResult(
            org_id=None,
            membership_status=None,
            role_id=None,
            is_new_org=False,
            requires_org_review=True,
            domain=domain,
            message=(
                "Personal email domains cannot auto-create an organization. "
                "Submit company details for review to become Super Admin."
            ),
        )

    existing = find_org_by_domain(domain)
    if existing:
        return _join_existing_org(
            org=existing,
            user_id=user_id,
            email=email,
            is_new_org=False,
        )

    org_name = (display_name or "").strip() or domain
    org, created = create_organization_atomic(
        domain=domain,
        name=org_name,
        created_by_user_id=user_id,
        status=C.ORG_STATUS_PROVISIONAL,
        super_admin_email=email,
    )
    if not org:
        return BootstrapResult(
            org_id=None,
            membership_status=None,
            role_id=None,
            is_new_org=False,
            requires_org_review=False,
            domain=domain,
            message="Failed to resolve organization",
        )

    if not created:
        return _join_existing_org(
            org=org,
            user_id=user_id,
            email=email,
            is_new_org=False,
        )

    org_id = str(org["_id"])
    upsert_membership(
        user_id=user_id,
        org_id=org_id,
        status=C.MEMBERSHIP_ACTIVE,
        email=email,
    )
    set_module_role(
        user_id=user_id,
        org_id=org_id,
        module=C.MODULE_RECRUITMENT,
        role_id=C.ROLE_SUPER_ADMIN,
    )
    write_audit(
        action=C.AUDIT_ORG_CREATED,
        actor_id=user_id,
        org_id=org_id,
        after={
            "domain": domain,
            "status": C.ORG_STATUS_PROVISIONAL,
            "role_id": C.ROLE_SUPER_ADMIN,
            "company_bucket": True,
            "super_admin_email": email,
        },
    )
    return BootstrapResult(
        org_id=org_id,
        membership_status=C.MEMBERSHIP_ACTIVE,
        role_id=C.ROLE_SUPER_ADMIN,
        is_new_org=True,
        requires_org_review=False,
        domain=domain,
        message="Organization created (provisional); you are Super Admin",
    )


def _join_existing_org(
    *,
    org: dict,
    user_id: str,
    email: str,
    is_new_org: bool,
) -> BootstrapResult:
    org_id = str(org["_id"])
    from app.rbac.org_service import (
        assert_email_matches_org_domain,
        resolve_org_domain,
    )

    try:
        domain = assert_email_matches_org_domain(email=email, org=org)
    except ValueError as exc:
        # Safety: never attach a mismatched domain to this org bucket.
        return BootstrapResult(
            org_id=None,
            membership_status=None,
            role_id=None,
            is_new_org=False,
            requires_org_review=True,
            domain=resolve_org_domain(org) or str(org.get("domain") or ""),
            message=str(exc),
        )

    # Re-link invite rows that were created against a stub user_id for this email.
    by_email = find_membership_by_email(org_id, email)
    if by_email and str(by_email.get("user_id")) != user_id:
        # Migrate membership + role mapping to the logging-in user id.
        old_uid = str(by_email.get("user_id"))
        old_role = get_module_role(old_uid, org_id, C.MODULE_RECRUITMENT)
        upsert_membership(
            user_id=user_id,
            org_id=org_id,
            status=by_email.get("status") or C.MEMBERSHIP_PENDING,
            email=email,
        )
        if old_role:
            set_module_role(
                user_id=user_id,
                org_id=org_id,
                module=C.MODULE_RECRUITMENT,
                role_id=old_role,
            )
        from app.rbac.roles import clear_module_role
        from app.database import get_db

        clear_module_role(old_uid, org_id, C.MODULE_RECRUITMENT)
        get_db()[C.COL_MEMBERSHIPS].delete_one(
            {"user_id": old_uid, "org_id": org_id}
        )

    existing_m = get_membership(user_id, org_id)
    existing_role = get_module_role(user_id, org_id, C.MODULE_RECRUITMENT)

    # Pre-assigned matrix role (invite or admin assign) → activate immediately.
    if existing_role:
        upsert_membership(
            user_id=user_id,
            org_id=org_id,
            status=C.MEMBERSHIP_ACTIVE,
            email=email,
        )
        return BootstrapResult(
            org_id=org_id,
            membership_status=C.MEMBERSHIP_ACTIVE,
            role_id=existing_role,
            is_new_org=is_new_org,
            requires_org_review=False,
            domain=domain,
            message=f"Welcome — role {existing_role} from permission matrix",
        )

    if existing_m and existing_m.get("status") == C.MEMBERSHIP_PENDING:
        return BootstrapResult(
            org_id=org_id,
            membership_status=C.MEMBERSHIP_PENDING,
            role_id=None,
            is_new_org=is_new_org,
            requires_org_review=False,
            domain=domain,
            message="Membership still pending role assignment",
        )

    # First time joining this org from the domain — wait for admin.
    upsert_membership(
        user_id=user_id,
        org_id=org_id,
        status=C.MEMBERSHIP_PENDING,
        email=email,
    )
    write_audit(
        action=C.AUDIT_MEMBERSHIP_PENDING,
        actor_id=user_id,
        org_id=org_id,
        after={"email": email, "status": C.MEMBERSHIP_PENDING},
    )
    notify_admins_pending_member(
        org_id=org_id, new_user_id=user_id, email=email
    )
    return BootstrapResult(
        org_id=org_id,
        membership_status=C.MEMBERSHIP_PENDING,
        role_id=None,
        is_new_org=is_new_org,
        requires_org_review=False,
        domain=domain,
        message=(
            "Joined existing organization — awaiting role assignment "
            "from a Super Admin or Content Admin"
        ),
    )


def ensure_company_org_bucket(
    *,
    user_id: str,
    email: str,
    company_name: str,
) -> BootstrapResult:
    """Ensure a Mongo ``organizations`` row when a company saves its profile.

    Runs on HR onboarding so new companies always get a bucket — including when
    the first login happened before RBAC was enabled. First creator is Super
    Admin; peers keep any pre-assigned matrix role.
    """
    from bson import ObjectId

    from app.database import get_db
    from app.rbac.models import utcnow

    email = (email or "").strip().lower()
    user_id = str(user_id)
    name = (company_name or "").strip() or extract_domain(email) or "Organization"

    result = resolve_org_on_login(
        user_id=user_id,
        email=email,
        display_name=name,
    )
    if result.org_id:
        # Peers must not rename the Super Admin's organisation bucket.
        if result.is_new_org and name:
            get_db()[C.COL_ORGANIZATIONS].update_one(
                {"_id": ObjectId(result.org_id)},
                {"$set": {"name": name, "updated_at": utcnow()}},
            )
        get_db()["hr_users"].update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"organization_id": result.org_id}},
        )

    if result.org_id and email:
        from app.rbac.org_service import stamp_super_admin_on_org

        org = get_db()[C.COL_ORGANIZATIONS].find_one(
            {"_id": ObjectId(result.org_id)},
            {"super_admin_email": 1, "created_by_user_id": 1},
        )
        # Stamp when this user is Super Admin, or backfill if the founding
        # creator never had email stored on the org document.
        is_founder = str((org or {}).get("created_by_user_id") or "") == user_id
        missing_email = not (org or {}).get("super_admin_email")
        if result.role_id == C.ROLE_SUPER_ADMIN or (is_founder and missing_email):
            stamp_super_admin_on_org(
                org_id=result.org_id,
                user_id=user_id,
                email=email,
            )
    return result
