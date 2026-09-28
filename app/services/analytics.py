"""Shared Job Performance Analytics service (KV140 E19).

AC2 — All metrics are calculated here. Screens must not invent their own formulas.
AC3 — Time-based metrics use median (average may be shown alongside, labelled).
AC5 — Withdrawn-within-24h, duplicates, and test data are excluded.
AC6 — Zero denominators surface as \"Not enough data\".
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bson import ObjectId

from app.services.application_policy import (
    APPLICATION_STATUS_SUPERSEDED,
    submitted_application_match,
)
from app.services.metric_definitions import (
    NOT_ENOUGH_DATA,
    get_definition,
    list_definitions,
)
from app.services.screening import (
    INITIAL_SCREENING_FINAL_PASS_THRESHOLD,
    l0_passed_from_score_doc,
)

logger = logging.getLogger(__name__)

COL_JOB_VIEWS = "job_detail_views"
COL_FEATURED = "featured_events"
COL_OFFERS = "offers"
COL_STAGE_EVENTS = "pipeline_stage_events"

# Minimum dwell time for a countable job view (seconds).
JOB_VIEW_MIN_SECONDS = 5
WITHDRAWAL_EXCLUSION_HOURS = 24

PIPELINE_STAGES = (
    "applied",
    "screening",
    "qualified",
    "approved",
    "interview",
    "offer",
    "hired",
)


def _as_utc_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            text = value.strip()
            if not text:
                return None
            if len(text) == 10 and text[4] == "-" and text[7] == "-":
                text = f"{text}T00:00:00+00:00"
            else:
                text = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _normalize_job_ids(job_ids: Sequence[Any]) -> List[Any]:
    normalized: set = set()
    for jid in job_ids:
        if jid is None or jid == "":
            continue
        normalized.add(jid)
        normalized.add(str(jid))
        try:
            normalized.add(int(jid))
        except (TypeError, ValueError):
            pass
    return list(normalized)


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.mean(values))


def _round_opt(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def _rate(numerator: int, denominator: int) -> Tuple[Optional[float], str]:
    """Return (rate 0-1 or None, display string). AC6."""
    if denominator <= 0:
        return None, NOT_ENOUGH_DATA
    rate = numerator / denominator
    return rate, f"{round(rate * 100, 1)}%"


def _metric_payload(
    key: str,
    *,
    value: Any = None,
    display: Optional[str] = None,
    numerator: Optional[int] = None,
    denominator: Optional[int] = None,
    average: Optional[float] = None,
    average_label: str = "Average",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    definition = get_definition(key)
    def_dict = definition.to_dict() if definition else {"key": key, "name": key}
    unit = (definition.unit if definition else "count")

    if display is None:
        if value is None and unit in {"rate", "days", "breakdown"}:
            display = NOT_ENOUGH_DATA
        elif value is None and unit == "count":
            display = "0"
            value = 0
        elif value is None:
            display = NOT_ENOUGH_DATA
        elif unit == "rate" and isinstance(value, (int, float)):
            display = f"{round(float(value) * 100, 1)}%"
        elif unit == "days" and isinstance(value, (int, float)):
            display = f"{round(float(value), 1)} days"
        else:
            display = str(value)

    payload: Dict[str, Any] = {
        "key": key,
        "name": def_dict.get("name", key),
        "value": value,
        "display": display,
        "unit": unit,
        "definition": {
            "name": def_dict.get("name"),
            "formula": def_dict.get("formula"),
            "excluded": def_dict.get("excluded"),
            "timeBasis": def_dict.get("time_basis"),
            "plainLanguage": def_dict.get("plain_language"),
            "usesMedian": def_dict.get("uses_median", False),
            "signedOffByProduct": def_dict.get("signed_off_by_product", False),
            "signedOffByClient": def_dict.get("signed_off_by_client", False),
        },
    }
    if numerator is not None:
        payload["numerator"] = numerator
    if denominator is not None:
        payload["denominator"] = denominator
    if average is not None:
        payload["average"] = average
        payload["averageLabel"] = average_label
        payload["averageDisplay"] = f"{average} days"
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Application eligibility (AC5)
# ---------------------------------------------------------------------------


def analytics_application_match(job_ids: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """Mongo match for applications included in analytics metrics.

    Excludes: in-progress stubs, superseded (duplicates), test data,
    and early withdrawals (handled in post-filter when dates matter).
    """
    base = submitted_application_match()
    # submitted_application_match already excludes in_progress + superseded.
    # Add explicit test-data exclusion.
    clauses: List[Dict[str, Any]] = [
        base,
        {"is_test": {"$ne": True}},
        {"test_data": {"$ne": True}},
        {"application_status": {"$ne": "withdrawn"}},
        {"status": {"$ne": "withdrawn"}},
    ]
    if job_ids is not None:
        normalized = _normalize_job_ids(job_ids)
        if not normalized:
            return {"_id": {"$exists": False}}
        clauses.append({"job_id": {"$in": normalized}})
    return {"$and": clauses}


def _is_early_withdrawal(app: dict) -> bool:
    """True when withdrawn within 24 hours of application (AC5)."""
    withdrawn_at = _as_utc_dt(app.get("withdrawn_at"))
    if not withdrawn_at:
        status = str(app.get("application_status") or app.get("status") or "").lower()
        if status != "withdrawn":
            return False
        # Withdrawn without timestamp — treat as excluded to be safe.
        return True
    applied = (
        _as_utc_dt(app.get("applied_date"))
        or _as_utc_dt(app.get("created_at"))
    )
    if not applied:
        oid = app.get("_id")
        if isinstance(oid, ObjectId):
            applied = oid.generation_time.replace(tzinfo=timezone.utc)
    if not applied:
        return True
    return (withdrawn_at - applied) <= timedelta(hours=WITHDRAWAL_EXCLUSION_HOURS)


def _effective_applied(app: dict) -> Optional[datetime]:
    for field in ("applied_date", "created_at"):
        dt = _as_utc_dt(app.get(field))
        if dt:
            return dt
    oid = app.get("_id")
    if isinstance(oid, ObjectId):
        return oid.generation_time.replace(tzinfo=timezone.utc)
    return None


def _in_period(
    dt: Optional[datetime],
    start: Optional[datetime],
    end: Optional[datetime],
) -> bool:
    if dt is None:
        return False
    if start is not None and dt < start:
        return False
    if end is not None and dt >= end:
        return False
    return True


def _load_eligible_applications(
    db,
    job_ids: Sequence[Any],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    *,
    date_field: str = "applied",
) -> List[dict]:
    match = analytics_application_match(job_ids)
    apps = list(db["candidateInformation"].find(match))
    out: List[dict] = []
    for app in apps:
        if _is_early_withdrawal(app):
            continue
        if date_field == "applied":
            when = _effective_applied(app)
        elif date_field == "hire":
            when = _as_utc_dt(app.get("hired_at") or app.get("hire_date"))
        else:
            when = _effective_applied(app)
        if start is not None or end is not None:
            if not _in_period(when, start, end):
                continue
        out.append(app)
    return out


def _app_key(app_or_score: dict) -> Tuple[str, str]:
    return (
        str(app_or_score.get("candidate_id") or ""),
        str(app_or_score.get("job_id") or ""),
    )


def _score_lookup(db, apps: Sequence[dict]) -> Dict[Tuple[str, str], dict]:
    if not apps:
        return {}
    candidate_ids = list({str(a.get("candidate_id")) for a in apps if a.get("candidate_id")})
    job_ids = _normalize_job_ids([a.get("job_id") for a in apps])
    scores = list(
        db["Scores"].find(
            {
                "candidate_id": {"$in": candidate_ids},
                "job_id": {"$in": job_ids},
            }
        )
    )
    by_key: Dict[Tuple[str, str], dict] = {}
    for s in scores:
        by_key[_app_key(s)] = s
    return by_key


def _is_qualified(score: Optional[dict]) -> bool:
    if not score:
        return False
    if score.get("l0_passed") is not None:
        return bool(l0_passed_from_score_doc(score))
    try:
        compat = float(score.get("compatibility") or score.get("llm_compatibility") or 0)
    except (TypeError, ValueError):
        compat = 0.0
    return compat >= INITIAL_SCREENING_FINAL_PASS_THRESHOLD


def _is_approved(score: Optional[dict]) -> bool:
    if not score:
        return False
    status = str(score.get("status") or "").strip().lower()
    return status == "approved" or bool(score.get("l1_approved_at"))


def _source_of(app: dict) -> str:
    raw = (
        app.get("attribution_source")
        or app.get("source")
        or app.get("found_us")
        or app.get("foundUs")
        or ""
    )
    text = str(raw).strip()
    return text if text else "Unknown"


def _technical_interview_enabled(job: dict) -> bool:
    """Jobs with Technical Interview toggle Off are excluded from that metric."""
    for key in (
        "technical_interview",
        "technicalInterview",
        "technical_interview_enabled",
        "technicalInterviewEnabled",
        "l2_enabled",
        "enableTechnicalInterview",
    ):
        if key in job and job[key] is not None:
            val = job[key]
            if isinstance(val, bool):
                return val
            text = str(val).strip().lower()
            if text in {"off", "false", "0", "no", "disabled"}:
                return False
            if text in {"on", "true", "1", "yes", "enabled"}:
                return True
    return True  # default On for legacy jobs


# ---------------------------------------------------------------------------
# Event recording (feeds metrics that need new collections)
# ---------------------------------------------------------------------------


def record_job_view(
    db,
    *,
    job_id: str,
    duration_seconds: float,
    viewer_type: str = "anonymous",
    is_org_user: bool = False,
    is_bot: bool = False,
    organization_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a job detail page view. Only views ≥5s count toward metrics."""
    now = datetime.now(timezone.utc)
    countable = (
        duration_seconds >= JOB_VIEW_MIN_SECONDS
        and not is_org_user
        and not is_bot
    )
    doc = {
        "job_id": str(job_id),
        "viewed_at": now,
        "duration_seconds": float(duration_seconds),
        "viewer_type": viewer_type,
        "is_org_user": bool(is_org_user),
        "is_bot": bool(is_bot),
        "countable": countable,
        "organization_id": organization_id,
        "session_id": session_id,
        "user_agent": user_agent,
    }
    result = db[COL_JOB_VIEWS].insert_one(doc)
    return {"id": str(result.inserted_id), "countable": countable}


