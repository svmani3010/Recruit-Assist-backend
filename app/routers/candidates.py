"""Candidate information and listing endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import AuthPrincipal, Role, assert_candidate_access, get_current_principal, require_hr
from app.database import get_db
from app.helpers import (
    apply_candidate_profile_picture,
    extract_profile_picture_from_payload,
    get_candidate_profile_picture,
    get_candidate_profile_pictures,
    resolve_candidate_contact,
)
from app.schemas import OnboardingRequest
from app.services.candidate_region import onboarding_region_persist_fields

router = APIRouter(tags=["candidates"])


@router.get("/api/jobs/{job_id}/application-eligibility")
async def get_job_application_eligibility(
    job_id: str,
    candidate_id: str = Query(...),
    principal=Depends(get_current_principal),
):
    """Whether a candidate may start or restart an application for this job."""
    assert_candidate_access(principal, candidate_id, job_id)
    from app.services.application_policy import check_application_eligibility

    return check_application_eligibility(candidate_id, job_id)


@router.post("/api/candidate/{candidate_id}/interview-window")
async def set_candidate_interview_window(
    candidate_id: str,
    request: Request,
    principal=Depends(get_current_principal),
):
    """Idempotently open the 24h L1 interview start window after L0 pass."""
    assert_candidate_access(principal, candidate_id)
    from app.services.application_policy import ensure_interview_window

    job_id = None
    application_id = None
    try:
        data = await request.json()
        job_id = data.get("jobId") or data.get("job_id")
        application_id = data.get("applicationId") or data.get("application_id")
    except Exception:
        pass

    if not job_id:
        raise HTTPException(status_code=400, detail="jobId is required")

    # Check if technical interview is enabled for this job
    db = get_db()
    job = db["jobview"].find_one({"jobid": str(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    technical_enabled = job.get("technicalInterviewEnabled", False)
    if not technical_enabled:
        raise HTTPException(
            status_code=403,
            detail="Technical interview is not enabled for this job"
        )

    return ensure_interview_window(
        db,
        candidate_id,
        str(job_id),
        application_id=application_id,
    )


@router.post("/api/candidate/{candidate_id}/interview-started")
async def mark_candidate_interview_started(
    candidate_id: str,
    request: Request,
    principal=Depends(get_current_principal),
):
    """Mark that the candidate began the L1 interview (stops 24h auto-expire)."""
    assert_candidate_access(principal, candidate_id)
    from app.services.application_policy import mark_interview_started

    job_id = None
    application_id = None
    try:
        data = await request.json()
        job_id = data.get("jobId") or data.get("job_id")
        application_id = data.get("applicationId") or data.get("application_id")
    except Exception:
        pass

    if not job_id:
        raise HTTPException(status_code=400, detail="jobId is required")

    # Check if technical interview is enabled for this job
    db = get_db()
    job = db["jobview"].find_one({"jobid": str(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    technical_enabled = job.get("technicalInterviewEnabled", False)
    if not technical_enabled:
        raise HTTPException(
            status_code=403,
            detail="Technical interview is not enabled for this job"
        )

    return mark_interview_started(
        db,
        candidate_id,
        str(job_id),
        application_id=application_id,
    )


@router.post("/api/candidate/{candidate_id}/abandon-interview")
async def abandon_candidate_interview(
    candidate_id: str,
    request: Request,
    principal=Depends(get_current_principal),
):
    """End a started L1 interview — one-shot attempt, cannot be resumed."""
    assert_candidate_access(principal, candidate_id)
    from app.services.application_policy import abandon_in_progress_interview

    job_id = None
    application_id = None
    try:
        data = await request.json()
        job_id = data.get("jobId") or data.get("job_id")
        application_id = data.get("applicationId") or data.get("application_id")
    except Exception:
        pass

    if not job_id:
        raise HTTPException(status_code=400, detail="jobId is required")

    return abandon_in_progress_interview(
        get_db(),
        candidate_id,
        str(job_id),
        application_id=application_id,
    )


def _job_id_variants(job_id) -> List:
    if job_id is None or job_id == "":
        return []
    variants = {job_id, str(job_id)}
    try:
        variants.add(int(job_id))
    except (TypeError, ValueError):
        pass
    return list(variants)


def _as_utc_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _effective_applied_sort_key(doc: dict) -> datetime:
    for field in ("applied_date", "created_at"):
        dt = _as_utc_dt(doc.get(field))
        if dt is not None:
            return dt
    oid = doc.get("_id")
    if isinstance(oid, ObjectId):
        return oid.generation_time.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)

# --- NEW ONBOARDING ROUTE ---
# Note: primary handler lives in resumes.py; kept for backwards compatibility.
@router.post("/api/profile/{candidate_id}/onboarding")
async def complete_onboarding_legacy(
    candidate_id: str,
    payload: OnboardingRequest,
    principal=Depends(get_current_principal),
):
    assert_candidate_access(principal, candidate_id)
    db = get_db()

    existing = db["candidates"].find_one({"_id": ObjectId(candidate_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Candidate not found")

    data = payload.model_dump(exclude_none=True)
    raw_picture = extract_profile_picture_from_payload(data)
    for key in (
        "profilePicture",
        "profile_picture",
        "picture",
        "photo",
        "avatar",
        "profilePhoto",
        "profile_photo",
    ):
        data.pop(key, None)

    if raw_picture:
        apply_candidate_profile_picture(candidate_id, raw_picture)

    data.update(
        onboarding_region_persist_fields(
            country_name_or_code=str(data.get("country") or ""),
            phone_country_code=str(
                data.get("phone_country_code") or data.get("phoneCountry") or ""
            ),
            explicit_country_field=str(data.get("country_field") or ""),
            existing_doc=existing,
        )
    )

    db["candidates"].update_one(
        {"_id": ObjectId(candidate_id)},
        {"$set": {**data, "onboardingCompleted": True}},
    )
    return {"status": "success", "message": "Onboarding completed"}


# --- EXISTING ROUTES ---
@router.post("/api/store_candidate_info")
async def store_candidate_info(
    request: Request, principal=Depends(get_current_principal)
):
    data = await request.json()
    candidate_id = data.get("candidate_id", "")
    job_id = data.get("job_id", "")
    application_id = (data.get("application_id") or "").strip()
    if not candidate_id:
        raise HTTPException(status_code=400, detail="candidate_id is required")
    if not application_id:
        raise HTTPException(
            status_code=400,
            detail="application_id is required. Restart the application from step 1.",
        )
    assert_candidate_access(principal, candidate_id, job_id or None)

    db = get_db()
    job_ids = _job_id_variants(job_id)
    app_query: dict[str, Any] = {
        "candidate_id": candidate_id,
        "application_id": application_id,
    }
    if job_ids:
        app_query["job_id"] = {"$in": job_ids}

    existing_app = db["candidateInformation"].find_one(app_query)
    if not existing_app:
        raise HTTPException(
            status_code=400,
            detail="Invalid application session. Please restart from step 1.",
        )

    from app.services.application_policy import (
        APPLICATION_STATUS_APPLIED,
        APPLICATION_STATUS_IN_PROGRESS,
        _resolve_application_status,
    )

    app_status = _resolve_application_status(existing_app)
    if app_status == APPLICATION_STATUS_APPLIED or existing_app.get("applied_date"):
        raise HTTPException(status_code=409, detail="This application has already been submitted.")
    if app_status != APPLICATION_STATUS_IN_PROGRESS:
        raise HTTPException(
            status_code=400,
            detail="This application session is no longer active. Please restart from step 1.",
        )

    cover_letter = {
        "type": data.get("coverLetterType", "text"),
        "value": data.get("coverLetter", "") or data.get("cover_letter", ""),
    }
    candidate_info = {
        "candidate_id": candidate_id,
        "job_id": job_id,
        "first_name": data.get("first_name", ""),
        "last_name": data.get("last_name", ""),
        "email": data.get("email", ""),
        # Never persist client-supplied / plaintext mobile on applications
        "phone": "",
        "linkedin": data.get("linkedin", ""),
        "cover_letter": cover_letter,
        "has_cover_letter": data.get("hasCoverLetter", data.get("has_cover_letter", "")),
        "work_mode": data.get("workMode", data.get("work_mode", "")),
        "current_location": data.get(
            "currentLocation", data.get("current_location", "")
        ),
        "preferred_city": data.get("preferredCity", data.get("preferred_city", "")),
        "notice_period": data.get("noticePeriod", data.get("notice_period", "")),
        "current_salary": data.get("currentSalary", data.get("current_salary", "")),
        "expected_salary": data.get("expectedSalary", data.get("expected_salary", "")),
        "job_change_reason": data.get(
            "jobChangeReason", data.get("job_change_reason", "")
        ),
        "career_gap_reason": data.get(
            "careerGap", data.get("career_gap_reason", "")
        ),
        "interview_date": data.get("interview_date", ""),
        "interview_time_slots": data.get(
            "interview_time", data.get("interview_time_slots", "")
        ),
        "legally_authorized": data.get(
            "authorized", data.get("legally_authorized", False)
        ),
        "open_to_relocate": data.get(
            "openToRelocate", data.get("open_to_relocate", "")
        ),
        "found_us": data.get("foundUs", data.get("found_us", "")),
    }
    profile_picture = (
        data.get("profilePicture")
        or data.get("profile_picture")
        or ""
    ).strip()
    if profile_picture:
        candidate_info["profile_picture"] = profile_picture

    extra_fields = {
        k: v
        for k, v in data.items()
        if k
        not in candidate_info
        and k not in {"coverLetterType", "coverLetter", "profilePicture", "profile_picture"}
    }
    if extra_fields:
        candidate_info["extra_onboarding_fields"] = extra_fields

    candidate_info["application_id"] = application_id
    candidate_info["application_status"] = APPLICATION_STATUS_IN_PROGRESS
    candidate_info["status"] = APPLICATION_STATUS_IN_PROGRESS

    get_db()["candidateInformation"].update_one(
        {"_id": existing_app["_id"]},
        {"$set": candidate_info},
    )

    if profile_picture:
        try:
            get_db()["candidates"].update_one(
                {"_id": ObjectId(candidate_id)},
                {"$set": {"profile_picture": profile_picture}},
            )
        except Exception:
            pass

    return {"status": "success", "application_id": application_id}


@router.get("/api/personal_info_status/{candidate_id}")
async def personal_info_status(
    candidate_id: str,
    job_id: Optional[str] = Query(None),
    principal=Depends(get_current_principal),
):
    assert_candidate_access(principal, candidate_id, job_id)
    doc = get_db()["ResumeV2"].find_one({"candidate_id": candidate_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate resume not found")
    personal_info = (doc.get("resume_json") or {}).get("personal_information", {}) or {}
    full_name = personal_info.get("full_name", "")
    parts = full_name.split() if full_name else []
    from app.helpers import resolve_candidate_contact

    contact = resolve_candidate_contact(candidate_id, audience="self")
    return {
        "ready": True,
        "personal_info": {
            "first_name": parts[0] if parts else "",
            "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
            "email": contact.get("email") or personal_info.get("email", ""),
            "phone": contact.get("phone") or "",
            "phoneDisplay": contact.get("phoneDisplay") or contact.get("phone") or "",
            "phoneCountry": contact.get("phoneCountry") or "",
            "phoneDialCode": contact.get("phoneDialCode") or "",
        },
    }


@router.get("/api/candidates/{job_id}")
async def get_candidates_for_job(job_id: str, principal=Depends(require_hr)):
    assert_candidate_access(principal, "", job_id)
    job_ids = _job_id_variants(job_id)
    if not job_ids:
        return []

    docs = list(get_db()["candidateInformation"].find({"job_id": {"$in": job_ids}}))
    from app.services.application_policy import (
        APPLICATION_STATUS_APPLIED,
        _resolve_application_status,
    )

    docs = [
        doc
        for doc in docs
        if _resolve_application_status(doc) == APPLICATION_STATUS_APPLIED
    ]
    docs.sort(key=_effective_applied_sort_key, reverse=True)

    candidate_ids = [doc.get("candidate_id", "") for doc in docs]
    profile_pictures = get_candidate_profile_pictures(candidate_ids)

    candidates = []
    for doc in docs:
        first_name = doc.get("first_name", "")
        last_name = doc.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip()
        applied = doc.get("applied_date") or doc.get("created_at") or ""
        cid = doc.get("candidate_id", "")
        profile_picture = (
            doc.get("profile_picture")
            or profile_pictures.get(cid)
        )
        contact = resolve_candidate_contact(cid, doc, job_id)
        candidates.append(
            {
                "application_id": doc.get("application_id"),
                "candidate_id": cid,
                "name": full_name if full_name else "N/A",
                "email": contact["email"] or doc.get("email", "N/A"),
                "profilePicture": profile_picture,
                "profile_picture": profile_picture,
                "age": doc.get("age", ""),
                "reason": doc.get("reason", ""),
                "score_breakdown": doc.get("score_breakdown", {}),
                "status": doc.get("application_status") or doc.get("status", "Pending"),
                "score": doc.get("score", None),
                "applied": applied,
            }
        )
    return candidates


@router.get("/api/candidate/{candidate_id}")
async def get_candidate_details(
    candidate_id: str,
    job_id: str,
    application_id: Optional[str] = Query(None),
    principal=Depends(require_hr),
):
    assert_candidate_access(principal, candidate_id, job_id)
    db = get_db()
    query: dict[str, Any] = {"candidate_id": candidate_id}
    job_ids = _job_id_variants(job_id)
    if job_ids:
        query["job_id"] = {"$in": job_ids}
    if application_id:
        query["application_id"] = application_id

    if application_id:
        candidate_doc = db["candidateInformation"].find_one(query)
    else:
        docs = list(db["candidateInformation"].find(query))
        if not docs:
            candidate_doc = None
        else:
            docs.sort(key=_effective_applied_sort_key, reverse=True)
            candidate_doc = docs[0]
    if not candidate_doc:
        raise HTTPException(status_code=404, detail="Candidate not found")
    candidate_doc["_id"] = str(candidate_doc.get("_id", ""))
    resume_doc = db["ResumeV2"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id}
    )
    candidate = candidate_doc.copy()
    candidate["resume_file_id"] = (
        resume_doc.get("resume_file_id") if resume_doc else None
    )
    candidate["name"] = (
        f"{candidate_doc.get('first_name', '')} {candidate_doc.get('last_name', '')}"
    ).strip()
    profile_picture = (
        candidate_doc.get("profile_picture")
        or get_candidate_profile_picture(candidate_id)
    )
    candidate["profilePicture"] = profile_picture
    candidate["profile_picture"] = profile_picture
    contact = resolve_candidate_contact(
        candidate_id, candidate_doc, job_id, audience="masked"
    )
    if contact["email"]:
        candidate["email"] = contact["email"]
    # KYC registered mobile only — never resume-extracted; masked until reveal
    candidate["phone"] = contact.get("phone") or ""
    candidate["phoneMasked"] = contact.get("phone") or ""
    candidate["phoneDisplay"] = ""
    candidate["phoneRevealed"] = False
    candidate["hasVerifiedMobile"] = bool(
        contact.get("phone") or contact.get("phoneCountry")
    )
    from app.services.candidate_mobile_identity import get_plaintext_mobile

    try:
        candidate["hasVerifiedMobile"] = bool(get_plaintext_mobile(candidate_id))
    except Exception:
        pass
    candidate["phoneCountry"] = contact.get("phoneCountry") or ""
    candidate["phoneDialCode"] = contact.get("phoneDialCode") or ""

    profile_role = ""
    profile_city = ""
    profile_country = ""
    try:
        oid = ObjectId(candidate_id)
        profile_doc = db["candidates"].find_one(
            {"_id": oid},
            {"role": 1, "city": 1, "country": 1},
        )
        if profile_doc:
            profile_role = str(profile_doc.get("role") or "").strip()
            profile_city = str(profile_doc.get("city") or "").strip()
            profile_country = str(profile_doc.get("country") or "").strip()
    except Exception:
        profile_doc = None
    candidate["role"] = (
        profile_role
        or str(candidate_doc.get("applied_for") or "").strip()
        or ""
    )
    candidate["profile_city"] = profile_city
    candidate["profile_country"] = profile_country
    return candidate


@router.get("/api/candidate_by_id/{candidate_id}")
async def get_candidate_information(
    candidate_id: str, principal=Depends(require_hr)
):
    doc = get_db()["candidateInformation"].find_one({"candidate_id": candidate_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found")
    job_id = doc.get("job_id")
    if job_id:
        assert_candidate_access(principal, candidate_id, str(job_id))
    full_name = f"{doc.get('first_name', '')} {doc.get('last_name', '')}".strip()
    profile_picture = (
        doc.get("profile_picture")
        or get_candidate_profile_picture(candidate_id)
    )
    contact = resolve_candidate_contact(
        candidate_id, doc, str(job_id) if job_id else None, audience="masked"
    )
    return {
        "candidate_id": doc.get("candidate_id"),
        "name": full_name if full_name else "N/A",
        "email": contact["email"] or doc.get("email", "N/A"),
        "phone": contact["phone"] or "N/A",
        "phoneMasked": contact["phone"] or "N/A",
        "phoneDisplay": "",
        "phoneRevealed": False,
        "phoneCountry": contact.get("phoneCountry") or "",
        "phoneDialCode": contact.get("phoneDialCode") or "",
        "profilePicture": profile_picture,
        "profile_picture": profile_picture,
    }


@router.get("/api/manual_candidates/{jobid}")
async def get_manual_candidates(jobid: str, principal=Depends(require_hr)):
    assert_candidate_access(principal, "", jobid)
    from fastapi.responses import JSONResponse

    db = get_db()
    try:
        candidates_cursor = db["ResumeV2"].find(
            {"job_id": jobid, "added_by": "HR"}, {"candidate_id": 1}
        )
        candidates = []
        for doc in candidates_cursor:
            candidate_id = doc.get("candidate_id")
            resume = db["ResumeV2"].find_one(
                {"candidate_id": candidate_id},
                {"resume_json.personal_information": 1},
            )
            personal_info = (
                (resume.get("resume_json") or {}).get("personal_information", {})
                if resume
                else {}
            )
            score_doc = (
                db["Scores"].find_one(
                    {"candidate_id": candidate_id, "job_id": jobid}
                )
                or {}
            )
            profile_picture = get_candidate_profile_picture(candidate_id)
            from app.helpers import resolve_candidate_contact

            contact = resolve_candidate_contact(str(candidate_id), audience="hr")
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "full_name": personal_info.get("full_name", "N/A"),
                    "email": contact.get("email") or personal_info.get("email", "N/A"),
                    "phone": contact.get("phone") or "N/A",
                    "phoneDisplay": contact.get("phoneDisplay") or contact.get("phone") or "N/A",
                    "profilePicture": profile_picture,
                    "profile_picture": profile_picture,
                    "llm_score": score_doc.get("llm_score"),
                    "score_breakdown": score_doc.get("score_breakdown", {}),
                    "reasons": score_doc.get("reasons", []),
                }
            )
        return JSONResponse(candidates)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/resume_json_personal/{candidate_id}")
async def get_resume_json_personal(
    candidate_id: str,
    job_id: Optional[str] = Query(None),
    principal=Depends(get_current_principal),
):
    """Synchronous PyMongo — must not be awaited."""
    from fastapi.responses import JSONResponse

    assert_candidate_access(principal, candidate_id, job_id)
    doc = get_db()["ResumeV2"].find_one(
        {"candidate_id": candidate_id},
        {"resume_json.personal_information": 1},
    )
    if (
        not doc
        or "resume_json" not in doc
        or "personal_information" not in doc["resume_json"]
    ):
        raise HTTPException(
            status_code=404, detail="Candidate or personal information not found"
        )
    return JSONResponse(doc["resume_json"]["personal_information"])

@router.post("/api/candidate/{candidate_id}/submit-application")
async def submit_application(
    candidate_id: str,
    request: Request,
    principal=Depends(get_current_principal),
):
    assert_candidate_access(principal, candidate_id)
    db = get_db()
    job_id = None
    application_id = None
    candidate_email = None
    job_role = None
    company_name = None
    try:
        data = await request.json()
        job_id = data.get("jobId") or data.get("job_id")
        application_id = data.get("applicationId") or data.get("application_id")
        candidate_email = data.get("candidateEmail") or data.get("candidate_email")
        job_role = data.get("jobRole") or data.get("job_role")
        company_name = data.get("companyName") or data.get("company_name")
    except Exception:
        pass

    now = datetime.now(timezone.utc)

    if application_id:
        query = {"candidate_id": candidate_id, "application_id": application_id}
    else:
        query = {"candidate_id": candidate_id}
        job_ids = _job_id_variants(job_id)
        if job_ids:
            query["job_id"] = {"$in": job_ids}
        docs = list(db["candidateInformation"].find(query))
        if not docs:
            return {"success": False, "modified_count": 0}
        docs.sort(key=_effective_applied_sort_key, reverse=True)
        query = {"_id": docs[0]["_id"]}

    prior = db["candidateInformation"].find_one(query) or {}
    from app.services.application_policy import _resolve_application_status

    already_applied = _resolve_application_status(prior) == "applied"

    result = db["candidateInformation"].update_one(
        query,
        {
            "$set": {
                "applied_date": prior.get("applied_date") or now,
                "application_status": "applied",
                "status": "applied",
                "interview_pending": False,
            }
        },
    )

    # Notify only on the first transition into Applied — avoids duplicate HR
    # alerts when FE finalize and backend auto-expire both run.
    if result.matched_count > 0 and not already_applied:
        info_doc = db["candidateInformation"].find_one(query) or {}
        resolved_email = (
            candidate_email
            or info_doc.get("email")
            or ""
        ).strip()
        resolved_role = job_role or info_doc.get("applied_for") or "this role"

        # Always resolve the job doc so we can attach hr_user_id. Previously we
        # only looked up the job when company_name was missing — but the
        # frontend always sends companyName (even "Not available"), so
        # hr_user_id stayed None and /api/notifications/hr (which filters by
        # principal.subject) never returned the notification.
        job_doc = {}
        if job_id:
            for jid in _job_id_variants(job_id):
                job_doc = db["jobview"].find_one({"jobid": jid}) or {}
                if job_doc:
                    break
            if not (company_name or "").strip() or str(company_name).strip().lower() in {
                "not available",
                "n/a",
                "na",
            }:
                company_name = (
                    job_doc.get("companyName")
                    or job_doc.get("company")
                    or company_name
                    or ""
                )

        try:
            from app.services.application_policy import get_hr_user_id_for_job
            from app.services.notifications import create_notification

            company_label = (company_name or "").strip() or "the company"
            candidate_picture = (
                info_doc.get("profile_picture")
                or get_candidate_profile_picture(candidate_id)
            )
            hr_user_id = (
                (job_doc.get("hr_user_id") if job_doc else None)
                or (get_hr_user_id_for_job(db, job_id) if job_id else None)
            )
            create_notification(
                audience="hr",
                notification_type="candidate_applied",
                message=(
                    f"{resolved_email or 'A candidate'} has applied for "
                    f"{resolved_role} at {company_label}."
                ),
                candidate_id=candidate_id,
                candidate_email=resolved_email or None,
                candidate_picture=candidate_picture,
                job_id=str(job_id) if job_id is not None else None,
                job_role=resolved_role,
                company_name=(company_name or None),
                hr_user_id=str(hr_user_id) if hr_user_id else None,
            )
        except Exception:
            pass

    return {
        "success": result.matched_count > 0,
        "modified_count": result.modified_count,
    }