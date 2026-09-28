"""Job Performance Analytics platform layer (KV141–KV149).

Builds on the shared metric calculators in ``analytics.py`` (KV140).
All screens must call these helpers — never re-implement formulas.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bson import ObjectId

from app.rbac import constants as C
from app.rbac.scope_filter import build_scope_filter, managed_team_ids, merge_filters
from app.services import analytics as core
from app.services.metric_definitions import (
    NOT_ENOUGH_DATA,
    DEFINITION_VERSION,
    get_definition,
    list_definitions,
)

logger = logging.getLogger(__name__)

COL_SAVED_VIEWS = "analytics_saved_views"
COL_INSIGHT_DISMISSALS = "analytics_insight_dismissals"
COL_SNAPSHOTS = "analytics_snapshots"
COL_REFRESH_META = "analytics_refresh_meta"
COL_JOB_EVENTS = "job_analytics_events"
COL_EXPORT_AUDIT = "analytics_export_audit"

WAITING_TOO_LONG_DEFAULT_DAYS = 7
ATTRIBUTION_TRACKING_START = datetime(2025, 1, 1, tzinfo=timezone.utc)

# Lower is better for polarity (a rise is bad)
LOWER_IS_BETTER = frozenset(
    {
        "time_to_hire",
        "time_to_fill",
        "stage_duration",
    }
)

HEADLINE_KEYS = (
    "total_jobs_posted",
    "active_jobs",
    "job_views",
    "total_applications",
    "qualified_candidates",
    "apply_rate",
    "screening_rate",
    "technical_interview_completion_rate",
    "approval_rate",
    "interview_conversion_rate",
    "interview_to_offer_rate",
    "offer_acceptance_rate",
    "hires",
    "time_to_hire",
    "time_to_fill",
    "featured_impressions",
    "featured_click_through_rate",
)

FUNNEL_STAGES = (
    ("job_viewed", "Job viewed"),
    ("applied", "Applied"),
    ("qualified", "Qualified at screening"),
    ("technical_interview_completed", "Technical interview completed"),
    ("approved", "Approved by recruiter"),
    ("interviewed", "Interviewed"),
    ("offered", "Offered"),
    ("offer_accepted", "Offer accepted"),
    ("hired", "Hired"),
)

SOURCE_CANONICAL = {
    "kovetted": "Kovetted marketplace search",
    "marketplace": "Kovetted marketplace search",
    "featured": "Featured placement",
    "recommendation": "Job recommendations",
    "recommendations": "Job recommendations",
    "job_alert": "Job alerts",
    "alerts": "Job alerts",
    "linkedin": "Shared link — LinkedIn",
    "whatsapp": "Shared link — WhatsApp",
    "twitter": "Shared link — X",
    "x": "Shared link — X",
    "facebook": "Shared link — Facebook",
    "email": "Shared link — email",
    "copied_link": "Shared link — copied link",
    "copy_link": "Shared link — copied link",
    "indeed": "External job board",
    "naukri": "External job board",
    "external": "External job board",
    "referral": "Candidate referral",
    "referrals": "Candidate referral",
    "direct": "Direct",
    "unknown": "Unknown",
    "": "Unknown",
}

DATE_PRESETS = {
    "last_7_days": 7,
    "last_30_days": 30,
    "last_90_days": 90,
}


@dataclass
class AnalyticsFilters:
    start: datetime
    end: datetime
    preset: Optional[str] = None
    job_id: Optional[str] = None
    department: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[str] = None
    recruiter_id: Optional[str] = None
    featured: Optional[bool] = None
    urgent: Optional[bool] = None
    compare: bool = True
    compare_start: Optional[datetime] = None
    compare_end: Optional[datetime] = None

    def chips(self) -> List[Dict[str, str]]:
        chips: List[Dict[str, str]] = []
        if self.preset:
            chips.append({"key": "preset", "label": self.preset.replace("_", " "), "value": self.preset})
        else:
            chips.append(
                {
                    "key": "date_range",
                    "label": f"{self.start.date()} → {self.end.date()}",
                    "value": f"{self.start.isoformat()}|{self.end.isoformat()}",
                }
            )
        mapping = [
            ("job_id", self.job_id, "Job"),
            ("department", self.department, "Department"),
            ("location", self.location, "Location"),
            ("work_mode", self.work_mode, "Work mode"),
            ("recruiter_id", self.recruiter_id, "Recruiter"),
        ]
        for key, val, label in mapping:
            if val:
                chips.append({"key": key, "label": f"{label}: {val}", "value": str(val)})
        if self.featured is not None:
            chips.append(
                {
                    "key": "featured",
                    "label": "Featured" if self.featured else "Not featured",
                    "value": str(self.featured).lower(),
                }
            )
        if self.urgent is not None:
            chips.append(
                {
                    "key": "urgent",
                    "label": "Urgent" if self.urgent else "Not urgent",
                    "value": str(self.urgent).lower(),
                }
            )
        if self.compare:
            chips.append({"key": "compare", "label": "Compare previous period", "value": "true"})
        return chips

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": _iso(self.start),
            "end": _iso(self.end),
            "preset": self.preset,
            "jobId": self.job_id,
            "department": self.department,
            "location": self.location,
            "workMode": self.work_mode,
            "recruiterId": self.recruiter_id,
            "featured": self.featured,
            "urgent": self.urgent,
            "compare": self.compare,
            "compareStart": _iso(self.compare_start) if self.compare_start else None,
            "compareEnd": _iso(self.compare_end) if self.compare_end else None,
            "chips": self.chips(),
        }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_filters(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    preset: Optional[str] = None,
    days: Optional[int] = None,
    job_id: Optional[str] = None,
    department: Optional[str] = None,
    location: Optional[str] = None,
    work_mode: Optional[str] = None,
    recruiter_id: Optional[str] = None,
    featured: Optional[bool] = None,
    urgent: Optional[bool] = None,
    compare: bool = True,
    compare_start: Optional[datetime] = None,
    compare_end: Optional[datetime] = None,
) -> AnalyticsFilters:
    now = datetime.now(timezone.utc)
    preset_key = (preset or "").strip().lower() or None

    if preset_key == "this_quarter":
        q = (now.month - 1) // 3
        start = datetime(now.year, q * 3 + 1, 1, tzinfo=timezone.utc)
        end = now
    elif preset_key == "this_year":
        start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
        end = now
    elif preset_key in DATE_PRESETS:
        end = now
        start = end - timedelta(days=DATE_PRESETS[preset_key])
    elif start is None and end is None:
        end = now
        start = end - timedelta(days=days if days is not None else 30)
        preset_key = preset_key or "last_30_days"
    else:
        start, end = core.resolve_period(start, end, days=days)

    assert start is not None and end is not None
    duration = end - start
    if compare and not compare_start and not compare_end:
        compare_end = start
        compare_start = start - duration

    return AnalyticsFilters(
        start=start,
        end=end,
        preset=preset_key,
        job_id=job_id,
        department=department,
        location=location,
        work_mode=work_mode,
        recruiter_id=recruiter_id,
        featured=featured,
        urgent=urgent,
        compare=compare,
        compare_start=compare_start,
        compare_end=compare_end,
    )


# ---------------------------------------------------------------------------
# Role-scoped job queries (KV147)
# ---------------------------------------------------------------------------


def team_member_user_ids(user_id: str, org_id: str) -> List[str]:
    """Manager + members of teams they manage."""
    from app.database import get_db

    db = get_db()
    ids = {str(user_id)}
    team_ids = managed_team_ids(user_id, org_id)
    if not team_ids:
        return list(ids)
    members = db[C.COL_TEAM_MEMBERSHIPS].find(
        {"team_id": {"$in": team_ids}},
        {"user_id": 1},
    )
    for m in members:
        if m.get("user_id"):
            ids.add(str(m["user_id"]))
    return list(ids)


def build_analytics_job_scope(
    *,
    user_id: str,
    org_id: Optional[str],
    role_id: Optional[str],
    scope_flag: str,
    filters: Optional[AnalyticsFilters] = None,
) -> Dict[str, Any]:
    """Data-level scope: Manager = own + team jobs; others with yes = org-wide.

    Dual-path matches the Jobs list: include legacy rows that only have
    ``hr_user_id`` / ``created_by`` and may lack ``organization_id``.
    """
    if not org_id:
        return {
            "$or": [
                {"hr_user_id": str(user_id)},
                {"created_by": str(user_id)},
            ]
        }

    role_id = (role_id or "").strip().lower()
    if role_id == C.ROLE_MANAGER:
        member_ids = team_member_user_ids(user_id, org_id)
        base: Dict[str, Any] = {
            "$or": [
                {"hr_user_id": {"$in": member_ids}},
                {"created_by": {"$in": member_ids}},
                {
                    "$and": [
                        {
                            "$or": [
                                {"organization_id": str(org_id)},
                                {"org_id": str(org_id)},
                            ]
                        },
                        {
                            "$or": [
                                {"hr_user_id": {"$in": member_ids}},
                                {"created_by": {"$in": member_ids}},
                            ]
                        },
                    ]
                },
            ]
        }
    else:
        base = build_scope_filter(
            scope=scope_flag,
            user_id=user_id,
            org_id=org_id,
            collection="jobview",
        )

    extra: Dict[str, Any] = {}
    if filters:
        if filters.job_id:
            extra["jobid"] = {
                "$in": core._normalize_job_ids([filters.job_id])  # noqa: SLF001
            }
        if filters.department:
            dept = filters.department.strip()
            extra["$or"] = [
                {"department": {"$regex": f"^{dept}$", "$options": "i"}},
                {"jd_category": {"$regex": dept, "$options": "i"}},
            ]
        if filters.location:
            loc = filters.location.strip()
            extra["location"] = {"$regex": loc, "$options": "i"}
        if filters.work_mode:
            extra["workMode"] = {"$regex": filters.work_mode.strip(), "$options": "i"}
        if filters.recruiter_id:
            extra["hr_user_id"] = str(filters.recruiter_id)
        if filters.featured is not None:
            extra["featured"] = True if filters.featured else {"$ne": True}
        if filters.urgent is not None:
            extra["urgent"] = True if filters.urgent else {"$ne": True}

    return merge_filters(base, extra) if extra else base


# ---------------------------------------------------------------------------
# Refresh metadata (KV148)
# ---------------------------------------------------------------------------


def touch_refresh(db, *, org_id: Optional[str] = None) -> datetime:
    now = datetime.now(timezone.utc)
    db[COL_REFRESH_META].update_one(
        {"org_id": org_id or "_global"},
        {"$set": {"last_refreshed_at": now, "definition_version": DEFINITION_VERSION}},
        upsert=True,
    )
    return now


def get_last_refresh(db, *, org_id: Optional[str] = None) -> Dict[str, Any]:
    doc = db[COL_REFRESH_META].find_one({"org_id": org_id or "_global"}) or {}
    last = core._as_utc_dt(doc.get("last_refreshed_at"))  # noqa: SLF001
    if not last:
        last = touch_refresh(db, org_id=org_id)
    return {
        "lastRefreshedAt": _iso(last),
        "definitionVersion": doc.get("definition_version") or DEFINITION_VERSION,
        "refreshIntervalMinutes": 15,
    }


# ---------------------------------------------------------------------------
# Period compare + polarity (KV141)
# ---------------------------------------------------------------------------


def _numeric(metric: Dict[str, Any]) -> Optional[float]:
    val = metric.get("value")
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    return None


def attach_period_change(
    current: Dict[str, Any],
    previous: Dict[str, Any],
    *,
    key: str,
) -> Dict[str, Any]:
    """KV141 AC2/AC3 — delta with correct good/bad polarity."""
    out = dict(current)
    cur = _numeric(current)
    prev = _numeric(previous)
    lower_better = key in LOWER_IS_BETTER or bool(
        (current.get("definition") or {}).get("usesMedian")
        and key.startswith("time_")
    )

    if cur is None or prev is None or prev == 0:
        if cur is None and prev is None:
            out["periodChange"] = None
            out["periodChangeDisplay"] = NOT_ENOUGH_DATA
            out["periodChangeDirection"] = "flat"
            out["periodChangeSentiment"] = "neutral"
            return out
        if prev == 0 and cur is not None and cur > 0:
            pct = 100.0
        else:
            out["periodChange"] = None
            out["periodChangeDisplay"] = NOT_ENOUGH_DATA
            out["periodChangeDirection"] = "flat"
            out["periodChangeSentiment"] = "neutral"
            return out
    else:
        pct = ((cur - prev) / abs(prev)) * 100.0

    direction = "up" if pct > 0.05 else ("down" if pct < -0.05 else "flat")
    if direction == "flat":
        sentiment = "neutral"
    elif lower_better:
        sentiment = "negative" if direction == "up" else "positive"
    else:
        sentiment = "positive" if direction == "up" else "negative"

    arrow = "up" if direction == "up" else ("down" if direction == "down" else "unchanged")
    name = current.get("name") or key
    display_val = current.get("display") or str(cur)
    change_phrase = (
        f"{name} {display_val}, {arrow} {abs(round(pct, 1))}% on previous period"
        if direction != "flat"
        else f"{name} {display_val}, unchanged vs previous period"
    )
    out["periodChange"] = round(pct, 1)
    out["periodChangeDisplay"] = change_phrase
    out["periodChangeDirection"] = direction
    out["periodChangeSentiment"] = sentiment
    out["previousValue"] = previous.get("value")
    out["previousDisplay"] = previous.get("display")
    out["lowerIsBetter"] = lower_better
    return out


def compute_offers_made(db, job_ids: Sequence[Any], start: datetime, end: datetime) -> Dict[str, Any]:
    normalized = [str(j) for j in core._normalize_job_ids(job_ids)]  # noqa: SLF001
    count = db[core.COL_OFFERS].count_documents(
        {
            "job_id": {"$in": normalized},
            "status": {"$in": ["made", "accepted", "declined", "pending"]},
            "offered_at": {"$gte": start, "$lt": end},
        }
    )
    return core._metric_payload(  # noqa: SLF001
        "offers_made",
        value=int(count),
        display=str(int(count)),
    )


def _empty_state() -> Dict[str, Any]:
    return {
        "isEmpty": True,
        "title": "No hiring data yet",
        "message": (
            "Once you post jobs and start receiving applications, this dashboard "
            "will show jobs posted, applications, screening rates, interviews, "
            "offers, hires, and time-to-hire."
        ),
        "actions": [
            {"label": "Post a job", "href": "/recruiters/jobs/new"},
            {"label": "View metric definitions", "href": "/api/recruit/analytics/definitions"},
        ],
    }


def build_headlines(
    db,
    *,
    job_scope_query: Dict[str, Any],
    filters: AnalyticsFilters,
) -> Dict[str, Any]:
    """KV141 — headline row with period-over-period changes."""
    jobs = core._scoped_jobs(db, job_scope_query)  # noqa: SLF001
    if filters.job_id:
        jobs = [j for j in jobs if str(j.get("jobid")) == str(filters.job_id)]
    job_ids = [j.get("jobid") for j in jobs if j.get("jobid")]

    def pack(start: datetime, end: datetime) -> Dict[str, Dict[str, Any]]:
        return {
            "total_jobs_posted": core.compute_total_jobs_posted(
                db, job_scope_query, start, end
            ),
            "active_jobs": core.compute_active_jobs(db, job_scope_query, at=end),
            "job_views": core.compute_job_views(db, job_ids, start, end),
            "total_applications": core.compute_total_applications(
                db, job_ids, start, end
            ),
            "qualified_candidates": core.compute_qualified_candidates(
                db, job_ids, start, end
            ),
            "apply_rate": core.compute_apply_rate(db, job_ids, start, end),
            "screening_rate": core.compute_screening_rate(db, job_ids, start, end),
            "technical_interview_completion_rate": core.compute_technical_interview_completion_rate(
                db, jobs, start, end
            ),
            "approval_rate": core.compute_approval_rate(db, job_ids, start, end),
            "interview_conversion_rate": core.compute_interview_conversion_rate(
                db, job_ids, start, end
            ),
            "interview_to_offer_rate": core.compute_interview_to_offer_rate(
                db, job_ids, start, end
            ),
            "offer_acceptance_rate": core.compute_offer_acceptance_rate(
                db, job_ids, start, end
            ),
            "hires": core.compute_hires(db, job_ids, start, end),
            "time_to_hire": core.compute_time_to_hire(db, job_ids, start, end),
            "time_to_fill": core.compute_time_to_fill(db, jobs, start, end),
            "featured_impressions": core.compute_featured_impressions(
                db, job_ids, start, end
            ),
            "featured_click_through_rate": core.compute_featured_ctr(
                db, job_ids, start, end
            ),
        }

    current = pack(filters.start, filters.end)
    previous = {}
    if filters.compare and filters.compare_start and filters.compare_end:
        previous = pack(filters.compare_start, filters.compare_end)

    headlines = []
    for key in HEADLINE_KEYS:
        metric = current.get(key) or core._metric_payload(key, value=None)  # noqa: SLF001
        if previous:
            metric = attach_period_change(metric, previous.get(key) or {}, key=key)
        headlines.append({**metric, "detailPath": f"/analytics/{key}"})

    has_signal = any(
        (h.get("value") not in (None, 0, 0.0) and h.get("display") != NOT_ENOUGH_DATA)
        or (isinstance(h.get("value"), (int, float)) and h.get("value") not in (0, 0.0))
        for h in headlines
    )
    # Empty only when there are no jobs in scope at all (Active jobs are
    # point-in-time and should clear this once live roles are visible).
    apps = current["total_applications"].get("value") or 0
    posted = current["total_jobs_posted"].get("value") or 0
    active = current["active_jobs"].get("value") or 0
    is_empty = (not job_ids) and posted == 0 and active == 0 and apps == 0

    return {
        "headlines": headlines,
        "emptyState": _empty_state() if is_empty else None,
        "isEmpty": is_empty,
        "hasSignal": has_signal and not is_empty,
    }


# ---------------------------------------------------------------------------
# Funnel (KV142)
# ---------------------------------------------------------------------------


def compute_funnel(
    db,
    *,
    jobs: Sequence[dict],
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    job_ids = [j.get("jobid") for j in jobs if j.get("jobid")]
    tech_jobs = [j for j in jobs if core._technical_interview_enabled(j)]  # noqa: SLF001
    tech_ids = [j.get("jobid") for j in tech_jobs if j.get("jobid")]

    views = int(core.compute_job_views(db, job_ids, start, end)["value"] or 0)
    apps = core._load_eligible_applications(db, job_ids, start, end)  # noqa: SLF001
    scores = core._score_lookup(db, apps)  # noqa: SLF001
    applied = len(apps)
    qualified = sum(
        1 for a in apps if core._is_qualified(scores.get(core._app_key(a)))  # noqa: SLF001
    )
    approved = sum(
        1 for a in apps if core._is_approved(scores.get(core._app_key(a)))  # noqa: SLF001
    )

    tech_completed = 0
    include_tech = bool(tech_ids)
    if include_tech:
        tech_completed = db["interviewInvites"].count_documents(
            {
                "job_id": {"$in": core._normalize_job_ids(tech_ids)},  # noqa: SLF001
                "status": {"$regex": r"^completed$", "$options": "i"},
                "$or": [
                    {"outcome_at": {"$gte": start, "$lt": end}},
                    {"created_at": {"$gte": start, "$lt": end}},
                ],
            }
        )

    interviewed_keys = core._interviewed_candidate_keys(db, job_ids)  # noqa: SLF001
    interviewed = sum(1 for a in apps if core._app_key(a) in interviewed_keys)  # noqa: SLF001

    normalized = [str(j) for j in core._normalize_job_ids(job_ids)]  # noqa: SLF001
    offered = db[core.COL_OFFERS].count_documents(
        {
            "job_id": {"$in": normalized},
            "status": {"$in": ["made", "accepted", "declined", "pending"]},
            "offered_at": {"$gte": start, "$lt": end},
        }
    )
    accepted = db[core.COL_OFFERS].count_documents(
        {
            "job_id": {"$in": normalized},
            "status": "accepted",
            "accepted_at": {"$gte": start, "$lt": end},
        }
    )
    hired = int(core.compute_hires(db, job_ids, start, end)["value"] or 0)

    raw_steps = [
        ("job_viewed", views, True),
        ("applied", applied, True),
        ("qualified", qualified, True),
        ("technical_interview_completed", tech_completed, include_tech),
        ("approved", approved, True),
        ("interviewed", interviewed, True),
        ("offered", offered, True),
        ("offer_accepted", accepted, True),
        ("hired", hired, True),
    ]
    steps = [(k, c) for k, c, inc in raw_steps if inc]
    labels = {k: label for k, label in FUNNEL_STAGES}

    top = steps[0][1] if steps else 0
    enriched = []
    largest_drop_idx = None
    largest_drop = -1.0
    prev_count = None
    for idx, (key, count) in enumerate(steps):
        from_prev = None
        from_prev_display = NOT_ENOUGH_DATA
        drop = None
        if prev_count is not None:
            if prev_count <= 0:
                from_prev_display = NOT_ENOUGH_DATA
            else:
                from_prev = count / prev_count
                from_prev_display = f"{round(from_prev * 100, 1)}%"
                drop = prev_count - count
                if drop > largest_drop:
                    largest_drop = drop
                    largest_drop_idx = idx
        from_top = None
        from_top_display = NOT_ENOUGH_DATA
        if top > 0:
            from_top = count / top
            from_top_display = f"{round(from_top * 100, 1)}%"
        enriched.append(
            {
                "key": key,
                "label": labels.get(key, key),
                "count": count,
                "conversionFromPrevious": from_prev,
                "conversionFromPreviousDisplay": from_prev_display,
                "conversionFromTop": from_top,
                "conversionFromTopDisplay": from_top_display,
                "isLargestDrop": False,
                "candidatesPath": f"/api/recruit/analytics/funnel/{key}/candidates",
            }
        )
        prev_count = count

    if largest_drop_idx is not None and largest_drop_idx < len(enriched):
        enriched[largest_drop_idx]["isLargestDrop"] = True

    return {
        "stages": enriched,
        "largestDropStage": enriched[largest_drop_idx]["key"]
        if largest_drop_idx is not None
        else None,
        "definitionVersion": DEFINITION_VERSION,
    }


def list_funnel_stage_candidates(
    db,
    *,
    jobs: Sequence[dict],
    stage: str,
    start: datetime,
    end: datetime,
    limit: int = 100,
) -> Dict[str, Any]:
    """KV142 AC5 — candidate list for a stage (no contact PII by default)."""
    job_ids = [j.get("jobid") for j in jobs if j.get("jobid")]
    apps = core._load_eligible_applications(db, job_ids, start, end)  # noqa: SLF001
    scores = core._score_lookup(db, apps)  # noqa: SLF001
    interviewed = core._interviewed_candidate_keys(db, job_ids)  # noqa: SLF001
    stage = stage.strip().lower()
    rows = []

    for app in apps:
        key = core._app_key(app)  # noqa: SLF001
        score = scores.get(key)
        include = False
        if stage == "applied":
            include = True
        elif stage == "qualified":
            include = core._is_qualified(score)  # noqa: SLF001
        elif stage == "approved":
            include = core._is_approved(score)  # noqa: SLF001
        elif stage == "interviewed":
            include = key in interviewed
        elif stage == "hired":
            include = bool(app.get("hired_at") or app.get("pipeline_status") == "hired")
        elif stage == "offered":
            offer = db[core.COL_OFFERS].find_one(
                {"candidate_id": key[0], "job_id": str(key[1])}
            )
            include = bool(offer)
        elif stage == "offer_accepted":
            offer = db[core.COL_OFFERS].find_one(
                {
                    "candidate_id": key[0],
                    "job_id": str(key[1]),
                    "status": "accepted",
                }
            )
            include = bool(offer)
        elif stage == "technical_interview_completed":
            inv = db["interviewInvites"].find_one(
                {
                    "candidate_id": key[0],
                    "job_id": {"$in": core._normalize_job_ids([key[1]])},  # noqa: SLF001
                    "status": {"$regex": r"^completed$", "$options": "i"},
                }
            )
            include = bool(inv)
        elif stage == "job_viewed":
            continue  # views are anonymous sessions, not candidates
        if include:
            rows.append(
                {
                    "candidateId": key[0],
                    "jobId": str(key[1]),
                    "applicationId": app.get("application_id"),
                    "appliedAt": _iso(core._effective_applied(app)),  # noqa: SLF001
                    "source": normalize_source(app),
                    "status": app.get("application_status") or app.get("status"),
                }
            )
        if len(rows) >= limit:
            break

    return {"stage": stage, "count": len(rows), "candidates": rows}


# ---------------------------------------------------------------------------
# Sources (KV144)
# ---------------------------------------------------------------------------


def normalize_source(app: dict) -> str:
    raw = (
        app.get("attribution_source")
        or app.get("utm_source")
        or app.get("source")
        or app.get("found_us")
        or app.get("foundUs")
        or ""
    )
    text = str(raw).strip()
    if not text:
        applied = core._effective_applied(app)  # noqa: SLF001
        if applied and applied < ATTRIBUTION_TRACKING_START:
            return f"Not tracked before {ATTRIBUTION_TRACKING_START.date()}"
        return "Unknown"
    key = text.lower().replace(" ", "_")
    for token, label in SOURCE_CANONICAL.items():
        if token and token in key:
            return label
    # Title-case free text carefully
    return text if len(text) < 80 else text[:77] + "..."


def compute_source_performance_ranked(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    apps = core._load_eligible_applications(db, job_ids, start, end)  # noqa: SLF001
    scores = core._score_lookup(db, apps)  # noqa: SLF001
    interviewed = core._interviewed_candidate_keys(db, job_ids)  # noqa: SLF001
    normalized = [str(j) for j in core._normalize_job_ids(job_ids)]  # noqa: SLF001
    offers = list(db[core.COL_OFFERS].find({"job_id": {"$in": normalized}}))
    offer_keys = {(str(o.get("candidate_id")), str(o.get("job_id"))) for o in offers}

    buckets: Dict[str, Dict[str, int]] = {}
    for app in apps:
        source = normalize_source(app)
        b = buckets.setdefault(
            source,
            {
                "applications": 0,
                "qualified": 0,
                "approved": 0,
                "interviewed": 0,
                "offers": 0,
                "hires": 0,
            },
        )
        key = core._app_key(app)  # noqa: SLF001
        score = scores.get(key)
        b["applications"] += 1
        if core._is_qualified(score):  # noqa: SLF001
            b["qualified"] += 1
        if core._is_approved(score):  # noqa: SLF001
            b["approved"] += 1
        if key in interviewed:
            b["interviewed"] += 1
        if key in offer_keys:
            b["offers"] += 1
        if app.get("pipeline_status") == "hired" or app.get("hired_at"):
            b["hires"] += 1

    def rate(n: int, d: int) -> Optional[float]:
        return (n / d) if d > 0 else None

    rows = []
    for source, b in buckets.items():
        q_rate = rate(b["qualified"], b["applications"])
        rows.append(
            {
                "source": source,
                **b,
                "qualifiedRate": q_rate,
                "qualifiedRateDisplay": (
                    f"{round(q_rate * 100, 1)}%" if q_rate is not None else NOT_ENOUGH_DATA
                ),
                "hireRate": rate(b["hires"], b["applications"]),
                "conversions": {
                    "applyToQualified": _step(b["qualified"], b["applications"]),
                    "qualifiedToApproved": _step(b["approved"], b["qualified"]),
                    "approvedToInterviewed": _step(b["interviewed"], b["approved"]),
                    "interviewedToOffer": _step(b["offers"], b["interviewed"]),
                    "offerToHire": _step(b["hires"], b["offers"]),
                },
            }
        )

    # Rank by quality: hires desc, then qualified rate, then volume
    by_quality = sorted(
        rows,
        key=lambda r: (
            r["hires"],
            r["qualifiedRate"] if r["qualifiedRate"] is not None else -1,
            r["applications"],
        ),
        reverse=True,
    )
    by_volume = sorted(rows, key=lambda r: r["applications"], reverse=True)
    for i, r in enumerate(by_quality):
        r["qualityRank"] = i + 1
    for i, r in enumerate(by_volume):
        r["volumeRank"] = i + 1

    return {
        "sources": by_quality,
        "rankedBy": "quality",
        "note": "Ranked by hires, then qualified rate, then volume — not volume alone.",
        "definition": get_definition("source_performance").to_dict()
        if get_definition("source_performance")
        else None,
    }


def _step(num: int, den: int) -> Dict[str, Any]:
    if den <= 0:
        return {"value": None, "display": NOT_ENOUGH_DATA, "numerator": num, "denominator": den}
    val = num / den
    return {
        "value": val,
        "display": f"{round(val * 100, 1)}%",
        "numerator": num,
        "denominator": den,
    }


# ---------------------------------------------------------------------------
# Per-job analytics (KV143)
# ---------------------------------------------------------------------------


def days_open(job: dict, *, at: Optional[datetime] = None) -> Optional[float]:
    at = at or datetime.now(timezone.utc)
    posted = core._as_utc_dt(job.get("posted_timestamp") or job.get("published_at"))  # noqa: SLF001
    if not posted:
        return None
    closed = core._as_utc_dt(job.get("closed_at"))  # noqa: SLF001
    end = closed or at
    return max(0.0, (end - posted).total_seconds() / 86400.0)


def compute_job_daily_trend(
    db, job: dict, *, start: Optional[datetime] = None, end: Optional[datetime] = None
) -> Dict[str, Any]:
    job_id = job.get("jobid")
    posted = core._as_utc_dt(job.get("posted_timestamp")) or (  # noqa: SLF001
        datetime.now(timezone.utc) - timedelta(days=30)
    )
    start = start or posted
    end = end or datetime.now(timezone.utc)
    apps = core._load_eligible_applications(db, [job_id], start, end)  # noqa: SLF001
    scores = core._score_lookup(db, apps)  # noqa: SLF001

    by_day: Dict[str, Dict[str, int]] = {}
    cursor = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor < end:
        key = cursor.date().isoformat()
        by_day[key] = {"applications": 0, "qualified": 0}
        cursor += timedelta(days=1)

    for app in apps:
        applied = core._effective_applied(app)  # noqa: SLF001
        if not applied:
            continue
        key = applied.astimezone(timezone.utc).date().isoformat()
        if key not in by_day:
            by_day[key] = {"applications": 0, "qualified": 0}
        by_day[key]["applications"] += 1
        if core._is_qualified(scores.get(core._app_key(app))):  # noqa: SLF001
            by_day[key]["qualified"] += 1

    events = list(
        db[COL_JOB_EVENTS].find(
            {
                "job_id": str(job_id),
                "occurred_at": {"$gte": start, "$lt": end},
            }
        )
    )
    event_markers = [
        {
            "date": _iso(core._as_utc_dt(e.get("occurred_at"))),  # noqa: SLF001
            "type": e.get("event_type"),
            "label": e.get("label") or e.get("event_type"),
        }
        for e in events
    ]

    series = [
        {"date": d, "applications": v["applications"], "qualified": v["qualified"]}
        for d, v in sorted(by_day.items())
    ]
    return {"series": series, "events": event_markers}


def compare_job_to_org(
    db,
    *,
    job: dict,
    org_jobs: Sequence[dict],
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    job_id = job.get("jobid")
    job_ids = [job_id]
    org_ids = [j.get("jobid") for j in org_jobs if j.get("jobid")]
    job_rate = core.compute_screening_rate(db, job_ids, start, end)
    # Org average screening rate across similar jobs (same workMode when present)
    work = str(job.get("workMode") or "").strip().lower()
    similar = [
        j
        for j in org_jobs
        if str(j.get("jobid")) != str(job_id)
        and (
            not work
            or str(j.get("workMode") or "").strip().lower() == work
        )
    ]
    if not similar:
        similar = [j for j in org_jobs if str(j.get("jobid")) != str(job_id)]
    sim_ids = [j.get("jobid") for j in similar if j.get("jobid")]
    org_rate = (
        core.compute_screening_rate(db, sim_ids, start, end)
        if sim_ids
        else core._metric_payload("screening_rate", value=None)  # noqa: SLF001
    )
    jv = job_rate.get("value")
    ov = org_rate.get("value")
    summary = None
    if jv is not None and ov is not None:
        summary = (
            f"This job's screening rate is {round(jv * 100, 1)}%. "
            f"Your average is {round(ov * 100, 1)}%."
        )
    elif jv is not None:
        summary = f"This job's screening rate is {round(jv * 100, 1)}%. Organisation average: {NOT_ENOUGH_DATA}."
    return {
        "jobScreeningRate": job_rate,
        "orgAverageScreeningRate": org_rate,
        "summary": summary,
    }


def job_list_analytics_columns(
    db,
    *,
    jobs: Sequence[dict],
    start: datetime,
    end: datetime,
) -> List[Dict[str, Any]]:
    """KV143 AC6 — sortable columns for dashboard job list."""
    rows = []
    for job in jobs:
        jid = job.get("jobid")
        if not jid:
            continue
        apps = core.compute_total_applications(db, [jid], start, end)
        qual = core.compute_qualified_candidates(db, [jid], start, end)
        rate = core.compute_screening_rate(db, [jid], start, end)
        open_days = days_open(job)
        rows.append(
            {
                "jobId": jid,
                "title": job.get("jobrole") or job.get("title"),
                "status": job.get("status"),
                "show": job.get("show"),
                "applications": apps.get("value"),
                "qualifiedCandidates": qual.get("value"),
                "screeningRate": rate.get("value"),
                "screeningRateDisplay": rate.get("display"),
                "daysOpen": round(open_days, 1) if open_days is not None else None,
                "closed": str(job.get("status") or "").lower() == "closed"
                or str(job.get("show") or "").lower() in {"off", "closed"},
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Time analysis (KV146)
# ---------------------------------------------------------------------------


def compute_time_analysis(
    db,
    *,
    jobs: Sequence[dict],
    start: datetime,
    end: datetime,
    waiting_threshold_days: int = WAITING_TOO_LONG_DEFAULT_DAYS,
    breakdown_by_recruiter: bool = False,
) -> Dict[str, Any]:
    job_ids = [j.get("jobid") for j in jobs if j.get("jobid")]
    tth = core.compute_time_to_hire(db, job_ids, start, end)
    ttf = core.compute_time_to_fill(db, jobs, start, end)
    stages = core.compute_stage_duration(db, job_ids, start, end)
    stage_rows = stages.get("stages") or []

    slowest = None
    for s in stage_rows:
        if s.get("medianDays") is None:
            continue
        if slowest is None or float(s["medianDays"]) > float(slowest["medianDays"]):
            slowest = s

    # Waiting too long — open stage events
    normalized = [str(j) for j in core._normalize_job_ids(job_ids)]  # noqa: SLF001
    now = datetime.now(timezone.utc)
    waiting = list(
        db[core.COL_STAGE_EVENTS].find(
            {
                "job_id": {"$in": normalized},
                "waiting": True,
            }
        )
    )
    waiting_too_long = []
    for ev in waiting:
        entered = core._as_utc_dt(ev.get("entered_at"))  # noqa: SLF001
        if not entered:
            continue
        days = (now - entered).total_seconds() / 86400.0
        if days >= waiting_threshold_days:
            waiting_too_long.append(
                {
                    "candidateId": ev.get("candidate_id"),
                    "jobId": ev.get("job_id"),
                    "stage": ev.get("stage"),
                    "daysWaiting": round(days, 1),
                    "enteredAt": _iso(entered),
                    "actionPath": (
                        f"/recruiters/pipeline/{ev.get('job_id')}/applicants"
                        f"?candidate={ev.get('candidate_id')}"
                    ),
                }
            )

    by_recruiter = None
    if breakdown_by_recruiter:
        by_recruiter = []
        for job in jobs:
            rid = job.get("hr_user_id")
            if not rid:
                continue
            jids = [job.get("jobid")]
            by_recruiter.append(
                {
                    "recruiterId": rid,
                    "jobId": job.get("jobid"),
                    "timeToHire": core.compute_time_to_hire(db, jids, start, end),
                    "timeToFill": core.compute_time_to_fill(db, [job], start, end),
                }
            )

    return {
        "timeToHire": tth,
        "timeToFill": ttf,
        "stageDurations": stage_rows,
        "slowestStage": slowest,
        "waitingTooLong": waiting_too_long,
        "waitingThresholdDays": waiting_threshold_days,
        "byRecruiter": by_recruiter,
        "definitionVersion": DEFINITION_VERSION,
    }


# ---------------------------------------------------------------------------
# Insights (KV149)
# ---------------------------------------------------------------------------


def _dismissal_key(org_id: str, insight_type: str, job_id: str) -> str:
    return f"{org_id}:{insight_type}:{job_id}"


def is_insight_dismissed(db, *, org_id: str, insight_type: str, job_id: str) -> bool:
    key = _dismissal_key(org_id or "_", insight_type, job_id)
    doc = db[COL_INSIGHT_DISMISSALS].find_one({"key": key})
    if not doc:
        return False
    until = core._as_utc_dt(doc.get("dismissed_until"))  # noqa: SLF001
    return bool(until and until > datetime.now(timezone.utc))


def dismiss_insight(
    db,
    *,
    org_id: str,
    user_id: str,
    insight_type: str,
    job_id: str,
    days: int = 14,
) -> Dict[str, Any]:
    until = datetime.now(timezone.utc) + timedelta(days=days)
    key = _dismissal_key(org_id or "_", insight_type, job_id)
    db[COL_INSIGHT_DISMISSALS].update_one(
        {"key": key},
        {
            "$set": {
                "key": key,
                "org_id": org_id,
                "insight_type": insight_type,
                "job_id": job_id,
                "dismissed_by": user_id,
                "dismissed_at": datetime.now(timezone.utc),
                "dismissed_until": until,
            }
        },
        upsert=True,
    )
    return {"dismissedUntil": _iso(until)}


def generate_insights(
    db,
    *,
    org_id: str,
    jobs: Sequence[dict],
    start: datetime,
    end: datetime,
    limit: int = 5,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    org_ids = [j.get("jobid") for j in jobs if j.get("jobid")]
    org_screening = core.compute_screening_rate(db, org_ids, start, end)
    org_rate = org_screening.get("value")
    insights: List[Dict[str, Any]] = []

    for job in jobs:
        jid = str(job.get("jobid") or "")
        if not jid:
            continue
        posted = core._as_utc_dt(job.get("posted_timestamp"))  # noqa: SLF001
        apps = int(
            core.compute_total_applications(db, [jid], start, end).get("value") or 0
        )
        qual = int(
            core.compute_qualified_candidates(db, [jid], start, end).get("value") or 0
        )
        rate_m = core.compute_screening_rate(db, [jid], start, end)
        rate = rate_m.get("value")

        # Few applications after 7 days
        if posted and (now - posted).days >= 7 and apps < 3:
            itype = "low_applications"
            if not is_insight_dismissed(db, org_id=org_id, insight_type=itype, job_id=jid):
                insights.append(
                    {
                        "type": itype,
                        "jobId": jid,
                        "impact": 80 - apps,
                        "message": (
                            f"Job “{job.get('jobrole') or jid}” has only {apps} "
                            f"application(s) after {(now - posted).days} days."
                        ),
                        "action": {
                            "label": "Review job description",
                            "href": f"/recruiters/jobs/{jid}/edit",
                        },
                    }
                )

        # Screening far below org average
        if (
            org_rate is not None
            and rate is not None
            and org_rate >= 0.1
            and rate < org_rate * 0.6
        ):
            itype = "low_screening_rate"
            if not is_insight_dismissed(db, org_id=org_id, insight_type=itype, job_id=jid):
                insights.append(
                    {
                        "type": itype,
                        "jobId": jid,
                        "impact": int((org_rate - rate) * 100),
                        "message": (
                            f"Screening rate for “{job.get('jobrole') or jid}” is "
                            f"{round(rate * 100, 1)}%, well below your average of "
                            f"{round(org_rate * 100, 1)}%."
                        ),
                        "action": {
                            "label": "Review screening criteria",
                            "href": f"/recruiters/jobs/{jid}/analytics",
                        },
                    }
                )

        # Urgent with no qualified
        if job.get("urgent") and qual == 0 and apps > 0:
            itype = "urgent_no_qualified"
            if not is_insight_dismissed(db, org_id=org_id, insight_type=itype, job_id=jid):
                insights.append(
                    {
                        "type": itype,
                        "jobId": jid,
                        "impact": 90,
                        "message": (
                            f"Urgent job “{job.get('jobrole') or jid}” has "
                            f"{apps} application(s) but no qualified candidates."
                        ),
                        "action": {
                            "label": "View applicants",
                            "href": f"/recruiters/pipeline/{jid}/applicants",
                        },
                    }
                )

        # Featured no better than organic — simple heuristic
        if job.get("featured"):
            organic = [
                j
                for j in jobs
                if not j.get("featured") and str(j.get("jobid")) != jid
            ]
            if organic:
                org_apps = core.compute_total_applications(
                    db, [j.get("jobid") for j in organic[:20]], start, end
                )
                feat_apps = apps
                org_avg = (org_apps.get("value") or 0) / max(1, min(20, len(organic)))
                if feat_apps <= org_avg:
                    itype = "featured_underperforming"
                    if not is_insight_dismissed(
                        db, org_id=org_id, insight_type=itype, job_id=jid
                    ):
                        insights.append(
                            {
                                "type": itype,
                                "jobId": jid,
                                "impact": 50,
                                "message": (
                                    f"Featured job “{job.get('jobrole') or jid}” is "
                                    "not outperforming organic jobs on applications."
                                ),
                                "action": {
                                    "label": "Review featuring",
                                    "href": f"/recruiters/jobs/{jid}",
                                },
                            }
                        )

    # Waiting candidates insight
    time_view = compute_time_analysis(db, jobs=jobs, start=start, end=end)
    if time_view["waitingTooLong"]:
        sample = time_view["waitingTooLong"][0]
        itype = "waiting_too_long"
        jid = str(sample.get("jobId") or "")
        if jid and not is_insight_dismissed(
            db, org_id=org_id, insight_type=itype, job_id=jid
        ):
            insights.append(
                {
                    "type": itype,
                    "jobId": jid,
                    "impact": 70 + len(time_view["waitingTooLong"]),
                    "message": (
                        f"{len(time_view['waitingTooLong'])} candidate(s) have been "
                        f"waiting longer than {time_view['waitingThresholdDays']} days "
                        "at a stage."
                    ),
                    "action": {
                        "label": "View waiting candidates",
                        "href": "/api/recruit/analytics/time?waiting=1",
                    },
                }
            )

    insights.sort(key=lambda x: x.get("impact", 0), reverse=True)
    return {"insights": insights[:limit], "generatedAt": _iso(now)}


# ---------------------------------------------------------------------------
# Saved views (KV145)
# ---------------------------------------------------------------------------


def save_view(
    db,
    *,
    org_id: str,
    user_id: str,
    name: str,
    filters: AnalyticsFilters,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    doc = {
        "org_id": org_id,
        "user_id": user_id,
        "name": name.strip(),
        "filters": filters.to_dict(),
        "updated_at": now,
    }
    existing = db[COL_SAVED_VIEWS].find_one(
        {"org_id": org_id, "user_id": user_id, "name": name.strip()}
    )
    if existing:
        db[COL_SAVED_VIEWS].update_one({"_id": existing["_id"]}, {"$set": doc})
        view_id = str(existing["_id"])
    else:
        doc["created_at"] = now
        view_id = str(db[COL_SAVED_VIEWS].insert_one(doc).inserted_id)
    return {"id": view_id, "name": name.strip(), "filters": filters.to_dict()}


def list_saved_views(db, *, org_id: str, user_id: str) -> List[Dict[str, Any]]:
    rows = db[COL_SAVED_VIEWS].find(
        {"org_id": org_id, "user_id": user_id}
    ).sort("updated_at", -1)
    return [
        {
            "id": str(r["_id"]),
            "name": r.get("name"),
            "filters": r.get("filters"),
            "updatedAt": _iso(core._as_utc_dt(r.get("updated_at"))),  # noqa: SLF001
        }
        for r in rows
    ]


def get_saved_view(db, *, view_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    try:
        oid = ObjectId(view_id)
    except Exception:
        return None
    doc = db[COL_SAVED_VIEWS].find_one({"_id": oid, "user_id": user_id})
    if not doc:
        return None
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name"),
        "filters": doc.get("filters"),
    }


# ---------------------------------------------------------------------------
# Export (KV147)
# ---------------------------------------------------------------------------


def export_metrics_csv(
    *,
    title: str,
    filters: AnalyticsFilters,
    metrics: Dict[str, Any],
) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Job Performance Analytics export"])
    writer.writerow(["Title", title])
    writer.writerow(["Period start", _iso(filters.start)])
    writer.writerow(["Period end", _iso(filters.end)])
    writer.writerow(["Filters", "; ".join(c["label"] for c in filters.chips())])
    writer.writerow(["Definition version", DEFINITION_VERSION])
    writer.writerow([])
    writer.writerow(["Metric", "Value", "Display", "Numerator", "Denominator"])
    for key, metric in metrics.items():
        if not isinstance(metric, dict):
            continue
        if key in {"source_performance", "stage_duration"}:
            continue
        writer.writerow(
            [
                metric.get("name") or key,
                metric.get("value"),
                metric.get("display"),
                metric.get("numerator", ""),
                metric.get("denominator", ""),
            ]
        )
    return buf.getvalue().encode("utf-8")


def export_metrics_pdf(
    *,
    title: str,
    filters: AnalyticsFilters,
    metrics: Dict[str, Any],
) -> bytes:
    """Minimal PDF (no candidate PII) — text layout via simple PDF objects."""
    lines = [
        title,
        f"Period: {_iso(filters.start)} → {_iso(filters.end)}",
        f"Filters: {'; '.join(c['label'] for c in filters.chips())}",
        f"Definitions: v{DEFINITION_VERSION}",
        "",
    ]
    for key, metric in metrics.items():
        if not isinstance(metric, dict) or key in {
            "source_performance",
            "stage_duration",
        }:
            continue
        lines.append(
            f"{metric.get('name') or key}: {metric.get('display')}"
        )
    # Very small hand-rolled PDF
    content_lines = []
    y = 800
    for line in lines[:60]:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_lines.append(f"BT /F1 11 Tf 40 {y} Td ({safe[:110]}) Tj ET")
        y -= 16
    stream = "\n".join(content_lines).encode("latin-1", errors="replace")
    objects = []
    objects.append(b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n")
    objects.append(b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n")
    objects.append(
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj\n"
    )
    objects.append(
        f"4 0 obj<< /Length {len(stream)} >>stream\n".encode()
        + stream
        + b"\nendstream endobj\n"
    )
    objects.append(b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode())
    out.extend(
        f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return bytes(out)


def audit_export(
    db,
    *,
    org_id: Optional[str],
    user_id: str,
    export_type: str,
    filters: AnalyticsFilters,
    include_candidates: bool = False,
) -> str:
    doc = {
        "org_id": org_id,
        "user_id": user_id,
        "export_type": export_type,
        "include_candidates": include_candidates,
        "filters": filters.to_dict(),
        "created_at": datetime.now(timezone.utc),
    }
    result = db[COL_EXPORT_AUDIT].insert_one(doc)
    return str(result.inserted_id)


# ---------------------------------------------------------------------------
# Reconciliation (KV148)
# ---------------------------------------------------------------------------


def reconcile_headline_counts(
    db, *, job_scope_query: Dict[str, Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    jobs = core._scoped_jobs(db, job_scope_query)  # noqa: SLF001
    job_ids = [j.get("jobid") for j in jobs if j.get("jobid")]
    metric_apps = int(
        core.compute_total_applications(db, job_ids, start, end).get("value") or 0
    )
    # Underlying count using same eligibility helper
    underlying = len(core._load_eligible_applications(db, job_ids, start, end))  # noqa: SLF001
    metric_jobs = int(
        core.compute_total_jobs_posted(db, job_scope_query, start, end).get("value") or 0
    )
    mismatches = []
    if metric_apps != underlying:
        mismatches.append(
            {
                "metric": "total_applications",
                "metricValue": metric_apps,
                "underlying": underlying,
            }
        )
    ok = not mismatches
    if not ok:
        logger.error("Analytics reconciliation mismatch: %s", mismatches)
    return {
        "ok": ok,
        "checkedAt": _iso(datetime.now(timezone.utc)),
        "applications": {"metric": metric_apps, "underlying": underlying},
        "jobsPosted": {"metric": metric_jobs},
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Full dashboard assembly
# ---------------------------------------------------------------------------


def assemble_home_dashboard(
    db,
    *,
    job_scope_query: Dict[str, Any],
    filters: AnalyticsFilters,
    org_id: Optional[str],
    include_insights: bool = True,
    role_id: Optional[str] = None,
) -> Dict[str, Any]:
    jobs = core._scoped_jobs(db, job_scope_query)  # noqa: SLF001
    if filters.job_id:
        jobs = [j for j in jobs if str(j.get("jobid")) == str(filters.job_id)]

    headlines = build_headlines(db, job_scope_query=job_scope_query, filters=filters)
    full = core.compute_dashboard(
        db,
        job_scope_query=job_scope_query,
        start=filters.start,
        end=filters.end,
        job_id=filters.job_id,
    )
    # Inject offers_made into metrics
    job_ids = [j.get("jobid") for j in jobs if j.get("jobid")]
    full["metrics"]["offers_made"] = compute_offers_made(
        db, job_ids, filters.start, filters.end
    )
    full["metrics"]["source_performance"] = core._metric_payload(  # noqa: SLF001
        "source_performance",
        value=None,
        extra=compute_source_performance_ranked(
            db, job_ids, filters.start, filters.end
        ),
    )

    funnel = compute_funnel(db, jobs=jobs, start=filters.start, end=filters.end)
    time_analysis = compute_time_analysis(
        db,
        jobs=jobs,
        start=filters.start,
        end=filters.end,
        breakdown_by_recruiter=(role_id == C.ROLE_SUPER_ADMIN),
    )
    insights = (
        generate_insights(
            db,
            org_id=org_id or "_",
            jobs=jobs,
            start=filters.start,
            end=filters.end,
        )
        if include_insights and not headlines["isEmpty"]
        else {"insights": []}
    )

    return {
        "filters": filters.to_dict(),
        "refresh": get_last_refresh(db, org_id=org_id),
        "definitionVersion": DEFINITION_VERSION,
        "headlines": headlines["headlines"],
        "emptyState": headlines["emptyState"],
        "isEmpty": headlines["isEmpty"],
        "funnel": funnel,
        "metrics": full["metrics"],
        "highlights": full.get("highlights"),
        "timeAnalysis": time_analysis,
        "insights": insights.get("insights") or [],
        "jobList": job_list_analytics_columns(
            db, jobs=jobs, start=filters.start, end=filters.end
        ),
        "definitions": list_definitions(),
        "notEnoughDataLabel": NOT_ENOUGH_DATA,
        "datePresets": list(DATE_PRESETS.keys())
        + ["this_quarter", "this_year", "custom"],
    }


def ensure_platform_indexes(db) -> None:
    db[COL_SAVED_VIEWS].create_index([("org_id", 1), ("user_id", 1), ("name", 1)])
    db[COL_INSIGHT_DISMISSALS].create_index("key", unique=True)
    db[COL_INSIGHT_DISMISSALS].create_index("dismissed_until")
    db[COL_REFRESH_META].create_index("org_id", unique=True)
    db[COL_JOB_EVENTS].create_index([("job_id", 1), ("occurred_at", -1)])
    db[COL_EXPORT_AUDIT].create_index([("org_id", 1), ("created_at", -1)])
    db[COL_SNAPSHOTS].create_index([("org_id", 1), ("computed_at", -1)])
