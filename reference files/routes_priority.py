"""
routes_priority.py
-------------------
E18: Priority Job Listing & Cyclic Rotation Engine — FastAPI wiring.

Assumes these already exist in the codebase (per the brief: "we already
have endpoints for the rest of the options like sorting job etc"):

    - `get_current_user` / `get_current_admin_or_hr`  -> auth dependencies
    - `db`                                             -> Motor AsyncIOMotorDatabase
    - `search_service.get_matching_jobs(filters)`      -> Stage 1+2 of the
      pipeline (relevance + location pre-filter, sorted by relevance then
      recency). This module only adds priority ranking ON TOP of that.

Collections used (create them if they don't exist yet):
    subscriptions        {employer_id, tier, status, requested_at, updated_at}
    entitlement_cache     {employer_id, tier, cached_at}   (5-min SLA, Section 1)
    round_counters        {query_footprint, count, updated_at}
    impressions            {job_id, employer_id, position, pool, badge,
                             shared_position, reason, query_footprint,
                             candidate_id, ts}
    admin_sla_flags       {employer_id, reason, date, guaranteed_hit_rate}

Mount with:  app.include_router(router, prefix="/api")
"""

from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from priority_engine import build_ranking, RankedJob

# --- placeholders for the app's existing infra -----------------------------
# Replace these imports with the real ones from your app package.
from auth import get_current_user, get_current_admin_or_hr          # noqa: E402
from database import db                                              # noqa: E402
from search_service import get_matching_jobs                         # noqa: E402
# ----------------------------------------------------------------------------

router = APIRouter(tags=["priority-listing"])

CACHE_SLA_MINUTES = 5
VALID_TIERS = ("enterprise", "premium", "standard")


# =============================================================================
# Section 1 — Subscription & Entitlement Sync
# =============================================================================

class BillingWebhookPayload(BaseModel):
    employer_id: str
    event: Literal["upgrade", "downgrade", "expire"]
    new_tier: Literal["enterprise", "premium", "standard"]
    occurred_at: Optional[datetime] = None


@router.post("/webhooks/billing")
async def billing_webhook(payload: BillingWebhookPayload):
    """
    Real-time listener for billing lifecycle events. Writes straight
    through to the entitlement cache so the 5-minute invalidation SLA is
    trivially met (we don't wait for a cron — we update on receipt).

    Downgrade/expire rule: the job's "Promoted" label and priority rank
    are stripped immediately (i.e. entitlement_cache reflects the new
    tier right away); the job document itself is untouched and stays live.
    """
    now = datetime.now(timezone.utc)

    await db.entitlement_cache.update_one(
        {"employer_id": payload.employer_id},
        {"$set": {
            "tier": payload.new_tier,
            "cached_at": now,
            "last_event": payload.event,
        }},
        upsert=True,
    )

    # Keep an auditable history on the subscription record too.
    await db.subscriptions.update_one(
        {"employer_id": payload.employer_id},
        {"$set": {"tier": payload.new_tier, "status": payload.event,
                   "updated_at": now}},
        upsert=True,
    )

    return {"status": "ok", "employer_id": payload.employer_id,
            "tier": payload.new_tier, "cached_at": now.isoformat()}


