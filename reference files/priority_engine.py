"""
priority_engine.py
-------------------
E18: Priority Job Listing & Cyclic Rotation Engine — core logic.

This module is pure computation (no DB / no network calls) so it can be
unit-tested in isolation. `routes_priority.py` wires it up to Mongo + cache.

Pipeline stages implemented here:
  1. Pool segregation      -> segregate_pools()
  2. Cyclic rotation math  -> rotate_pool()
  3. Employer cap          -> enforce_employer_cap()
  4. Organic reservation   -> reserve_organic_slots()
  5. Orchestration         -> build_ranking()
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

ENTERPRISE_SLOTS = (1, 2)          # Pool A -> positions 1-2
PREMIUM_SLOTS = tuple(range(3, 11))  # Pool B -> positions 3-10
TOP_N = 10
MAX_PER_EMPLOYER = 3
MIN_ORGANIC_SLOTS = 4


@dataclass
class RankedJob:
    job: dict
    position: int
    pool: str                      # "enterprise" | "premium" | "standard"
    badge: Optional[str] = None    # "Promoted" | "Priority listing" | None
    shared_position: bool = False  # True if bumped from its guaranteed slot
    reason: Optional[str] = None   # populated when shared_position is True


# ---------------------------------------------------------------------------
# Stage 1 — Pool segregation
# ---------------------------------------------------------------------------

def segregate_pools(
    matched_jobs: List[dict],
    entitlements: Dict[str, str],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    matched_jobs: jobs already relevance/location filtered + sorted
                  (Stage 1 + 2 of the pipeline — done upstream by the
                  existing search service before this module is called).
    entitlements: {employer_id: "enterprise" | "premium" | "standard"}
                  sourced from the cached subscription sync (Section 1).

    Jobs never seen in `entitlements` are treated as "standard" (lapsed /
    no active plan), matching the "Pool C" fallback rule.
    """
    pool_a, pool_b, pool_c = [], [], []
    for job in matched_jobs:
        tier = entitlements.get(job["employer_id"], "standard")
        if tier == "enterprise":
            pool_a.append(job)
        elif tier == "premium":
            pool_b.append(job)
        else:
            pool_c.append(job)
    return pool_a, pool_b, pool_c


# ---------------------------------------------------------------------------
# Stage 2 — Cyclic rotation ("The Matrix")
# ---------------------------------------------------------------------------

def rotate_pool(pool: List[dict], round_counter: int) -> List[dict]:
    """
    Applies the dynamic modulo rotation:

        new_index = (base_rank + round_counter) mod N

    where base_rank is the job's 0-based index in the relevance-sorted
    pool and N is the pool size. This is the same cyclic-shift construction
    discussed earlier (a Latin-square rotation) — over N consecutive
    rounds every job visits every slot in the pool exactly once, so
    fairness holds by construction rather than by chance.

    Returns a new list, reordered by rotated slot.
    """
    n = len(pool)
    if n == 0:
        return []

    rotated: List[Optional[dict]] = [None] * n
    for base_rank, job in enumerate(pool):
        new_index = (base_rank + round_counter) % n
        rotated[new_index] = job
    return rotated  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Stage 3 — Employer concentration cap
# ---------------------------------------------------------------------------

def enforce_employer_cap(
    slotted_jobs: List[RankedJob],
    max_per_employer: int = MAX_PER_EMPLOYER,
) -> Tuple[List[RankedJob], List[RankedJob]]:
    """
    Walks the priority-ranked list (positions 1-10) in order. The first
    `max_per_employer` jobs from a given employer keep their slot; any
    further job from that employer is stripped of priority status and
    returned separately so the caller can push it into the organic pool.

    Returns (kept, bumped).
    """
    seen_counts: Dict[str, int] = {}
    kept: List[RankedJob] = []
    bumped: List[RankedJob] = []

    for ranked in slotted_jobs:
        employer_id = ranked.job["employer_id"]
        seen_counts[employer_id] = seen_counts.get(employer_id, 0) + 1
        if seen_counts[employer_id] <= max_per_employer:
            kept.append(ranked)
        else:
            ranked.shared_position = True
            ranked.reason = "EMPLOYER_CAP"
            ranked.badge = None
            bumped.append(ranked)

    return kept, bumped


