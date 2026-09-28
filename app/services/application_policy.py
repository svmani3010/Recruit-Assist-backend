"""One-application-per-candidate-per-job with HR-configurable reapply cooldown."""

from __future__ import annotations

import calendar
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.database import get_db

logger = logging.getLogger(__name__)

APPLICATION_STATUS_IN_PROGRESS = "in_progress"
APPLICATION_STATUS_APPLIED = "applied"
APPLICATION_STATUS_SUPERSEDED = "superseded"

DEFAULT_REAPPLY_COOLDOWN_MONTHS = 6
INTERVIEW_START_DEADLINE_HOURS = 24


def _job_id_variants(job_id: Any) -> List[Any]:
    if job_id is None or job_id == "":
        return []
    variants = {job_id, str(job_id)}
    try:
        variants.add(int(job_id))
    except (TypeError, ValueError):
        pass
    return list(variants)


def _as_utc_dt(value: Any) -> Optional[datetime]:
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


def add_months(dt: datetime, months: int) -> datetime:
    months = int(months)
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _resolve_application_status(doc: dict) -> str:
    explicit = (doc.get("application_status") or "").strip().lower()
    if explicit in {
        APPLICATION_STATUS_IN_PROGRESS,
        APPLICATION_STATUS_APPLIED,
        APPLICATION_STATUS_SUPERSEDED,
    }:
        return explicit

    legacy_status = (doc.get("status") or "").strip().lower()
    if legacy_status == APPLICATION_STATUS_SUPERSEDED:
        return APPLICATION_STATUS_SUPERSEDED
    if doc.get("applied_date") or legacy_status == APPLICATION_STATUS_APPLIED:
        return APPLICATION_STATUS_APPLIED
    if legacy_status in {"pending", "applied"}:
        return APPLICATION_STATUS_APPLIED if legacy_status == "applied" else APPLICATION_STATUS_IN_PROGRESS
    return APPLICATION_STATUS_IN_PROGRESS


def get_hr_user_id_for_job(db, job_id: str) -> Optional[str]:
    job_ids = _job_id_variants(job_id)
    if not job_ids:
        return None
    job = db["jobview"].find_one({"jobid": {"$in": job_ids}}) or {}
    hr_user_id = job.get("hr_user_id") or job.get("hrUserId")
    return str(hr_user_id) if hr_user_id else None


