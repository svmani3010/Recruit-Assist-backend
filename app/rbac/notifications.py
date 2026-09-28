"""Notify Super/Content admins when a domain peer joins as pending."""

from __future__ import annotations

import logging

from app.rbac import constants as C
from app.rbac.models import utcnow
from app.rbac.org_service import list_admin_user_ids

logger = logging.getLogger(__name__)


def notify_admins_pending_member(
    *,
    org_id: str,
    new_user_id: str,
    email: str,
) -> int:
    """Persist in-app notifications for org admins. Returns recipient count."""
    from app.database import get_db

    admin_ids = list_admin_user_ids(org_id)
    recipients = [uid for uid in admin_ids if uid != str(new_user_id)]
    if not recipients:
        logger.info(
            "No admins to notify for pending member %s in org %s",
            email,
            org_id,
        )
        return 0

    now = utcnow()
    docs = [
        {
            "org_id": str(org_id),
            "recipient_user_id": rid,
            "type": "pending_member",
            "message": (
                f"New member joined from your domain ({email}) — assign a role."
            ),
            "payload": {
                "new_user_id": str(new_user_id),
                "email": (email or "").strip().lower(),
            },
            "read": False,
            "created_at": now,
        }
        for rid in recipients
    ]
    get_db()[C.COL_RBAC_NOTIFICATIONS].insert_many(docs)
    return len(docs)
