"""Deadline Expiration Worker for Technical Interview Postponement.

This worker identifies postponed interviews whose deadlines have passed
and marks them as NOT_ATTENDED with appropriate notifications and cleanup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.database import get_db
from app.rbac import constants as C
from app.schemas import TechnicalInterviewStatus
from app.services.technical_interview_postponement import (
    mark_not_attended,
    cancel_interview_reminders,
)

logger = logging.getLogger(__name__)


def process_expired_deadlines() -> dict:
    """Process all postponed interviews with expired deadlines.
    
    This function should be called periodically (e.g., every minute)
    by a scheduler/worker to check for expired deadlines.
    
    Returns:
        Summary of processed interviews
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    
    # Find all POSTPONED interviews with expired deadlines
    expired_interviews = list(db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].find({
        "status": TechnicalInterviewStatus.POSTPONED,
        "deadlineAt": {"$lte": now},
        "startedAt": None  # Only process if not started
    }))
    
    processed_count = 0
    failed_count = 0
    errors = []
    
    for interview in expired_interviews:
        application_id = interview.get("applicationId")
        job_id = interview.get("jobId")
        candidate_id = interview.get("candidateId")
        
        try:
            # Mark as not attended
            mark_not_attended(application_id)
            
            # Send candidate notification
            send_candidate_expiry_notification(application_id, job_id, candidate_id)
            
            # Send recruiter notification
            send_recruiter_expiry_notification(application_id, job_id, candidate_id)
            
            processed_count += 1
            logger.info(
                f"Marked interview {application_id} as NOT_ATTENDED for candidate {candidate_id}"
            )
            
        except Exception as e:
            failed_count += 1
            error_msg = f"Failed to process expired interview {application_id}: {str(e)}"
            errors.append(error_msg)
            logger.error(error_msg, exc_info=True)
    
    return {
        "processed": processed_count,
        "failed": failed_count,
        "errors": errors,
        "timestamp": now.isoformat()
    }


def send_candidate_expiry_notification(application_id: str, job_id: str, candidate_id: str) -> None:
    """Send notification to candidate that their interview deadline has expired."""
    db = get_db()
    
    # Get candidate info
    candidate = db["candidateInformation"].find_one({"candidate_id": candidate_id})
    if not candidate:
        logger.warning(f"Candidate {candidate_id} not found for expiry notification")
        return
    
    # Get job info
    job = db["jobview"].find_one({"jobid": job_id})
    if not job:
        logger.warning(f"Job {job_id} not found for expiry notification")
        return
    
    # Get postponement record for deadline info
    postponement = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].find_one({
        "applicationId": application_id
    })
    
    deadline = postponement.get("deadlineAt") if postponement else None
    
    # Prepare notification data
    notification_data = {
        "candidate_id": candidate_id,
        "job_id": job_id,
        "job_title": job.get("jobrole", "Unknown Position"),
        "company": job.get("companyName", "Unknown Company"),
        "deadline": deadline.isoformat() if deadline else None,
        "application_id": application_id,
        "event_type": "technical_interview_expired"
    }
    
    # Send through candidate's preferred channels
    # This would integrate with your existing notification service
    try:
        from app.services.candidate_comms import notify_candidate_milestone
        
        notify_candidate_milestone(
            event="technical_interview_expired",
            candidate_id=candidate_id,
            job_id=job_id,
            data=notification_data
        )
        
        logger.info(f"Sent expiry notification to candidate {candidate_id}")
        
    except ImportError:
        logger.warning("candidate_comms module not available, skipping notification")
    except Exception as e:
        logger.error(f"Failed to send expiry notification to candidate {candidate_id}: {e}")


