"""Organization and Membership helpers."""

from __future__ import annotations

from typing import Any, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.rbac import constants as C
from app.rbac.models import utcnow


def _db():
    from app.database import get_db

    return get_db()


def _oid(value: str | ObjectId) -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    return ObjectId(str(value))


def extract_domain(email: str) -> str:
    return (email or "").strip().lower().split("@")[-1]


def is_consumer_domain(domain: str) -> bool:
    return (domain or "").strip().lower() in C.CONSUMER_EMAIL_DOMAINS


def resolve_org_domain(org: Optional[dict]) -> str:
    """Canonical company domain for an org (from domain field or Super Admin email)."""
    if not org:
        return ""
    domain = (org.get("domain") or "").strip().lower()
    if domain:
        return domain
    sa = (org.get("super_admin_email") or "").strip().lower()
    if "@" in sa:
        return extract_domain(sa)
    return ""


def assert_email_matches_org_domain(
    *,
    email: str,
    org: Optional[dict],
    org_id: Optional[str] = None,
) -> str:
    """Require invitee/joiner email to use the same domain as the organisation.

    Returns the normalised org domain. Raises ValueError with a user-facing message.
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ValueError("Invalid email")

    email_domain = extract_domain(email)
    if is_consumer_domain(email_domain):
        raise ValueError(
            "Personal email domains cannot join a company organisation. "
            "Use your work email."
        )

    doc = org
    if doc is None and org_id:
        doc = get_org(org_id)
    org_domain = resolve_org_domain(doc)
    if not org_domain:
        raise ValueError(
            "This organisation has no company domain configured. "
            "Contact support before inviting teammates."
        )
    if email_domain != org_domain:
        raise ValueError(
            f"Only @{org_domain} email addresses can join this organisation. "
            f"'{email}' is not allowed."
        )
    return org_domain


def find_org_by_domain(domain: str) -> Optional[dict]:
    domain = (domain or "").strip().lower()
    if not domain:
        return None
    return _db()[C.COL_ORGANIZATIONS].find_one({"domain": domain})


def get_org(org_id: str) -> Optional[dict]:
    try:
        return _db()[C.COL_ORGANIZATIONS].find_one({"_id": _oid(org_id)})
    except Exception:
        return None


def get_org_default_technical_interview_enabled(org_id: str) -> bool:
    """Get organization's default technical interview enabled setting.
    
    Platform default is True if not configured at org level.
    """
    org = get_org(org_id)
    if not org:
        return True  # Platform default
    
    settings = org.get("settings") or {}
    return settings.get("defaultTechnicalInterviewEnabled", True)  # Platform default


def set_org_default_technical_interview_enabled(org_id: str, enabled: bool) -> None:
    """Set organization's default technical interview enabled setting."""
    _db()[C.COL_ORGANIZATIONS].update_one(
        {"_id": _oid(org_id)},
        {"$set": {"settings.defaultTechnicalInterviewEnabled": enabled}}
    )


def get_org_postponement_config(org_id: str) -> dict:
    """Get organization's technical interview postponement configuration."""
    org = get_org(org_id)
    if not org:
        return {}
    
    return org.get("settings", {}).get("postponement", {})


def set_org_postponement_config(org_id: str, config: dict) -> None:
    """Set organization's technical interview postponement configuration."""
    _db()[C.COL_ORGANIZATIONS].update_one(
        {"_id": _oid(org_id)},
        {"$set": {"settings.postponement": config}}
    )


def create_organization_atomic(
    *,
    domain: str,
    name: str,
    created_by_user_id: str,
    status: str = C.ORG_STATUS_PROVISIONAL,
    settings: Optional[dict] = None,
    super_admin_email: Optional[str] = None,
) -> tuple[Optional[dict], bool]:
    """Insert org with unique domain.

    Returns `(org_doc, created)`. On duplicate-key race, returns existing org
    and `created=False`.
    """
    domain = (domain or "").strip().lower()
    email = (super_admin_email or "").strip().lower() or None
    now = utcnow()
    doc: dict[str, Any] = {
        "domain": domain,
        "name": name or domain,
        "status": status,
        "settings": settings or {},
        "created_at": now,
        "updated_at": now,
        "created_by_user_id": str(created_by_user_id),
        "super_admin_user_id": str(created_by_user_id),
        "super_admin_email": email,
    }
    try:
        result = _db()[C.COL_ORGANIZATIONS].insert_one(doc)
        doc["_id"] = result.inserted_id
        return doc, True
    except DuplicateKeyError:
        existing = find_org_by_domain(domain)
        return existing, False


