"""
routes_priority.py
------------------
E18 Priority Job Listing — FastAPI wiring.

Zero-UI-risk rules for this phase:
  - GET /api/jobs and other marketplace list endpoints are untouched.
  - Existing subscribe / limits / sponsor routes keep the same paths and
    response shapes so any current UI calling them continues to work.
  - Plan-based ranking is applied only on POST /api/search/priority.
  - Search responses keep legacy fields (tier, is_sponsored) and add
    optional fields (pool, is_priority, shared_position, reason, round).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.database import get_db
from app.services.priority_engine import RankedJob, build_ranking
from app.services.priority_list import load_priority_context, restamp_after_publish
from app.services.search_service import get_matching_jobs

router = APIRouter(tags=["priority-listing"])

# Kept for existing UIs that still show package-style limits.
TIER_LIMITS = {
    "basic": 0,
    "premium": 5,
    "enterprise": 10,
}

VALID_TIERS = ("basic", "premium", "enterprise")


class SponsorJobRequest(BaseModel):
    job_id: str
    employer_id: str
    is_sponsored: bool = True


class TierLimitsResponse(BaseModel):
    tier: str
    current_sponsored: int
    max_sponsored: int
    available_slots: int


class SubscribeRequest(BaseModel):
    tier: Literal["enterprise", "premium", "basic"] = Field(
        ..., description="Plan the employer wants to move to"
    )


class PrioritySearchRequest(BaseModel):
    keywords: str
    role: Optional[str] = None
    location: Optional[str] = None
    max_distance_km: Optional[float] = 50


def _query_footprint(req: PrioritySearchRequest) -> str:
    return f"{req.keywords.strip().lower()}|{(req.location or '').strip().lower()}"


def _next_round_counter(db, footprint: str) -> int:
    doc = db.round_counters.find_one_and_update(
        {"query_footprint": footprint},
        {
            "$inc": {"count": 1},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
        upsert=True,
        return_document=True,
    )
    return int((doc or {}).get("count") or 1)


def _serialize(ranked: List[RankedJob]) -> List[dict]:
    """Legacy fields first; new fields are additive for newer UIs."""
    out = []
    for r in ranked:
        out.append(
            {
                "job": r.job,
                "position": r.position,
                "tier": r.pool,
                "is_sponsored": r.is_sponsored,
                "badge": r.badge,
                "pool": r.pool,
                "is_priority": r.badge is not None,
                "shared_position": r.shared_position,
                "reason": r.reason,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Legacy subscription / sponsor routes — unchanged contracts
# ---------------------------------------------------------------------------


@router.post("/subscriptions/subscribe")
def subscribe(body: SubscribeRequest):
    """Direct subscription upgrade - no payment gateway required."""
    try:
        db = get_db()
        now = datetime.now(timezone.utc)

        sample_job = db.jobview.find_one()
        employer_id = (
            sample_job.get("employer_id") if sample_job else "test_employer_direct"
        )

        db.subscriptions.update_one(
            {"employer_id": employer_id},
            {
                "$set": {
                    "employer_id": employer_id,
                    "tier": body.tier,
                    "status": "active",
                    "updated_at": now,
                }
            },
            upsert=True,
        )

        db.entitlement_cache.update_one(
            {"employer_id": employer_id},
            {
                "$set": {
                    "tier": body.tier,
                    "cached_at": now,
                    "last_event": "upgrade",
                }
            },
            upsert=True,
        )

        # Plan change: restamp live jobs (does not unpublish anything).
        restamp_after_publish(db)

        return {
            "status": "active",
            "tier": body.tier,
            "message": f"Successfully upgraded to {body.tier} tier.",
        }
    except Exception as e:
        print(f"Subscription error: {str(e)}")
        import traceback

        traceback.print_exc()
        raise HTTPException(
            status_code=500, detail=f"Subscription request failed: {str(e)}"
        )


@router.get("/subscriptions/current")
def get_current_subscription():
    """Get the current subscription tier for the authenticated employer."""
    try:
        db = get_db()
        sample_job = db.jobview.find_one()
        employer_id = (
            sample_job.get("employer_id") if sample_job else "test_employer_direct"
        )

        cached = db.entitlement_cache.find_one({"employer_id": employer_id})
        if cached:
            return {"tier": cached.get("tier", "basic")}

        sub = db.subscriptions.find_one({"employer_id": employer_id})
        if sub:
            return {"tier": sub.get("tier", "basic")}

        return {"tier": "basic"}
    except Exception as e:
        print(f"Error getting current subscription: {str(e)}")
        return {"tier": "basic"}


@router.get("/subscriptions/limits")
def get_tier_limits():
    """Get current tier limits for sponsored jobs."""
    try:
        db = get_db()
        employer_id = "test_employer_direct"

        cached = db.entitlement_cache.find_one({"employer_id": employer_id})
        tier = cached.get("tier", "basic") if cached else "basic"

        current_sponsored = db.sponsored_jobs.count_documents(
            {"employer_id": employer_id, "is_sponsored": True}
        )

        max_sponsored = TIER_LIMITS.get(tier, 0)
        available_slots = max(0, max_sponsored - current_sponsored)

        return TierLimitsResponse(
            tier=tier,
            current_sponsored=current_sponsored,
            max_sponsored=max_sponsored,
            available_slots=available_slots,
        )
    except Exception as e:
        print(f"Error getting tier limits: {str(e)}")
        return TierLimitsResponse(
            tier="basic",
            current_sponsored=0,
            max_sponsored=0,
            available_slots=0,
        )


@router.post("/jobs/sponsor")
def sponsor_job(request: SponsorJobRequest):
    """
    Toggle job sponsorship status.

    Kept for current UIs. Plan-based search no longer requires this flag;
    priority_list + employer plan drive ranking on /search/priority.
    """
    try:
        db = get_db()

        if request.is_sponsored:
            limits = get_tier_limits()
            if limits.available_slots <= 0:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Cannot sponsor more jobs. Your {limits.tier} plan "
                        f"allows {limits.max_sponsored} sponsored jobs."
                    ),
                )

        db.sponsored_jobs.update_one(
            {"job_id": request.job_id, "employer_id": request.employer_id},
            {
                "$set": {
                    "is_sponsored": request.is_sponsored,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

        db.jobview.update_one(
            {"jobid": request.job_id},
            {"$set": {"is_sponsored": request.is_sponsored}},
        )

        action = "sponsored" if request.is_sponsored else "unsponsored"
        return {"status": "success", "action": action, "job_id": request.job_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error sponsoring job: {str(e)}")
        import traceback

        traceback.print_exc()
        raise HTTPException(
            status_code=500, detail=f"Failed to sponsor job: {str(e)}"
        )


# ---------------------------------------------------------------------------
# Plan-based priority search (only surface changed this phase)
# ---------------------------------------------------------------------------


@router.post("/search/priority")
def priority_search(req: PrioritySearchRequest):
    """
    Relevance/location match, then plan-based ranking with rotation,
    employer cap, and organic reserve. Does not alter GET /api/jobs.
    """
    try:
        matched_jobs = get_matching_jobs(
            keywords=req.keywords,
            role=req.role,
            location=req.location,
            max_distance_km=req.max_distance_km,
        )
        if not matched_jobs:
            return {"results": [], "round": 0}

        db = get_db()
        ctx = load_priority_context(db)
        entitlements = ctx["entitlements"]

        # Employers not in cache default to standard inside the ranker.
        footprint = _query_footprint(req)
        round_counter = _next_round_counter(db, footprint)

        ranked = build_ranking(
            matched_jobs,
            entitlements,
            round_counter,
            max_per_employer=int(ctx["maxSlotsPerEmployer"]),
            min_organic=int(ctx["organicReserve"]),
        )
        results = _serialize(ranked)

        now = datetime.now(timezone.utc)
        impressions = []
        for r in results:
            job = r["job"] or {}
            impressions.append(
                {
                    "job_id": job.get("jobid") or job.get("job_id") or job.get("_id"),
                    "employer_id": job.get("employer_id"),
                    "position": r["position"],
                    "pool": r["pool"],
                    "badge": r["badge"],
                    "shared_position": r["shared_position"],
                    "reason": r["reason"],
                    "query_footprint": footprint,
                    "ts": now,
                }
            )
        if impressions:
            db.impressions.insert_many(impressions)

        return {"results": results, "round": round_counter}
    except Exception as e:
        print(f"Error in priority search: {str(e)}")
        import traceback

        traceback.print_exc()
        raise HTTPException(
            status_code=500, detail=f"Priority search failed: {str(e)}"
        )