async def get_entitlements_for_employers(employer_ids: List[str]) -> dict:
    """
    Reads from entitlement_cache. Any entry older than CACHE_SLA_MINUTES
    is treated as stale and re-synced from the source-of-truth
    `subscriptions` collection (belt-and-braces on top of the webhook
    push above, so a missed webhook can't silently overstay its tier).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=CACHE_SLA_MINUTES)
    cached = await db.entitlement_cache.find(
        {"employer_id": {"$in": employer_ids}}
    ).to_list(length=None)

    entitlements = {}
    stale_ids = []
    for row in cached:
        if row["cached_at"] >= cutoff:
            entitlements[row["employer_id"]] = row["tier"]
        else:
            stale_ids.append(row["employer_id"])

    missing_ids = [eid for eid in employer_ids
                   if eid not in entitlements and eid not in stale_ids] + stale_ids

    if missing_ids:
        fresh = await db.subscriptions.find(
            {"employer_id": {"$in": missing_ids}, "status": {"$ne": "expire"}}
        ).to_list(length=None)
        now = datetime.now(timezone.utc)
        for row in fresh:
            entitlements[row["employer_id"]] = row["tier"]
            await db.entitlement_cache.update_one(
                {"employer_id": row["employer_id"]},
                {"$set": {"tier": row["tier"], "cached_at": now}},
                upsert=True,
            )

    return entitlements  # employer_ids not found here default to "standard"


# =============================================================================
# Sections 2 + 3 + 4 — Priority Search
# =============================================================================

class PrioritySearchRequest(BaseModel):
    keywords: str
    role: Optional[str] = None
    location: Optional[str] = None
    max_distance_km: Optional[float] = 50


def query_footprint(req: PrioritySearchRequest) -> str:
    """Stable cache key for the round_counter, e.g. 'software engineer|bangalore'."""
    return f"{req.keywords.strip().lower()}|{(req.location or '').strip().lower()}"


async def next_round_counter(footprint: str) -> int:
    doc = await db.round_counters.find_one_and_update(
        {"query_footprint": footprint},
        {"$inc": {"count": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        upsert=True,
        return_document=True,
    )
    return doc["count"]


def serialize(ranked: List[RankedJob]) -> List[dict]:
    out = []
    for r in ranked:
        out.append({
            "job": r.job,
            "position": r.position,
            "pool": r.pool,
            "badge": r.badge,              # "Promoted" | "Priority listing" | None
            "is_priority": r.badge is not None,
            "shared_position": r.shared_position,
            "reason": r.reason,
        })
    return out


@router.post("/search/priority")
async def priority_search(
    req: PrioritySearchRequest,
    candidate=Depends(get_current_user),
):
    """
    Full pipeline: existing relevance/location search -> pool segregation
    -> cyclic rotation -> employer cap -> organic reservation -> impression
    logging. Returns the same job objects your existing search endpoint
    returns, enriched with position/badge/pool metadata.
    """
    matched_jobs = await get_matching_jobs(
        keywords=req.keywords, role=req.role,
        location=req.location, max_distance_km=req.max_distance_km,
    )
    if not matched_jobs:
        return {"results": [], "round": 0}

    employer_ids = list({job["employer_id"] for job in matched_jobs})
    entitlements = await get_entitlements_for_employers(employer_ids)

    footprint = query_footprint(req)
    round_counter = await next_round_counter(footprint)

    ranked = build_ranking(matched_jobs, entitlements, round_counter)
    results = serialize(ranked)

    # Impression logging (Section 5) — fire-and-forget style insert_many.
    now = datetime.now(timezone.utc)
    impressions = [{
        "job_id": r["job"].get("job_id") or r["job"].get("_id"),
        "employer_id": r["job"]["employer_id"],
        "position": r["position"],
        "pool": r["pool"],
        "badge": r["badge"],
        "shared_position": r["shared_position"],
        "reason": r["reason"],
        "query_footprint": footprint,
        "candidate_id": getattr(candidate, "id", None),
        "ts": now,
    } for r in results]
    if impressions:
        await db.impressions.insert_many(impressions)

    return {"results": results, "round": round_counter}


# =============================================================================
# Subscribe (no payment gateway — plan request saved for HR/admin approval)
# =============================================================================

class SubscribeRequest(BaseModel):
    tier: Literal["enterprise", "premium"] = Field(
        ..., description="Plan the employer wants to move to")


@router.post("/subscriptions/subscribe")
async def subscribe(
    body: SubscribeRequest,
    employer=Depends(get_current_user),
):
    """
    No payment gateway integration. Clicking "Subscribe" simply records
    the requested plan against the employer's account with status
    'pending', for HR/admin to approve from their console. Approval is a
    separate admin action (see `/admin/subscriptions/{employer_id}/approve`
    below) that actually flips the entitlement.
    """
    now = datetime.now(timezone.utc)
    await db.subscriptions.update_one(
        {"employer_id": employer.id},
        {"$set": {
            "employer_id": employer.id,
            "requested_tier": body.tier,
            "status": "pending",
            "requested_at": now,
        }},
        upsert=True,
    )
    return {"status": "pending", "tier": body.tier,
            "message": "Plan request submitted. An admin will confirm your "
                       "priority listing shortly."}


@router.post("/admin/subscriptions/{employer_id}/approve")
async def approve_subscription(
    employer_id: str,
    admin=Depends(get_current_admin_or_hr),
):
    """
    HR/admin-only: promotes a pending request into an active tier, which
    is what actually makes the employer's jobs eligible for Pool A/B.
    This reuses the same billing_webhook() write path so cache
    invalidation behaves identically to a real billing event.
    """
    sub = await db.subscriptions.find_one({"employer_id": employer_id})
    if not sub or sub.get("status") != "pending":
        raise HTTPException(404, "No pending subscription request for this employer")

    payload = BillingWebhookPayload(
        employer_id=employer_id, event="upgrade", new_tier=sub["requested_tier"])
    await billing_webhook(payload)

    await db.subscriptions.update_one(
        {"employer_id": employer_id},
        {"$set": {"status": "active", "tier": sub["requested_tier"],
                   "approved_by": getattr(admin, "id", None)}},
    )
    return {"status": "active", "tier": sub["requested_tier"]}


# =============================================================================
# Section 4/5 — Hot Jobs conflict note + Section 5 — Admin SLA reporting
# =============================================================================

async def daily_sla_report():
    """
    Intended to run as a daily cron (e.g. via APScheduler / Celery beat —
    wire the scheduler in your existing task-runner, this just does the
    computation). For each employer with priority impressions in the last
    24h, checks what fraction landed in their *guaranteed* slot range
    (1-2 for enterprise, 3-10 for premium) vs. got bumped, and flags any
    employer under a 90% hit rate, categorized by bump reason.
    """
    since = datetime.now(timezone.utc) - timedelta(days=1)
    pipeline = [
        {"$match": {"ts": {"$gte": since}, "pool": {"$in": ["enterprise", "premium"]}}},
        {"$group": {
            "_id": "$employer_id",
            "total": {"$sum": 1},
            "guaranteed_hits": {"$sum": {"$cond": [{"$eq": ["$shared_position", False]}, 1, 0]}},
            "shared_position_count": {"$sum": {"$cond": [{"$eq": ["$reason", "EMPLOYER_CAP"]}, 1, 0]}},
            "organic_reservation_count": {"$sum": {"$cond": [{"$eq": ["$reason", "ORGANIC_RESERVATION"]}, 1, 0]}},
        }},
    ]
    rows = await db.impressions.aggregate(pipeline).to_list(length=None)

    flags = []
    now = datetime.now(timezone.utc)
    for row in rows:
        hit_rate = row["guaranteed_hits"] / row["total"] if row["total"] else 1.0
        if hit_rate < 0.90:
            reason = ("Shared Position" if row["shared_position_count"] >= row["organic_reservation_count"]
                       else "Organic Reservation")
            flags.append({
                "employer_id": row["_id"],
                "reason": reason,
                "guaranteed_hit_rate": round(hit_rate, 4),
                "date": now.date().isoformat(),
            })

    if flags:
        await db.admin_sla_flags.insert_many(flags)
    return flags


@router.get("/admin/reports/priority-sla")
async def get_priority_sla_report(admin=Depends(get_current_admin_or_hr)):
    """On-demand view of today's flags (in addition to the daily cron write)."""
    flags = await daily_sla_report()
    return {"flags": flags, "count": len(flags)}
