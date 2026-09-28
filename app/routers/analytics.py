"""Job Performance Analytics API (KV140–KV149).

All metrics come from the shared analytics services.
Access: any authenticated HR user (role matrix for analytics.view deferred).
Job scope matches the Jobs list so counts stay consistent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth import AuthPrincipal, Role, require_hr
from app.database import get_db
from app.hr_tenant import ensure_hr_tenant_ready, hr_jobs_filter, require_hr_job
from app.rbac import constants as C
from app.services import analytics as analytics_service
from app.services import analytics_platform as platform
from app.services.metric_definitions import (
    definitions_sign_off_status,
    list_definitions,
)
from app.settings import get_settings

router = APIRouter(tags=["analytics"])

_ANALYTICS_DENIED = (
    "You do not have access to Job Performance Analytics. "
    "Contact your Super Admin if you need access."
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class JobViewEventRequest(BaseModel):
    job_id: str
    duration_seconds: float = Field(..., ge=0)
    session_id: Optional[str] = None
    is_bot: bool = False


class FeaturedEventRequest(BaseModel):
    job_id: str
    event_type: str = Field(..., description="impression | click")
    is_bot: bool = False


class OfferUpsertRequest(BaseModel):
    candidate_id: str
    job_id: str
    status: str = Field(..., description="made | accepted | declined | pending")
    offered_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None


class HireRequest(BaseModel):
    candidate_id: str
    job_id: str
    hired_at: Optional[datetime] = None


class StageExitRequest(BaseModel):
    candidate_id: str
    job_id: str
    stage: str
    entered_at: datetime
    exited_at: Optional[datetime] = None
    waiting: bool = False


class SaveViewRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    preset: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    job_id: Optional[str] = None
    department: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[str] = None
    recruiter_id: Optional[str] = None
    featured: Optional[bool] = None
    urgent: Optional[bool] = None
    compare: bool = True


class DismissInsightRequest(BaseModel):
    insight_type: str
    job_id: str


class JobEventRequest(BaseModel):
    job_id: str
    event_type: str = Field(
        ...,
        description="featured|urgent|shared|technical_interview_toggle|difficulty_change",
    )
    label: Optional[str] = None
    occurred_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Auth + scope
# ---------------------------------------------------------------------------


async def require_analytics_access(
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
) -> AuthPrincipal:
    """Any authenticated HR user can open analytics for now (role matrix later)."""
    return principal


def _analytics_job_scope(
    principal: AuthPrincipal,
    filters: Optional[platform.AnalyticsFilters] = None,
) -> dict:
    """Use the same job visibility as Jobs / Pipeline so Active counts match."""
    from app.routers.jobs import _hr_jobs_query

    extra = None
    if filters and filters.job_id:
        extra = {
            "jobid": {
                "$in": analytics_service._normalize_job_ids([filters.job_id])
            }
        }
    return _hr_jobs_query(principal, extra=extra)


def _org_id(principal: AuthPrincipal) -> Optional[str]:
    org = getattr(principal, "org_id", None)
    return str(org) if org else None


def _role_id(principal: AuthPrincipal) -> Optional[str]:
    return getattr(principal, "role_id", None) or None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text}T00:00:00+00:00"
    else:
        text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid datetime: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise HTTPException(status_code=400, detail=f"Invalid boolean: {value}")


def _filters_from_query(
    *,
    start: Optional[str],
    end: Optional[str],
    preset: Optional[str],
    days: Optional[int],
    job_id: Optional[str],
    department: Optional[str],
    location: Optional[str],
    work_mode: Optional[str],
    recruiter_id: Optional[str],
    featured: Optional[str],
    urgent: Optional[str],
    compare: bool,
    compare_start: Optional[str],
    compare_end: Optional[str],
) -> platform.AnalyticsFilters:
    return platform.resolve_filters(
        start=_parse_iso(start),
        end=_parse_iso(end),
        preset=preset,
        days=days,
        job_id=job_id,
        department=department,
        location=location,
        work_mode=work_mode,
        recruiter_id=recruiter_id,
        featured=_parse_bool(featured),
        urgent=_parse_bool(urgent),
        compare=compare,
        compare_start=_parse_iso(compare_start),
        compare_end=_parse_iso(compare_end),
    )


def _assert_job_in_analytics_scope(principal: AuthPrincipal, job_id: str, db) -> dict:
    """Allow access when the job is inside the caller's analytics scope (KV147)."""
    scope = _analytics_job_scope(principal)
    from app.rbac.scope_filter import merge_filters

    query = merge_filters(
        scope,
        {"jobid": {"$in": analytics_service._normalize_job_ids([job_id])}},
    )
    job = db["jobview"].find_one(query)
    if not job:
        # Fall back to legacy ownership check for solo HR accounts
        return require_hr_job(principal, job_id, db)
    return job


