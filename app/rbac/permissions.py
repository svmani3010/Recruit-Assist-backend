"""Permission resolution: org + user role + Yes/No matrix folder.

Always prefers live Membership / ModuleRoleMapping from the DB.
The sheet in ``app/rbac/matrix/`` (seeded into ``permission_matrix``) is the
source of truth. Scopes are only ``yes`` or ``no`` (stored as ``none``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.rbac import constants as C
from app.rbac.ladder import normalize_role_id
from app.rbac.matrix.loader import file_allows, matrix_denied_message
from app.rbac.org_service import get_membership
from app.rbac.roles import get_matrix_entry, get_module_role


@dataclass
class PermissionDecision:
    allowed: bool
    scope: str
    role_id: Optional[str] = None
    reason: str = ""


def _is_yes(entry: Optional[dict]) -> bool:
    if not entry:
        return False
    if entry.get("allowed") is True:
        return True
    if entry.get("allowed") is False:
        return False
    scope = str(entry.get("scope") or C.SCOPE_NONE).strip().lower()
    if scope in {C.SCOPE_NONE, "no", "n", "false", "0"}:
        return False
    # Anything else (yes, org, team, own, assigned, config, full) → yes
    return True


def can(
    *,
    user_id: str,
    org_id: Optional[str],
    module: str,
    action: str,
    membership_status: Optional[str] = None,
) -> PermissionDecision:
    """Authorise ``action`` using the Yes/No matrix + live membership."""
    deny = matrix_denied_message() or C.DENY_CONTACT_MESSAGE

    if not org_id:
        return PermissionDecision(
            allowed=False,
            scope=C.SCOPE_NONE,
            reason="No organization on session",
        )

    user_id = str(user_id)
    org_id = str(org_id)

    live = get_membership(user_id, org_id)

    # Prefer live membership; never trust a stale "active" session without a row.
    if live is None:
        if action == C.ACTION_WHOAMI:
            return PermissionDecision(
                allowed=True,
                scope=C.SCOPE_YES,
                role_id=None,
                reason="Identity allowed without membership row",
            )
        return PermissionDecision(
            allowed=False,
            scope=C.SCOPE_NONE,
            reason="No organisation membership. " + deny,
        )

    status = live.get("status") or membership_status

    if status == C.MEMBERSHIP_PENDING:
        if action == C.ACTION_WHOAMI:
            return PermissionDecision(
                allowed=True,
                scope=C.SCOPE_YES,
                role_id=None,
                reason="Pending membership identity",
            )
        return PermissionDecision(
            allowed=False,
            scope=C.SCOPE_NONE,
            reason=(
                "Your account is in the organisation but no role has been "
                "assigned yet. " + deny
            ),
        )

    role_id = normalize_role_id(get_module_role(user_id, org_id, module))
    if not role_id:
        return PermissionDecision(
            allowed=False,
            scope=C.SCOPE_NONE,
            reason="No role mapped for module. " + deny,
        )

    entry = get_matrix_entry(role_id, module, action)
    # Prefer Mongo seed; fall back to on-disk sheet if row missing.
    # Default to "yes" if action is not mentioned in the matrix
    if entry is None:
        # Try to get from file as backup
        file_allowed = file_allows(role_id, action, module=module)
        if file_allowed is None:
            # Default to "yes" if not mentioned in matrix
            return PermissionDecision(
                allowed=True,
                scope=C.SCOPE_YES,
                role_id=role_id,
                reason="Action not specified in matrix - default allowed",
            )
        allowed = file_allowed
    else:
        allowed = _is_yes(entry)

    if not allowed:
        return PermissionDecision(
            allowed=False,
            scope=C.SCOPE_NONE,
            role_id=role_id,
            reason=deny,
        )

    return PermissionDecision(
        allowed=True,
        scope=C.SCOPE_YES,
        role_id=role_id,
        reason="Allowed by permission matrix (yes)",
    )