def get_reapply_cooldown_months(db, hr_user_id: Optional[str]) -> int:
    if not hr_user_id:
        return DEFAULT_REAPPLY_COOLDOWN_MONTHS
    try:
        doc = db["hr_users"].find_one({"_id": ObjectId(hr_user_id)})
    except Exception:
        doc = None
    if not doc:
        return DEFAULT_REAPPLY_COOLDOWN_MONTHS
    policy = doc.get("application_policy") or {}
    raw = policy.get("reapply_cooldown_months", DEFAULT_REAPPLY_COOLDOWN_MONTHS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_REAPPLY_COOLDOWN_MONTHS


def get_application_policy(hr_user_id: str) -> dict:
    db = get_db()
    try:
        doc = db["hr_users"].find_one({"_id": ObjectId(hr_user_id)})
    except Exception:
        doc = None
    policy = (doc or {}).get("application_policy") or {}
    months = policy.get("reapply_cooldown_months", DEFAULT_REAPPLY_COOLDOWN_MONTHS)
    try:
        months = max(1, min(60, int(months)))
    except (TypeError, ValueError):
        months = DEFAULT_REAPPLY_COOLDOWN_MONTHS
    return {"reapply_cooldown_months": months}


def save_application_policy(hr_user_id: str, reapply_cooldown_months: int) -> dict:
    months = max(1, min(60, int(reapply_cooldown_months)))
    db = get_db()
    db["hr_users"].update_one(
        {"_id": ObjectId(hr_user_id)},
        {"$set": {"application_policy": {"reapply_cooldown_months": months}}},
    )
    return {"reapply_cooldown_months": months}


def get_latest_active_application(
    db,
    candidate_id: str,
    job_id: str,
) -> Optional[dict]:
    job_ids = _job_id_variants(job_id)
    if not job_ids:
        return None

    docs = list(
        db["candidateInformation"].find(
            {"candidate_id": candidate_id, "job_id": {"$in": job_ids}}
        )
    )
    active = [
        doc
        for doc in docs
        if _resolve_application_status(doc) != APPLICATION_STATUS_SUPERSEDED
    ]
    if not active:
        return None
    active.sort(key=_effective_applied_sort_key, reverse=True)
    return active[0]


def _get_scores_doc(db, candidate_id: str, job_id: str) -> Optional[dict]:
    job_ids = _job_id_variants(job_id)
    if not job_ids:
        return None
    doc = db["Scores"].find_one(
        {"candidate_id": candidate_id, "job_id": {"$in": job_ids}}
    )
    if doc:
        return doc
    return db["Scores"].find_one({"candidate_id": candidate_id, "job_id": job_id})


def interview_completed_from_scores(score_doc: Optional[dict]) -> bool:
    """True once the candidate has finished (or been scored on) the L1 interview."""
    if not score_doc:
        return False
    if score_doc.get("interview_report"):
        return True
    status = str(score_doc.get("status") or "").strip().lower()
    if not status:
        return False
    if status.startswith("l1"):
        return True
    markers = (
        "l1 passed",
        "l1 rejected",
        "l1 completed",
        "interview completed",
    )
    return any(marker in status for marker in markers)


def l0_passed_for_application(db, candidate_id: str, job_id: str) -> bool:
    from app.services.screening import l0_passed_from_score_doc, serialize_l0_result

    score_doc = _get_scores_doc(db, candidate_id, job_id)
    if not score_doc:
        return False
    result = serialize_l0_result(score_doc)
    if not result.get("ready"):
        return False
    return bool(result.get("l0_passed") or l0_passed_from_score_doc(score_doc))


def finalize_application_to_applied(
    db,
    app_doc: dict,
    *,
    reason: Optional[str] = None,
    notify_hr: bool = False,
) -> dict:
    """Flip an in-progress application to applied (terminal). Idempotent."""
    if not app_doc or not app_doc.get("_id"):
        return app_doc
    if _resolve_application_status(app_doc) == APPLICATION_STATUS_APPLIED:
        return app_doc

    now = datetime.now(timezone.utc)
    update: Dict[str, Any] = {
        "applied_date": app_doc.get("applied_date") or now,
        "application_status": APPLICATION_STATUS_APPLIED,
        "status": APPLICATION_STATUS_APPLIED,
        "interview_pending": False,
    }
    if reason:
        update["finalize_reason"] = reason
    db["candidateInformation"].update_one({"_id": app_doc["_id"]}, {"$set": update})
    merged = {**app_doc, **update}

    if notify_hr:
        _notify_hr_application_finalized(db, merged, reason=reason)

    return merged


def _notify_hr_application_finalized(
    db,
    app_doc: dict,
    *,
    reason: Optional[str] = None,
) -> None:
    """Best-effort HR ping when an application becomes Applied (once per finalize)."""
    try:
        from app.helpers import get_candidate_profile_picture
        from app.services.notifications import create_notification

        candidate_id = app_doc.get("candidate_id")
        job_id = app_doc.get("job_id")
        if not candidate_id or job_id is None:
            return

        job_doc = {}
        for jid in _job_id_variants(job_id):
            job_doc = db["jobview"].find_one({"jobid": jid}) or {}
            if job_doc:
                break

        company_name = (
            job_doc.get("companyName")
            or job_doc.get("company")
            or app_doc.get("company_name")
            or ""
        )
        job_role = (
            job_doc.get("jobrole")
            or job_doc.get("jobRole")
            or job_doc.get("jobTitle")
            or app_doc.get("applied_for")
            or "this role"
        )
        email = (app_doc.get("email") or "").strip()
        hr_user_id = (
            job_doc.get("hr_user_id")
            or job_doc.get("hrUserId")
            or get_hr_user_id_for_job(db, job_id)
        )
        reason_note = ""
        create_notification(
            audience="hr",
            notification_type="candidate_applied",
            message=(
                f"{email or 'A candidate'} has applied for "
                f"{job_role} at {(company_name or 'the company').strip() or 'the company'}"
                f"{reason_note}."
            ),
            candidate_id=str(candidate_id),
            candidate_email=email or None,
            candidate_picture=get_candidate_profile_picture(str(candidate_id)),
            job_id=str(job_id),
            job_role=job_role,
            company_name=(company_name or None),
            hr_user_id=str(hr_user_id) if hr_user_id else None,
        )
    except Exception:
        # Never fail finalize because notification delivery failed.
        pass


def ensure_interview_window(
    db,
    candidate_id: str,
    job_id: str,
    *,
    application_id: Optional[str] = None,
    hours: int = INTERVIEW_START_DEADLINE_HOURS,
) -> Dict[str, Any]:
    """Idempotently set interview_pending + interview_start_deadline on the app."""
    latest = None
    if application_id:
        latest = db["candidateInformation"].find_one(
            {"candidate_id": candidate_id, "application_id": application_id}
        )
    if not latest:
        latest = get_latest_active_application(db, candidate_id, job_id)
    if not latest:
        raise HTTPException(status_code=404, detail="Application not found")

    if _resolve_application_status(latest) == APPLICATION_STATUS_APPLIED:
        deadline = _as_utc_dt(latest.get("interview_start_deadline"))
        return {
            "application_id": latest.get("application_id"),
            "application_status": APPLICATION_STATUS_APPLIED,
            "interview_pending": False,
            "interview_started": bool(latest.get("interview_started_at")),
            "interview_start_deadline": deadline.isoformat() if deadline else None,
            "created": False,
        }

    if not l0_passed_for_application(db, candidate_id, job_id):
        raise HTTPException(
            status_code=409,
            detail="Initial screening must be passed before opening the interview window.",
        )

    existing = _as_utc_dt(latest.get("interview_start_deadline"))
    if existing is not None:
        db["candidateInformation"].update_one(
            {"_id": latest["_id"]},
            {"$set": {"interview_pending": True}},
        )
        return {
            "application_id": latest.get("application_id"),
            "application_status": _resolve_application_status(latest),
            "interview_pending": True,
            "interview_started": bool(latest.get("interview_started_at")),
            "interview_start_deadline": existing.isoformat(),
            "created": False,
        }

    deadline = datetime.now(timezone.utc) + timedelta(hours=max(1, int(hours)))
    db["candidateInformation"].update_one(
        {"_id": latest["_id"]},
        {
            "$set": {
                "interview_pending": True,
                "interview_start_deadline": deadline,
            }
        },
    )
    return {
        "application_id": latest.get("application_id"),
        "application_status": APPLICATION_STATUS_IN_PROGRESS,
        "interview_pending": True,
        "interview_started": bool(latest.get("interview_started_at")),
        "interview_start_deadline": deadline.isoformat(),
        "created": True,
    }


def mark_interview_started(
    db,
    candidate_id: str,
    job_id: str,
    *,
    application_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record that the candidate began L1 within the start window (idempotent)."""
    latest = None
    if application_id:
        latest = db["candidateInformation"].find_one(
            {"candidate_id": candidate_id, "application_id": application_id}
        )
    if not latest:
        latest = get_latest_active_application(db, candidate_id, job_id)
    if not latest:
        raise HTTPException(status_code=404, detail="Application not found")

    now = datetime.now(timezone.utc)
    update: Dict[str, Any] = {
        "interview_pending": True,
    }
    if not latest.get("interview_started_at"):
        update["interview_started_at"] = now
    if _as_utc_dt(latest.get("interview_start_deadline")) is None:
        # Safety: if they somehow begin without a window, open one anchored now.
        update["interview_start_deadline"] = now + timedelta(
            hours=INTERVIEW_START_DEADLINE_HOURS
        )

    db["candidateInformation"].update_one({"_id": latest["_id"]}, {"$set": update})
    refreshed = db["candidateInformation"].find_one({"_id": latest["_id"]}) or {
        **latest,
        **update,
    }
    deadline = _as_utc_dt(refreshed.get("interview_start_deadline"))
    return {
        "application_id": refreshed.get("application_id"),
        "application_status": _resolve_application_status(refreshed),
        "interview_pending": True,
        "interview_started": True,
        "interview_start_deadline": deadline.isoformat() if deadline else None,
    }


def _maybe_expire_interview_window(db, app_doc: dict, candidate_id: str, job_id: str) -> dict:
    """If the 24h *start* window lapsed without beginning L1, mark applied.

    Once the candidate has started the interview, they may finish after the
    start deadline — do not auto-expire those apps.
    """
    if not app_doc:
        return app_doc
    if _resolve_application_status(app_doc) != APPLICATION_STATUS_IN_PROGRESS:
        return app_doc

    if app_doc.get("interview_started_at"):
        return app_doc

    deadline = _as_utc_dt(app_doc.get("interview_start_deadline"))
    pending = bool(app_doc.get("interview_pending"))
    if not pending or deadline is None:
        return app_doc

    score_doc = _get_scores_doc(db, candidate_id, job_id)
    if interview_completed_from_scores(score_doc):
        return app_doc

    now = datetime.now(timezone.utc)
    if now < deadline:
        return app_doc

    return finalize_application_to_applied(
        db, app_doc, reason="interview_expired", notify_hr=True
    )


def _maybe_finalize_abandoned_interview(
    db, app_doc: dict, candidate_id: str, job_id: str
) -> dict:
    """If L1 was started but not finished, treat it as a consumed one-shot attempt."""
    if not app_doc:
        return app_doc
    if _resolve_application_status(app_doc) != APPLICATION_STATUS_IN_PROGRESS:
        return app_doc
    if not app_doc.get("interview_started_at"):
        return app_doc

    score_doc = _get_scores_doc(db, candidate_id, job_id)
    if interview_completed_from_scores(score_doc):
        return app_doc

    try:
        from app.services.screening import abandon_l1_interview

        abandon_l1_interview(candidate_id, job_id)
    except Exception as exc:
        logger.warning(
            "abandon_l1_interview failed for candidate=%s job=%s: %s",
            candidate_id,
            job_id,
            exc,
        )

    return finalize_application_to_applied(
        db, app_doc, reason="interview_abandoned", notify_hr=True
    )


def _maybe_finalize_completed_interview(
    db, app_doc: dict, candidate_id: str, job_id: str
) -> dict:
    """If L1 finished but status is still in_progress, flip to Applied for HR."""
    if not app_doc:
        return app_doc
    if _resolve_application_status(app_doc) != APPLICATION_STATUS_IN_PROGRESS:
        return app_doc

    score_doc = _get_scores_doc(db, candidate_id, job_id)
    if not interview_completed_from_scores(score_doc):
        return app_doc

    return finalize_application_to_applied(
        db, app_doc, reason="interview_completed", notify_hr=True
    )


def _eligibility_interview_fields(
    db,
    app_doc: Optional[dict],
    candidate_id: str,
    job_id: str,
) -> Dict[str, Any]:
    if not app_doc:
        return {
            "resume_interview": False,
            "interview_pending": False,
            "interview_started": False,
            "interview_start_deadline": None,
            "interview_expired": False,
            "interview_completed": False,
            "l0_passed": False,
        }

    score_doc = _get_scores_doc(db, candidate_id, job_id)
    interview_done = interview_completed_from_scores(score_doc)
    l0_passed = l0_passed_for_application(db, candidate_id, job_id)
    deadline = _as_utc_dt(app_doc.get("interview_start_deadline"))
    pending = bool(app_doc.get("interview_pending"))
    started = bool(app_doc.get("interview_started_at"))
    now = datetime.now(timezone.utc)
    expired = bool(
        pending
        and deadline is not None
        and now >= deadline
        and not interview_done
        and not started
    )
    app_status = _resolve_application_status(app_doc)
    resume = (
        app_status == APPLICATION_STATUS_IN_PROGRESS
        and l0_passed
        and not interview_done
        and not expired
        and not started
        and (pending or deadline is not None)
    )
    return {
        "resume_interview": resume,
        "interview_pending": pending,
        "interview_started": started,
        "interview_start_deadline": deadline.isoformat() if deadline else None,
        "interview_expired": expired,
        "interview_completed": interview_done,
        "l0_passed": l0_passed,
    }


def check_application_eligibility(candidate_id: str, job_id: str) -> Dict[str, Any]:
    db = get_db()
    hr_user_id = get_hr_user_id_for_job(db, job_id)
    cooldown_months = get_reapply_cooldown_months(db, hr_user_id)
    latest = get_latest_active_application(db, candidate_id, job_id)
    now = datetime.now(timezone.utc)

    empty_interview = {
        "resume_interview": False,
        "interview_pending": False,
        "interview_started": False,
        "interview_start_deadline": None,
        "interview_expired": False,
        "interview_completed": False,
        "l0_passed": False,
    }

    score_block = None
    try:
        from app.services.profile_score import apply_gate_block

        score_block = apply_gate_block(candidate_id)
    except Exception:
        logger.exception("Profile score gate failed for candidate %s", candidate_id)

    if not latest:
        if score_block:
            return {
                **score_block,
                "application_status": None,
                "cooldown_months": cooldown_months,
                "next_eligible_at": None,
                "application_id": None,
                **empty_interview,
            }
        return {
            "eligible": True,
            "reason": "no_prior_application",
            "application_status": None,
            "cooldown_months": cooldown_months,
            "next_eligible_at": None,
            "application_id": None,
            **empty_interview,
        }

    latest = _maybe_expire_interview_window(db, latest, candidate_id, job_id)
    latest = _maybe_finalize_abandoned_interview(db, latest, candidate_id, job_id)
    latest = _maybe_finalize_completed_interview(db, latest, candidate_id, job_id)
    interview_fields = _eligibility_interview_fields(db, latest, candidate_id, job_id)
    app_status = _resolve_application_status(latest)

    # If the listing itself has expired / been paused, never offer resume_interview.
    try:
        from app.helpers import is_job_expired_for_candidates

        job_doc = db["jobview"].find_one({"jobid": {"$in": _job_id_variants(job_id)}}) or {}
        if is_job_expired_for_candidates(job_doc):
            interview_fields = {
                **interview_fields,
                "resume_interview": False,
                "job_expired": True,
            }
    except Exception:
        logger.exception("Job expiry check failed for job %s", job_id)

    if app_status == APPLICATION_STATUS_IN_PROGRESS:
        if interview_fields["resume_interview"]:
            return {
                "eligible": True,
                "reason": "resume_interview",
                "application_status": app_status,
                "cooldown_months": cooldown_months,
                "next_eligible_at": None,
                "application_id": latest.get("application_id"),
                **interview_fields,
            }
        return {
            "eligible": True,
            "reason": "restart_in_progress",
            "application_status": app_status,
            "cooldown_months": cooldown_months,
            "next_eligible_at": None,
            "application_id": latest.get("application_id"),
            **interview_fields,
        }

    if app_status != APPLICATION_STATUS_APPLIED:
        return {
            "eligible": True,
            "reason": "no_prior_application",
            "application_status": app_status,
            "cooldown_months": cooldown_months,
            "next_eligible_at": None,
            "application_id": latest.get("application_id"),
            **interview_fields,
        }

    anchor = _effective_applied_sort_key(latest)
    next_eligible = add_months(anchor, cooldown_months)

    if now >= next_eligible:
        if score_block:
            return {
                **score_block,
                "application_status": app_status,
                "cooldown_months": cooldown_months,
                "next_eligible_at": next_eligible.isoformat(),
                "application_id": latest.get("application_id"),
                "last_applied_at": anchor.isoformat(),
                **interview_fields,
            }
        return {
            "eligible": True,
            "reason": "reapply_allowed",
            "application_status": app_status,
            "cooldown_months": cooldown_months,
            "next_eligible_at": next_eligible.isoformat(),
            "application_id": latest.get("application_id"),
            "last_applied_at": anchor.isoformat(),
            **interview_fields,
        }

    return {
        "eligible": False,
        "reason": "cooldown_active",
        "application_status": app_status,
        "cooldown_months": cooldown_months,
        "next_eligible_at": next_eligible.isoformat(),
        "application_id": latest.get("application_id"),
        "last_applied_at": anchor.isoformat(),
        **interview_fields,
    }


def abandon_in_progress_interview(
    db,
    candidate_id: str,
    job_id: str,
    *,
    application_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Explicitly end a started-but-unfinished L1 attempt (one-shot policy)."""
    latest = None
    if application_id:
        latest = db["candidateInformation"].find_one(
            {"candidate_id": candidate_id, "application_id": application_id}
        )
    if not latest:
        latest = get_latest_active_application(db, candidate_id, job_id)
    if not latest:
        raise HTTPException(status_code=404, detail="Application not found")
    if not latest.get("interview_started_at"):
        raise HTTPException(
            status_code=409,
            detail="Interview has not started yet.",
        )

    score_doc = _get_scores_doc(db, candidate_id, job_id)
    if interview_completed_from_scores(score_doc):
        raise HTTPException(
            status_code=409,
            detail="Interview already completed.",
        )

    from app.services.screening import abandon_l1_interview

    abandon_l1_interview(candidate_id, job_id)
    finalized = finalize_application_to_applied(
        db,
        latest,
        reason="interview_abandoned",
        notify_hr=True,
    )
    return {
        "application_id": finalized.get("application_id"),
        "application_status": _resolve_application_status(finalized),
        "abandoned": True,
    }


def assert_can_start_application(candidate_id: str, job_id: str) -> Dict[str, Any]:
    eligibility = check_application_eligibility(candidate_id, job_id)
    if eligibility.get("resume_interview"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "You already have a pending interview window for this job. Return within 24 hours to begin — once started, the interview cannot be resumed.",
                "code": "resume_interview_required",
                "next_eligible_at": None,
                "application_status": eligibility.get("application_status"),
                "cooldown_months": eligibility.get("cooldown_months"),
                "application_id": eligibility.get("application_id"),
                "resume_interview": True,
                "interview_start_deadline": eligibility.get("interview_start_deadline"),
            },
        )
    if not eligibility["eligible"]:
        if eligibility.get("reason") == "profile_score":
            raise HTTPException(
                status_code=403,
                detail={
                    "message": eligibility.get("message")
                    or "Complete your profile to apply.",
                    "code": "profile_score",
                    "reason": "profile_score",
                    "profile_score_incomplete": True,
                    "profile_score": eligibility.get("profile_score"),
                    "application_status": eligibility.get("application_status"),
                    "cooldown_months": eligibility.get("cooldown_months"),
                },
            )
        next_at = eligibility.get("next_eligible_at")
        detail = (
            "You have already applied for this job. "
            f"You can apply again after {next_at}."
            if next_at
            else "You have already applied for this job."
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": detail,
                "code": "application_not_eligible",
                "next_eligible_at": next_at,
                "application_status": eligibility.get("application_status"),
                "cooldown_months": eligibility.get("cooldown_months"),
            },
        )
    return eligibility


