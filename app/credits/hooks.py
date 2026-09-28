"""Event hooks so no feature writes a credit balance directly."""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("Credit hook failed: %s", getattr(fn, "__name__", fn))
        return None


def on_profile_score_100(candidate_id: str, *, first_time: bool = True) -> Optional[dict[str, Any]]:
    """Award once when profile reaches apply-eligible (100). Idempotent via event_id.

    `first_time` is kept for callers; awarding is gated by ledger idempotency, not
    the candidate's profileScoreReached100At flag — so a terms-blocked or
    pre-credits unlock still grants after terms are accepted / score refreshes.
    """
    _ = first_time
    from app.credits.service import award, referral_progress

    result = _safe(
        award,
        candidate_id=candidate_id,
        rule_id="profile_score_100",
        event_id=f"profile100:{candidate_id}",
        actor={"type": "system", "id": "profile_score"},
        skip_if_no_terms=True,
    )
    if result and result.get("awarded") and not result.get("duplicate"):
        _safe(referral_progress, candidate_id, "profile_100")
    return result


def on_resume_uploaded(candidate_id: str, fingerprint: str, previous: Optional[str]) -> Optional[dict[str, Any]]:
    if previous and previous == fingerprint:
        return {"awarded": False, "reason": "unchanged", "message": "Re-uploading the same resume does not earn credits."}
    from app.credits.service import award

    return _safe(
        award,
        candidate_id=candidate_id,
        rule_id="resume_material_update",
        event_id=f"resume:{fingerprint}",
        actor={"type": "candidate", "id": candidate_id},
        metadata={"fingerprint": fingerprint},
        skip_if_no_terms=True,
    )


def on_application_submitted(
    candidate_id: str,
    job_id: str,
    match_score: Optional[float],
) -> Optional[dict[str, Any]]:
    from app.credits.config import load_credit_config
    from app.credits.service import award

    threshold = float(load_credit_config().get("applyRelevanceThreshold") or 60)
    if match_score is None or float(match_score) < threshold:
        return {
            "awarded": False,
            "reason": "not_relevant",
            "message": "Application credits are only awarded when the role is a relevant match.",
        }
    return _safe(
        award,
        candidate_id=candidate_id,
        rule_id="apply_relevant",
        event_id=f"apply:{candidate_id}:{job_id}",
        actor={"type": "candidate", "id": candidate_id},
        metadata={"jobId": job_id, "matchScore": match_score},
        skip_if_no_terms=True,
    )


def on_employer_view(candidate_id: str, employer_id: str, job_id: str) -> Optional[dict[str, Any]]:
    from app.credits.config import load_credit_config
    from app.credits.ledger import count_rule_awards, period_start
    from app.credits.service import award
    from datetime import timedelta

    if load_credit_config().get("abuse", {}).get("suspendEmployerViewsFor"):
        blocked = load_credit_config()["abuse"].get("suspendEmployerViewsFor") or []
        if employer_id in blocked:
            return {"awarded": False, "reason": "employer_suspended"}
    since = period_start("day")
    # unique per employer per application per 30 days
    from datetime import datetime, timezone

    thirty = datetime.now(timezone.utc) - timedelta(days=30)
    dup = count_rule_awards(
        candidate_id,
        "employer_view",
        since=thirty,
        extra={"metadata.employerId": employer_id, "metadata.jobId": job_id},
    )
    if dup:
        return {"awarded": False, "reason": "duplicate_view"}
    return _safe(
        award,
        candidate_id=candidate_id,
        rule_id="employer_view",
        event_id=f"view:{employer_id}:{job_id}:{candidate_id}:{since.date().isoformat() if since else 'd'}",
        actor={"type": "employer", "id": employer_id},
        metadata={"employerId": employer_id, "jobId": job_id},
        skip_if_no_terms=True,
    )


def on_shortlist(candidate_id: str, job_id: str, employer_id: str = "") -> Optional[dict[str, Any]]:
    from app.credits.service import award, referral_progress

    result = _safe(
        award,
        candidate_id=candidate_id,
        rule_id="employer_shortlist",
        event_id=f"shortlist:{candidate_id}:{job_id}",
        actor={"type": "employer", "id": employer_id or "hr"},
        metadata={"jobId": job_id},
        skip_if_no_terms=True,
    )
    _safe(referral_progress, candidate_id, "shortlist")
    return result


def on_interview_attended(candidate_id: str, job_id: str, interview_id: str) -> Optional[dict[str, Any]]:
    from app.credits.service import award

    return _safe(
        award,
        candidate_id=candidate_id,
        rule_id="interview_attended",
        event_id=f"interview:{interview_id or job_id}",
        actor={"type": "employer", "id": "interviewer"},
        metadata={"jobId": job_id, "interviewId": interview_id},
        skip_if_no_terms=True,
    )


def on_hire(candidate_id: str, job_id: str, *, premium: bool = False) -> Optional[dict[str, Any]]:
    from app.credits.config import load_credit_config
    from app.credits.service import referral_progress

    config = load_credit_config()
    stage = "hire_premium" if premium and config.get("premiumRolesDefined") else "hire_standard"
    return _safe(referral_progress, candidate_id, stage)
