"""Per-HR tenant isolation — each recruiter only sees and mutates their own bucket."""

from __future__ import annotations

import re
from typing import Any, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.auth import AuthPrincipal, Role
from app.company_branding import attach_hr_branding_to_job_payload


def get_hr_user_doc(principal: AuthPrincipal, db) -> Optional[dict]:
    if principal.role != Role.HR:
        return None
    try:
        return db["hr_users"].find_one({"_id": ObjectId(principal.subject)})
    except Exception:
        return None


def is_company_name_locked(hr_doc: Optional[dict]) -> bool:
    if not hr_doc:
        return False
    if hr_doc.get("company_name_locked"):
        return True
    return bool(str(hr_doc.get("companyName") or "").strip())


def hr_jobs_filter(principal: AuthPrincipal) -> dict[str, Any]:
    """Mongo filter scoping jobview queries to the authenticated HR account."""
    if principal.role == Role.SERVICE:
        return {}
    return {"hr_user_id": principal.subject}


def require_hr_job(principal: AuthPrincipal, job_id: str, db) -> dict:
    """Return the job document or raise 404 if it is outside this HR's bucket."""
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")

    job_ids: list[Any] = [job_id]
    try:
        as_int = int(job_id)
        if as_int != job_id:
            job_ids.append(as_int)
    except (TypeError, ValueError):
        pass

    query: dict[str, Any] = {"jobid": {"$in": job_ids}}
    if principal.role != Role.SERVICE:
        query["hr_user_id"] = principal.subject
    job = db["jobview"].find_one(query)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def stamp_hr_job_payload(data: dict, principal: AuthPrincipal, db) -> dict:
    """Force hr_user_id, frozen company name, and branding onto a job payload."""
    hr_doc = get_hr_user_doc(principal, db)
    if not hr_doc:
        return data

    data["hr_user_id"] = principal.subject
    # Server-stamp organisation_id when RBAC session is present — never trust client.
    org_id = getattr(principal, "org_id", None) or hr_doc.get("organization_id")
    if org_id:
        data["organization_id"] = str(org_id)
    else:
        data.pop("organization_id", None)
    company = str(hr_doc.get("companyName") or "").strip()
    if company:
        data["companyName"] = company

    return attach_hr_branding_to_job_payload(data, principal.subject, db)


def propagate_company_name_to_jobs(hr_user_id: str, company_name: str, db) -> int:
    """Sync frozen company name onto every job owned by this HR account."""
    if not company_name:
        return 0
    result = db["jobview"].update_many(
        {"hr_user_id": hr_user_id},
        {"$set": {"companyName": company_name}},
    )
    return int(result.modified_count)


def backfill_hr_job_ownership(hr_user_id: str, db) -> int:
    """Claim legacy jobs that match this HR's company name but lack hr_user_id."""
    try:
        hr_doc = db["hr_users"].find_one({"_id": ObjectId(hr_user_id)})
    except Exception:
        return 0
    if not hr_doc:
        return 0

    company = str(hr_doc.get("companyName") or "").strip()
    if not company:
        return 0

    result = db["jobview"].update_many(
        {
            "$or": [
                {"hr_user_id": {"$exists": False}},
                {"hr_user_id": None},
                {"hr_user_id": ""},
            ],
            "companyName": {
                "$regex": f"^{re.escape(company)}$",
                "$options": "i",
            },
        },
        {"$set": {"hr_user_id": hr_user_id, "companyName": company}},
    )
    return int(result.modified_count)


def ensure_hr_tenant_ready(principal: AuthPrincipal, db) -> None:
    """Backfill ownership for legacy rows before scoped HR reads."""
    if principal.role != Role.HR:
        return
    backfill_hr_job_ownership(principal.subject, db)
