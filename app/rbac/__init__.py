"""Multi-tenant org RBAC (dual-path scaffold).

Enable with ``RBAC_ORG_ENABLED=true``. When off, existing JWT + hr_user_id
bucket behaviour is unchanged.
"""

from __future__ import annotations


def startup_rbac() -> None:
    """Create indexes and seed Recruitment matrix (idempotent)."""
    from app.rbac.indexes import ensure_rbac_indexes
    from app.rbac.seed_matrix import seed_permission_matrix
    from app.settings import get_settings

    if not getattr(get_settings(), "rbac_org_enabled", False):
        return
    ensure_rbac_indexes()
    seed_permission_matrix()