def send_recruiter_expiry_notification(application_id: str, job_id: str, candidate_id: str) -> None:
    """Send notification to recruiter that candidate's interview deadline has expired."""
    db = get_db()
    
    # Get job info to find HR
    job = db["jobview"].find_one({"jobid": job_id})
    if not job:
        logger.warning(f"Job {job_id} not found for recruiter expiry notification")
        return
    
    hr_user_id = job.get("hr_user_id") or job.get("hrUserId")
    if not hr_user_id:
        logger.warning(f"No HR user ID found for job {job_id}")
        return
    
    # Get candidate info
    candidate = db["candidateInformation"].find_one({"candidate_id": candidate_id})
    if not candidate:
        logger.warning(f"Candidate {candidate_id} not found for recruiter notification")
        return
    
    # Get postponement record
    postponement = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].find_one({
        "applicationId": application_id
    })
    
    deadline = postponement.get("deadlineAt") if postponement else None
    postponement_number = postponement.get("postponementNumber", 0) if postponement else 0
    
    # Prepare notification data
    notification_data = {
        "hr_user_id": str(hr_user_id),
        "candidate_id": candidate_id,
        "candidate_name": f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip(),
        "job_id": job_id,
        "job_title": job.get("jobrole", "Unknown Position"),
        "deadline": deadline.isoformat() if deadline else None,
        "postponement_number": postponement_number,
        "application_id": application_id,
        "event_type": "technical_interview_expired"
    }
    
    # Send notification to HR
    # This would integrate with your existing HR notification system
    try:
        # Example: Send email, in-app notification, etc.
        logger.info(f"Sent expiry notification to HR {hr_user_id} for candidate {candidate_id}")
        
    except Exception as e:
        logger.error(f"Failed to send expiry notification to HR {hr_user_id}: {e}")


def cleanup_stale_reminders() -> dict:
    """Clean up reminder jobs that are still pending but past their scheduled time.
    
    This is a safety net to ensure no stale reminders accumulate.
    
    Returns:
        Summary of cleaned up reminders
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    
    # Find reminders that are still pending but scheduled time has passed
    stale_reminders = list(db[C.COL_INTERVIEW_REMINDERS].find({
        "status": "pending",
        "scheduledAt": {"$lt": now}
    }))
    
    cleaned_count = 0
    
    for reminder in stale_reminders:
        try:
            db[C.COL_INTERVIEW_REMINDERS].update_one(
                {"_id": reminder["_id"]},
                {"$set": {
                    "status": "expired",
                    "expiredAt": now
                }}
            )
            cleaned_count += 1
            
        except Exception as e:
            logger.error(f"Failed to cleanup stale reminder {reminder['_id']}: {e}")
    
    return {
        "cleaned": cleaned_count,
        "timestamp": now.isoformat()
    }


def get_upcoming_deadlines(hours_ahead: int = 24) -> list:
    """Get interviews with deadlines in the next N hours.
    
    Useful for dashboards and proactive notifications.
    
    Args:
        hours_ahead: Number of hours to look ahead (default: 24)
    
    Returns:
        List of interviews with upcoming deadlines
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    future = now + timedelta(hours=hours_ahead)
    
    upcoming = list(db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].find({
        "status": TechnicalInterviewStatus.POSTPONED,
        "deadlineAt": {
            "$gt": now,
            "$lte": future
        },
        "startedAt": None
    }).sort("deadlineAt", 1))
    
    return upcoming


def get_deadline_statistics() -> dict:
    """Get statistics about deadline states.
    
    Returns:
        Dictionary with deadline statistics
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    
    # Count by status
    status_counts = {}
    for status in [
        TechnicalInterviewStatus.POSTPONED,
        TechnicalInterviewStatus.NOT_ATTENDED,
        TechnicalInterviewStatus.IN_PROGRESS,
        TechnicalInterviewStatus.COMPLETED
    ]:
        count = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].count_documents({
            "status": status
        })
        status_counts[status] = count
    
    # Count expiring soon (next 12 hours)
    twelve_hours_future = now + timedelta(hours=12)
    expiring_soon = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].count_documents({
        "status": TechnicalInterviewStatus.POSTPONED,
        "deadlineAt": {
            "$gt": now,
            "$lte": twelve_hours_future
        },
        "startedAt": None
    })
    
    # Count expired but not yet processed (should be 0 normally)
    expired_unprocessed = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].count_documents({
        "status": TechnicalInterviewStatus.POSTPONED,
        "deadlineAt": {"$lte": now},
        "startedAt": None
    })
    
    return {
        "status_counts": status_counts,
        "expiring_soon_12h": expiring_soon,
        "expired_unprocessed": expired_unprocessed,
        "timestamp": now.isoformat()
    }


if __name__ == "__main__":
    # For manual testing or one-off execution
    logging.basicConfig(level=logging.INFO)
    
    logger.info("Starting deadline expiration worker...")
    result = process_expired_deadlines()
    logger.info(f"Deadline expiration worker completed: {result}")
    
    logger.info("Starting stale reminder cleanup...")
    cleanup_result = cleanup_stale_reminders()
    logger.info(f"Stale reminder cleanup completed: {cleanup_result}")
    
    logger.info("Getting deadline statistics...")
    stats = get_deadline_statistics()
    logger.info(f"Deadline statistics: {stats}")