def stamp_super_admin_on_org(
    *,
    org_id: str,
    user_id: str,
    email: str,
) -> None:
    """Ensure org bucket carries the founding Super Admin email/id."""
    email = (email or "").strip().lower()
    if not org_id or not email:
        return
    _db()[C.COL_ORGANIZATIONS].update_one(
        {"_id": _oid(org_id)},
        {
            "$set": {
                "super_admin_email": email,
                "super_admin_user_id": str(user_id),
                "updated_at": utcnow(),
            },
            "$setOnInsert": {},
        },
    )

def get_membership(user_id: str, org_id: str) -> Optional[dict]:
    return _db()[C.COL_MEMBERSHIPS].find_one(
        {"user_id": str(user_id), "org_id": str(org_id)}
    )


def find_membership_by_email(org_id: str, email: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    if not email:
        return None
    return _db()[C.COL_MEMBERSHIPS].find_one(
        {"org_id": str(org_id), "email": email}
    )


def ensure_hr_user_stub(
    *,
    email: str,
    org_id: str,
    name: Optional[str] = None,
) -> str:
    """Find or create an hr_users row so a role can be pre-assigned by email."""
    import re

    email = (email or "").strip().lower()
    coll = _db()["hr_users"]
    existing = coll.find_one(
        {"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}}
    )
    if existing:
        coll.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    "organization_id": str(org_id),
                    "email": email,
                }
            },
        )
        return str(existing["_id"])

    domain = extract_domain(email)
    doc = {
        "email": email,
        "name": name,
        "picture": None,
        "company_domain": domain,
        "onboarding_complete": False,
        "organization_id": str(org_id),
        "invited": True,
        "auth_provider": "",
    }
    result = coll.insert_one(doc)
    return str(result.inserted_id)


def upsert_membership(
    *,
    user_id: str,
    org_id: str,
    status: str,
    email: str,
) -> dict:
    now = utcnow()
    coll = _db()[C.COL_MEMBERSHIPS]
    coll.update_one(
        {"user_id": str(user_id), "org_id": str(org_id)},
        {
            "$set": {
                "status": status,
                "email": (email or "").strip().lower(),
                "updated_at": now,
            },
            "$setOnInsert": {
                "user_id": str(user_id),
                "org_id": str(org_id),
                "created_at": now,
            },
        },
        upsert=True,
    )
    return get_membership(user_id, org_id) or {}


def list_admin_user_ids(org_id: str, module: str = C.MODULE_RECRUITMENT) -> list[str]:
    """User ids with Super Admin or Content Admin role in the org/module."""
    rows = _db()[C.COL_MODULE_ROLE_MAPPINGS].find(
        {
            "org_id": str(org_id),
            "module": module,
            "role_id": {"$in": list(C.ADMIN_ROLES)},
        },
        {"user_id": 1},
    )
    return [str(r["user_id"]) for r in rows]


def get_org_setting(org: dict, key: str, default: bool = False) -> bool:
    settings = org.get("settings") if isinstance(org, dict) else None
    if not isinstance(settings, dict) or key not in settings:
        return default
    value = settings[key]
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def queue_org_create_request(
    *,
    requester_user_id: str,
    email: str,
    company_name: str,
    company_domain: str = "",
    notes: str = "",
) -> dict:
    now = utcnow()
    doc = {
        "requester_user_id": str(requester_user_id),
        "email": (email or "").strip().lower(),
        "company_name": (company_name or "").strip(),
        "company_domain": (company_domain or "").strip().lower(),
        "notes": notes or "",
        "status": C.ORG_STATUS_PENDING_REVIEW,
        "created_at": now,
        "updated_at": now,
    }
    result = _db()[C.COL_ORG_CREATE_REQUESTS].insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc
