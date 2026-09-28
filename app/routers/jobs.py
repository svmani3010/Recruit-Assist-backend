"""Job posting and jobview endpoints."""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.auth import AuthPrincipal, Role, optional_hr, require_hr
from app.company_branding import (
    attach_company_branding,
    attach_hr_branding_to_job_payload,
)
from app.database import get_db
from app.experience_years import attach_experience_years_fields, enrich_job_experience_years
from app.job_text_sanitize import (
    sanitize_generated_job_description,
    sanitize_job_narrative_fields,
)
from app.hr_tenant import (
    ensure_hr_tenant_ready,
    hr_jobs_filter,
    require_hr_job,
    stamp_hr_job_payload,
)
from app.schemas import (
    GenerateJobDescriptionRequest,
    GenerateJobKeywordsRequest,
    JobCreateRequest,
    JobShowToggleRequest,
    DirectShareRequest,
    JobUpdateRequest,
    TechnicalInterviewToggleRequest,
    TechnicalInterviewToggleResponse,
    normalize_difficulty_level,
)
from app.services.jobs_proximity import sort_jobs_by_resolved_region
from app.services.screening import get_gemini, run_in_thread
from app.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


def _job_difficulty_level(data: dict) -> str:
    """Read and normalize difficulty_level from a job payload (default Intermediate)."""
    return normalize_difficulty_level(data.get("difficulty_level"))


def _hr_jobs_query(principal: AuthPrincipal, *, extra: Optional[dict] = None) -> dict:
    """Legacy hr_user_id filter, or RBAC scope filter when org RBAC is active."""
    from app.auth import Role
    from app.rbac.scope_filter import build_scope_filter, merge_filters

    settings = get_settings()
    use_rbac = (
        getattr(settings, "rbac_org_enabled", False)
        and getattr(principal, "role", None) == Role.HR
        and getattr(principal, "org_id", None)
    )

    if use_rbac:
        from app.rbac import constants as C
        from app.rbac.audit import audit_permission_refused
        from app.rbac.permissions import can

        decision = can(
            user_id=principal.subject,
            org_id=principal.org_id,
            module=C.MODULE_RECRUITMENT,
            action=C.ACTION_JOBS_LIST,
            membership_status=principal.membership_status,
        )
        if not decision.allowed:
            audit_permission_refused(
                actor_id=principal.subject,
                org_id=principal.org_id,
                module=C.MODULE_RECRUITMENT,
                permission_action=C.ACTION_JOBS_LIST,
                detail={"reason": decision.reason},
            )
            raise HTTPException(status_code=403, detail=decision.reason)
        scope = build_scope_filter(
            scope=decision.scope,
            user_id=principal.subject,
            org_id=principal.org_id,
            collection="jobview",
        )
        return merge_filters(scope, extra or {})

    base = hr_jobs_filter(principal)
    return merge_filters(base, extra or {}) if (base or extra) else {}


def _require_rbac_action(
    principal: AuthPrincipal,
    action: str,
    *,
    detail_fallback: str = "This action is not permitted for your role.",
) -> None:
    """Enforce matrix permission whenever RBAC is on and the caller is HR."""
    from app.auth import Role

    settings = get_settings()
    if not getattr(settings, "rbac_org_enabled", False):
        return
    if getattr(principal, "role", None) != Role.HR:
        return

    org_id = getattr(principal, "org_id", None)
    if not org_id:
        raise HTTPException(
            status_code=403,
            detail=detail_fallback,
        )

    from app.rbac import constants as C
    from app.rbac.audit import audit_permission_refused
    from app.rbac.permissions import can

    decision = can(
        user_id=principal.subject,
        org_id=org_id,
        module=C.MODULE_RECRUITMENT,
        action=action,
        membership_status=getattr(principal, "membership_status", None),
    )
    if decision.allowed:
        return

    audit_permission_refused(
        actor_id=principal.subject,
        org_id=org_id,
        module=C.MODULE_RECRUITMENT,
        permission_action=action,
        detail={"reason": decision.reason},
    )
    raise HTTPException(
        status_code=403,
        detail=decision.reason or detail_fallback,
    )


# --- Helper Functions (Must be defined before routes) ---


def _parse_job_description(jd_result: Any) -> dict:
    """Require a real JD JSON object; do not publish jobs with a parse error."""
    jd_data = jd_result
    if not isinstance(jd_data, dict):
        try:
            jd_data = json.loads(jd_result)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=502,
                detail="Could not parse the job description. Please try again.",
            ) from exc
    if not isinstance(jd_data, dict) or jd_data.get("error"):
        raise HTTPException(
            status_code=502,
            detail="Could not parse the job description. Please try again.",
        )
    return jd_data


def _save_job_as_draft(db, data: dict, principal: AuthPrincipal, jobid: str, parse_error: str) -> str:
    """Keep the posting so HR can retry publish when Gemini is available again."""
    now = datetime.now(timezone.utc)
    data["jobid"] = jobid
    data["status"] = "Draft"
    data["show"] = "off"
    data["updated_timestamp"] = now
    data["posted_timestamp"] = data.get("posted_timestamp") or now
    data["jd_parse_error"] = parse_error
    data["jd_parse_failed_at"] = now
    data["difficulty_level"] = _job_difficulty_level(data)
    data = _sync_category_fields(data)

    result = db["jobview"].update_one(
        {"jobid": jobid, **hr_jobs_filter(principal)},
        {"$set": data},
    )
    if result.matched_count == 0:
        db["jobview"].insert_one(data)
    return jobid


def _draft_saved_response(jobid: str, parse_error: str) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={
            "message": (
                "Job description could not be parsed right now. "
                "Your posting was saved as a draft so you can continue from here "
                "and publish when the service is available again."
            ),
            "jobid": jobid,
            "status": "Draft",
            "saved_as_draft": True,
            "reason": "jd_parse_failed",
            "detail": parse_error,
        },
    )


def generate_unique_jobid() -> str:
    """Generate a jobid that isn't already used in the jobview collection.

    Existing jobids elsewhere in this file (see _normalize_job_ids, which
    tries int(jid)) are treated as numeric strings, so this generates a
    random 6-digit numeric string and re-rolls on the rare collision.
    """
    db = get_db()
    for _ in range(20):  # 20 attempts is effectively guaranteed at this ID space size
        candidate = str(random.randint(100000, 999999))
        if not db["jobview"].find_one({"jobid": candidate}):
            return candidate
    raise RuntimeError("Could not generate a unique jobid after 20 attempts")