# ---------------------------------------------------------------------------
# Stage 4 — Organic reservation
# ---------------------------------------------------------------------------

def reserve_organic_slots(
    priority_jobs: List[RankedJob],
    organic_jobs: List[dict],
    top_n: int = TOP_N,
    min_organic: int = MIN_ORGANIC_SLOTS,
) -> Tuple[List[RankedJob], List[RankedJob]]:
    """
    Ensures at least `min_organic` of the top `top_n` slots are organic,
    PROVIDED organic matches actually exist for this query. If priority
    jobs currently occupy more than (top_n - min_organic) of the top
    slots, the lowest-ranked priority jobs in the current rotation round
    are bumped below position `top_n` to make room.

    Returns (kept_in_top_n, bumped_below_top_n).
    """
    if not organic_jobs:
        # Nothing organic to reserve room for — priority jobs keep their slots.
        return priority_jobs, []

    max_priority_allowed = top_n - min_organic
    if len(priority_jobs) <= max_priority_allowed:
        return priority_jobs, []

    # Lowest-ranked = highest position number = latest in the rotated list,
    # since priority_jobs is already ordered by ascending position.
    kept = priority_jobs[:max_priority_allowed]
    bumped = priority_jobs[max_priority_allowed:]
    for ranked in bumped:
        ranked.shared_position = True
        ranked.reason = "ORGANIC_RESERVATION"
        ranked.badge = None

    return kept, bumped


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def build_ranking(
    matched_jobs: List[dict],
    entitlements: Dict[str, str],
    round_counter: int,
) -> List[RankedJob]:
    """
    Runs the full pipeline (Sections 3 + 4 of the spec) on an already
    relevance/location-filtered and sorted job list, and returns the
    final ordered list of RankedJob for the top `TOP_N` positions plus
    any bumped jobs that fall through to organic below position 10.

    `matched_jobs` must already be sorted by keyword relevance then
    recency (Stage 2 of the spec — owned by the existing search service).
    """
    pool_a, pool_b, pool_c = segregate_pools(matched_jobs, entitlements)

    rotated_a = rotate_pool(pool_a, round_counter)
    rotated_b = rotate_pool(pool_b, round_counter)

    slotted: List[RankedJob] = []
    for i, job in enumerate(rotated_a[: len(ENTERPRISE_SLOTS)]):
        slotted.append(RankedJob(job=job, position=ENTERPRISE_SLOTS[i],
                                  pool="enterprise", badge="Promoted"))
    for i, job in enumerate(rotated_b[: len(PREMIUM_SLOTS)]):
        slotted.append(RankedJob(job=job, position=PREMIUM_SLOTS[i],
                                  pool="premium", badge="Priority listing"))

    slotted.sort(key=lambda r: r.position)

    kept, cap_bumped = enforce_employer_cap(slotted)
    kept, organic_bumped = reserve_organic_slots(kept, pool_c)

    # Re-number kept priority jobs into contiguous positions, then fill
    # the remainder of the top-10 window with organic matches.
    final: List[RankedJob] = []
    organic_iter = iter(pool_c)
    priority_iter = iter(kept)
    next_priority = next(priority_iter, None)

    for pos in range(1, TOP_N + 1):
        if next_priority is not None and next_priority.position == pos:
            next_priority.position = pos
            final.append(next_priority)
            next_priority = next(priority_iter, None)
        else:
            organic_job = next(organic_iter, None)
            if organic_job is not None:
                final.append(RankedJob(job=organic_job, position=pos,
                                        pool="standard", badge=None))
            # else: leave the slot for any remaining priority job to fill

    # Anything left over (bumped priority jobs + leftover organic) goes
    # below position 10, ordered: bumped priority first (still relevant,
    # just demoted), then remaining organic by original relevance order.
    below_fold: List[RankedJob] = []
    pos_cursor = TOP_N + 1
    for ranked in cap_bumped + organic_bumped:
        ranked.position = pos_cursor
        below_fold.append(ranked)
        pos_cursor += 1
    for organic_job in organic_iter:
        below_fold.append(RankedJob(job=organic_job, position=pos_cursor,
                                     pool="standard", badge=None))
        pos_cursor += 1

    return final + below_fold
