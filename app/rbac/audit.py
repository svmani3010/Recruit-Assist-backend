"""Audit log writer for RBAC events (including permission refusals)."""

from __future__ import annotations

from typing import Any, Optional

from app.rbac import constants as C
from app.rbac.models import utcnow


def _db():
    from app.database import get_db

    return get_db()


def write_audit(
    *,
    action: str,
    actor_id: Optional[str] = None,
    org_id: Optional[str] = None,
    target: Any = None,
    before: Any = None,
    after: Any = None,
    module: Optional[str] = None,
    permission_action: Optional[str] = None,
) -> None:
    doc = {
        "actor_id": str(actor_id) if actor_id else None,
        "org_id": str(org_id) if org_id else None,
        "action": action,
        "target": target,
        "before": before,
        "after": after,
        "module": module,
        "permission_action": permission_action,
        "timestamp": utcnow(),
    }
    _db()[C.COL_AUDIT_LOGS].insert_one(doc)


def audit_permission_refused(
    *,
    actor_id: Optional[str],
    org_id: Optional[str],
    module: str,
    permission_action: str,
    detail: Optional[dict] = None,
) -> None:
    write_audit(
        action=C.AUDIT_PERMISSION_REFUSED,
        actor_id=actor_id,
        org_id=org_id,
        module=module,
        permission_action=permission_action,
        target=detail or {},
    )


def audit_role_admin_change(
    *,
    actor_id: str,
    org_id: str,
    target_user_id: str,
    module: str,
    before_role: Optional[str],
    after_role: Optional[str],
) -> None:
    write_audit(
        action=C.AUDIT_ROLE_ADMIN_CHANGE,
        actor_id=actor_id,
        org_id=org_id,
        module=module,
        target={"user_id": target_user_id},
        before={"role_id": before_role},
        after={"role_id": after_role},
    )


def audit_technical_interview_toggle(
    *,
    actor_id: str,
    org_id: Optional[str],
    job_id: str,
    before_enabled: bool,
    after_enabled: bool,
    candidates_affected: Optional[int] = None,
    option_chosen: Optional[str] = None,
) -> None:
    write_audit(
        action=C.AUDIT_TECHNICAL_INTERVIEW_TOGGLE,
        actor_id=actor_id,
        org_id=org_id,
        module=C.MODULE_RECRUITMENT,
        target={"job_id": job_id},
        before={"technicalInterviewEnabled": before_enabled},
        after={
            "technicalInterviewEnabled": after_enabled,
            "candidates_affected": candidates_affected,
            "option_chosen": option_chosen,
        },
        permission_action=C.ACTION_JOBS_CONFIG_TECHNICAL_INTERVIEW,
    )