def _compose_job_text(data: dict) -> str:
    manual_kw = data.get("jd_keywords") or []
    if isinstance(manual_kw, str):
        manual_kw = [k.strip() for k in manual_kw.split(",") if k.strip()]
    return f"""
    Job Role: {data.get("jobrole", "")}
    Job Description: {data.get("jobDescription", "")}
    Roles and Responsibilities: {data.get("rolesResponsibilities", "")}
    Educational Qualifications: {data.get("educationalQualifications", "")}
    Experience Required: {data.get("experienceRequired", "")}
    Desired Skills: {data.get("desiredSkills", "")}
    Required Certifications: {data.get("requiredCertifications", "")}
    Good to Have: {data.get("goodToHave", "")}
    Employment Type: {data.get("employmentType", "")}
    Work Mode: {data.get("workMode", "")}
    Job Location: {data.get("location", "")}
    Joining Requirement: {data.get("joiningRequirement", "")}
    Salary Package: {data.get("salaryPackage", "")}
    Category: {data.get("category", "")}
    Manual Keywords: {", ".join(manual_kw)}
    """


def _sync_category_fields(data: dict) -> dict:
    """Keep category (single string for filters) and jd_category (list) in sync."""
    jd_category: List[str] = list(data.get("jd_category") or [])
    category = (data.get("category") or "").strip()
    if category and not jd_category:
        jd_category = [category]
    elif jd_category and not category:
        category = jd_category[0]
    data["jd_category"] = jd_category
    if category:
        data["category"] = category
    return data


def _resolve_jd_keywords_ai(data: dict, jd_data: Any) -> List[str]:
    provided = list(data.get("jd_keywords_ai") or [])
    if provided:
        return [str(k).strip() for k in provided if str(k).strip()]
    return _extract_jd_keywords_ai(jd_data)


def _extract_jd_keywords_ai(jd_data: Any) -> List[str]:
    if isinstance(jd_data, dict):
        keywords = jd_data.get("jd_keywords_ai", [])
        if isinstance(keywords, list):
            return [str(k) for k in keywords]
    return []


def _attach_applicants_count(jobs: list) -> list:
    """Attach live applicant counts for the job marketplace UI."""
    from app.services.application_policy import submitted_application_match

    db = get_db()
    for job in jobs:
        if "jobid" in job:
            job["applicantsCount"] = db["candidateInformation"].count_documents(
                submitted_application_match(job["jobid"])
            )
    return jobs


def _attach_new_applicants_count(jobs: list, job_views: dict) -> list:
    """Attach per-job newApplicantsCount, scoped to a specific HR caller's
    last viewed timestamp for that job. Only called for authenticated HR requests."""
    from app.services.application_policy import submitted_application_match

    db = get_db()
    for job in jobs:
        if "jobid" in job:
            job_id = job["jobid"]
            query = submitted_application_match(job_id)
            last_viewed = job_views.get(job_id)
            if last_viewed:
                query = {**query, "applied_date": {"$gte": last_viewed}}
            job["newApplicantsCount"] = db["candidateInformation"].count_documents(query)
    return jobs


def _enrich_public_job_doc(job: dict, db) -> dict:
    """Backfill fields legacy job postings may be missing for public listings."""
    if not job.get("posted_timestamp"):
        job_id = job.get("jobid")
        if job_id:
            jd = db["JobDetailsV2"].find_one(
                {"job_id": job_id}, {"posted_timestamp": 1}
            )
            if jd and jd.get("posted_timestamp"):
                job["posted_timestamp"] = jd["posted_timestamp"]

    if not job.get("posted_timestamp"):
        oid = job.get("_id")
        if isinstance(oid, ObjectId):
            job["posted_timestamp"] = oid.generation_time.replace(tzinfo=timezone.utc)

    job = attach_company_branding(job, db, persist_backfill=True)
    job = enrich_job_experience_years(job)
    job = sanitize_job_narrative_fields(job)
    job = _normalize_job_timestamps(job)
    job.pop("_id", None)
    
    # Attach sponsored job information
    job_id = job.get("jobid")
    if job_id:
        sponsored = db["sponsored_jobs"].find_one({"job_id": job_id, "is_sponsored": True})
        if sponsored:
            job["is_sponsored"] = True
            # Get employer tier for badge
            employer_id = job.get("employer_id")
            if employer_id:
                entitlement = db["entitlement_cache"].find_one({"employer_id": employer_id})
                if entitlement:
                    job["tier"] = entitlement.get("tier", "basic")
    
    return job


def _attach_hr_branding_to_job(data: dict, principal: AuthPrincipal, db) -> dict:
    return stamp_hr_job_payload(data, principal, db)


def _as_utc_dt(value) -> Optional[datetime]:
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
            # Date-only → treat as UTC midnight (avoid browser local midnight skew)
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


def _format_utc_iso(value) -> Optional[str]:
    """Always emit an explicit UTC instant ending in Z for the frontend."""
    dt = _as_utc_dt(value)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _normalize_job_timestamps(job: dict) -> dict:
    if not job:
        return job
    for key in ("posted_timestamp", "updated_timestamp", "postedAt", "postedDate"):
        if key in job and job[key] is not None:
            formatted = _format_utc_iso(job[key])
            if formatted:
                job[key] = formatted
    return job


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1)
    return dt.replace(month=dt.month + 1, day=1)


def _pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return ((current - previous) / previous) * 100.0


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _normalize_job_ids(job_ids: List[str]) -> List:
    """Match job_id stored as str or int in candidateInformation."""
    normalized = set()
    for jid in job_ids:
        if jid is None:
            continue
        normalized.add(jid)
        normalized.add(str(jid))
        try:
            normalized.add(int(jid))
        except (TypeError, ValueError):
            pass
    return list(normalized)


def _non_draft_job_ids(db, hr_user_id: Optional[str] = None) -> List[str]:
    query: dict = {"status": {"$ne": "Draft"}}
    if hr_user_id:
        query["hr_user_id"] = hr_user_id
    jobs = list(
        db["jobview"].find(
            query,
            {"jobid": 1, "_id": 0},
        )
    )
    return [j["jobid"] for j in jobs if j.get("jobid")]


