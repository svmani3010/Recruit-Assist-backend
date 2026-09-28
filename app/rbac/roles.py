"""ModuleRoleMapping + PermissionMatrix lookups."""

from __future__ import annotations

from typing import Optional

from app.rbac import constants as C
from app.rbac.models import utcnow


def _db():
    from app.database import get_db

    return get_db()


def set_module_role(
    *,
    user_id: str,
    org_id: str,
    module: str,
    role_id: str,
) -> dict:
    from app.rbac.role_cache import invalidate_role

    now = utcnow()
    coll = _db()[C.COL_MODULE_ROLE_MAPPINGS]
    coll.update_one(
        {
            "user_id": str(user_id),
            "org_id": str(org_id),
            "module": module,
        },
        {
            "$set": {
                "role_id": role_id,
                "updated_at": now,
            },
            "$setOnInsert": {
                "user_id": str(user_id),
                "org_id": str(org_id),
                "module": module,
                "created_at": now,
            },
        },
        upsert=True,
    )
    invalidate_role(str(user_id), str(org_id), module)
    return get_module_role(user_id, org_id, module, use_cache=False) or {}


def clear_module_role(user_id: str, org_id: str, module: str) -> None:
    from app.rbac.role_cache import invalidate_role

    _db()[C.COL_MODULE_ROLE_MAPPINGS].delete_one(
        {
            "user_id": str(user_id),
            "org_id": str(org_id),
            "module": module,
        }
    )
    invalidate_role(str(user_id), str(org_id), module)


def get_module_role(
    user_id: str, org_id: str, module: str, *, use_cache: bool = True
) -> Optional[str]:
    from app.rbac.ladder import normalize_role_id

    if use_cache:
        from app.rbac.role_cache import get_cached_role, set_cached_role

        cached = get_cached_role(str(user_id), str(org_id), module)
        if cached is not None:
            return normalize_role_id(cached) if cached else None

    row = _db()[C.COL_MODULE_ROLE_MAPPINGS].find_one(
        {
            "user_id": str(user_id),
            "org_id": str(org_id),
            "module": module,
        },
        {"role_id": 1},
    )
    role_id = normalize_role_id(
        str(row["role_id"]) if row and row.get("role_id") else ""
    )
    if use_cache:
        from app.rbac.role_cache import set_cached_role

        set_cached_role(str(user_id), str(org_id), module, role_id or "")
    return role_id or None


def list_user_roles(user_id: str, org_id: str) -> list[dict]:
    rows = _db()[C.COL_MODULE_ROLE_MAPPINGS].find(
        {"user_id": str(user_id), "org_id": str(org_id)}
    )
    return [
        {"module": r.get("module"), "role_id": r.get("role_id")}
        for r in rows
    ]


def get_matrix_entry(role_id: str, module: str, action: str) -> Optional[dict]:
    return _db()[C.COL_PERMISSION_MATRIX].find_one(
        {
            "role_id": role_id,
            "module": module,
            "action": action,
        }
    )
