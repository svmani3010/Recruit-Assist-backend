"""Seed Mongo ``permission_matrix`` from ``app/rbac/matrix/*.json`` (Yes/No only)."""

from __future__ import annotations

import logging

from app.database import get_db
from app.rbac import constants as C
from app.rbac.matrix.loader import iter_yes_no_rows, load_recruitment_actions
from app.rbac.models import utcnow

logger = logging.getLogger(__name__)


def seed_permission_matrix() -> int:
    """Upsert every action×role Yes/No cell from the matrix folder.

    Legacy scope words are already normalised to yes/no by the loader.
    Stored field remains ``scope`` for compatibility: ``yes`` or ``none``.
    """
    db = get_db()
    coll = db[C.COL_PERMISSION_MATRIX]
    now = utcnow()
    sheet = load_recruitment_actions()
    version = sheet.get("version")
    count = 0

    for module, action, role_id, flag in iter_yes_no_rows():
        scope = C.SCOPE_YES if flag == "yes" else C.SCOPE_NONE
        coll.update_one(
            {
                "role_id": role_id,
                "module": module,
                "action": action,
            },
            {
                "$set": {
                    "scope": scope,
                    "allowed": flag == "yes",
                    "config_setting_key": None,
                    "config_default": False,
                    "notes": f"sheet v{version} yes/no",
                    "source": "app/rbac/matrix/recruitment_actions.json",
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        count += 1

    # Drop obsolete recruiter rows if any (role renamed to ``user``).
    coll.delete_many({"role_id": "recruiter", "module": C.MODULE_RECRUITMENT})

    logger.info(
        "Permission matrix seeded from folder (%s Yes/No cells, module=%s)",
        count,
        sheet.get("module"),
    )
    return count