def _count_applicants_in_period(
    db, job_ids: List[str], start: Optional[datetime], end: Optional[datetime]
) -> int:
    """Count applicants for given jobs, optionally within [start, end).

    Uses applied_date when present; falls back to Mongo ObjectId creation
    time for legacy records that never received applied_date on submit.
    """
    normalized_ids = _normalize_job_ids(job_ids)
    if not normalized_ids:
        return 0

    from app.services.application_policy import submitted_application_match

    submitted = submitted_application_match()
    match_stage: dict = {**submitted, "job_id": {"$in": normalized_ids}}
    pipeline: List[dict] = [{"$match": match_stage}]

    if start is not None and end is not None:
        start_naive = _naive_utc(start)
        end_naive = _naive_utc(end)
        pipeline.extend(
            [
                {
                    "$addFields": {
                        "effective_applied": {
                            "$ifNull": ["$applied_date", {"$toDate": "$_id"}]
                        }
                    }
                },
                {
                    "$match": {
                        "effective_applied": {
                            "$gte": start_naive,
                            "$lt": end_naive,
                        }
                    }
                },
            ]
        )

    pipeline.append({"$count": "total"})
    result = list(db["candidateInformation"].aggregate(pipeline))
    return int(result[0]["total"]) if result else 0


def _compute_hiring_velocity_days(
    db, start: datetime, end: datetime, job_ids: Optional[List[str]] = None
) -> Optional[float]:
    """Average days from application to HR L1 approval in [start, end)."""
    score_query: dict = {
        "status": {"$regex": r"^approved$", "$options": "i"},
        "l1_approved_at": {"$gte": start, "$lt": end},
    }
    if job_ids is not None:
        score_query["job_id"] = {"$in": _normalize_job_ids(job_ids)}
    approved_scores = list(db["Scores"].find(score_query))
    if not approved_scores:
        return None

    deltas: List[float] = []
    for score in approved_scores:
        approved_at = _as_utc_dt(score.get("l1_approved_at"))
        if not approved_at:
            continue
        candidate = db["candidateInformation"].find_one(
            {
                "candidate_id": score.get("candidate_id"),
                "job_id": score.get("job_id"),
            }
        )
        if not candidate:
            continue
        applied_at = _as_utc_dt(candidate.get("applied_date"))
        if not applied_at:
            continue
        days = (approved_at - applied_at).total_seconds() / 86400.0
        if days >= 0:
            deltas.append(days)

    if not deltas:
        return None
    return sum(deltas) / len(deltas)


def _compute_recruit_analytics(db, hr_user_id: Optional[str] = None) -> dict:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)

    this_month_start = _month_start(now)
    next_month_start = _next_month_start(this_month_start)
    last_month_start = _month_start(this_month_start - timedelta(days=1))
    this_month_end = next_month_start

    this_year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    next_year_start = this_year_start.replace(year=this_year_start.year + 1)
    last_year_start = this_year_start.replace(year=this_year_start.year - 1)

    active_query: dict = {"show": "on"}
    if hr_user_id:
        active_query["hr_user_id"] = hr_user_id

    active_jobs = list(
        db["jobview"].find(active_query, {"jobid": 1, "posted_timestamp": 1, "_id": 0})
    )
    active_job_ids = [j["jobid"] for j in active_jobs if "jobid" in j]
    all_job_ids = _non_draft_job_ids(db, hr_user_id)

    active_jobs_this_week = 0
    for job in active_jobs:
        posted = _as_utc_dt(job.get("posted_timestamp"))
        if posted and posted >= week_start:
            active_jobs_this_week += 1

    # Applicant counts across all non-draft jobs (not active-only)
    total_applicants_overall = _count_applicants_in_period(db, all_job_ids, None, None)
    total_applicants_month = _count_applicants_in_period(
        db, all_job_ids, this_month_start, this_month_end
    )
    total_applicants_last_month = _count_applicants_in_period(
        db, all_job_ids, last_month_start, this_month_start
    )
    total_applicants_year = _count_applicants_in_period(
        db, all_job_ids, this_year_start, next_year_start
    )
    total_applicants_last_year = _count_applicants_in_period(
        db, all_job_ids, last_year_start, this_year_start
    )

    applicants_month_mom_percent = _pct_change(
        float(total_applicants_month), float(total_applicants_last_month)
    )
    applicants_year_yoy_percent = _pct_change(
        float(total_applicants_year), float(total_applicants_last_year)
    )

    new_applicants_today = _count_applicants_in_period(
        db, all_job_ids, today_start, today_start + timedelta(days=1)
    )

    apps_this_month = total_applicants_month
    apps_last_month = total_applicants_last_month
    apps_mom_percent = applicants_month_mom_percent

    velocity_this_month = _compute_hiring_velocity_days(
        db, this_month_start, this_month_end, all_job_ids
    )
    velocity_last_month = _compute_hiring_velocity_days(
        db, last_month_start, this_month_start, all_job_ids
    )
    velocity_mom_delta = None
    if velocity_this_month is not None and velocity_last_month is not None:
        velocity_mom_delta = velocity_this_month - velocity_last_month

    return {
        "activeJobs": len(active_job_ids),
        "activeJobsThisWeek": active_jobs_this_week,
        "totalApplicants": total_applicants_overall,
        "totalApplicantsOverall": total_applicants_overall,
        "totalApplicantsMonth": total_applicants_month,
        "totalApplicantsYear": total_applicants_year,
        "applicantsMomPercent": round(applicants_month_mom_percent, 1),
        "applicantsYearYoyPercent": round(applicants_year_yoy_percent, 1),
        "newApplicantsToday": new_applicants_today,
        "applicationsThisMonth": apps_this_month,
        "applicationsMomPercent": round(apps_mom_percent, 1),
        "hiringVelocityDays": round(velocity_this_month, 1) if velocity_this_month is not None else None,
        "hiringVelocityMomDeltaDays": round(velocity_mom_delta, 1) if velocity_mom_delta is not None else None,
    }

# --- API Routes ---

