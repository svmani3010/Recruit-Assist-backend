"""Shared role-ladder helpers for assign / invite (no self-escalation)."""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.rbac import constants as C

# Cumulative recruitment ladder — higher index = more power.
RECRUITMENT_LADDER = [
    C.ROLE_USER,
    C.ROLE_MANAGER,
    C.ROLE_CONTENT_ADMIN,
    C.ROLE_SUPER_ADMIN,
]

# Functional roles: assignable by Super Admin only (not on the power ladder).
FUNCTIONAL_ROLES = frozenset(
    {C.ROLE_IT_ADMIN_OPS, C.ROLE_BGV_VENDOR, C.ROLE_VIEWER}
)

# Old id → sheet id
ROLE_ALIASES = {
    "recruiter": C.ROLE_USER,
}


def normalize_role_id(role_id: Optional[str]) -> Optional[str]:
    if not role_id:
        return None
    rid = str(role_id).strip().lower()
    return ROLE_ALIASES.get(rid, rid)


def role_rank(role_id: Optional[str]) -> int:
    rid = normalize_role_id(role_id)
    if not rid or rid not in RECRUITMENT_LADDER:
        return -1
    return RECRUITMENT_LADDER.index(rid)


def assert_can_assign_role(
    *,
    actor_role: Optional[str],
    target_role: str,
    target_current_role: Optional[str] = None,
    actor_user_id: str,
    target_user_id: str,
) -> None:
    if str(actor_user_id) == str(target_user_id):
        raise HTTPException(
            status_code=403, detail="You cannot change your own role"
        )
    target_role = normalize_role_id(target_role) or ""
    actor_role = normalize_role_id(actor_role)
    target_current_role = normalize_role_id(target_current_role)

    if target_role not in C.ALL_RECRUITMENT_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role_id")
    if actor_role not in RECRUITMENT_LADDER:
        raise HTTPException(status_code=403, detail="Cannot assign roles")

    actor_r = role_rank(actor_role)

    # Role administration is Super Admin only per permission sheet.
    if actor_role != C.ROLE_SUPER_ADMIN:
        raise HTTPException(
            status_code=403,
            detail=C.DENY_CONTACT_MESSAGE,
        )

    if target_role in FUNCTIONAL_ROLES:
        return

    if role_rank(target_role) >= actor_r:
        raise HTTPException(
            status_code=403,
            detail="Cannot assign a role at or above your own level",
        )
    if (
        target_current_role in RECRUITMENT_LADDER
        and role_rank(target_current_role) >= actor_r
    ):
        raise HTTPException(
            status_code=403,
            detail="Cannot change a user at or above your own level",
        )


def assignable_roles_for(actor_role: Optional[str]) -> list[str]:
    actor_role = normalize_role_id(actor_role)
    if actor_role != C.ROLE_SUPER_ADMIN:
        return []
    return list(RECRUITMENT_LADDER[:-1]) + sorted(FUNCTIONAL_ROLES)
