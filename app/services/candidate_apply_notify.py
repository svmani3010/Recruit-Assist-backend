"""HR email when a candidate submits an application (once per candidate + job)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from app.database import get_db
from app.helpers import get_job_role_helper
from app.services.application_policy import get_hr_user_id_for_job, _job_id_variants

logger = logging.getLogger(__name__)


def resolve_hr_recipients_for_job(db, job_id: str) -> List[str]:
    """Prefer the job owner's HR email; fall back to global HR_EMAILS."""
    from app.services.hr_calendar_oauth import get_hr_email_for_user_id
    from app.settings import get_settings

    recipients: List[str] = []
    hr_user_id = get_hr_user_id_for_job(db, job_id)
    if hr_user_id:
        owner_email = (get_hr_email_for_user_id(hr_user_id) or "").strip()
        if owner_email:
            recipients.append(owner_email)

    if recipients:
        return recipients

    return list(get_settings().hr_email_list or [])


def send_candidate_apply_email(
    *,
    job_id: str,
    candidate_id: Optional[str] = None,
    candidate_name: Optional[str] = None,
    job_role: Optional[str] = None,
    submission_date: Optional[str] = None,
    mark_sent: bool = True,
) -> tuple[bool, str]:
    """
    Send the HR apply card email once per candidate/job pair.
    Returns (sent, message).
    """
    from email_utils import notify_hr

    job_id_str = str(job_id or "").strip()
    if not job_id_str:
        return False, "job_id is required"

    db = get_db()
    candidate_id_str = str(candidate_id or "").strip()

    if candidate_id_str and mark_sent:
        job_variants = _job_id_variants(job_id_str)
        info = None
        if job_variants:
            info = db["candidateInformation"].find_one(
                {"candidate_id": candidate_id_str, "job_id": {"$in": job_variants}}
            )
        if info and info.get("apply_hr_email_sent_at"):
            return True, "Notification already sent for this application"

    resolved_role = (job_role or "").strip()
    if not resolved_role or resolved_role == "N/A":
        if candidate_id_str:
            resolved_role = get_job_role_helper(job_id_str, candidate_id_str)
        else:
            job_doc = db["jobview"].find_one({"jobid": job_id_str}) or {}
            if job_id_str.isdigit() and not job_doc:
                job_doc = db["jobview"].find_one({"jobid": int(job_id_str)}) or {}
            resolved_role = (
                job_doc.get("jobrole")
                or job_doc.get("jobRole")
                or job_doc.get("jobTitle")
                or "N/A"
            )

    recipients = resolve_hr_recipients_for_job(db, job_id_str)
    if not recipients:
        return False, "No HR recipients configured for this job"

    payload = {
        "job_id": job_id_str,
        "job_role": resolved_role,
        "candidate_name": (candidate_name or "").strip() or "Candidate",
        "submission_date": (submission_date or "").strip() or None,
    }

    sent = notify_hr("candidate_apply", payload, recipients=recipients)
    if not sent:
        return False, "Email could not be delivered (check Mailgun / EMAIL_ENABLED)"

    if candidate_id_str:
        try:
            from app.services.candidate_comms import notify_candidate_milestone

            notify_candidate_milestone(
                event="application_submitted",
                candidate_id=candidate_id_str,
                job_id=job_id_str,
                data={
                    "candidate_name": payload["candidate_name"],
                    "job_role": resolved_role,
                    "status": "Application submitted",
                },
            )
        except Exception:
            logger.exception(
                "Candidate application milestone notify failed candidate=%s",
                candidate_id_str,
            )

    if candidate_id_str and mark_sent:
        now = datetime.now(timezone.utc)
        job_variants = _job_id_variants(job_id_str)
        if job_variants:
            db["candidateInformation"].update_one(
                {"candidate_id": candidate_id_str, "job_id": {"$in": job_variants}},
                {"$set": {"apply_hr_email_sent_at": now}},
                upsert=False,
            )

    return True, "Candidate application notification sent to HR"
