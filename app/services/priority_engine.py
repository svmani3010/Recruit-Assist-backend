"""
priority_engine.py
------------------
E18 Priority Job Listing — pure ranking (no DB / network).

Plan-based, always on: every matching job from an Enterprise/Premium
employer is eligible. No per-job sponsor tick.

Pipeline:
  1. Pool segregation
  2. Cyclic rotation
  3. Employer cap (default 3 in top 10)
  4. Organic reservation (default 4 in top 10)
  5. Orchestration
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

ENTERPRISE_SLOTS = (1, 2)
PREMIUM_SLOTS = tuple(range(3, 11))
TOP_N = 10
MAX_PER_EMPLOYER = 3
MIN_ORGANIC_SLOTS = 4

# Candidate-facing badge (KV135 AC5). HR uses "Priority listing" separately.
BADGE_PROMOTED = "Promoted"


@dataclass
class RankedJob:
    job: dict
    position: int
    pool: str  # "enterprise" | "premium" | "standard"
    badge: Optional[str] = None
    shared_position: bool = False
    reason: Optional[str] = None

    @property
    def tier(self) -> str:
        """Alias for older response shapes that used `tier`."""
        return self.pool

    @property
    def is_sponsored(self) -> bool:
        """Alias: paid placement in this result set."""
        return self.badge is not None


def segregate_pools(
    matched_jobs: List[dict],
    entitlements: Dict[str, str],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    matched_jobs: already relevance/location filtered + sorted
                  (relevance then recency) by the search service.
    entitlements: {employer_id: "enterprise" | "premium" | "standard"}
    """
    pool_a: List[dict] = []
    pool_b: List[dict] = []
    pool_c: List[dict] = []
    for job in matched_jobs:
        employer_id = str(job.get("employer_id") or "")
        tier = (entitlements.get(employer_id) or "standard").strip().lower()
        if tier == "basic":
            tier = "standard"
        if tier == "enterprise":
            pool_a.append(job)
        elif tier == "premium":
            pool_b.append(job)
        else:
            pool_c.append(job)
    return pool_a, pool_b, pool_c


def rotate_pool(pool: List[dict], round_counter: int) -> List[dict]:
    """Cyclic shift so each job shares top slots fairly across searches."""
    n = len(pool)
    if n == 0:
        return []
    rotated: List[Optional[dict]] = [None] * n
    for base_rank, job in enumerate(pool):
        new_index = (base_rank + round_counter) % n
        rotated[new_index] = job
    return [j for j in rotated if j is not None]


def enforce_employer_cap(
    slotted_jobs: List[RankedJob],
    max_per_employer: int = MAX_PER_EMPLOYER,
) -> Tuple[List[RankedJob], List[RankedJob]]:
    seen_counts: Dict[str, int] = {}
    kept: List[RankedJob] = []
    bumped: List[RankedJob] = []

    for ranked in slotted_jobs:
        employer_id = str(ranked.job.get("employer_id") or "")
        seen_counts[employer_id] = seen_counts.get(employer_id, 0) + 1
        if seen_counts[employer_id] <= max_per_employer:
            kept.append(ranked)
        else:
            ranked.shared_position = True
            ranked.reason = "EMPLOYER_CAP"
            ranked.badge = None
            bumped.append(ranked)

    return kept, bumped


def reserve_organic_slots(
    priority_jobs: List[RankedJob],
    organic_jobs: List[dict],
    top_n: int = TOP_N,
    min_organic: int = MIN_ORGANIC_SLOTS,
) -> Tuple[List[RankedJob], List[RankedJob]]:
    if not organic_jobs:
        return priority_jobs, []

    max_priority_allowed = top_n - min_organic
    if len(priority_jobs) <= max_priority_allowed:
        return priority_jobs, []

    kept = priority_jobs[:max_priority_allowed]
    bumped = priority_jobs[max_priority_allowed:]
    for ranked in bumped:
        ranked.shared_position = True
        ranked.reason = "ORGANIC_RESERVATION"
        ranked.badge = None

    return kept, bumped


def build_ranking(
    matched_jobs: List[dict],
    entitlements: Dict[str, str],
    round_counter: int,
    *,
    max_per_employer: int = MAX_PER_EMPLOYER,
    min_organic: int = MIN_ORGANIC_SLOTS,
) -> List[RankedJob]:
    """
    Full fairness pipeline on an already filtered/sorted job list.

    Enterprise fills slots 1-2; Premium fills 3-10; Standard fills organic
    reserve and the rest. Excess priority jobs are marked Shared position.
    """
    pool_a, pool_b, pool_c = segregate_pools(matched_jobs, entitlements)

    rotated_a = rotate_pool(pool_a, round_counter)
    rotated_b = rotate_pool(pool_b, round_counter)

    slotted: List[RankedJob] = []
    for i, job in enumerate(rotated_a[: len(ENTERPRISE_SLOTS)]):
        slotted.append(
            RankedJob(
                job=job,
                position=ENTERPRISE_SLOTS[i],
                pool="enterprise",
                badge=BADGE_PROMOTED,
            )
        )
    for i, job in enumerate(rotated_b[: len(PREMIUM_SLOTS)]):
        slotted.append(
            RankedJob(
                job=job,
                position=PREMIUM_SLOTS[i],
                pool="premium",
                badge=BADGE_PROMOTED,
            )
        )

    slotted.sort(key=lambda r: r.position)

    kept, cap_bumped = enforce_employer_cap(slotted, max_per_employer=max_per_employer)
    kept, organic_bumped = reserve_organic_slots(
        kept, pool_c, top_n=TOP_N, min_organic=min_organic
    )

    # Jobs that matched but did not get a guaranteed slot this round.
    overflow_enterprise = rotated_a[len(ENTERPRISE_SLOTS) :]
    overflow_premium = rotated_b[len(PREMIUM_SLOTS) :]
    for job in overflow_enterprise:
        organic_bumped.append(
            RankedJob(
                job=job,
                position=0,
                pool="enterprise",
                badge=None,
                shared_position=True,
                reason="SHARED_POSITION",
            )
        )
    for job in overflow_premium:
        organic_bumped.append(
            RankedJob(
                job=job,
                position=0,
                pool="premium",
                badge=None,
                shared_position=True,
                reason="SHARED_POSITION",
            )
        )

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
                final.append(
                    RankedJob(
                        job=organic_job,
                        position=pos,
                        pool="standard",
                        badge=None,
                    )
                )
            elif next_priority is not None:
                # No organic left for this slot — keep priority in place.
                next_priority.position = pos
                final.append(next_priority)
                next_priority = next(priority_iter, None)

    # Drain any remaining kept priority that did not land on its exact slot.
    while next_priority is not None:
        next_priority.position = len(final) + 1
        final.append(next_priority)
        next_priority = next(priority_iter, None)

    below_fold: List[RankedJob] = []
    pos_cursor = max((r.position for r in final), default=TOP_N) + 1
    for ranked in cap_bumped + organic_bumped:
        ranked.position = pos_cursor
        below_fold.append(ranked)
        pos_cursor += 1
    for organic_job in organic_iter:
        below_fold.append(
            RankedJob(
                job=organic_job,
                position=pos_cursor,
                pool="standard",
                badge=None,
            )
        )
        pos_cursor += 1

    return final + below_fold
