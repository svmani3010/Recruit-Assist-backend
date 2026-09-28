"""Resume upload endpoints."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import (
    AuthPrincipal,
    assert_candidate_access,
    create_candidate_token,
    get_current_principal,
    get_current_principal_optional,
    require_candidate,
    require_hr,
)
from io import BytesIO

from app.branding_images import branding_kind_from_metadata, optimize_branding_image
from app.database import get_db, get_gridfs
from app.helpers import (
    extract_personal_info,
    extract_resume_text_for_upload,
    file_bytes_to_preview_pdf,
    grid_file_to_preview_pdf,
    insert_resume_mongodb_single_chunk,
    resume_content_type_for_suffix,
    validate_upload_suffix,
    resolve_profile_picture,
    apply_candidate_profile_picture,
    apply_candidate_profile_picture_bytes,
    extract_profile_picture_from_payload,
)
from app.rate_limit import limiter
from app.services.screening import (
    finalscreening_hr,
    get_gemini,
    run_in_thread,
    run_initial_screening_and_store,
)
from app.settings import get_settings
from app.company_branding import attach_company_branding

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from typing import Annotated, Optional

import gridfs



logger = logging.getLogger(__name__)
router = APIRouter(tags=["resumes"])


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


def _format_job_location(job_doc: dict) -> str:
    loc = job_doc.get("location") or job_doc.get("jobLocation") or ""
    if isinstance(loc, list):
        return ", ".join(str(item) for item in loc if item) or "Not available"
    return str(loc).strip() or "Not available"


def _format_salary(job_doc: dict) -> str:
    if job_doc.get("salaryUndisclosed"):
        return "Undisclosed"
    pkg = job_doc.get("salaryPackage")
    if isinstance(pkg, list):
        text = ", ".join(str(item).strip() for item in pkg if str(item).strip())
        return text or "Not available"
    text = str(pkg or "").strip()
    return text or "Not available"


def _extract_skills(job_doc: dict) -> list:
    skills = job_doc.get("desiredSkills") or []
    if isinstance(skills, str):
        parsed = [part.strip() for part in skills.split(",") if part.strip()]
    elif isinstance(skills, list):
        parsed = [str(item).strip() for item in skills if str(item).strip()]
    else:
        parsed = []

    if not parsed:
        ai_keywords = job_doc.get("jd_keywords_ai") or job_doc.get("jd_keywords") or []
        if isinstance(ai_keywords, list):
            parsed = [str(item).strip() for item in ai_keywords if str(item).strip()]

    return parsed[:4]


def _resolve_badge(job_doc: dict) -> tuple[str, str]:
    work_mode_raw = job_doc.get("workMode")
    if isinstance(work_mode_raw, list):
        work_mode = " ".join(str(item) for item in work_mode_raw)
    else:
        work_mode = str(work_mode_raw or "")

    work_mode_lower = work_mode.lower()
    if "remote" in work_mode_lower:
        return "remote", "Remote"
    if "hybrid" in work_mode_lower:
        return "hybrid", "Hybrid"

    employment = str(job_doc.get("employmentType") or "Full Time").strip()
    employment_lower = employment.lower()
    if "part" in employment_lower:
        return "parttime", "Part Time"
    if "contract" in employment_lower:
        return "contract", "Contract"
    if "intern" in employment_lower:
        return "internship", "Internship"
    return "fulltime", employment or "Full Time"


def _profile_job_entry(
    db,
    job_id: str,
    applied_at=None,
    status=None,
    saved_at=None,
) -> Optional[dict]:
    from app.helpers import is_job_expired_for_candidates

    job_doc = db["jobview"].find_one({"jobid": job_id}) or {}
    if not job_doc:
        return None

    job_doc = attach_company_branding(job_doc, db)
    badge_type, badge_label = _resolve_badge(job_doc)
    job_expired = is_job_expired_for_candidates(job_doc)

    entry = {
        "id": str(job_id),
        "title": job_doc.get("jobrole") or job_doc.get("jobRole") or "Not available",
        "company": job_doc.get("companyName") or job_doc.get("company") or "Not available",
        "companyLogo": job_doc.get("companyLogo") or "",
        "location": _format_job_location(job_doc),
        "salary": _format_salary(job_doc),
        "skills": _extract_skills(job_doc),
        "badgeType": badge_type,
        "badgeLabel": badge_label,
        "employmentType": job_doc.get("employmentType") or "",
        "workMode": job_doc.get("workMode") or "",
        # Listing / expiry signals for My Jobs (Continue Interview CTA).
        "jobExpired": job_expired,
        "applicationsClosed": job_expired,
        "show": job_doc.get("show") or "",
        "jobStatus": job_doc.get("status") or "",
        "joiningRequirement": job_doc.get("joiningRequirement") or "",
    }

    if applied_at is not None:
        dt = _as_utc_dt(applied_at)
        entry["appliedAt"] = dt.isoformat() if dt else str(applied_at)
        entry["status"] = status or "Applied"

    if saved_at is not None:
        dt = _as_utc_dt(saved_at)
        entry["savedAt"] = dt.isoformat() if dt else str(saved_at)

    return entry


def _get_applied_jobs_for_candidate(db, candidate_id: str) -> list:
    from app.services.application_policy import check_application_eligibility

    docs = list(db["candidateInformation"].find({"candidate_id": candidate_id}))
    by_job: dict[str, dict] = {}

    for doc in docs:
        job_id = doc.get("job_id")
        if not job_id:
            continue
        key = str(job_id)
        sort_key = _effective_applied_sort_key(doc)
        if key not in by_job or sort_key > by_job[key]["sort"]:
            from app.services.application_policy import _resolve_application_status

            app_status = _resolve_application_status(doc)
            display_status = "Applied" if app_status == "applied" else (
                "In progress" if app_status == "in_progress" else "Applied"
            )
            by_job[key] = {
                "sort": sort_key,
                "job_id": key,
                "applied_at": doc.get("applied_date") or doc.get("created_at"),
                "status": display_status,
            }

    jobs = []
    for item in sorted(by_job.values(), key=lambda row: row["sort"], reverse=True):
        eligibility = check_application_eligibility(candidate_id, item["job_id"])
        # Eligibility may auto-expire a stale interview window — prefer that status.
        resolved = (eligibility.get("application_status") or "").strip().lower()
        display_status = (
            "In progress"
            if resolved == "in_progress"
            else "Applied"
            if resolved == "applied"
            else item["status"]
        )
        entry = _profile_job_entry(
            db,
            item["job_id"],
            applied_at=item["applied_at"],
            status=display_status,
        )
        if entry:
            job_expired = bool(entry.get("jobExpired"))
            # Incomplete applications on an expired listing must not keep the
            # "Continue Interview" CTA — surface a terminal Job Expired status.
            if job_expired and resolved == "in_progress":
                entry["status"] = "Job Expired"
                resume_interview = False
                can_continue = False
            else:
                resume_interview = bool(eligibility.get("resume_interview")) and not job_expired
                # Continue CTA: any unfinished in-progress attempt on a still-open job.
                can_continue = (resolved == "in_progress") and not job_expired

            entry["canReapply"] = (
                eligibility.get("eligible", False)
                and not eligibility.get("resume_interview")
                and not job_expired
            )
            entry["nextEligibleAt"] = eligibility.get("next_eligible_at")
            entry["applicationStatus"] = (
                "job_expired" if (job_expired and resolved == "in_progress") else eligibility.get("application_status")
            )
            entry["resumeInterview"] = resume_interview
            entry["canContinueInterview"] = can_continue
            jobs.append(entry)
    return jobs


def _get_saved_jobs_for_candidate(db, candidate_id: str, saved_ids: list, saved_meta: dict) -> list:
    jobs = []
    for job_id in saved_ids:
        saved_at = (saved_meta or {}).get(str(job_id))
        entry = _profile_job_entry(db, str(job_id), saved_at=saved_at)
        if entry:
            jobs.append(entry)
    return jobs


### =================================================================================
### 1. PROFILE LIBRARY (GET) 
### Fetches the user's active resume from the profile library on login/refresh
### =================================================================================
@router.get("/api/profile/{candidate_id}")
async def get_profile(
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    assert_candidate_access(principal, candidate_id)

    db = get_db()
    try:
        candidate_doc = db["candidates"].find_one({"_id": ObjectId(candidate_id)})
        if not candidate_doc:
            return JSONResponse(
                status_code=404,
                content={"error": "candidate_not_found"}
            )

        profile_doc = db["ProfileLibrary"].find_one({
            "candidate_id": candidate_id,
            "is_active": True
        })

        resume_data = None
        if profile_doc:
            resume_data = {
                "file_id": profile_doc["file_id"],
                "filename": profile_doc["filename"],
                "fileUrl": f"/api/files/{profile_doc['file_id']}"
            }

        profile_picture = resolve_profile_picture(candidate_doc)
        saved_job_ids = [str(job_id) for job_id in (candidate_doc.get("saved_job_ids") or [])]
        saved_job_meta = candidate_doc.get("saved_job_meta") or {}
        applied_jobs = _get_applied_jobs_for_candidate(db, candidate_id)
        saved_jobs = _get_saved_jobs_for_candidate(db, candidate_id, saved_job_ids, saved_job_meta)

        from app.helpers import resolve_candidate_contact
        from app.services.profile_score import (
            candidate_score_payload,
            personal_info_from_doc,
        )

        contact = resolve_candidate_contact(candidate_id, audience="self")
        info = personal_info_from_doc(candidate_doc, phone=contact.get("phone") or "")
        info.update(
            {
                "role": candidate_doc.get("role", ""),
                "phone": contact.get("phone") or info.get("phone") or "",
                "phoneDisplay": contact.get("phoneDisplay")
                or contact.get("phone")
                or "",
                "phoneCountry": contact.get("phoneCountry") or info.get("phoneCountry") or "",
                "phoneDialCode": contact.get("phoneDialCode") or "",
                "phoneNational": contact.get("phoneNational") or "",
                "street": candidate_doc.get("street", ""),
            }
        )
        if resume_data:
            info["resume"] = {
                **(info.get("resume") or {}),
                "file_id": resume_data["file_id"],
                "fileId": resume_data["file_id"],
                "parsed": bool(candidate_doc.get("resumeParsed", True)),
                "parseError": candidate_doc.get("resumeParseError") or "",
            }

        try:
            score = candidate_score_payload(candidate_id)
        except Exception:
            score = candidate_doc.get("profileScore")

        return JSONResponse({
            "status": "success",
            "has_resume": resume_data is not None,
            "resume": resume_data,
            "profilePicture": profile_picture,
            "profile_picture": profile_picture,
            "saved_job_ids": saved_job_ids,
            "applied_jobs": applied_jobs,
            "saved_jobs": saved_jobs,
            "personal_info": info,
            "skills": candidate_doc.get("skills") or info.get("skills") or [],
            "education": candidate_doc.get("education") or info.get("education") or {},
            "userEditedFields": candidate_doc.get("userEditedFields") or [],
            "freshness": candidate_doc.get("freshness") or {},
            "credits": candidate_doc.get("credits") or [],
            "profileScore": score,
            "email_verified": info.get("emailVerified"),
        })

    except InvalidId:
        return JSONResponse(status_code=400, content={"error": "invalid_candidate_id"})
    except Exception as e:
        logger.exception("get_profile failed: %s", e)
        return JSONResponse(
            status_code=500,
            content={"error": "get_profile_failed", "detail": str(e)}
        )


@router.post("/api/profile/{candidate_id}/saved-jobs/{job_id}")
async def toggle_saved_job(
    candidate_id: str,
    job_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    assert_candidate_access(principal, candidate_id)

    db = get_db()
    try:
        candidate_doc = db["candidates"].find_one({"_id": ObjectId(candidate_id)})
        if not candidate_doc:
            return JSONResponse(status_code=404, content={"error": "candidate_not_found"})

        saved_ids = [str(item) for item in (candidate_doc.get("saved_job_ids") or [])]
        saved_meta = dict(candidate_doc.get("saved_job_meta") or {})
        job_key = str(job_id)

        if job_key in saved_ids:
            saved_ids = [item for item in saved_ids if item != job_key]
            saved_meta.pop(job_key, None)
        else:
            saved_ids.append(job_key)
            saved_meta[job_key] = datetime.now(timezone.utc).isoformat()

        db["candidates"].update_one(
            {"_id": ObjectId(candidate_id)},
            {"$set": {"saved_job_ids": saved_ids, "saved_job_meta": saved_meta}},
        )

        return JSONResponse({
            "status": "success",
            "saved_job_ids": saved_ids,
            "saved": job_key in saved_ids,
        })
    except InvalidId:
        return JSONResponse(status_code=400, content={"error": "invalid_candidate_id"})
    except Exception as e:
        logger.exception("toggle_saved_job failed: %s", e)
        return JSONResponse(
            status_code=500,
            content={"error": "toggle_saved_job_failed", "detail": str(e)},
        )

from app.schemas import OnboardingRequest, ProfilePictureUpdateRequest
from app.services.candidate_region import onboarding_region_persist_fields

_PROFILE_PICTURE_KEYS = {
    "profilePicture",
    "profile_picture",
    "picture",
    "photo",
    "avatar",
    "profilePhoto",
    "profile_photo",
}


@router.post("/api/profile/{candidate_id}/onboarding")
async def complete_onboarding(
    candidate_id: str,
    payload: OnboardingRequest,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    assert_candidate_access(principal, candidate_id)

    db = get_db()
    existing = db["candidates"].find_one({"_id": ObjectId(candidate_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Candidate not found")

    payload_dict = payload.model_dump(exclude_unset=True)
    raw_picture = extract_profile_picture_from_payload(payload_dict)
    update_fields = {
        key: value
        for key, value in payload_dict.items()
        if key not in _PROFILE_PICTURE_KEYS
    }
    update_fields["onboardingCompleted"] = True
    update_fields.update(
        onboarding_region_persist_fields(
            country_name_or_code=str(update_fields.get("country") or ""),
            phone_country_code=str(
                update_fields.get("phone_country_code")
                or update_fields.get("phoneCountry")
                or ""
            ),
            explicit_country_field=str(update_fields.get("country_field") or ""),
            existing_doc=existing,
        )
    )

    result = db["candidates"].update_one(
        {"_id": ObjectId(candidate_id)},
        {"$set": update_fields},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Candidate not found")

    stored_picture = None
    if raw_picture is not None:
        stored_picture = apply_candidate_profile_picture(candidate_id, raw_picture)

    return {
        "success": True,
        "profilePicture": stored_picture or resolve_profile_picture(
            db["candidates"].find_one({"_id": ObjectId(candidate_id)})
        ),
    }


@router.post("/api/profile/{candidate_id}/profile-picture")
async def update_profile_picture(
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
    file: Optional[UploadFile] = File(None),
    profilePicture: Optional[str] = Form(None),
):
    assert_candidate_access(principal, candidate_id)

    stored = None
    if file is not None:
        raw = await file.read()
        content_type = (file.content_type or "").split(";")[0].strip().lower()
        if not content_type and file.filename and "." in file.filename:
            ext = file.filename.rsplit(".", 1)[-1].lower()
            content_type = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "webp": "image/webp",
                "gif": "image/gif",
            }.get(ext, "")
        stored = apply_candidate_profile_picture_bytes(
            candidate_id, raw, content_type
        )
        if not stored:
            raise HTTPException(status_code=400, detail="Invalid profile picture")
    elif profilePicture:
        stored = apply_candidate_profile_picture(candidate_id, profilePicture)
        if not stored:
            raise HTTPException(status_code=400, detail="Invalid profile picture")
    else:
        raise HTTPException(status_code=400, detail="No profile picture provided")

    return {"success": True, "profilePicture": stored}

### =================================================================================
### 2. PROFILE LIBRARY UPLOAD (POST)
### Uploads resume to library, parses once, saves to GridFS and ProfileLibrary
### =================================================================================
@router.post("/api/profile/upload_resume")
async def profile_upload_resume(
    resume: UploadFile = File(...),
    candidate_id: str = Form(...),
    principal: Annotated[AuthPrincipal, Depends(require_candidate)] = ...,
):
    assert_candidate_access(principal, candidate_id)

    suffix = validate_upload_suffix(resume.filename)
    db = get_db()
    fs = get_gridfs()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await resume.read())
        tmp_path = tmp.name

    try:
        from app.helpers import (
            extract_resume_text_for_upload,
            resume_content_type_for_suffix,
        )

        try:
            extracted_text = await run_in_thread(
                extract_resume_text_for_upload, tmp_path, suffix
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Parse with Gemini ONLY ONCE during profile upload
        structured_resume = await run_in_thread(
            get_gemini().resume_parser, extracted_text
        )

        if isinstance(structured_resume, str):
            structured_resume = json.loads(structured_resume)
        if not isinstance(structured_resume, dict):
            raise ValueError("Parsed resume is not a JSON object")

        # Store the ORIGINAL upload (PDF/DOC/DOCX) so browser preview matches
        # the filename. Previously Word files were converted and only the PDF
        # bytes were saved while ProfileLibrary kept the .docx name — preview broke.
        content_type = resume_content_type_for_suffix(suffix)
        with open(tmp_path, "rb") as f:
            file_id = fs.put(
                f,
                filename=resume.filename,
                content_type=content_type,
                metadata={
                    "candidate_id": candidate_id,
                    "kind": "profile_resume",
                    "original_filename": resume.filename,
                    "suffix": suffix,
                },
            )

        db["ProfileLibrary"].update_many(
            {"candidate_id": candidate_id, "is_active": True},
            {"$set": {"is_active": False}},
        )

        profile_resume_doc = {
            "candidate_id": candidate_id,
            "file_id": str(file_id),
            "filename": resume.filename,
            "content_type": content_type,
            "structured_resume": structured_resume,
            "is_active": True,
        }
        db["ProfileLibrary"].insert_one(profile_resume_doc)
        try:
            from app.credits.hooks import on_resume_uploaded
            from app.credits.service import resume_fingerprint

            fp = resume_fingerprint(extracted_text or "")
            cand = db["candidates"].find_one({"_id": ObjectId(candidate_id)}) or {}
            previous = cand.get("resumeFingerprint")
            on_resume_uploaded(candidate_id, fp, previous)
            db["candidates"].update_one(
                {"_id": ObjectId(candidate_id)},
                {"$set": {"resumeFingerprint": fp}},
            )
        except Exception:
            pass

        parsed_ok = bool(isinstance(structured_resume, dict) and structured_resume)
        resume_updates = {
            "resumeUploaded": True,
            "resumeParsed": parsed_ok,
            "resumeParseError": None if parsed_ok else "Could not parse resume",
            "resumeUploadedAt": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        if parsed_ok:
            try:
                from app.services.profile_score import stamp_freshness

                existing = db["candidates"].find_one({"_id": ObjectId(candidate_id)}) or {}
                resume_updates["freshness"] = stamp_freshness(existing, "resume_uploaded")
            except Exception:
                pass
        db["candidates"].update_one(
            {"_id": ObjectId(candidate_id)},
            {"$set": resume_updates},
        )
        try:
            from app.services.profile_score import recalculate_and_persist

            recalculate_and_persist(candidate_id)
        except Exception:
            logger.exception("Profile score recalc after resume upload failed")

        return JSONResponse({
            "status": "success",
            "message": "Resume added to profile library",
            "file_id": str(file_id),
            "filename": resume.filename,
            "content_type": content_type,
            "previewable": suffix in {".pdf", ".doc", ".docx"},
            "parsed": parsed_ok,
            "parseError": None if parsed_ok else "Could not parse resume",
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("profile_upload_resume failed: %s", e)
        detail = str(e) or "profile_upload_failed"
        # Surface conversion / LibreOffice issues as 400 so the UI can show them.
        lower = detail.lower()
        if any(
            token in lower
            for token in ("libreoffice", "soffice", "convert", ".doc", "word")
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Could not process this Word document. "
                    "Please upload a PDF or DOCX file and try again."
                ),
            ) from e
        raise HTTPException(
            status_code=500,
            detail="Resume upload failed. Please try again or use a PDF.",
        ) from e
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


class ParseResumePayload(BaseModel):
    candidate_id: str
    file_id: Optional[str] = None


@router.post("/api/profile/parse_resume")
async def parse_profile_resume(
    payload: ParseResumePayload,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    """Return confirm-UI fields from the stored structured resume (never auto-applies)."""
    candidate_id = payload.candidate_id
    assert_candidate_access(principal, candidate_id)

    db = get_db()
    query: dict = {"candidate_id": candidate_id, "is_active": True}
    if payload.file_id:
        query["file_id"] = str(payload.file_id)
    profile_doc = db["ProfileLibrary"].find_one(query) or db["ProfileLibrary"].find_one(
        {"candidate_id": candidate_id, "is_active": True}
    )
    candidate_doc = db["candidates"].find_one({"_id": ObjectId(candidate_id)}) or {}
    structured = (profile_doc or {}).get("structured_resume")
    from app.services.profile_score import extraction_from_structured_resume, recalculate_and_persist

    extraction = extraction_from_structured_resume(
        structured,
        mobile_verified=bool(
            candidate_doc.get("mobile_verified") or candidate_doc.get("mobileVerified")
        ),
        email_verified=bool(
            candidate_doc.get("emailVerified")
            or candidate_doc.get("email_verified")
            or candidate_doc.get("email")
        ),
    )
    parsed_ok = bool(extraction.get("parsed"))
    db["candidates"].update_one(
        {"_id": ObjectId(candidate_id)},
        {
            "$set": {
                "resumeParsed": parsed_ok,
                "resumeParseError": None if parsed_ok else extraction.get("parseError"),
            }
        },
    )
    try:
        recalculate_and_persist(candidate_id)
    except Exception:
        logger.exception("Profile score recalc after parse_resume failed")
    return JSONResponse(extraction)


### =================================================================================
### FILE SERVING (GET) 
### Streams a stored resume/file back by its GridFS file_id
### =================================================================================
@router.get("/api/files/{file_id}")
async def get_file(
    file_id: str,
    principal: Annotated[Optional[AuthPrincipal], Depends(get_current_principal_optional)] = None,
):
    fs = get_gridfs()

    try:
        file_obj_id = ObjectId(file_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    try:
        grid_file = fs.get(file_obj_id)
    except gridfs.NoFile:
        raise HTTPException(status_code=404, detail="File not found")

    metadata = dict(grid_file.metadata or {})
    file_kind = str(metadata.get("kind") or "").strip().lower()
    kind = branding_kind_from_metadata(metadata.get("kind"))
    # Avatars and company branding are loaded in <img src> without auth headers.
    if not kind and file_kind != "candidate_profile_picture":
        _authorize_private_file(principal, metadata)

    headers = {
        "Content-Disposition": f'inline; filename="{grid_file.filename}"',
    }
    if kind:
        headers["Cache-Control"] = "public, max-age=604800, immutable"
    elif file_kind == "candidate_profile_picture":
        headers["Cache-Control"] = "private, max-age=86400"

    if kind and not metadata.get("optimized"):
        raw = grid_file.read()
        try:
            optimized, content_type, ext = optimize_branding_image(raw, kind)
            stored_name = f"company_{kind}{ext}"
            metadata.update(
                {
                    "optimized": True,
                    "original_bytes": len(raw),
                    "stored_bytes": len(optimized),
                }
            )
            fs.delete(file_obj_id)
            try:
                fs.put(
                    optimized,
                    _id=file_obj_id,
                    filename=stored_name,
                    content_type=content_type,
                    metadata=metadata,
                )
            except Exception:
                fs.put(
                    raw,
                    _id=file_obj_id,
                    filename=grid_file.filename,
                    content_type=grid_file.content_type or "application/octet-stream",
                    metadata=dict(grid_file.metadata or {}),
                )
                raise
            return StreamingResponse(
                BytesIO(optimized),
                media_type=content_type,
                headers=headers,
            )
        except Exception:
            logger.warning(
                "Legacy branding optimize failed for %s; serving original",
                file_id,
                exc_info=True,
            )
            return StreamingResponse(
                BytesIO(raw),
                media_type=grid_file.content_type or "application/octet-stream",
                headers=headers,
            )

    return StreamingResponse(
        grid_file,
        media_type=grid_file.content_type or "application/pdf",
        headers=headers,
    )


@router.get("/api/files/{file_id}/preview")
async def get_file_preview(
    file_id: str,
    principal: Annotated[Optional[AuthPrincipal], Depends(get_current_principal_optional)] = None,
):
    """Stream a resume as PDF for in-browser preview (PDF/DOC/DOCX)."""
    fs = get_gridfs()

    try:
        file_obj_id = ObjectId(file_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    try:
        grid_file = fs.get(file_obj_id)
    except gridfs.NoFile:
        raise HTTPException(status_code=404, detail="File not found")

    metadata = dict(grid_file.metadata or {})
    file_kind = str(metadata.get("kind") or "").strip().lower()
    kind = branding_kind_from_metadata(metadata.get("kind"))
    if not kind and file_kind != "candidate_profile_picture":
        _authorize_private_file(principal, metadata)

    try:
        pdf_bytes = grid_file_to_preview_pdf(grid_file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("file preview conversion failed for %s", file_id)
        raise HTTPException(
            status_code=500,
            detail="Could not convert resume to PDF for preview.",
        ) from exc

    base_name = os.path.splitext(grid_file.filename or "resume")[0] or "resume"
    headers = {
        "Content-Disposition": f'inline; filename="{base_name}.pdf"',
    }
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers=headers,
    )


@router.post("/api/resume/preview")
@limiter.limit("30/minute")
async def preview_resume_upload(
    request: Request,
    resume: UploadFile = File(...),
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)] = ...,
):
    """Ephemeral PDF preview for a local resume pick (before it is stored)."""
    suffix = validate_upload_suffix(resume.filename)
    raw = await resume.read()
    if not raw:
        raise HTTPException(status_code=400, detail="The selected file is empty.")
    try:
        pdf_bytes = file_bytes_to_preview_pdf(
            raw, filename=resume.filename or "resume", suffix=suffix
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("resume preview upload conversion failed")
        raise HTTPException(
            status_code=500,
            detail="Could not convert resume to PDF for preview.",
        ) from exc

    base_name = os.path.splitext(resume.filename or "resume")[0] or "resume"
    headers = {"Content-Disposition": f'inline; filename="{base_name}.pdf"'}
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers=headers,
    )


def _authorize_private_file(principal: Optional[AuthPrincipal], metadata: dict) -> None:
    """Resumes and other private GridFS objects require an authenticated owner or HR."""
    from app.auth import Role
    from app.hr_tenant import require_hr_job

    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if principal.role == Role.SERVICE:
        return

    candidate_id = str(metadata.get("candidate_id") or "")
    job_id = str(metadata.get("job_id") or "")

    if principal.role == Role.CANDIDATE:
        if candidate_id and principal.candidate_id == candidate_id:
            return
        raise HTTPException(status_code=403, detail="Not authorized to access this file")

    if principal.role == Role.HR:
        db = get_db()
        if job_id:
            require_hr_job(principal, job_id, db)
            return
        if candidate_id:
            job_ids = {
                str(doc.get("job_id"))
                for doc in db["ResumeV2"].find({"candidate_id": candidate_id}, {"job_id": 1})
                if doc.get("job_id") is not None
            }
            for owned_job_id in job_ids:
                try:
                    require_hr_job(principal, owned_job_id, db)
                    return
                except HTTPException:
                    continue
        raise HTTPException(status_code=403, detail="Not authorized to access this file")

    raise HTTPException(status_code=403, detail="Not authorized to access this file")


### =================================================================================
### 3. APPLY FOR JOB (Refactored Original API)
### Accepts either an existing file_id OR a new custom PDF
### =================================================================================
@router.post("/api/apply_job")
@router.post("/api/upload_resume")
@limiter.limit("20/minute")
async def apply_job(
    request: Request,
    background_tasks: BackgroundTasks,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    jobid: str = Form(...),
    jobrole: str = Form(...),
    candidate_id: str = Form(...),
    existing_file_id: Optional[str] = Form(None),
    resume: Optional[UploadFile] = File(None),
):
    assert_candidate_access(principal, candidate_id, jobid)
    if not existing_file_id and not resume:
        return JSONResponse(
            status_code=400, 
            content={"error": "Must provide either an existing file_id or a new resume file."}
        )

    db = get_db()
    fs = get_gridfs()
    
    structured_resume = None
    file_id = None
    pdf_filename = None
    tmp_path = None

    try:
        from app.services.application_policy import (
            assert_can_start_application,
            assert_job_is_open_for_applications,
            create_application_stub,
            supersede_prior_applications,
        )

        assert_job_is_open_for_applications(jobid)
        eligibility = assert_can_start_application(candidate_id, jobid)
        application_id = str(uuid.uuid4())

        # SCENARIO A: Existing profile resume
        if existing_file_id:
            existing_doc = db["ProfileLibrary"].find_one({
                "file_id": existing_file_id,
                "candidate_id": candidate_id,
                "is_active": True
            })

            if not existing_doc:
                return JSONResponse(status_code=403, content={"error": "Unauthorized or invalid file ID."})
            
            structured_resume = existing_doc["structured_resume"]
            file_id = existing_file_id
            pdf_filename = existing_doc["filename"]

        # SCENARIO B: New custom resume upload
        elif resume:
            suffix = validate_upload_suffix(resume.filename)
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(await resume.read())
                tmp_path = tmp.name

            # Same path as profile upload: PDF direct; Word via LibreOffice with
            # .docx text fallback so missing soffice does not block Job Apply.
            try:
                extracted_text = await run_in_thread(
                    extract_resume_text_for_upload, tmp_path, suffix
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            structured_resume = await run_in_thread(
                get_gemini().resume_parser, extracted_text
            )
            if isinstance(structured_resume, str):
                structured_resume = json.loads(structured_resume)
            if not isinstance(structured_resume, dict):
                raise ValueError("Parsed resume is not a JSON object")

            content_type = resume_content_type_for_suffix(suffix)
            with open(tmp_path, "rb") as f:
                new_fs_id = fs.put(
                    f,
                    filename=resume.filename,
                    content_type=content_type,
                    metadata={
                        "candidate_id": candidate_id,
                        "job_id": jobid,
                        "custom_upload": True,
                        "original_filename": resume.filename,
                        "suffix": suffix,
                    },
                )

            file_id = str(new_fs_id)
            pdf_filename = resume.filename

        # COMMON WORKFLOW: Proceed with application submission
        insert_resume_mongodb_single_chunk(
            structured_resume,
            candidate_id=candidate_id,
            job_id=jobid,
            job_role=jobrole,
            file_id=file_id,
            filename=pdf_filename,
        )

        if eligibility.get("reason") == "reapply_allowed":
            supersede_prior_applications(db, candidate_id, jobid, application_id)
        elif eligibility.get("reason") == "restart_in_progress":
            # Abandoned mid-flow restart — replace the stub. Interview-resume
            # apps never reach here (assert_can_start_application blocks them).
            supersede_prior_applications(db, candidate_id, jobid, application_id)

        create_application_stub(db, candidate_id, jobid, application_id=application_id)
        
        personal_info = extract_personal_info(structured_resume)
        
        db["Scores"].update_one(
            {"candidate_id": candidate_id, "job_id": jobid},
            {
                "$set": {
                    "candidate_id": candidate_id,
                    "job_id": jobid,
                    "compatibility": 0,
                    "status": "processing",
                }
            },
            upsert=True,
        )
        
        background_tasks.add_task(
            run_initial_screening_and_store, jobid, candidate_id
        )

        return JSONResponse(
            {
                "candidate_id": candidate_id,
                "application_id": application_id,
                "status": "Application submitted, initial screening started",
                "personal_info": personal_info,
                "resume_file_id": file_id,
                "access_token": create_candidate_token(candidate_id, jobid),
                "token_type": "bearer",
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("apply_job failed: %s", e)
        detail = str(e) or "apply_job_failed"
        lower = detail.lower()
        if any(
            token in lower
            for token in ("libreoffice", "soffice", "convert", ".doc", "word")
        ):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "resume_conversion_failed",
                    "detail": (
                        "Could not process this Word document. "
                        "Please upload a PDF or DOCX and try again."
                    ),
                },
            )
        return JSONResponse(
            status_code=500,
            content={"error": "apply_job_failed", "detail": detail},
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


### =================================================================================
### 4. HR FINAL SCREENING (Original untouched endpoint)
### =================================================================================
@router.post("/api/resume-screening")
async def upload_resume_hr(
    resume: UploadFile = File(...),
    jobid: str = Form(...),
    jobrole: str = Form(...),
    background_tasks: BackgroundTasks = None,
    principal=Depends(require_hr),
):
    assert_candidate_access(principal, "", jobid)
    candidate_id = str(uuid.uuid4())
    suffix = validate_upload_suffix(resume.filename)
    fs = get_gridfs()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await resume.read())
        tmp_path = tmp.name

    try:
        try:
            extracted_text = await run_in_thread(
                extract_resume_text_for_upload, tmp_path, suffix
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        structured_resume = await run_in_thread(
            get_gemini().resume_parser, extracted_text
        )
        if isinstance(structured_resume, str):
            structured_resume = json.loads(structured_resume)
        if not isinstance(structured_resume, dict):
            raise ValueError("Parsed resume is not a JSON object")

        content_type = resume_content_type_for_suffix(suffix)
        with open(tmp_path, "rb") as f:
            file_id = fs.put(
                f,
                filename=resume.filename,
                content_type=content_type,
                metadata={
                    "candidate_id": candidate_id,
                    "job_id": jobid,
                    "added_by": "HR",
                    "job_role": jobrole,
                    "original_filename": resume.filename,
                    "suffix": suffix,
                },
            )

        insert_resume_mongodb_single_chunk(
            structured_resume,
            candidate_id=candidate_id,
            job_id=jobid,
            job_role=jobrole,
            file_id=file_id,
            added_by="HR",
            filename=resume.filename,
        )
        if background_tasks:
            background_tasks.add_task(finalscreening_hr, jobid, candidate_id)

        return JSONResponse(
            {
                "candidate_id": candidate_id,
                "status": "resume stored, final screening started",
                "resume_file_id": str(file_id),
            }
        )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


### =================================================================================
### 5. COVER LETTER UPLOAD (Original untouched endpoint)
### =================================================================================
@router.post("/api/upload_cover_letter")
async def upload_cover_letter(
    filename: str = Form(...),
    cover_letter: UploadFile = File(...),
    _principal=Depends(get_current_principal),
):
    suffix = validate_upload_suffix(cover_letter.filename)
    fs = get_gridfs()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await cover_letter.read())
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            file_id = fs.put(
                f,
                filename=cover_letter.filename,
                content_type=cover_letter.content_type,
            )
        return {"file_id": str(file_id), "filename": cover_letter.filename}
    finally:
        os.unlink(tmp_path)