def _filter_query_params():
    return dict(
        start=Query(None),
        end=Query(None),
        preset=Query(None, description="last_7_days|last_30_days|last_90_days|this_quarter|this_year|custom"),
        days=Query(None, ge=1, le=366),
        job_id=Query(None),
        department=Query(None),
        location=Query(None),
        work_mode=Query(None),
        recruiter_id=Query(None),
        featured=Query(None),
        urgent=Query(None),
        compare=Query(True),
        compare_start=Query(None),
        compare_end=Query(None),
    )


# ---------------------------------------------------------------------------
# Definitions & home dashboard
# ---------------------------------------------------------------------------


@router.get("/api/recruit/analytics/definitions")
async def get_metric_definitions(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    _ = principal
    return {
        "definitions": list_definitions(),
        "signOff": definitions_sign_off_status(),
    }


@router.get("/api/recruit/analytics/dashboard")
async def get_analytics_dashboard(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
    start: Optional[str] = None,
    end: Optional[str] = None,
    preset: Optional[str] = Query("last_30_days"),
    days: Optional[int] = Query(None, ge=1, le=366),
    job_id: Optional[str] = None,
    department: Optional[str] = None,
    location: Optional[str] = None,
    work_mode: Optional[str] = None,
    recruiter_id: Optional[str] = None,
    featured: Optional[str] = None,
    urgent: Optional[str] = None,
    compare: bool = True,
    compare_start: Optional[str] = None,
    compare_end: Optional[str] = None,
):
    """KV141 home analytics dashboard (headlines, funnel, insights, job list)."""
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    filters = _filters_from_query(
        start=start,
        end=end,
        preset=preset,
        days=days,
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
    if job_id:
        _assert_job_in_analytics_scope(principal, job_id, db)
    scope = _analytics_job_scope(principal, filters)
    return platform.assemble_home_dashboard(
        db,
        job_scope_query=scope,
        filters=filters,
        org_id=_org_id(principal),
        role_id=_role_id(principal),
    )


@router.get("/api/recruit/analytics/jobs/{job_id}")
async def get_job_analytics(
    job_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
    start: Optional[str] = None,
    end: Optional[str] = None,
    preset: Optional[str] = Query("last_30_days"),
    days: Optional[int] = None,
    compare: bool = True,
):
    """KV143 — per-job analytics including funnel, trend, org comparison."""
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    _assert_job_in_analytics_scope(principal, job_id, db)
    filters = platform.resolve_filters(
        start=_parse_iso(start),
        end=_parse_iso(end),
        preset=preset,
        days=days,
        job_id=job_id,
        compare=compare,
    )
    scope = _analytics_job_scope(principal, filters)
    org_jobs = analytics_service._scoped_jobs(db, scope)
    jobs = [j for j in org_jobs if str(j.get("jobid")) == str(job_id)]
    if not jobs:
        raise HTTPException(status_code=404, detail="Job not found in analytics scope")
    job = jobs[0]
    dash = platform.assemble_home_dashboard(
        db,
        job_scope_query=scope,
        filters=filters,
        org_id=_org_id(principal),
        include_insights=False,
        role_id=_role_id(principal),
    )
    return {
        **dash,
        "jobId": job_id,
        "daysOpen": platform.days_open(job),
        "dailyTrend": platform.compute_job_daily_trend(
            db, job, start=filters.start, end=filters.end
        ),
        "vsOrganisation": platform.compare_job_to_org(
            db,
            job=job,
            org_jobs=org_jobs,
            start=filters.start,
            end=filters.end,
        ),
        "closedJobAnalyticsAvailable": True,
    }


@router.get("/api/recruit/analytics/funnel")
async def get_funnel(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
    start: Optional[str] = None,
    end: Optional[str] = None,
    preset: Optional[str] = Query("last_30_days"),
    job_id: Optional[str] = None,
    department: Optional[str] = None,
    location: Optional[str] = None,
    work_mode: Optional[str] = None,
    recruiter_id: Optional[str] = None,
):
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    filters = _filters_from_query(
        start=start,
        end=end,
        preset=preset,
        days=None,
        job_id=job_id,
        department=department,
        location=location,
        work_mode=work_mode,
        recruiter_id=recruiter_id,
        featured=None,
        urgent=None,
        compare=False,
        compare_start=None,
        compare_end=None,
    )
    scope = _analytics_job_scope(principal, filters)
    jobs = analytics_service._scoped_jobs(db, scope)
    return {
        "filters": filters.to_dict(),
        "funnel": platform.compute_funnel(
            db, jobs=jobs, start=filters.start, end=filters.end
        ),
        "refresh": platform.get_last_refresh(db, org_id=_org_id(principal)),
    }


@router.get("/api/recruit/analytics/funnel/{stage}/candidates")
async def get_funnel_candidates(
    stage: str,
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
    start: Optional[str] = None,
    end: Optional[str] = None,
    preset: Optional[str] = Query("last_30_days"),
    job_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    filters = platform.resolve_filters(
        start=_parse_iso(start),
        end=_parse_iso(end),
        preset=preset,
        job_id=job_id,
        compare=False,
    )
    scope = _analytics_job_scope(principal, filters)
    jobs = analytics_service._scoped_jobs(db, scope)
    return platform.list_funnel_stage_candidates(
        db,
        jobs=jobs,
        stage=stage,
        start=filters.start,
        end=filters.end,
        limit=limit,
    )


@router.get("/api/recruit/analytics/sources")
async def get_source_performance(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
    start: Optional[str] = None,
    end: Optional[str] = None,
    preset: Optional[str] = Query("last_30_days"),
    job_id: Optional[str] = None,
):
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    filters = platform.resolve_filters(
        start=_parse_iso(start),
        end=_parse_iso(end),
        preset=preset,
        job_id=job_id,
        compare=False,
    )
    scope = _analytics_job_scope(principal, filters)
    jobs = analytics_service._scoped_jobs(db, scope)
    job_ids = [j.get("jobid") for j in jobs if j.get("jobid")]
    return {
        "filters": filters.to_dict(),
        "metric": platform.compute_source_performance_ranked(
            db, job_ids, filters.start, filters.end
        ),
        "refresh": platform.get_last_refresh(db, org_id=_org_id(principal)),
    }


@router.get("/api/recruit/analytics/time")
async def get_time_analysis(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
    start: Optional[str] = None,
    end: Optional[str] = None,
    preset: Optional[str] = Query("last_30_days"),
    job_id: Optional[str] = None,
    waiting_threshold_days: int = Query(7, ge=1, le=90),
):
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    filters = platform.resolve_filters(
        start=_parse_iso(start),
        end=_parse_iso(end),
        preset=preset,
        job_id=job_id,
        compare=False,
    )
    scope = _analytics_job_scope(principal, filters)
    jobs = analytics_service._scoped_jobs(db, scope)
    role_id = _role_id(principal)
    return {
        "filters": filters.to_dict(),
        "analysis": platform.compute_time_analysis(
            db,
            jobs=jobs,
            start=filters.start,
            end=filters.end,
            waiting_threshold_days=waiting_threshold_days,
            breakdown_by_recruiter=(role_id == C.ROLE_SUPER_ADMIN),
        ),
        "refresh": platform.get_last_refresh(db, org_id=_org_id(principal)),
    }


@router.get("/api/recruit/analytics/insights")
async def get_insights(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
    start: Optional[str] = None,
    end: Optional[str] = None,
    preset: Optional[str] = Query("last_30_days"),
):
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    filters = platform.resolve_filters(
        start=_parse_iso(start), end=_parse_iso(end), preset=preset, compare=False
    )
    scope = _analytics_job_scope(principal, filters)
    jobs = analytics_service._scoped_jobs(db, scope)
    return platform.generate_insights(
        db,
        org_id=_org_id(principal) or "_",
        jobs=jobs,
        start=filters.start,
        end=filters.end,
    )


@router.post("/api/recruit/analytics/insights/dismiss")
async def dismiss_insight(
    payload: DismissInsightRequest,
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    db = get_db()
    return platform.dismiss_insight(
        db,
        org_id=_org_id(principal) or "_",
        user_id=principal.subject,
        insight_type=payload.insight_type,
        job_id=payload.job_id,
    )


# ---------------------------------------------------------------------------
# Saved views (KV145)
# ---------------------------------------------------------------------------


@router.get("/api/recruit/analytics/saved-views")
async def list_views(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    db = get_db()
    org_id = _org_id(principal)
    if not org_id:
        return {"views": []}
    return {
        "views": platform.list_saved_views(
            db, org_id=org_id, user_id=principal.subject
        )
    }


@router.post("/api/recruit/analytics/saved-views")
async def save_view(
    payload: SaveViewRequest,
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    db = get_db()
    org_id = _org_id(principal)
    if not org_id:
        raise HTTPException(status_code=400, detail="Organisation required to save views")
    filters = platform.resolve_filters(
        start=_parse_iso(payload.start),
        end=_parse_iso(payload.end),
        preset=payload.preset,
        job_id=payload.job_id,
        department=payload.department,
        location=payload.location,
        work_mode=payload.work_mode,
        recruiter_id=payload.recruiter_id,
        featured=payload.featured,
        urgent=payload.urgent,
        compare=payload.compare,
    )
    return platform.save_view(
        db,
        org_id=org_id,
        user_id=principal.subject,
        name=payload.name,
        filters=filters,
    )


@router.get("/api/recruit/analytics/saved-views/{view_id}")
async def get_view(
    view_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    db = get_db()
    view = platform.get_saved_view(db, view_id=view_id, user_id=principal.subject)
    if not view:
        raise HTTPException(status_code=404, detail="Saved view not found")
    return view


# ---------------------------------------------------------------------------
# Export (KV147)
# ---------------------------------------------------------------------------


@router.get("/api/recruit/analytics/export")
async def export_analytics(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
    format: str = Query("csv"),
    start: Optional[str] = None,
    end: Optional[str] = None,
    preset: Optional[str] = Query("last_30_days"),
    job_id: Optional[str] = None,
):
    """Aggregate export only — no candidate contact details."""
    fmt = (format or "csv").strip().lower()
    if fmt not in {"csv", "pdf"}:
        raise HTTPException(status_code=400, detail="format must be csv or pdf")
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    filters = platform.resolve_filters(
        start=_parse_iso(start),
        end=_parse_iso(end),
        preset=preset,
        job_id=job_id,
        compare=False,
    )
    scope = _analytics_job_scope(principal, filters)
    dash = platform.assemble_home_dashboard(
        db,
        job_scope_query=scope,
        filters=filters,
        org_id=_org_id(principal),
        include_insights=False,
        role_id=_role_id(principal),
    )
    metrics = dash.get("metrics") or {}
    platform.audit_export(
        db,
        org_id=_org_id(principal),
        user_id=principal.subject,
        export_type=fmt,
        filters=filters,
        include_candidates=False,
    )
    title = "Job Performance Analytics"
    if fmt == "pdf":
        body = platform.export_metrics_pdf(
            title=title, filters=filters, metrics=metrics
        )
        return Response(
            content=body,
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="analytics-export.pdf"'
            },
        )
    body = platform.export_metrics_csv(title=title, filters=filters, metrics=metrics)
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="analytics-export.csv"'},
    )


# ---------------------------------------------------------------------------
# Refresh / reconcile (KV148)
# ---------------------------------------------------------------------------


@router.get("/api/recruit/analytics/refresh")
async def get_refresh(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    db = get_db()
    return platform.get_last_refresh(db, org_id=_org_id(principal))


@router.post("/api/recruit/analytics/refresh")
async def force_refresh(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    db = get_db()
    ts = platform.touch_refresh(db, org_id=_org_id(principal))
    return {"lastRefreshedAt": ts.isoformat().replace("+00:00", "Z")}


@router.get("/api/recruit/analytics/reconcile")
async def reconcile(
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
    preset: Optional[str] = Query("last_30_days"),
):
    db = get_db()
    filters = platform.resolve_filters(preset=preset, compare=False)
    scope = _analytics_job_scope(principal, filters)
    return platform.reconcile_headline_counts(
        db,
        job_scope_query=scope,
        start=filters.start,
        end=filters.end,
    )


# ---------------------------------------------------------------------------
# Event ingestion
# ---------------------------------------------------------------------------


@router.post("/api/analytics/job-views")
async def post_job_view(payload: JobViewEventRequest, request: Request):
    db = get_db()
    job = db["jobview"].find_one(
        {"jobid": {"$in": analytics_service._normalize_job_ids([payload.job_id])}}
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    is_org_user = False
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        try:
            from jose import jwt

            token = auth.split(" ", 1)[1]
            claims = jwt.get_unverified_claims(token)
            subject = str(claims.get("sub") or "")
            role = str(claims.get("role") or "").lower()
            if role == "hr" and subject and subject == str(job.get("hr_user_id") or ""):
                is_org_user = True
            org_id = claims.get("org_id") or claims.get("organization_id")
            job_org = job.get("organization_id") or job.get("org_id")
            if role == "hr" and org_id and job_org and str(org_id) == str(job_org):
                is_org_user = True
        except Exception:
            pass

    ua = request.headers.get("user-agent") or ""
    is_bot = payload.is_bot or _looks_like_bot(ua)
    result = analytics_service.record_job_view(
        db,
        job_id=str(payload.job_id),
        duration_seconds=payload.duration_seconds,
        viewer_type="authenticated" if is_org_user else "anonymous",
        is_org_user=is_org_user,
        is_bot=is_bot,
        organization_id=str(job.get("organization_id") or "") or None,
        session_id=payload.session_id,
        user_agent=ua[:500] if ua else None,
    )
    return {"success": True, **result}


@router.post("/api/analytics/featured-events")
async def post_featured_event(payload: FeaturedEventRequest, request: Request):
    db = get_db()
    job = db["jobview"].find_one(
        {"jobid": {"$in": analytics_service._normalize_job_ids([payload.job_id])}}
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ua = request.headers.get("user-agent") or ""
    is_bot = payload.is_bot or _looks_like_bot(ua)
    try:
        result = analytics_service.record_featured_event(
            db,
            job_id=str(payload.job_id),
            event_type=payload.event_type,
            organization_id=str(job.get("organization_id") or "") or None,
            is_bot=is_bot,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **result}


@router.post("/api/recruit/analytics/offers")
async def upsert_offer(
    payload: OfferUpsertRequest,
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    _assert_job_in_analytics_scope(principal, payload.job_id, db)
    try:
        result = analytics_service.upsert_offer(
            db,
            candidate_id=payload.candidate_id,
            job_id=payload.job_id,
            status=payload.status,
            offered_at=payload.offered_at,
            decided_at=payload.decided_at,
            organization_id=_org_id(principal),
            created_by=principal.subject,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **result}


@router.post("/api/recruit/analytics/hires")
async def mark_hire(
    payload: HireRequest,
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    _assert_job_in_analytics_scope(principal, payload.job_id, db)
    result = analytics_service.mark_hired(
        db,
        candidate_id=payload.candidate_id,
        job_id=payload.job_id,
        hired_at=payload.hired_at,
        organization_id=_org_id(principal),
    )
    return {"success": True, **result}


@router.post("/api/recruit/analytics/stage-exits")
async def post_stage_exit(
    payload: StageExitRequest,
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    _assert_job_in_analytics_scope(principal, payload.job_id, db)
    if payload.waiting:
        doc = {
            "candidate_id": payload.candidate_id,
            "job_id": payload.job_id,
            "stage": payload.stage,
            "entered_at": payload.entered_at,
            "waiting": True,
            "organization_id": _org_id(principal),
        }
        result = db[analytics_service.COL_STAGE_EVENTS].insert_one(doc)
        return {"success": True, "id": str(result.inserted_id), "waiting": True}
    try:
        result = analytics_service.record_stage_exit(
            db,
            candidate_id=payload.candidate_id,
            job_id=payload.job_id,
            stage=payload.stage,
            entered_at=payload.entered_at,
            exited_at=payload.exited_at,
            organization_id=_org_id(principal),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **result}


@router.post("/api/recruit/analytics/job-events")
async def post_job_event(
    payload: JobEventRequest,
    principal: Annotated[AuthPrincipal, Depends(require_analytics_access)],
):
    """Markers for daily trend charts (featuring, urgent, sharing, toggle)."""
    db = get_db()
    _assert_job_in_analytics_scope(principal, payload.job_id, db)
    now = payload.occurred_at or datetime.now(timezone.utc)
    doc = {
        "job_id": str(payload.job_id),
        "event_type": payload.event_type,
        "label": payload.label or payload.event_type,
        "occurred_at": now,
        "created_by": principal.subject,
        "organization_id": _org_id(principal),
    }
    result = db[platform.COL_JOB_EVENTS].insert_one(doc)
    return {"success": True, "id": str(result.inserted_id)}


def _looks_like_bot(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    tokens = (
        "bot",
        "spider",
        "crawl",
        "slurp",
        "facebookexternalhit",
        "preview",
        "headless",
        "phantom",
        "selenium",
    )
    return any(t in ua for t in tokens)