@router.post("/api/jobs")
async def post_job(payload: JobCreateRequest, principal: Annotated[AuthPrincipal, Depends(require_hr)]):
    from app.services.technical_interview_toggle import validate_technical_interview_config
    
    posted_timestamp = datetime.now(timezone.utc)
    data = payload.model_dump()
    data = sanitize_job_narrative_fields(data)
    db = get_db()
    data = _attach_hr_branding_to_job(data, principal, db)
    data = await attach_experience_years_fields(data)
    job_id = generate_unique_jobid()
    
    # 1. Set explicit defaults so it never ends up "Closed" by accident
    data["jobid"] = job_id
    data["posted_timestamp"] = posted_timestamp

    # Handle technical interview toggle at publish time
    technical_enabled = data.get("technicalInterviewEnabled", False)
    technical_questions = data.get("technicalQuestions", [])
    technical_weightage = data.get("technicalWeightage")
    difficulty_level = _job_difficulty_level(data)
    
    if technical_enabled:
        # Validate technical interview configuration when enabling
        validation_errors = validate_technical_interview_config(
            technical_enabled,
            technical_questions,
            difficulty_level
        )
        if validation_errors:
            raise HTTPException(
                status_code=400,
                detail="Cannot publish with technical interview enabled: " + "; ".join(validation_errors)
            )
        
        # Ensure technical weightage is set
        if technical_weightage is None:
            technical_weightage = 20  # Default 20%
            data["technicalWeightage"] = technical_weightage
        
        # Add technical to section weightage
        section_weightage = data.get("sectionWeightage", {})
        if "technical" not in section_weightage:
            section_weightage["technical"] = technical_weightage
            data["sectionWeightage"] = section_weightage
    else:
        # If toggle is Off at publish time, discard technical interview fields
        data.pop("technicalQuestions", None)
        data.pop("technicalWeightage", None)
        data.pop("technicalInterviewEnabled", None)
        
        # Remove technical from section weightage if present
        section_weightage = data.get("sectionWeightage", {})
        if "technical" in section_weightage:
            del section_weightage["technical"]
            data["sectionWeightage"] = section_weightage

    data = _sync_category_fields(data)
    jd_category: List[str] = list(data.get("jd_category") or [])
    jd_keywords: List[str] = list(data.get("jd_keywords") or [])
    data["jd_category"] = jd_category
    data["jd_keywords"] = jd_keywords
    
    job_role = data.get("jobrole", "")
    section_weightage = data.get("sectionWeightage", {})
    data["difficulty_level"] = difficulty_level

    # Parse JD before going live. On failure, keep a draft so HR can retry.
    try:
        jd_result = await run_in_thread(get_gemini().jd_parser, _compose_job_text(data))
        jd_data = _parse_job_description(jd_result)
    except HTTPException as exc:
        if exc.status_code == 502:
            _save_job_as_draft(db, data, principal, job_id, str(exc.detail))
            return _draft_saved_response(job_id, str(exc.detail))
        raise
    except Exception as exc:
        logger.exception("JD parsing failed while posting job")
        _save_job_as_draft(db, data, principal, job_id, str(exc))
        return _draft_saved_response(job_id, str(exc))

    data["status"] = "Open"
    data["show"] = data.get("show") or "on"
    data.pop("jd_parse_error", None)
    data.pop("jd_parse_failed_at", None)

    try:
        jd_keywords_ai = _resolve_jd_keywords_ai(data, jd_data)
    except Exception as e:
        # Prevent the whole post from failing if keyword extraction fails
        print(f"Keyword extraction failed: {str(e)}")
        jd_keywords_ai = []
    data["jd_keywords_ai"] = jd_keywords_ai

    # 3. NOW save to the database. If anything above crashed, this never runs!
    db = get_db()
    
    # Insert into jobview
    db["jobview"].insert_one(data)

    # Insert into JobDetailsV2
    db["JobDetailsV2"].insert_one(
        {
            "chunk_name": f"JD_{job_id}_{job_role}",
            "job_role": job_role,
            "job_id": job_id,
            "sectionWeightage": section_weightage,
            "difficulty_level": difficulty_level,
            "jd_category": jd_category,
            "jd_keywords": jd_keywords,
            "jd_keywords_ai": jd_keywords_ai,
            "posted_timestamp": posted_timestamp,
            "jd_json": jd_data,
        }
    )

    # Best-effort: stamp plan priority after a successful publish.
    # Failures here must never roll back the job going live.
    try:
        from app.services.priority_list import restamp_after_publish

        restamp_after_publish(db, job_id)
    except Exception:
        pass

    return {"message": "Job added successfully", "jobid": job_id}


@router.post("/api/recruit/jobs/draft")
async def save_draft(request: Request, _principal=Depends(require_hr)):
    """Upsert a draft job posting.

    Accepts the raw job payload as JSON (same field names used elsewhere
    in this file: jobrole, jobDescription, educationalQualifications,
    experienceRequired, desiredSkills, employmentType, workMode, location,
    salaryPackage, plus companyName / rolesResponsibilities / salaryUndisclosed
    if your schema supports them). Deliberately NOT validated against
    JobCreateRequest, since a draft is allowed to be incomplete.

    If the payload includes a `jobid` that already exists, that document
    is updated in place. Otherwise a new draft is created with a fresh
    jobid. Drafts are stored in the same `jobview` collection as published
    jobs, with status="Draft" and show="off" so they're automatically
    excluded from /api/jobviewmain and the public careers listing.
    """
    from app.rbac import constants as C

    data = await request.json()
    data = sanitize_job_narrative_fields(data)
    db = get_db()
    now = datetime.now(timezone.utc)

    jobid = data.get("jobid")
    # Create vs update — matrix: create for new drafts, update for existing.
    if jobid:
        _require_rbac_action(
            _principal,
            C.ACTION_JOBS_UPDATE,
            detail_fallback="This action is not permitted for your role.",
        )
    else:
        _require_rbac_action(
            _principal,
            C.ACTION_JOBS_CREATE,
            detail_fallback="This action is not permitted for your role. You cannot post a job.",
        )

    data["status"] = "Draft"
    data["show"] = "off"
    data["updated_timestamp"] = now
    data["difficulty_level"] = _job_difficulty_level(data)
    data = _sync_category_fields(data)
    data = _attach_hr_branding_to_job(data, _principal, db)
    data = await attach_experience_years_fields(data)

    if jobid:
        require_hr_job(_principal, jobid, db)
        result = db["jobview"].update_one(
            {"jobid": jobid, **hr_jobs_filter(_principal)},
            {"$set": data},
        )
        if result.matched_count == 0:
            # jobid was passed but no longer exists (e.g. deleted) -> create fresh
            _require_rbac_action(
                _principal,
                C.ACTION_JOBS_CREATE,
                detail_fallback="This action is not permitted for your role. You cannot post a job.",
            )
            jobid = generate_unique_jobid()
            data["jobid"] = jobid
            data["posted_timestamp"] = now
            db["jobview"].insert_one(data)
    else:
        jobid = generate_unique_jobid()
        data["jobid"] = jobid
        data["posted_timestamp"] = now
        db["jobview"].insert_one(data)

    return {"message": "Draft saved", "jobid": jobid}


