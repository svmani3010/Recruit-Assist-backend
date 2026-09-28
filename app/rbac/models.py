"""Document shapes and small helpers for RBAC collections."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, TypedDict


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrganizationDoc(TypedDict, total=False):
    _id: Any
    domain: str
    name: str
    status: str  # provisional | verified
    settings: dict
    created_at: datetime
    updated_at: datetime
    created_by_user_id: str


class MembershipDoc(TypedDict, total=False):
    _id: Any
    user_id: str
    org_id: str
    status: str  # active | pending
    email: str
    created_at: datetime
    updated_at: datetime


class ModuleRoleMappingDoc(TypedDict, total=False):
    _id: Any
    user_id: str
    org_id: str
    module: str
    role_id: str
    updated_at: datetime


class PermissionMatrixDoc(TypedDict, total=False):
    _id: Any
    role_id: str
    module: str
    action: str
    scope: str
    config_setting_key: Optional[str]
    config_default: bool
    notes: str


class AuditLogDoc(TypedDict, total=False):
    _id: Any
    actor_id: Optional[str]
    org_id: Optional[str]
    action: str
    target: Any
    before: Any
    after: Any
    module: Optional[str]
    permission_action: Optional[str]
    timestamp: datetime


class SessionPayload(TypedDict, total=False):
    user_id: str
    org_id: Optional[str]
    membership_status: Optional[str]
    issued_at: str
    email: Optional[str]