def supersede_prior_applications(
    db,
    candidate_id: str,
    job_id: str,
    superseded_by_application_id: str,
) -> None:
    job_ids = _job_id_variants(job_id)
    now = datetime.now(timezone.utc)
    db["candidateInformation"].update_many(
        {
            "candidate_id": candidate_id,
            "job_id": {"$in": job_ids},
            "application_status": {"$ne": APPLICATION_STATUS_SUPERSEDED},
        },
        {
            "$set": {
                "application_status": APPLICATION_STATUS_SUPERSEDED,
                "status": APPLICATION_STATUS_SUPERSEDED,
                "superseded_by": superseded_by_application_id,
                "superseded_at": now,
            }
        },
    )


def create_application_stub(
    db,
    candidate_id: str,
    job_id: str,
    application_id: Optional[str] = None,
) -> str:
    app_id = application_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db["candidateInformation"].insert_one(
        {
            "candidate_id": candidate_id,
            "job_id": job_id,
            "application_id": app_id,
            "application_status": APPLICATION_STATUS_IN_PROGRESS,
            "status": APPLICATION_STATUS_IN_PROGRESS,
            "created_at": now,
        }
    )
    return app_id


def submitted_application_match(job_id: Any = None) -> Dict[str, Any]:
    """Mongo filter for applications HR should see (submitted, not stubs)."""
    match: Dict[str, Any] = {
        "$nor": [
            {"application_status": APPLICATION_STATUS_IN_PROGRESS},
            {"status": APPLICATION_STATUS_IN_PROGRESS},
            {"application_status": APPLICATION_STATUS_SUPERSEDED},
            {"status": APPLICATION_STATUS_SUPERSEDED},
        ],
        "$or": [
            {"application_status": APPLICATION_STATUS_APPLIED},
            {"applied_date": {"$exists": True, "$nin": [None, ""]}},
            {"status": APPLICATION_STATUS_APPLIED},
        ],
    }
    job_ids = _job_id_variants(job_id)
    if job_ids:
        match["job_id"] = {"$in": job_ids}
    return match


def assert_job_is_open_for_applications(job_id: str) -> dict:
    db = get_db()
    job_ids = _job_id_variants(job_id)
    job = db["jobview"].find_one({"jobid": {"$in": job_ids}}) if job_ids else None
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    status = str(job.get("status") or "").strip().lower()
    show = str(job.get("show") or "").strip().lower()
    if status in {"draft", "closed"} or show in {"off", "closed"} or show != "on":
        raise HTTPException(
            status_code=400,
            detail="This job is not open for applications.",
        )
    from app.helpers import is_application_deadline_passed

    if is_application_deadline_passed(job.get("joiningRequirement")):
        raise HTTPException(
            status_code=400,
            detail="The application window for this role has ended.",
        )
    return job