@router.get("/api/recruit/jobs/drafts")
async def get_drafts(principal=Depends(require_hr)):
    """List saved drafts for the authenticated HR account only."""
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    drafts = list(
        db["jobview"]
        .find({"status": "Draft", **hr_jobs_filter(principal)}, {"_id": 0})
        .sort("updated_timestamp", -1)
    )
    return drafts


@router.delete("/api/recruit/jobs/draft/{jobid}")
async def delete_draft(jobid: str, principal=Depends(require_hr)):
    """Remove a draft owned by the authenticated HR account."""
    db = get_db()
    require_hr_job(principal, jobid, db)
    result = db["jobview"].delete_one(
        {"jobid": jobid, "status": "Draft", **hr_jobs_filter(principal)}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Draft not found")
    return {"success": True}


@router.post("/api/recruit/jobs/{jobid}/publish")
async def publish_draft(
    jobid: str,
    payload: JobCreateRequest,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    """Promote an existing draft (found by jobid) into a live posting.

    Mirrors post_job()'s pipeline (JD parsing via Gemini, JobDetailsV2
    chunk) but updates the existing jobview document in place instead of
    inserting a duplicate. posted_timestamp is set to publish time so
    "Posted … ago" reflects when the job went live, not when the draft
    was first saved.
    """
    from app.services.technical_interview_toggle import validate_technical_interview_config
    
    db = get_db()
    require_hr_job(principal, jobid, db)
    existing = db["jobview"].find_one({"jobid": jobid, **hr_jobs_filter(principal)})
    if not existing:
        raise HTTPException(status_code=404, detail="Draft not found")

    data = payload.model_dump()
    data = sanitize_job_narrative_fields(data)
    data = _attach_hr_branding_to_job(data, principal, db)
    data = await attach_experience_years_fields(data)
    now = datetime.now(timezone.utc)
    data["jobid"] = jobid
    # Live "posted" time = publish moment (not draft creation time).
    data["posted_timestamp"] = now
    data["updated_timestamp"] = now
    data["status"] = "Open"
    data["show"] = data.get("show") or "on"
    data.pop("jd_parse_error", None)
    data.pop("jd_parse_failed_at", None)

    # Handle technical interview toggle at publish time
    technical_enabled = data.get("technicalInterviewEnabled", False)
    technical_questions = data.get("technicalQuestions", [])
    technical_weightage = data.get("technicalWeightage")
    difficulty_level = _job_difficulty_level(data)
    
    if technical_enabled:
        # Validate technical interview configuration when enabling
        validation_errors = validate_technical_interview_config(
            technical_enabled,
            technical_questions,
            difficulty_level
        )
        if validation_errors:
            raise HTTPException(
                status_code=400,
                detail="Cannot publish with technical interview enabled: " + "; ".join(validation_errors)
            )
        
        # Ensure technical weightage is set
        if technical_weightage is None:
            technical_weightage = 20  # Default 20%
            data["technicalWeightage"] = technical_weightage
        
        # Add technical to section weightage
        section_weightage = data.get("sectionWeightage", {})
        if "technical" not in section_weightage:
            section_weightage["technical"] = technical_weightage
            data["sectionWeightage"] = section_weightage
    else:
        # If toggle is Off at publish time, discard technical interview fields
        data.pop("technicalQuestions", None)
        data.pop("technicalWeightage", None)
        data.pop("technicalInterviewEnabled", None)
        
        # Remove technical from section weightage if present
        section_weightage = data.get("sectionWeightage", {})
        if "technical" in section_weightage:
            del section_weightage["technical"]
            data["sectionWeightage"] = section_weightage

    data = _sync_category_fields(data)
    jd_category: List[str] = list(data.get("jd_category") or [])
    jd_keywords: List[str] = list(data.get("jd_keywords") or [])
    data["jd_category"] = jd_category
    data["jd_keywords"] = jd_keywords

    job_role = data.get("jobrole", "")
    section_weightage = data.get("sectionWeightage", {})
    data["difficulty_level"] = difficulty_level

    try:
        jd_result = await run_in_thread(get_gemini().jd_parser, _compose_job_text(data))
        jd_data = _parse_job_description(jd_result)
    except HTTPException as exc:
        if exc.status_code == 502:
            _save_job_as_draft(db, data, principal, jobid, str(exc.detail))
            return _draft_saved_response(jobid, str(exc.detail))
        raise
    except Exception as exc:
        logger.exception("JD parsing failed while publishing job")
        _save_job_as_draft(db, data, principal, jobid, str(exc))
        return _draft_saved_response(jobid, str(exc))

    try:
        jd_keywords_ai = _resolve_jd_keywords_ai(data, jd_data)
    except Exception as e:
        print(f"Keyword extraction failed: {str(e)}")
        jd_keywords_ai = []
    data["jd_keywords_ai"] = jd_keywords_ai

    db["jobview"].replace_one({"jobid": jobid}, data)

    db["JobDetailsV2"].delete_many({"job_id": jobid})
    db["JobDetailsV2"].insert_one(
        {
            "chunk_name": f"JD_{jobid}_{job_role}",
            "job_role": job_role,
            "job_id": jobid,
            "sectionWeightage": section_weightage,
            "difficulty_level": difficulty_level,
            "jd_category": jd_category,
            "jd_keywords": jd_keywords,
            "jd_keywords_ai": jd_keywords_ai,
            "posted_timestamp": data["posted_timestamp"],
            "jd_json": jd_data,
        }
    )

    try:
        from app.services.priority_list import restamp_after_publish

        restamp_after_publish(db, jobid)
    except Exception:
        pass

    return {"message": "Job published successfully", "jobid": jobid}


@router.get("/api/jobview")
async def get_all_jobs(principal=Depends(require_hr)):
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    query = _hr_jobs_query(principal, extra={"status": {"$ne": "Draft"}})
    jobs = list(db["jobview"].find(query, {"_id": 0}))
    jobs = [_normalize_job_timestamps(j) for j in jobs]
    return _attach_applicants_count(jobs)


@router.get("/api/jobviewmain")
@router.get("/api/jobs")
async def get_public_jobs(
    principal: Annotated[Optional[AuthPrincipal], Depends(optional_hr)] = None,
    resolvedRegion: Optional[str] = Query(
        None,
        description="ISO country code — when set, only jobs in that country are returned.",
    ),
    regionType: Optional[str] = Query(
        "country",
        description="Ignored for listing; country filter only.",
    ),
):
    """Public career-site listing — only jobs marked show=on.

    Optional country filter: pass resolvedRegion (ISO alpha-2). Only jobs in
    that country are returned. This endpoint does not sort by distance.

    Anonymous callers get the plain active-jobs list, same as always.
    An authenticated HR caller additionally gets newApplicantsCount per
    job, scoped to their own previousLoginAt (set at login — see
    auth_hr.py). This never runs for anonymous requests, so the public
    careers page is unaffected.
    """
    db = get_db()
    jobs = list(db["jobview"].find({"show": "on"}))
    jobs = [_enrich_public_job_doc(j, db) for j in jobs]
    jobs = _attach_applicants_count(jobs)

    if principal and principal.role == Role.HR:
        hr_doc = db["hr_users"].find_one({"_id": ObjectId(principal.subject)})
        if hr_doc:
            job_views = hr_doc.get("job_views", {})
            jobs = _attach_new_applicants_count(jobs, job_views)

    if resolvedRegion:
        jobs = sort_jobs_by_resolved_region(
            jobs,
            resolved_region=resolvedRegion,
            region_type=regionType or "country",
        )

    return jobs


@router.get("/api/recruit/dashboard/stats")
async def get_dashboard_stats(principal=Depends(require_hr)):
    """HR analytics scoped to the authenticated recruiter's job bucket."""
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    hr_user_id = principal.subject if principal.role == Role.HR else None
    return _compute_recruit_analytics(db, hr_user_id)


@router.get("/api/recruit/jobs/active")
async def list_active_hr_jobs(principal=Depends(require_hr)):
    """Active public jobs for the authenticated HR dashboard only."""
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    jobs = list(
        db["jobview"].find(_hr_jobs_query(principal, extra={"show": "on"}))
    )
    jobs = [_enrich_public_job_doc(j, db) for j in jobs]
    jobs = _attach_applicants_count(jobs)

    if principal.role == Role.HR:
        hr_doc = db["hr_users"].find_one({"_id": ObjectId(principal.subject)})
        if hr_doc:
            job_views = hr_doc.get("job_views", {})
            jobs = _attach_new_applicants_count(jobs, job_views)

    return jobs


@router.get("/api/recruit/jobs")
async def list_recruit_jobs(principal=Depends(require_hr)):
    """Summarized job list for Settings → Job Listing tab (excludes drafts)."""
    db = get_db()
    ensure_hr_tenant_ready(principal, db)
    jobs = list(
        db["jobview"].find(
            {"status": {"$ne": "Draft"}, **hr_jobs_filter(principal)},
            {"_id": 0},
        )
    )
    jobs = _attach_applicants_count(jobs)

    rows = []
    for job in jobs:
        show = job.get("show")
        if job.get("status") == "Draft":
            status = "Draft"
        elif show == "on":
            status = "Active"
        elif show == "closed":
            status = "Closed"
        else:
            status = "Paused"

        posted = _as_utc_dt(job.get("posted_timestamp"))
        date_str = posted.strftime("%b %d, %Y") if posted else "Not available"

        rows.append(
            {
                "id": job.get("jobid"),
                "title": job.get("jobrole") or "Untitled Job",
                "location": job.get("location") or "Remote",
                "date": date_str,
                "status": status,
                "applicants": job.get("applicantsCount", 0),
                "priorityListing": bool(job.get("priorityListing")),
                "priorityLabel": "Priority listing" if job.get("priorityListing") else None,
                "_sort": posted or datetime.min.replace(tzinfo=timezone.utc),
            }
        )

    rows.sort(key=lambda item: item["_sort"], reverse=True)
    for item in rows:
        item.pop("_sort", None)
    return rows


@router.patch("/api/jobview/{jobid}")
async def update_job(
    jobid: str,
    payload: JobUpdateRequest,
    principal=Depends(require_hr),
):
    db = get_db()
    require_hr_job(principal, jobid, db)
    data = payload.model_dump(exclude_unset=True)
    data = sanitize_job_narrative_fields(data)
    if not data:
        raise HTTPException(status_code=400, detail="No updatable fields provided")
    if "experienceYearsRequired" in data:
        merged = db["jobview"].find_one({"jobid": jobid, **hr_jobs_filter(principal)}) or {}
        merged.update(data)
        merged = await attach_experience_years_fields(merged)
        data["experienceYears"] = merged.get("experienceYears", [])
        data["experienceYearsMin"] = merged.get("experienceYearsMin")
        data["experienceYearsMax"] = merged.get("experienceYearsMax")
    result = db["jobview"].update_one(
        {"jobid": jobid, **hr_jobs_filter(principal)},
        {"$set": data},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True}


@router.post("/api/upd/jobs")
async def update_jd_details(request: Request, principal=Depends(require_hr)):
    data = await request.json()
    job_id = data.get("jobid")
    if not job_id:
        raise HTTPException(status_code=400, detail="jobid is required")
    db = get_db()
    require_hr_job(principal, job_id, db)
    db["JobDetailsV2"].delete_many({"job_id": job_id})
    job_role = data.get("jobrole", "")
    section_weightage = data.get("sectionWeightage", {})
    difficulty_level = _job_difficulty_level(data)
    jd_category: List[str] = list(data.get("jd_category") or [])
    jd_keywords: List[str] = list(data.get("jd_keywords") or [])
    data = _sync_category_fields(data)
    try:
        jd_result = await run_in_thread(get_gemini().jd_parser, _compose_job_text(data))
        jd_data = _parse_job_description(jd_result)
    except HTTPException:
        raise
    except Exception:
        logger.exception("JD parsing failed while updating job details")
        raise HTTPException(
            status_code=502,
            detail="Could not parse the job description. Please try again.",
        )
    jd_keywords_ai = _resolve_jd_keywords_ai(data, jd_data)
    db["JobDetailsV2"].insert_one(
        {
            "chunk_name": f"JD_{job_id}_{job_role}",
            "job_role": job_role,
            "job_id": job_id,
            "sectionWeightage": section_weightage,
            "difficulty_level": difficulty_level,
            "jd_category": jd_category,
            "jd_keywords": jd_keywords,
            "jd_keywords_ai": jd_keywords_ai,
            "jd_json": jd_data,
        }
    )
    # Keep jobview in sync with freshly parsed AI keywords after an edit update.
    db["jobview"].update_one(
        {"jobid": job_id, **hr_jobs_filter(principal)},
        {
            "$set": {
                "jd_keywords_ai": jd_keywords_ai,
                "difficulty_level": difficulty_level,
            }
        },
    )
    return {"message": "JD chunk updated", "jobid": job_id}


@router.patch("/api/jobview/{jobid}/show")
async def toggle_show(
    jobid: str,
    payload: JobShowToggleRequest,
    principal=Depends(require_hr),
):
    db = get_db()
    require_hr_job(principal, jobid, db)
    result = db["jobview"].update_one(
        {"jobid": jobid, **hr_jobs_filter(principal)},
        {"$set": {"show": payload.show}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "show": payload.show}


@router.patch("/api/jobview/{jobid}/technical-interview-toggle")
async def toggle_technical_interview(
    jobid: str,
    payload: TechnicalInterviewToggleRequest,
    principal=Depends(require_hr),
):
    """Toggle technical interview requirement for a job.
    
    Handles both draft jobs (simple toggle) and live jobs (complex candidate handling).
    Validates permissions and logs all changes.
    """
    from app.rbac import constants as C
    from app.rbac.audit import audit_technical_interview_toggle
    from app.services.technical_interview_toggle import (
        count_candidates_affected_by_toggle,
        rebalance_weightage_with_technical,
        validate_technical_interview_config,
        create_technical_interview_stages,
        advance_candidates_without_technical,
        cancel_pending_technical_reminders,
        write_technical_toggle_analytics,
    )
    
    db = get_db()
    require_hr_job(principal, jobid, db)
    
    # Enforce RBAC permission for technical interview configuration
    _require_rbac_action(principal, C.ACTION_JOBS_CONFIG_TECHNICAL_INTERVIEW)
    
    # Get current job state
    job = db["jobview"].find_one({"jobid": jobid})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    current_enabled = job.get("technicalInterviewEnabled", False)
    new_enabled = payload.technicalInterviewEnabled
    
    # If no change, return early
    if current_enabled == new_enabled:
        return TechnicalInterviewToggleResponse(
            message="No change in technical interview setting",
            jobId=jobid,
            technicalInterviewEnabled=current_enabled
        )
    
    is_draft = job.get("status") == "Draft"
    
    # Validate technical interview configuration when enabling
    if new_enabled:
        validation_errors = validate_technical_interview_config(
            new_enabled,
            job.get("technicalQuestions"),
            job.get("difficulty_level")
        )
        if validation_errors and not is_draft:
            raise HTTPException(
                status_code=400,
                detail="Technical interview configuration incomplete: " + "; ".join(validation_errors)
            )
    
    candidates_affected = None
    option_chosen = payload.optionChosen
    weightage_rebalanced = None
    
    if is_draft:
        # Draft job: simple toggle, no candidate handling needed
        update_data = {
            "technicalInterviewEnabled": new_enabled,
            "updated_timestamp": datetime.now(timezone.utc)
        }
        
        # Store original weightage for rebalancing reference
        current_weightage = job.get("sectionWeightage", {})
        if current_weightage:
            update_data["originalSectionWeightage"] = current_weightage
        
        result = db["jobview"].update_one(
            {"jobid": jobid, **hr_jobs_filter(principal)},
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Job not found")
        
        message = f"Technical interview {'enabled' if new_enabled else 'disabled'} for draft job"
    
    else:
        # Live job: complex candidate handling
        affected_counts = count_candidates_affected_by_toggle(jobid, current_enabled, new_enabled)
        
        if not current_enabled and new_enabled:
            # Off → On: require option choice for existing candidates
            candidates_without_technical = affected_counts.get("candidates_without_technical", 0)
            
            if candidates_without_technical > 0:
                if not option_chosen:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "option_required",
                            "message": f"{candidates_without_technical} candidates have passed screening without a technical interview. Please choose how to handle them.",
                            "candidates_affected": candidates_without_technical,
                            "options": ["leave_existing", "invite_existing"]
                        }
                    )
                
                if option_chosen == "invite_existing":
                    # Get candidate IDs and create technical interview stages
                    candidates = list(db["candidateInformation"].find({
                        "job_id": jobid,
                        "application_status": {"$in": ["applied", "in_progress"]},
                        "l0_passed": True,
                        "l1_status": {"$in": [None, "not_started", "skipped"]}
                    }, {"candidate_id": 1}))
                    
                    candidate_ids = [c["candidate_id"] for c in candidates]
                    processed = create_technical_interview_stages(jobid, candidate_ids)
                    candidates_affected = processed
                
                candidates_affected = candidates_without_technical
        
        elif current_enabled and not new_enabled:
            # On → Off: handle in-progress and completed candidates
            candidates_in_progress = affected_counts.get("candidates_in_progress", 0)
            candidates_completed = affected_counts.get("candidates_completed", 0)
            
            if candidates_in_progress > 0 or candidates_completed > 0:
                if not option_chosen:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "option_required",
                            "message": f"{candidates_in_progress} candidates have technical interviews in progress and {candidates_completed} have completed. Please choose how to handle them.",
                            "candidates_in_progress": candidates_in_progress,
                            "candidates_completed": candidates_completed,
                            "options": ["keep_completed", "advance_in_progress"]
                        }
                    )
                
                if option_chosen == "advance_in_progress" and candidates_in_progress > 0:
                    # Get in-progress candidate IDs and advance them
                    candidates = list(db["candidateInformation"].find({
                        "job_id": jobid,
                        "l1_status": "in_progress"
                    }, {"candidate_id": 1}))
                    
                    candidate_ids = [c["candidate_id"] for c in candidates]
                    processed = advance_candidates_without_technical(jobid, candidate_ids)
                    candidates_affected = processed
                
                # Cancel pending reminders
                cancel_pending_technical_reminders(jobid)
            
            candidates_affected = candidates_in_progress + candidates_completed
        
        # Rebalance weightage
        current_weightage = job.get("sectionWeightage", {})
        technical_weightage = job.get("technicalWeightage", 20)  # Default 20%
        weightage_rebalanced = rebalance_weightage_with_technical(
            current_weightage,
            technical_weightage,
            new_enabled
        )
        
        # Update job
        update_data = {
            "technicalInterviewEnabled": new_enabled,
            "sectionWeightage": weightage_rebalanced,
            "updated_timestamp": datetime.now(timezone.utc)
        }
        
        # Store original weightage for reference
        if current_weightage:
            update_data["originalSectionWeightage"] = current_weightage
        
        result = db["jobview"].update_one(
            {"jobid": jobid, **hr_jobs_filter(principal)},
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Job not found")
        
        message = f"Technical interview {'enabled' if new_enabled else 'disabled'} for live job"
    
    # Write audit log
    audit_technical_interview_toggle(
        actor_id=principal.subject,
        org_id=getattr(principal, "org_id", None),
        job_id=jobid,
        before_enabled=current_enabled,
        after_enabled=new_enabled,
        candidates_affected=candidates_affected,
        option_chosen=option_chosen,
    )
    
    # Write analytics event
    write_technical_toggle_analytics(
        job_id=jobid,
        old_value=current_enabled,
        new_value=new_enabled,
        actor_id=principal.subject,
        org_id=getattr(principal, "org_id", None),
        candidates_affected=candidates_affected,
        option_chosen=option_chosen,
    )
    
    return TechnicalInterviewToggleResponse(
        message=message,
        jobId=jobid,
        technicalInterviewEnabled=new_enabled,
        candidatesAffected=candidates_affected,
        optionChosen=option_chosen,
        weightageRebalanced=weightage_rebalanced
    )


@router.post("/api/jobview/{jobid}/mark-viewed")
async def mark_job_viewed(jobid: str, principal=Depends(require_hr)):
    db = get_db()
    require_hr_job(principal, jobid, db)
    now = datetime.now(timezone.utc)
    db["hr_users"].update_one(
        {"_id": ObjectId(principal.subject)},
        {"$set": {f"job_views.{jobid}": now}}
    )
    return {"success": True}


@router.get("/api/jobview/{job_id}/onboarding")
async def get_onboarding_questions(job_id: str):
    job = get_db()["jobview"].find_one({"jobid": job_id})
    if not job or "onboarding" not in job:
        raise HTTPException(status_code=404, detail="Job or onboarding not found")
    return job["onboarding"]


@router.post("/api/generate-job-description")
async def generate_job_description(
    payload: GenerateJobDescriptionRequest,
    _principal=Depends(require_hr),
):
    try:
        result = await run_in_thread(
            get_gemini().job_description_generator, payload.job_title
        )
        return sanitize_generated_job_description(result)
    except Exception as e:
        logger.exception("generate-job-description failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/generate-job-keywords")
async def generate_job_keywords(
    payload: GenerateJobKeywordsRequest,
    _principal=Depends(require_hr),
):
    """Generate AI search keywords from the current JD draft content."""
    try:
        result = await run_in_thread(
            get_gemini().job_keywords_generator,
            payload.model_dump(),
        )
        keywords = result.get("jd_keywords_ai") or []
        if not isinstance(keywords, list):
            keywords = []
        return {"jd_keywords_ai": [str(k).strip() for k in keywords if str(k).strip()]}
    except Exception as e:
        logger.exception("generate-job-keywords failed")
        raise HTTPException(status_code=500, detail=str(e))
@router.get("/api/jobs/{job_id}/share-info")
def get_job_share_info(
    job_id: str,
    principal: AuthPrincipal = Depends(optional_hr),
    db: Any = Depends(get_db)
):
    """Get metadata for sharing a job."""
    job = db.jobview.find_one({"jobid": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    import os
    base_url = os.environ.get("FRONTEND_URL", "https://kovetted.com")
    
    title = job.get("title") or job.get("jobrole") or "Job Opportunity"
    company = job.get("companyName") or "Kovetted"
    
    import re
    def slugify(text: str) -> str:
        text = str(text).lower()
        text = re.sub(r'[^a-z0-9]+', '-', text)
        return text.strip('-')

    import urllib.parse
    company_logo = job.get("companyLogo")
    if not company_logo:
        encoded_company = urllib.parse.quote(company)
        company_logo = f"https://ui-avatars.com/api/?name={encoded_company}&background=random&size=120"
        
    url = f"{base_url}/jobs/{slugify(company)}/{slugify(title)}-{job_id}"
    
    return {
        "title": title,
        "company": company,
        "url": url,
        "previewImage": company_logo,
        "description": job.get("description", f"Join our team as a {title} at {company}..."),
    }


@router.post("/api/jobs/{job_id}/direct-share")
def direct_share_job(
    job_id: str,
    payload: DirectShareRequest,
    principal: AuthPrincipal = Depends(require_hr),
    db: Any = Depends(get_db)
):
    """Send an email invite to view the job."""
    job = db.jobview.find_one({"jobid": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    title = job.get("title") or job.get("jobrole") or "Job Opportunity"
    company = job.get("companyName") or "Kovetted"
    
    import os
    import re
    base_url = os.environ.get("FRONTEND_URL", "https://kovetted.com")
    def slugify(text: str) -> str:
        text = str(text).lower()
        text = re.sub(r'[^a-z0-9]+', '-', text)
        return text.strip('-')
    
    job_url = f"{base_url}/jobs/{slugify(company)}/{slugify(title)}-{job_id}"
    
    from app.email.service import EmailService
    email_service = EmailService()
    
    # Simple HTML payload
    body_html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333;">
        <h2>Check out this job opportunity!</h2>
        <p><strong>{company}</strong> is hiring for a <strong>{title}</strong>.</p>
        {f'<p><em>"{payload.message}"</em></p>' if payload.message else ''}
        <p><a href="{job_url}" style="padding: 10px 20px; background-color: #00adee; color: #fff; text-decoration: none; border-radius: 5px;">View Job Details</a></p>
      </body>
    </html>
    """
    
    subject = f"Job Opportunity: {title} at {company}"
    
    result = email_service.send_email_detailed(
        subject=subject,
        body_html=body_html,
        recipients=payload.recipients
    )
    
    if result.success and not result.skipped:
        return {"ok": True, "sent": len(result.recipients)}
    else:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {result.error}")
