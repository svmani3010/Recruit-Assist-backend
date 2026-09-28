"""Company logo/banner resolution and propagation for job listings."""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple

from bson import ObjectId


def normalize_company_name(name: str) -> str:
    return " ".join(str(name or "").strip().lower().split())


def _branding_from_hr_doc(hr_doc: Optional[dict]) -> Tuple[str, str]:
    if not hr_doc:
        return "", ""
    logo = str(hr_doc.get("companyLogo") or hr_doc.get("company_logo") or "").strip()
    banner = str(
        hr_doc.get("companyBanner") or hr_doc.get("company_banner") or ""
    ).strip()
    return logo, banner


def _branding_from_job_doc(job: dict) -> Tuple[str, str]:
    logo = str(
        job.get("companyLogo") or job.get("company_logo") or job.get("logoUrl") or ""
    ).strip()
    banner = str(
        job.get("companyBanner")
        or job.get("company_banner")
        or job.get("bannerUrl")
        or ""
    ).strip()
    return logo, banner


def find_hr_doc_for_job(job: dict, db) -> Optional[dict]:
    """Resolve the HR profile that owns a job (by id first, then company name)."""
    hr_user_id = job.get("hr_user_id") or job.get("hrUserId")
    if hr_user_id:
        try:
            hr_doc = db["hr_users"].find_one({"_id": ObjectId(str(hr_user_id))})
            if hr_doc:
                return hr_doc
        except Exception:
            pass

    company_name = str(job.get("companyName") or job.get("company") or "").strip()
    if not company_name:
        return None

    hr_doc = db["hr_users"].find_one({"companyName": company_name})
    if hr_doc:
        return hr_doc

    return db["hr_users"].find_one(
        {
            "companyName": {
                "$regex": f"^{re.escape(company_name)}$",
                "$options": "i",
            }
        }
    )


def attach_company_branding(job: dict, db, *, persist_backfill: bool = False) -> dict:
    """Attach logo/banner to a job, preferring the latest HR profile branding."""
    hr_doc = find_hr_doc_for_job(job, db)
    hr_logo, hr_banner = _branding_from_hr_doc(hr_doc)
    job_logo, job_banner = _branding_from_job_doc(job)

    logo = hr_logo or job_logo
    banner = hr_banner or job_banner

    if logo:
        job["companyLogo"] = logo
    else:
        job.pop("companyLogo", None)

    if banner:
        job["companyBanner"] = banner
    else:
        job.pop("companyBanner", None)

    if hr_doc and hr_doc.get("_id"):
        job["hr_user_id"] = str(hr_doc["_id"])

    if persist_backfill and hr_doc:
        job_id = job.get("jobid")
        if job_id:
            set_fields: dict[str, Any] = {"hr_user_id": str(hr_doc["_id"])}
            if hr_logo:
                set_fields["companyLogo"] = hr_logo
            if hr_banner:
                set_fields["companyBanner"] = hr_banner
            if len(set_fields) > 1:
                db["jobview"].update_one({"jobid": job_id}, {"$set": set_fields})

    return job


def attach_hr_branding_to_job_payload(data: dict, hr_user_id: str, db) -> dict:
    """Copy authenticated HR user's branding onto a job payload before save."""
    try:
        hr_doc = db["hr_users"].find_one({"_id": ObjectId(hr_user_id)})
    except Exception:
        hr_doc = None

    if not hr_doc:
        return data

    data["hr_user_id"] = hr_user_id
    logo, banner = _branding_from_hr_doc(hr_doc)
    if logo:
        data["companyLogo"] = logo
    if banner:
        data["companyBanner"] = banner
    return data


def propagate_hr_branding_to_jobs(
    hr_user_id: str,
    db,
    *,
    logo: Optional[str] = None,
    banner: Optional[str] = None,
    company_name: Optional[str] = None,
) -> int:
    """Push HR branding onto all jobs owned by this account (including legacy rows)."""
    try:
        hr_doc = db["hr_users"].find_one({"_id": ObjectId(hr_user_id)})
    except Exception:
        return 0

    if not hr_doc:
        return 0

    hr_logo, hr_banner = _branding_from_hr_doc(hr_doc)
    final_logo = logo if logo is not None else hr_logo
    final_banner = banner if banner is not None else hr_banner
    resolved_company = (
        company_name
        if company_name is not None
        else str(hr_doc.get("companyName") or "").strip()
    )

    set_fields: dict[str, str] = {"hr_user_id": hr_user_id}
    if final_logo:
        set_fields["companyLogo"] = final_logo
    if final_banner:
        set_fields["companyBanner"] = final_banner

    result = db["jobview"].update_many({"hr_user_id": hr_user_id}, {"$set": set_fields})
    return int(result.modified_count)


def backfill_all_job_branding(db) -> int:
    """One-shot repair: attach HR branding to every job that is missing it."""
    updated = 0
    jobs = list(db["jobview"].find({}))
    for job in jobs:
        before_logo, before_banner = _branding_from_job_doc(job)
        attach_company_branding(job, db, persist_backfill=True)
        after_logo, after_banner = _branding_from_job_doc(job)
        if after_logo != before_logo or after_banner != before_banner:
            updated += 1
    return updated