def record_featured_event(
    db,
    *,
    job_id: str,
    event_type: str,
    organization_id: Optional[str] = None,
    is_bot: bool = False,
) -> Dict[str, Any]:
    event_type = str(event_type).strip().lower()
    if event_type not in {"impression", "click"}:
        raise ValueError("event_type must be 'impression' or 'click'")
    now = datetime.now(timezone.utc)
    doc = {
        "job_id": str(job_id),
        "event_type": event_type,
        "occurred_at": now,
        "organization_id": organization_id,
        "is_bot": bool(is_bot),
        "countable": not is_bot,
    }
    result = db[COL_FEATURED].insert_one(doc)
    return {"id": str(result.inserted_id), "event_type": event_type}


def upsert_offer(
    db,
    *,
    candidate_id: str,
    job_id: str,
    status: str,
    offered_at: Optional[datetime] = None,
    decided_at: Optional[datetime] = None,
    organization_id: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update an offer. status: made | accepted | declined | pending."""
    status = str(status).strip().lower()
    if status not in {"made", "accepted", "declined", "pending"}:
        raise ValueError("status must be made|accepted|declined|pending")
    now = datetime.now(timezone.utc)
    offered_at = offered_at or now
    query = {"candidate_id": str(candidate_id), "job_id": str(job_id)}
    existing = db[COL_OFFERS].find_one(query)
    fields: Dict[str, Any] = {
        "candidate_id": str(candidate_id),
        "job_id": str(job_id),
        "status": status,
        "offered_at": offered_at,
        "updated_at": now,
        "organization_id": organization_id,
    }
    if created_by:
        fields["created_by"] = created_by
    if status in {"accepted", "declined"}:
        fields["decided_at"] = decided_at or now
    if status == "accepted":
        fields["accepted_at"] = decided_at or now
    if existing:
        db[COL_OFFERS].update_one({"_id": existing["_id"]}, {"$set": fields})
        offer_id = str(existing["_id"])
    else:
        fields["created_at"] = now
        result = db[COL_OFFERS].insert_one(fields)
        offer_id = str(result.inserted_id)
    return {"id": offer_id, "status": status}


def mark_hired(
    db,
    *,
    candidate_id: str,
    job_id: str,
    hired_at: Optional[datetime] = None,
    organization_id: Optional[str] = None,
) -> Dict[str, Any]:
    now = hired_at or datetime.now(timezone.utc)
    result = db["candidateInformation"].update_many(
        {
            "candidate_id": str(candidate_id),
            "job_id": {"$in": _normalize_job_ids([job_id])},
            "application_status": {"$ne": APPLICATION_STATUS_SUPERSEDED},
        },
        {
            "$set": {
                "hired_at": now,
                "hire_date": now,
                "pipeline_status": "hired",
                "organization_id": organization_id,
            }
        },
    )
    # Ensure accepted offer exists for time-to-hire linkage when possible.
    offer = db[COL_OFFERS].find_one(
        {"candidate_id": str(candidate_id), "job_id": str(job_id)}
    )
    if offer and offer.get("status") != "accepted":
        db[COL_OFFERS].update_one(
            {"_id": offer["_id"]},
            {
                "$set": {
                    "status": "accepted",
                    "accepted_at": offer.get("accepted_at") or now,
                    "decided_at": offer.get("decided_at") or now,
                    "updated_at": now,
                }
            },
        )
    elif not offer:
        upsert_offer(
            db,
            candidate_id=candidate_id,
            job_id=job_id,
            status="accepted",
            offered_at=now,
            decided_at=now,
            organization_id=organization_id,
        )
    return {
        "candidate_id": str(candidate_id),
        "job_id": str(job_id),
        "hired_at": now.isoformat(),
        "matched": int(result.matched_count),
    }


def record_stage_exit(
    db,
    *,
    candidate_id: str,
    job_id: str,
    stage: str,
    entered_at: datetime,
    exited_at: Optional[datetime] = None,
    organization_id: Optional[str] = None,
) -> Dict[str, Any]:
    stage = str(stage).strip().lower()
    if stage not in PIPELINE_STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    exited_at = exited_at or datetime.now(timezone.utc)
    days = max(0.0, (exited_at - entered_at).total_seconds() / 86400.0)
    doc = {
        "candidate_id": str(candidate_id),
        "job_id": str(job_id),
        "stage": stage,
        "entered_at": entered_at,
        "exited_at": exited_at,
        "duration_days": days,
        "organization_id": organization_id,
        "waiting": False,
    }
    result = db[COL_STAGE_EVENTS].insert_one(doc)
    return {"id": str(result.inserted_id), "duration_days": days}


def ensure_analytics_indexes(db) -> None:
    db[COL_JOB_VIEWS].create_index([("job_id", 1), ("viewed_at", -1)])
    db[COL_JOB_VIEWS].create_index([("countable", 1), ("viewed_at", -1)])
    db[COL_FEATURED].create_index([("job_id", 1), ("occurred_at", -1)])
    db[COL_FEATURED].create_index([("event_type", 1), ("occurred_at", -1)])
    db[COL_OFFERS].create_index(
        [("candidate_id", 1), ("job_id", 1)], unique=True
    )
    db[COL_OFFERS].create_index([("status", 1), ("offered_at", -1)])
    db[COL_OFFERS].create_index([("accepted_at", -1)])
    db[COL_STAGE_EVENTS].create_index(
        [("stage", 1), ("exited_at", -1)]
    )
    db["candidateInformation"].create_index([("hired_at", -1)])
    db["candidateInformation"].create_index([("is_test", 1)])
    try:
        from app.services.analytics_platform import ensure_platform_indexes

        ensure_platform_indexes(db)
    except Exception:
        logger.exception("Failed creating analytics platform indexes")


# ---------------------------------------------------------------------------
# Metric calculators
# ---------------------------------------------------------------------------


def _scoped_jobs(
    db,
    job_scope_query: Dict[str, Any],
    *,
    include_drafts: bool = False,
) -> List[dict]:
    query = dict(job_scope_query or {})
    if not include_drafts:
        query = {
            "$and": [
                query,
                {"status": {"$ne": "Draft"}},
                {"status": {"$ne": "draft"}},
            ]
        } if query else {"status": {"$nin": ["Draft", "draft"]}}
    return list(db["jobview"].find(query))


def compute_total_jobs_posted(
    db, job_scope_query: Dict[str, Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    jobs = _scoped_jobs(db, job_scope_query)
    count = 0
    for job in jobs:
        status = str(job.get("status") or "").strip().lower()
        if status in {"draft", "pending", "pending_approval", "deleted"}:
            continue
        if job.get("deleted") or job.get("is_deleted"):
            continue
        show = str(job.get("show") or "").strip().lower()
        if show in {"off"} and status in {"draft", ""}:
            continue
        posted = (
            _as_utc_dt(job.get("posted_timestamp"))
            or _as_utc_dt(job.get("published_at"))
            or _as_utc_dt(job.get("created_at"))
            or _as_utc_dt(job.get("updated_at"))
        )
        if _in_period(posted, start, end):
            count += 1
    return _metric_payload("total_jobs_posted", value=count, display=str(count))


def compute_active_jobs(
    db, job_scope_query: Dict[str, Any], at: Optional[datetime] = None
) -> Dict[str, Any]:
    """Point-in-time: live jobs, aligned with Jobs list Active (show=on)."""
    _ = at  # reserved for historical snapshots
    jobs = _scoped_jobs(db, job_scope_query)
    count = 0
    for job in jobs:
        show = str(job.get("show") or "").strip().lower()
        status = str(job.get("status") or "").strip().lower()
        if job.get("deleted") or job.get("is_deleted"):
            continue
        if status in {"draft", "deleted", "pending", "pending_approval"}:
            continue
        if show in {"off", "closed", "scheduled"}:
            continue
        if status in {"closed", "paused"}:
            continue
        # Jobs UI: Active == show "on" (preferred). Also accept open/active status.
        is_live = show == "on" or status in {"open", "active", "published"}
        if not is_live:
            continue
        # Scheduled / not yet live
        if job.get("scheduled") and not (
            job.get("posted_timestamp") or job.get("published_at")
        ):
            continue
        live_at = _as_utc_dt(job.get("goes_live_at") or job.get("scheduled_at"))
        if live_at and live_at > datetime.now(timezone.utc):
            continue
        count += 1
    return _metric_payload("active_jobs", value=count, display=str(count))


def compute_job_views(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    normalized = _normalize_job_ids(job_ids)
    if not normalized:
        return _metric_payload("job_views", value=0, display="0")
    count = db[COL_JOB_VIEWS].count_documents(
        {
            "job_id": {"$in": [str(j) for j in normalized]},
            "countable": True,
            "viewed_at": {"$gte": start, "$lt": end},
        }
    )
    return _metric_payload("job_views", value=int(count), display=str(int(count)))


def compute_total_applications(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    apps = _load_eligible_applications(db, job_ids, start, end)
    n = len(apps)
    return _metric_payload("total_applications", value=n, display=str(n))


def compute_qualified_candidates(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    """Qualified by screening date when available, else application date."""
    apps = _load_eligible_applications(db, job_ids)
    scores = _score_lookup(db, apps)
    count = 0
    for app in apps:
        score = scores.get(_app_key(app))
        if not _is_qualified(score):
            continue
        screened_at = None
        if score:
            screened_at = (
                _as_utc_dt(score.get("l0_completed_at"))
                or _as_utc_dt(score.get("screened_at"))
                or _as_utc_dt(score.get("updated_at"))
            )
        when = screened_at or _effective_applied(app)
        if _in_period(when, start, end):
            count += 1
    return _metric_payload("qualified_candidates", value=count, display=str(count))


def compute_apply_rate(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    apps = compute_total_applications(db, job_ids, start, end)
    views = compute_job_views(db, job_ids, start, end)
    num = int(apps["value"] or 0)
    den = int(views["value"] or 0)
    rate, display = _rate(num, den)
    return _metric_payload(
        "apply_rate",
        value=rate,
        display=display,
        numerator=num,
        denominator=den,
    )


def compute_screening_rate(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    apps = _load_eligible_applications(db, job_ids, start, end)
    scores = _score_lookup(db, apps)
    qualified = sum(1 for a in apps if _is_qualified(scores.get(_app_key(a))))
    den = len(apps)
    rate, display = _rate(qualified, den)
    return _metric_payload(
        "screening_rate",
        value=rate,
        display=display,
        numerator=qualified,
        denominator=den,
    )


def compute_technical_interview_completion_rate(
    db,
    jobs: Sequence[dict],
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    enabled_ids = [
        j.get("jobid") for j in jobs if j.get("jobid") and _technical_interview_enabled(j)
    ]
    normalized = _normalize_job_ids(enabled_ids)
    if not normalized:
        return _metric_payload(
            "technical_interview_completion_rate",
            value=None,
            display=NOT_ENOUGH_DATA,
            numerator=0,
            denominator=0,
        )
    invites = list(
        db["interviewInvites"].find(
            {
                "job_id": {"$in": normalized},
                "created_at": {"$gte": start, "$lt": end},
            }
        )
    )
    # Also accept invitation_date field name variants
    if not invites:
        invites = list(
            db["interviewInvites"].find(
                {
                    "job_id": {"$in": [str(j) for j in normalized]},
                    "created_at": {"$gte": start, "$lt": end},
                }
            )
        )
    invited = len(invites)
    completed = sum(
        1
        for inv in invites
        if str(inv.get("status") or "").lower() == "completed"
    )
    rate, display = _rate(completed, invited)
    return _metric_payload(
        "technical_interview_completion_rate",
        value=rate,
        display=display,
        numerator=completed,
        denominator=invited,
    )


def compute_approval_rate(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    apps = _load_eligible_applications(db, job_ids)
    scores = _score_lookup(db, apps)
    qualified_keys = set()
    approved_in_period = 0
    for app in apps:
        key = _app_key(app)
        score = scores.get(key)
        if not _is_qualified(score):
            continue
        qualified_keys.add(key)
        if not _is_approved(score):
            continue
        decided = _as_utc_dt(score.get("l1_approved_at")) if score else None
        if _in_period(decided, start, end):
            approved_in_period += 1
    # Denominator: qualified candidates (same exclusion set); use period-qualified set size
    # Spec: approved ÷ qualified. Use qualified in scope (all-time eligible apps that are qualified)
    # with approvals dated in period — common funnel reporting. Prefer period-aligned:
    qualified_in_period = 0
    for app in apps:
        score = scores.get(_app_key(app))
        if not _is_qualified(score):
            continue
        screened_at = None
        if score:
            screened_at = (
                _as_utc_dt(score.get("l0_completed_at"))
                or _as_utc_dt(score.get("screened_at"))
            )
        when = screened_at or _effective_applied(app)
        if _in_period(when, start, end):
            qualified_in_period += 1
    den = qualified_in_period
    rate, display = _rate(approved_in_period, den)
    return _metric_payload(
        "approval_rate",
        value=rate,
        display=display,
        numerator=approved_in_period,
        denominator=den,
    )


def compute_interview_conversion_rate(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    apps = _load_eligible_applications(db, job_ids)
    scores = _score_lookup(db, apps)
    approved_apps = [a for a in apps if _is_approved(scores.get(_app_key(a)))]
    approved_count = len(approved_apps)
    if approved_count == 0:
        return _metric_payload(
            "interview_conversion_rate",
            value=None,
            display=NOT_ENOUGH_DATA,
            numerator=0,
            denominator=0,
        )

    normalized = _normalize_job_ids(job_ids)
    completed_invites = list(
        db["interviewInvites"].find(
            {
                "job_id": {"$in": normalized},
                "status": {"$regex": r"^completed$", "$options": "i"},
            }
        )
    )
    completed_keys = {
        (str(i.get("candidate_id")), str(i.get("job_id"))) for i in completed_invites
    }
    # Interview date filter: at least one completed interview whose outcome/scheduled is in period
    interviewed = 0
    for app in approved_apps:
        key = _app_key(app)
        if key not in completed_keys:
            continue
        matching = [
            i
            for i in completed_invites
            if (str(i.get("candidate_id")), str(i.get("job_id"))) == key
        ]
        dates = [
            _as_utc_dt(i.get("outcome_at"))
            or _as_utc_dt(i.get("completed_at"))
            or _as_utc_dt(i.get("scheduled_date"))
            for i in matching
        ]
        dates = [d for d in dates if d]
        if any(_in_period(d, start, end) for d in dates) or (
            not dates and start is None
        ):
            interviewed += 1
        elif dates and any(_in_period(d, start, end) for d in dates):
            interviewed += 1

    # If period filtering excluded everyone but we have completions, count any completion
    # for approved candidates when interview date missing.
    if interviewed == 0 and completed_keys:
        for app in approved_apps:
            if _app_key(app) in completed_keys:
                # Prefer counting when any invite falls in period via created_at
                matching = [
                    i
                    for i in completed_invites
                    if _app_key(i) == _app_key(app)
                ]
                for inv in matching:
                    when = (
                        _as_utc_dt(inv.get("outcome_at"))
                        or _as_utc_dt(inv.get("completed_at"))
                        or _as_utc_dt(inv.get("created_at"))
                    )
                    if _in_period(when, start, end):
                        interviewed += 1
                        break

    rate, display = _rate(interviewed, approved_count)
    return _metric_payload(
        "interview_conversion_rate",
        value=rate,
        display=display,
        numerator=interviewed,
        denominator=approved_count,
    )


def _interviewed_candidate_keys(
    db, job_ids: Sequence[Any]
) -> set:
    normalized = _normalize_job_ids(job_ids)
    invites = db["interviewInvites"].find(
        {
            "job_id": {"$in": normalized},
            "status": {"$regex": r"^completed$", "$options": "i"},
        },
        {"candidate_id": 1, "job_id": 1},
    )
    return {(str(i.get("candidate_id")), str(i.get("job_id"))) for i in invites}


def compute_interview_to_offer_rate(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    interviewed_keys = _interviewed_candidate_keys(db, job_ids)
    den = len(interviewed_keys)
    if den == 0:
        return _metric_payload(
            "interview_to_offer_rate",
            value=None,
            display=NOT_ENOUGH_DATA,
            numerator=0,
            denominator=0,
        )
    normalized = [str(j) for j in _normalize_job_ids(job_ids)]
    offers = list(
        db[COL_OFFERS].find(
            {
                "job_id": {"$in": normalized},
                "status": {"$in": ["made", "accepted", "declined", "pending"]},
                "offered_at": {"$gte": start, "$lt": end},
            }
        )
    )
    # Count unique interviewed candidates who received an offer in period
    offered = {
        (str(o.get("candidate_id")), str(o.get("job_id")))
        for o in offers
        if (str(o.get("candidate_id")), str(o.get("job_id"))) in interviewed_keys
    }
    num = len(offered)
    rate, display = _rate(num, den)
    return _metric_payload(
        "interview_to_offer_rate",
        value=rate,
        display=display,
        numerator=num,
        denominator=den,
    )


def compute_offer_acceptance_rate(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    normalized = [str(j) for j in _normalize_job_ids(job_ids)]
    # Exclude offers still awaiting a decision
    decided = list(
        db[COL_OFFERS].find(
            {
                "job_id": {"$in": normalized},
                "status": {"$in": ["accepted", "declined"]},
                "decided_at": {"$gte": start, "$lt": end},
            }
        )
    )
    made_decided = len(decided)
    accepted = sum(1 for o in decided if o.get("status") == "accepted")
    rate, display = _rate(accepted, made_decided)
    return _metric_payload(
        "offer_acceptance_rate",
        value=rate,
        display=display,
        numerator=accepted,
        denominator=made_decided,
    )


def compute_hires(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    apps = _load_eligible_applications(db, job_ids, start, end, date_field="hire")
    # Also catch pipeline_status hired with hired_at in period via broader query
    match = analytics_application_match(job_ids)
    extra = list(
        db["candidateInformation"].find(
            {
                "$and": [
                    match,
                    {
                        "$or": [
                            {"pipeline_status": "hired"},
                            {"hired_at": {"$exists": True, "$ne": None}},
                        ]
                    },
                ]
            }
        )
    )
    keys = set()
    for app in extra:
        if _is_early_withdrawal(app):
            continue
        when = _as_utc_dt(app.get("hired_at") or app.get("hire_date"))
        if _in_period(when, start, end):
            keys.add(_app_key(app))
    for app in apps:
        keys.add(_app_key(app))
    n = len(keys)
    return _metric_payload("hires", value=n, display=str(n))


def compute_time_to_hire(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    """Median days application → offer accepted, for hires in period (AC3)."""
    match = analytics_application_match(job_ids)
    hires = list(
        db["candidateInformation"].find(
            {
                "$and": [
                    match,
                    {"hired_at": {"$gte": start, "$lt": end}},
                ]
            }
        )
    )
    deltas: List[float] = []
    for app in hires:
        if _is_early_withdrawal(app):
            continue
        applied = _effective_applied(app)
        # Prefer offer accepted_at; fall back to hired_at
        offer = db[COL_OFFERS].find_one(
            {
                "candidate_id": str(app.get("candidate_id")),
                "job_id": str(app.get("job_id")),
                "status": "accepted",
            }
        )
        accepted = None
        if offer:
            accepted = _as_utc_dt(offer.get("accepted_at") or offer.get("decided_at"))
        if not accepted:
            accepted = _as_utc_dt(app.get("hired_at") or app.get("hire_date"))
        if not applied or not accepted:
            continue  # excluded: missing dates
        days = (accepted - applied).total_seconds() / 86400.0
        if days >= 0:
            deltas.append(days)

    median = _round_opt(_median(deltas))
    average = _round_opt(_mean(deltas))
    if median is None:
        return _metric_payload(
            "time_to_hire",
            value=None,
            display=NOT_ENOUGH_DATA,
            average=None,
        )
    return _metric_payload(
        "time_to_hire",
        value=median,
        display=f"{median} days",
        average=average,
        extra={"sampleSize": len(deltas)},
    )


def compute_time_to_fill(
    db, jobs: Sequence[dict], start: datetime, end: datetime
) -> Dict[str, Any]:
    deltas: List[float] = []
    for job in jobs:
        job_id = job.get("jobid")
        if not job_id:
            continue
        posted = _as_utc_dt(job.get("posted_timestamp") or job.get("published_at"))
        if not posted:
            continue
        first = db[COL_OFFERS].find_one(
            {
                "job_id": {"$in": [str(job_id), job_id]},
                "status": "accepted",
                "accepted_at": {"$gte": start, "$lt": end},
            },
            sort=[("accepted_at", 1)],
        )
        if not first:
            continue  # excluded: no accepted offer
        accepted = _as_utc_dt(first.get("accepted_at") or first.get("decided_at"))
        if not accepted:
            continue
        days = (accepted - posted).total_seconds() / 86400.0
        if days >= 0:
            deltas.append(days)

    median = _round_opt(_median(deltas))
    average = _round_opt(_mean(deltas))
    if median is None:
        return _metric_payload(
            "time_to_fill",
            value=None,
            display=NOT_ENOUGH_DATA,
        )
    return _metric_payload(
        "time_to_fill",
        value=median,
        display=f"{median} days",
        average=average,
        extra={"sampleSize": len(deltas)},
    )


def compute_stage_duration(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    normalized = [str(j) for j in _normalize_job_ids(job_ids)]
    events = list(
        db[COL_STAGE_EVENTS].find(
            {
                "job_id": {"$in": normalized},
                "waiting": {"$ne": True},
                "exited_at": {"$gte": start, "$lt": end},
            }
        )
    )
    by_stage: Dict[str, List[float]] = {s: [] for s in PIPELINE_STAGES}
    for ev in events:
        stage = str(ev.get("stage") or "").lower()
        if stage not in by_stage:
            continue
        days = ev.get("duration_days")
        if days is None:
            entered = _as_utc_dt(ev.get("entered_at"))
            exited = _as_utc_dt(ev.get("exited_at"))
            if entered and exited:
                days = (exited - entered).total_seconds() / 86400.0
        if days is not None and days >= 0:
            by_stage[stage].append(float(days))

    # Waiting = candidates still in stage (no exit)
    waiting_counts: Dict[str, int] = {}
    for stage in PIPELINE_STAGES:
        waiting_counts[stage] = db[COL_STAGE_EVENTS].count_documents(
            {
                "job_id": {"$in": normalized},
                "stage": stage,
                "waiting": True,
            }
        )

    stages_out = []
    for stage in PIPELINE_STAGES:
        vals = by_stage[stage]
        median = _round_opt(_median(vals))
        average = _round_opt(_mean(vals))
        stages_out.append(
            {
                "stage": stage,
                "medianDays": median,
                "averageDays": average,
                "display": f"{median} days" if median is not None else NOT_ENOUGH_DATA,
                "sampleSize": len(vals),
                "waiting": waiting_counts.get(stage, 0),
            }
        )

    has_data = any(s["medianDays"] is not None for s in stages_out)
    return _metric_payload(
        "stage_duration",
        value=stages_out if has_data else None,
        display="See stages" if has_data else NOT_ENOUGH_DATA,
        extra={"stages": stages_out},
    )


def compute_source_performance(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    apps = _load_eligible_applications(db, job_ids, start, end)
    scores = _score_lookup(db, apps)
    interviewed = _interviewed_candidate_keys(db, job_ids)
    normalized = [str(j) for j in _normalize_job_ids(job_ids)]
    offers = list(db[COL_OFFERS].find({"job_id": {"$in": normalized}}))
    offer_keys = {(str(o.get("candidate_id")), str(o.get("job_id"))) for o in offers}
    hire_keys = set()
    for app in apps:
        if app.get("pipeline_status") == "hired" or app.get("hired_at"):
            hire_keys.add(_app_key(app))

    buckets: Dict[str, Dict[str, int]] = {}
    for app in apps:
        source = _source_of(app)
        bucket = buckets.setdefault(
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
        key = _app_key(app)
        score = scores.get(key)
        bucket["applications"] += 1
        if _is_qualified(score):
            bucket["qualified"] += 1
        if _is_approved(score):
            bucket["approved"] += 1
        if key in interviewed:
            bucket["interviewed"] += 1
        if key in offer_keys:
            bucket["offers"] += 1
        if key in hire_keys:
            bucket["hires"] += 1

    rows = []
    for source, b in sorted(buckets.items(), key=lambda x: (-x[1]["applications"], x[0])):
        def step_rate(num: int, den: int) -> Dict[str, Any]:
            rate, display = _rate(num, den)
            return {"value": rate, "display": display, "numerator": num, "denominator": den}

        rows.append(
            {
                "source": source,
                "applications": b["applications"],
                "qualified": b["qualified"],
                "approved": b["approved"],
                "interviewed": b["interviewed"],
                "offers": b["offers"],
                "hires": b["hires"],
                "conversions": {
                    "applyToQualified": step_rate(b["qualified"], b["applications"]),
                    "qualifiedToApproved": step_rate(b["approved"], b["qualified"]),
                    "approvedToInterviewed": step_rate(b["interviewed"], b["approved"]),
                    "interviewedToOffer": step_rate(b["offers"], b["interviewed"]),
                    "offerToHire": step_rate(b["hires"], b["offers"]),
                },
            }
        )

    return _metric_payload(
        "source_performance",
        value=rows,
        display=str(len(rows)),
        extra={"sources": rows},
    )


def compute_featured_impressions(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    normalized = [str(j) for j in _normalize_job_ids(job_ids)]
    count = db[COL_FEATURED].count_documents(
        {
            "job_id": {"$in": normalized},
            "event_type": "impression",
            "countable": True,
            "occurred_at": {"$gte": start, "$lt": end},
        }
    )
    return _metric_payload(
        "featured_impressions", value=int(count), display=str(int(count))
    )


def compute_featured_ctr(
    db, job_ids: Sequence[Any], start: datetime, end: datetime
) -> Dict[str, Any]:
    impressions = compute_featured_impressions(db, job_ids, start, end)
    normalized = [str(j) for j in _normalize_job_ids(job_ids)]
    clicks = db[COL_FEATURED].count_documents(
        {
            "job_id": {"$in": normalized},
            "event_type": "click",
            "countable": True,
            "occurred_at": {"$gte": start, "$lt": end},
        }
    )
    den = int(impressions["value"] or 0)
    rate, display = _rate(int(clicks), den)
    return _metric_payload(
        "featured_click_through_rate",
        value=rate,
        display=display,
        numerator=int(clicks),
        denominator=den,
    )


# ---------------------------------------------------------------------------
# Dashboard aggregation (single entry point — AC2)
# ---------------------------------------------------------------------------


def resolve_period(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    *,
    days: Optional[int] = None,
) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if end is None:
        end = now
    if start is None:
        period_days = days if days is not None else 30
        start = end - timedelta(days=period_days)
    return start, end


def compute_dashboard(
    db,
    *,
    job_scope_query: Dict[str, Any],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    days: Optional[int] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute every hiring metric via this shared service."""
    start, end = resolve_period(start, end, days=days)
    jobs = _scoped_jobs(db, job_scope_query)
    if job_id:
        jobs = [j for j in jobs if str(j.get("jobid")) == str(job_id)]
    job_ids = [j.get("jobid") for j in jobs if j.get("jobid")]

    metrics = {
        "total_jobs_posted": compute_total_jobs_posted(db, job_scope_query, start, end),
        "active_jobs": compute_active_jobs(db, job_scope_query, at=end),
        "job_views": compute_job_views(db, job_ids, start, end),
        "apply_rate": compute_apply_rate(db, job_ids, start, end),
        "total_applications": compute_total_applications(db, job_ids, start, end),
        "qualified_candidates": compute_qualified_candidates(db, job_ids, start, end),
        "screening_rate": compute_screening_rate(db, job_ids, start, end),
        "technical_interview_completion_rate": compute_technical_interview_completion_rate(
            db, jobs, start, end
        ),
        "approval_rate": compute_approval_rate(db, job_ids, start, end),
        "interview_conversion_rate": compute_interview_conversion_rate(
            db, job_ids, start, end
        ),
        "interview_to_offer_rate": compute_interview_to_offer_rate(
            db, job_ids, start, end
        ),
        "offer_acceptance_rate": compute_offer_acceptance_rate(db, job_ids, start, end),
        "hires": compute_hires(db, job_ids, start, end),
        "time_to_hire": compute_time_to_hire(db, job_ids, start, end),
        "time_to_fill": compute_time_to_fill(db, jobs, start, end),
        "stage_duration": compute_stage_duration(db, job_ids, start, end),
        "source_performance": compute_source_performance(db, job_ids, start, end),
        "featured_impressions": compute_featured_impressions(db, job_ids, start, end),
        "featured_click_through_rate": compute_featured_ctr(db, job_ids, start, end),
    }

    # Highlight set for Complete Analytics Dashboard copy
    highlights = {
        "applications": metrics["total_applications"],
        "qualifiedCandidates": metrics["qualified_candidates"],
        "screeningRate": metrics["screening_rate"],
        "interviewConversion": metrics["interview_conversion_rate"],
        "timeToHire": metrics["time_to_hire"],
        "sourcePerformance": metrics["source_performance"],
    }

    return {
        "period": {
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "jobCount": len(job_ids),
        "jobId": job_id,
        "metrics": metrics,
        "highlights": highlights,
        "definitions": list_definitions(),
        "notEnoughDataLabel": NOT_ENOUGH_DATA,
    }
