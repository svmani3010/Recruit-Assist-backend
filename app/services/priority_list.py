"""Plan-based priority listing registry (`priority_list`).

One plan document per subscription tier, plus one job document per live
job. A paid plan applies to every live job automatically. Standard has
no guaranteed position. Search reads this collection; it is refreshed
from the entitlement cache at least every five minutes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

COLLECTION = "priority_list"
SYNC_SLA = timedelta(minutes=5)

PLAN_ROWS = (
    {
        "docType": "plan",
        "plan": "enterprise",
        "listingPosition": 2,
        "badge": "Promoted",
        "hrLabel": "Priority listing",
        "maxSlotsPerEmployer": 3,
        "organicReserve": 4,
        "guaranteeThreshold": 0.9,
    },
    {
        "docType": "plan",
        "plan": "premium",
        "listingPosition": 10,
        "badge": "Priority listing",
        "hrLabel": "Priority listing",
        "maxSlotsPerEmployer": 3,
        "organicReserve": 4,
        "guaranteeThreshold": 0.9,
    },
    {
        "docType": "plan",
        "plan": "standard",
        "listingPosition": None,
        "badge": None,
        "hrLabel": None,
        "maxSlotsPerEmployer": 3,
        "organicReserve": 4,
        "guaranteeThreshold": 0.9,
    },
)

_TIER_TO_PLAN = {
    "enterprise": "enterprise",
    "premium": "premium",
    "standard": "standard",
    "basic": "standard",
}

MAX_PER_EMPLOYER_DEFAULT = 3
ORGANIC_RESERVE_DEFAULT = 4


def _plans_by_code() -> Dict[str, dict]:
    return {row["plan"]: row for row in PLAN_ROWS}


def _normalize_plan(tier: Optional[str]) -> str:
    return _TIER_TO_PLAN.get((tier or "").strip().lower(), "standard")


def ensure_priority_list_indexes(db) -> None:
    if COLLECTION not in db.list_collection_names():
        db.create_collection(COLLECTION)
    col = db[COLLECTION]
    col.create_index(
        [("docType", 1), ("job_id", 1)],
        unique=True,
        partialFilterExpression={"docType": "job"},
        name="uniq_job",
    )
    col.create_index(
        [("docType", 1), ("plan", 1)],
        unique=True,
        partialFilterExpression={"docType": "plan"},
        name="uniq_plan",
    )
    col.create_index(
        [("docType", 1), ("key", 1)],
        unique=True,
        partialFilterExpression={"docType": "meta"},
        name="uniq_meta",
    )
    col.create_index([("employer_id", 1), ("priorityListing", 1)], name="employer_priority")
    col.create_index([("priorityListing", 1), ("plan", 1)], name="priority_plan")


def _employer_plans(db) -> Dict[str, str]:
    """employer_id -> plan. Entitlement cache wins over subscriptions."""
    plans: Dict[str, str] = {}
    for row in db["subscriptions"].find({}, {"employer_id": 1, "tier": 1, "status": 1}):
        employer_id = row.get("employer_id")
        if not employer_id or row.get("status") not in (None, "active", "upgrade"):
            continue
        if row.get("tier"):
            plans[str(employer_id)] = _normalize_plan(row.get("tier"))
    for row in db["entitlement_cache"].find({}, {"employer_id": 1, "tier": 1}):
        employer_id = row.get("employer_id")
        if employer_id and row.get("tier"):
            plans[str(employer_id)] = _normalize_plan(row.get("tier"))
    return plans


def sync_priority_list(db) -> Dict[str, int]:
    """Upsert plan rows and one row per live job. Drop rows for jobs no longer live."""
    ensure_priority_list_indexes(db)
    now = datetime.now(timezone.utc)
    plans = _plans_by_code()
    col = db[COLLECTION]

    for row in PLAN_ROWS:
        col.update_one(
            {"docType": "plan", "plan": row["plan"]},
            {"$set": {**row, "updated_at": now}},
            upsert=True,
        )

    employer_plans = _employer_plans(db)
    live_jobs = list(
        db["jobview"].find(
            {"show": "on", "status": {"$ne": "Draft"}},
            {
                "_id": 0,
                "jobid": 1,
                "employer_id": 1,
                "hr_user_id": 1,
                "organization_id": 1,
                "jobrole": 1,
                "show": 1,
                "status": 1,
                "posted_timestamp": 1,
            },
        )
    )

    live_ids = []
    priority_count = 0
    for job in live_jobs:
        job_id = str(job.get("jobid") or "").strip()
        if not job_id:
            continue
        employer_id = str(job.get("employer_id") or job.get("hr_user_id") or "")
        plan_code = employer_plans.get(employer_id, "standard")
        plan = plans[plan_code]
        priority = plan["listingPosition"] is not None
        if priority:
            priority_count += 1
        live_ids.append(job_id)
        col.update_one(
            {"docType": "job", "job_id": job_id},
            {"$set": {
                "docType": "job",
                "job_id": job_id,
                "employer_id": employer_id or None,
                "organization_id": job.get("organization_id"),
                "jobrole": job.get("jobrole"),
                "show": job.get("show"),
                "status": job.get("status"),
                "posted_timestamp": job.get("posted_timestamp"),
                "plan": plan_code,
                "listingPosition": plan["listingPosition"],
                "priorityListing": priority,
                "badge": plan["badge"],
                "hrLabel": plan["hrLabel"],
                "updated_at": now,
            }},
            upsert=True,
        )
        db["jobview"].update_one(
            {"jobid": job_id},
            {"$set": {
                "priorityListing": priority,
                "listingPlan": plan_code,
                "listingBadge": plan["badge"],
                "listingPosition": plan["listingPosition"],
            }},
        )

    col.delete_many({"docType": "job", "job_id": {"$nin": live_ids}})
    # Jobs that left the live set should not keep a stale priority flag.
    if live_ids:
        db["jobview"].update_many(
            {"jobid": {"$nin": live_ids}, "priorityListing": True},
            {"$set": {
                "priorityListing": False,
                "listingPlan": "standard",
                "listingBadge": None,
                "listingPosition": None,
            }},
        )

    col.update_one(
        {"docType": "meta", "key": "sync"},
        {"$set": {
            "docType": "meta",
            "key": "sync",
            "synced_at": now,
            "liveJobs": len(live_ids),
            "priorityJobs": priority_count,
        }},
        upsert=True,
    )
    logger.info(
        "priority_list synced live=%s priority=%s", len(live_ids), priority_count
    )
    return {"liveJobs": len(live_ids), "priorityJobs": priority_count}


def ensure_priority_list(db, *, force: bool = False) -> Dict[str, int]:
    """Create the collection and refresh it when the five-minute SLA has elapsed."""
    ensure_priority_list_indexes(db)
    if not force:
        meta = db[COLLECTION].find_one({"docType": "meta", "key": "sync"})
        synced_at = (meta or {}).get("synced_at")
        if isinstance(synced_at, datetime):
            if synced_at.tzinfo is None:
                synced_at = synced_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - synced_at < SYNC_SLA:
                return {
                    "liveJobs": int((meta or {}).get("liveJobs") or 0),
                    "priorityJobs": int((meta or {}).get("priorityJobs") or 0),
                    "skipped": 1,
                }
    result = sync_priority_list(db)
    result["skipped"] = 0
    return result


def load_entitlements(db) -> Dict[str, str]:
    """employer_id -> enterprise | premium | standard (plan-based, always on)."""
    ensure_priority_list(db)
    entitlements: Dict[str, str] = {}
    rank = {"enterprise": 2, "premium": 1, "standard": 0}

    for row in db["entitlement_cache"].find({}, {"employer_id": 1, "tier": 1}):
        employer_id = row.get("employer_id")
        if employer_id:
            entitlements[str(employer_id)] = _normalize_plan(row.get("tier"))

    for row in db[COLLECTION].find({"docType": "job"}, {"employer_id": 1, "plan": 1}):
        employer_id = row.get("employer_id")
        if not employer_id:
            continue
        plan = _normalize_plan(row.get("plan"))
        previous = entitlements.get(str(employer_id), "standard")
        if rank.get(plan, 0) >= rank.get(previous, 0):
            entitlements[str(employer_id)] = plan

    return entitlements


def load_plan_settings(db) -> Dict[str, Any]:
    """Configurable fairness knobs from priority_list plan docs."""
    ensure_priority_list(db)
    max_slots = MAX_PER_EMPLOYER_DEFAULT
    organic = ORGANIC_RESERVE_DEFAULT
    for row in db[COLLECTION].find({"docType": "plan"}):
        if row.get("maxSlotsPerEmployer") is not None:
            max_slots = int(row["maxSlotsPerEmployer"])
        if row.get("organicReserve") is not None:
            organic = int(row["organicReserve"])
    return {
        "maxSlotsPerEmployer": max_slots,
        "organicReserve": organic,
    }


def load_priority_context(db) -> Dict[str, Any]:
    """Search context for the plan-based ranker. Keeps legacy keys for callers."""
    entitlements = load_entitlements(db)
    settings = load_plan_settings(db)
    sponsored_jobs = {
        str(row["job_id"]): True
        for row in db[COLLECTION].find(
            {"docType": "job", "priorityListing": True},
            {"job_id": 1},
        )
        if row.get("job_id")
    }
    # Legacy alias: employer_tiers used "basic" for standard.
    employer_tiers = {
        eid: ("basic" if plan == "standard" else plan)
        for eid, plan in entitlements.items()
    }
    return {
        "entitlements": entitlements,
        "employer_tiers": employer_tiers,
        "sponsored_jobs": sponsored_jobs,
        "maxSlotsPerEmployer": settings["maxSlotsPerEmployer"],
        "organicReserve": settings["organicReserve"],
    }


def restamp_after_publish(db, job_id: Optional[str] = None) -> None:
    """Best-effort sync so a newly published job picks up its plan immediately."""
    try:
        sync_priority_list(db)
    except Exception:
        logger.exception(
            "priority_list restamp after publish failed (job_id=%s)", job_id
        )
