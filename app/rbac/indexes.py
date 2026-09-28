"""Idempotent Mongo indexes for RBAC collections."""

from __future__ import annotations

import logging

from app.database import get_db
from app.rbac import constants as C

logger = logging.getLogger(__name__)


def ensure_rbac_indexes() -> None:
    db = get_db()

    db[C.COL_ORGANIZATIONS].create_index("domain", unique=True)
    db[C.COL_ORGANIZATIONS].create_index("status")

    db[C.COL_MEMBERSHIPS].create_index(
        [("user_id", 1), ("org_id", 1)], unique=True
    )
    db[C.COL_MEMBERSHIPS].create_index([("org_id", 1), ("status", 1)])
    db[C.COL_MEMBERSHIPS].create_index("email")

    db[C.COL_MODULE_ROLE_MAPPINGS].create_index(
        [("user_id", 1), ("org_id", 1), ("module", 1)], unique=True
    )
    db[C.COL_MODULE_ROLE_MAPPINGS].create_index(
        [("org_id", 1), ("module", 1), ("role_id", 1)]
    )

    db[C.COL_PERMISSION_MATRIX].create_index(
        [("role_id", 1), ("module", 1), ("action", 1)], unique=True
    )

    db[C.COL_TEAMS].create_index([("org_id", 1), ("name", 1)])
    db[C.COL_TEAM_MEMBERSHIPS].create_index(
        [("team_id", 1), ("user_id", 1)], unique=True
    )
    db[C.COL_TEAM_MEMBERSHIPS].create_index([("user_id", 1), ("is_manager", 1)])

    db[C.COL_AUDIT_LOGS].create_index([("org_id", 1), ("timestamp", -1)])
    db[C.COL_AUDIT_LOGS].create_index([("actor_id", 1), ("timestamp", -1)])
    db[C.COL_AUDIT_LOGS].create_index([("action", 1), ("timestamp", -1)])

    db[C.COL_ORG_CREATE_REQUESTS].create_index(
        [("requester_user_id", 1), ("status", 1)]
    )
    db[C.COL_ORG_CREATE_REQUESTS].create_index("created_at")

    # Session / role-cache Mongo fallbacks (TTL)
    db[C.COL_RBAC_SESSIONS].create_index("session_id", unique=True)
    db[C.COL_RBAC_SESSIONS].create_index("expires_at", expireAfterSeconds=0)
    db[C.COL_RBAC_ROLE_CACHE].create_index(
        [("user_id", 1), ("org_id", 1), ("module", 1)], unique=True
    )
    db[C.COL_RBAC_ROLE_CACHE].create_index("expires_at", expireAfterSeconds=0)

    db[C.COL_RBAC_NOTIFICATIONS].create_index(
        [("org_id", 1), ("created_at", -1)]
    )

    logger.info("RBAC indexes ensured")
