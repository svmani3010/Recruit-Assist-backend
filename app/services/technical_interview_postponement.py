"""Technical Interview Postponement Service.

This service handles the complete lifecycle of technical interview postponement,
including deadline calculation, reminder scheduling, and state management.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytz
from bson import ObjectId
from fastapi import HTTPException

from app.database import get_db
from app.rbac import constants as C
from app.schemas import TechnicalInterviewStatus

logger = logging.getLogger(__name__)


def get_platform_postponement_config() -> Dict[str, Any]:
    """Get platform-level postponement configuration."""
    return {
        "postponeTechnicalInterviewEnabled": C.POSTPONE_TECHNICAL_INTERVIEW_ENABLED,
        "postponementWindowHours": C.DEFAULT_POSTPONEMENT_WINDOW_HOURS,
        "maxPostponements": C.DEFAULT_MAX_POSTPONEMENTS,
        "extensionMaxHours": C.DEFAULT_EXTENSION_MAX_HOURS,
        "reminderIntervalHours": C.DEFAULT_REMINDER_INTERVAL_HOURS,
        "finalReminderHours": C.DEFAULT_FINAL_REMINDER_HOURS,
        "quietHoursStart": C.DEFAULT_QUIET_HOURS_START,
        "quietHoursEnd": C.DEFAULT_QUIET_HOURS_END,
        "automaticRejectionAfterNotAttended": C.DEFAULT_AUTO_REJECTION_AFTER_NOT_ATTENDED,
    }


def get_org_postponement_config(org_id: str) -> Dict[str, Any]:
    """Get organisation-level postponement configuration with platform defaults as fallback."""
    from app.rbac.org_service import get_org
    
    org = get_org(org_id)
    if not org:
        return get_platform_postponement_config()
    
    org_settings = org.get("settings", {})
    platform_config = get_platform_postponement_config()
    
    # Get postponement-specific settings
    postponement_settings = org_settings.get("postponement", {})
    
    return {
        "postponeTechnicalInterviewEnabled": postponement_settings.get(
            "postponeTechnicalInterviewEnabled",
            platform_config["postponeTechnicalInterviewEnabled"]
        ),
        "postponementWindowHours": postponement_settings.get(
            "postponementWindowHours",
            platform_config["postponementWindowHours"]
        ),
        "maxPostponements": postponement_settings.get(
            "maxPostponements",
            platform_config["maxPostponements"]
        ),
        "extensionMaxHours": postponement_settings.get(
            "extensionMaxHours",
            platform_config["extensionMaxHours"]
        ),
        "reminderIntervalHours": postponement_settings.get(
            "reminderIntervalHours",
            platform_config["reminderIntervalHours"]
        ),
        "finalReminderHours": postponement_settings.get(
            "finalReminderHours",
            platform_config["finalReminderHours"]
        ),
        "quietHoursStart": postponement_settings.get(
            "quietHoursStart",
            platform_config["quietHoursStart"]
        ),
        "quietHoursEnd": postponement_settings.get(
            "quietHoursEnd",
            platform_config["quietHoursEnd"]
        ),
        "automaticRejectionAfterNotAttended": postponement_settings.get(
            "automaticRejectionAfterNotAttended",
            platform_config["automaticRejectionAfterNotAttended"]
        ),
    }


def get_job_postponement_config(job_id: str) -> Dict[str, Any]:
    """Get job-level postponement configuration with org/platform defaults as fallback."""
    db = get_db()
    job = db["jobview"].find_one({"jobid": job_id})
    
    if not job:
        return get_platform_postponement_config()
    
    # Get org config if job has org_id
    org_id = job.get("org_id")
    base_config = get_org_postponement_config(org_id) if org_id else get_platform_postponement_config()
    
    # Job-level override for enable/disable
    job_postpone_enabled = job.get("technicalInterviewPostponeEnabled")
    if job_postpone_enabled is not None:
        base_config["postponeTechnicalInterviewEnabled"] = job_postpone_enabled
    
    return base_config


def calculate_deadline(postponed_at: datetime, postponement_window_hours: int) -> datetime:
    """Calculate deadline based on postponement time and configured window.
    
    Always returns UTC time for server-side consistency.
    """
    return postponed_at + timedelta(hours=postponement_window_hours)


def format_deadline_local(deadline_utc: datetime, timezone_str: str) -> str:
    """Format deadline in candidate's local timezone.
    
    Args:
        deadline_utc: UTC deadline datetime
        timezone_str: IANA timezone string (e.g., 'Asia/Kolkata')
    
    Returns:
        Formatted deadline string in local timezone
    """
    try:
        tz = pytz.timezone(timezone_str)
        deadline_local = deadline_utc.astimezone(tz)
        return deadline_local.strftime("%d %B %Y, %I:%M %p")
    except Exception as e:
        logger.warning(f"Failed to format deadline in timezone {timezone_str}: {e}")
        # Fallback to UTC formatting
        return deadline_utc.strftime("%d %B %Y, %I:%M %p UTC")


def get_remaining_hours(deadline_utc: datetime) -> float:
    """Calculate remaining hours until deadline.
    
    Returns negative value if deadline has passed.
    """
    now = datetime.now(timezone.utc)
    delta = deadline_utc - now
    return delta.total_seconds() / 3600


def get_countdown_state(remaining_hours: float) -> str:
    """Determine countdown state based on remaining hours.
    
    Returns:
        'normal', 'amber', 'red', or 'expired'
    """
    if remaining_hours <= 0:
        return "expired"
    elif remaining_hours <= 2:  # Less than 2 hours
        return "red"
    elif remaining_hours <= 12:  # Less than 12 hours
        return "amber"
    else:
        return "normal"


def create_postponement_record(
    application_id: str,
    candidate_id: str,
    job_id: str,
    candidate_timezone: str,
    interview_config: Dict[str, Any],
    postponement_config: Dict[str, Any]
) -> Dict[str, Any]:
    """Create a new postponement record.
    
    Args:
        application_id: Application identifier
        candidate_id: Candidate identifier
        job_id: Job identifier
        candidate_timezone: Candidate's timezone at postponement
        interview_config: Snapshot of interview configuration
        postponement_config: Postponement configuration to use
    
    Returns:
        Created postponement record
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    
    # Check for existing postponement record
    existing = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].find_one({
        "applicationId": application_id,
        "candidateId": candidate_id,
        "jobId": job_id,
        "status": {"$in": [TechnicalInterviewStatus.POSTPONED, TechnicalInterviewStatus.PENDING]}
    })
    
    postponement_number = 1
    if existing:
        postponement_number = existing.get("postponementNumber", 0) + 1
    
    # Calculate deadline
    postponement_window = postponement_config.get("postponementWindowHours", C.DEFAULT_POSTPONEMENT_WINDOW_HOURS)
    deadline_at = calculate_deadline(now, postponement_window)
    
    # Create postponement record
    record = {
        "applicationId": application_id,
        "candidateId": candidate_id,
        "jobId": job_id,
        "interviewId": f"interview_{application_id}_{postponement_number}",
        "status": TechnicalInterviewStatus.POSTPONED,
        "postponedAt": now,
        "deadlineAt": deadline_at,
        "candidateTimezoneAtPostponement": candidate_timezone,
        "postponementNumber": postponement_number,
        "maxPostponements": postponement_config.get("maxPostponements", C.DEFAULT_MAX_POSTPONEMENTS),
        "interviewConfigurationSnapshot": interview_config,
        "reminderConfigurationSnapshot": {
            "intervalHours": postponement_config.get("reminderIntervalHours", C.DEFAULT_REMINDER_INTERVAL_HOURS),
            "finalReminderHours": postponement_config.get("finalReminderHours", C.DEFAULT_FINAL_REMINDER_HOURS),
            "quietHoursStart": postponement_config.get("quietHoursStart", C.DEFAULT_QUIET_HOURS_START),
            "quietHoursEnd": postponement_config.get("quietHoursEnd", C.DEFAULT_QUIET_HOURS_END),
        },
        "startedAt": None,
        "completedAt": None,
        "extendedAt": None,
        "extensionHours": None,
        "reopenedAt": None,
        "notAttendedAt": None,
        "createdAt": now,
        "updatedAt": now,
    }
    
    # Update existing or insert new
    if existing:
        db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].update_one(
            {"_id": existing["_id"]},
            {"$set": record}
        )
        record["_id"] = existing["_id"]
    else:
        result = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].insert_one(record)
        record["_id"] = result.inserted_id
    
    return record


def get_postponement_record(application_id: str) -> Optional[Dict[str, Any]]:
    """Get current postponement record for an application."""
    db = get_db()
    return db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].find_one({
        "applicationId": application_id
    })


def get_candidate_postponed_interviews(candidate_id: str) -> List[Dict[str, Any]]:
    """Get all active postponed interviews for a candidate.
    
    Returns interviews sorted by deadline (earliest first).
    """
    db = get_db()
    interviews = list(db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].find({
        "candidateId": candidate_id,
        "status": TechnicalInterviewStatus.POSTPONED
    }).sort("deadlineAt", 1))
    
    return interviews


def can_postpone(application_id: str) -> tuple[bool, str]:
    """Check if candidate can postpone interview.
    
    Returns:
        (can_postpone, reason)
    """
    record = get_postponement_record(application_id)
    if not record:
        return True, "No existing postponement record"
    
    if record.get("status") != TechnicalInterviewStatus.PENDING:
        return False, f"Interview status is {record.get('status')}, cannot postpone"
    
    if record.get("postponementNumber", 0) >= record.get("maxPostponements", 1):
        return False, "Maximum postponements reached"
    
    return True, "Can postpone"


def can_start_interview(application_id: str) -> tuple[bool, str]:
    """Check if candidate can start interview.
    
    Returns:
        (can_start, reason)
    """
    record = get_postponement_record(application_id)
    if not record:
        return True, "No postponement record, can start"
    
    status = record.get("status")
    if status == TechnicalInterviewStatus.IN_PROGRESS:
        return False, "Interview already in progress"
    
    if status == TechnicalInterviewStatus.COMPLETED:
        return False, "Interview already completed"
    
    if status == TechnicalInterviewStatus.POSTPONED:
        deadline = record.get("deadlineAt")
        if deadline and datetime.now(timezone.utc) >= deadline:
            return False, "Deadline has passed"
        return True, "Can start postponed interview"
    
    if status == TechnicalInterviewStatus.NOT_ATTENDED:
        return False, "Interview marked as not attended"
    
    if status == TechnicalInterviewStatus.REJECTED:
        return False, "Interview rejected"
    
    return True, "Can start interview"


def start_interview(application_id: str) -> Dict[str, Any]:
    """Mark interview as started and cancel reminders.
    
    Returns:
        Updated postponement record
    """
    db = get_db()
    record = get_postponement_record(application_id)
    
    if not record:
        raise HTTPException(status_code=404, detail="Postponement record not found")
    
    can_start, reason = can_start_interview(application_id)
    if not can_start:
        raise HTTPException(status_code=400, detail=reason)
    
    now = datetime.now(timezone.utc)
    
    # Update record
    updated = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].update_one(
        {"applicationId": application_id},
        {
            "$set": {
                "status": TechnicalInterviewStatus.IN_PROGRESS,
                "startedAt": now,
                "updatedAt": now
            }
        }
    )
    
    if updated.matched_count == 0:
        raise HTTPException(status_code=404, detail="Failed to update postponement record")
    
    # Cancel pending reminders
    cancel_interview_reminders(application_id)
    
    # Refresh and return record
    return get_postponement_record(application_id)


def extend_deadline(application_id: str, extension_hours: int, actor_id: str) -> Dict[str, Any]:
    """Extend interview deadline (recruiter action).
    
    Args:
        application_id: Application identifier
        extension_hours: Hours to extend
        actor_id: ID of the recruiter performing the extension
    
    Returns:
        Updated postponement record
    """
    db = get_db()
    record = get_postponement_record(application_id)
    
    if not record:
        raise HTTPException(status_code=404, detail="Postponement record not found")
    
    if record.get("status") != TechnicalInterviewStatus.POSTPONED:
        raise HTTPException(status_code=400, detail="Can only extend postponed interviews")
    
    # Get configuration to validate extension
    job_id = record.get("jobId")
    config = get_job_postponement_config(job_id)
    max_extension = config.get("extensionMaxHours", C.DEFAULT_EXTENSION_MAX_HOURS)
    
    if extension_hours > max_extension:
        raise HTTPException(
            status_code=400,
            detail=f"Extension cannot exceed {max_extension} hours"
        )
    
    old_deadline = record.get("deadlineAt")
    new_deadline = old_deadline + timedelta(hours=extension_hours)
    
    now = datetime.now(timezone.utc)
    
    # Update record
    updated = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].update_one(
        {"applicationId": application_id},
        {
            "$set": {
                "deadlineAt": new_deadline,
                "extendedAt": now,
                "extensionHours": extension_hours,
                "updatedAt": now
            }
        }
    )
    
    if updated.matched_count == 0:
        raise HTTPException(status_code=404, detail="Failed to update postponement record")
    
    # Reschedule reminders with new deadline
    reschedule_interview_reminders(application_id, new_deadline)
    
    # Write audit event
    from app.rbac.audit import write_audit
    write_audit(
        action=C.AUDIT_TECHNICAL_INTERVIEW_DEADLINE_EXTENDED,
        actor_id=actor_id,
        target={"applicationId": application_id, "jobId": job_id},
        before={"deadlineAt": old_deadline},
        after={"deadlineAt": new_deadline, "extensionHours": extension_hours},
        module=C.MODULE_RECRUITMENT,
        permission_action=C.ACTION_TECHNICAL_INTERVIEW_EXTEND
    )
    
    # Refresh and return record
    return get_postponement_record(application_id)


def reopen_interview(application_id: str, new_deadline_hours: int, actor_id: str) -> Dict[str, Any]:
    """Reopen a missed interview (recruiter action).
    
    Args:
        application_id: Application identifier
        new_deadline_hours: New deadline in hours from now
        actor_id: ID of the recruiter performing the reopening
    
    Returns:
        Updated postponement record
    """
    db = get_db()
    record = get_postponement_record(application_id)
    
    if not record:
        raise HTTPException(status_code=404, detail="Postponement record not found")
    
    if record.get("status") != TechnicalInterviewStatus.NOT_ATTENDED:
        raise HTTPException(status_code=400, detail="Can only reopen not-attended interviews")
    
    now = datetime.now(timezone.utc)
    new_deadline = now + timedelta(hours=new_deadline_hours)
    
    # Update record
    updated = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].update_one(
        {"applicationId": application_id},
        {
            "$set": {
                "status": TechnicalInterviewStatus.POSTPONED,
                "deadlineAt": new_deadline,
                "reopenedAt": now,
                "notAttendedAt": None,
                "updatedAt": now
            }
        }
    )
    
    if updated.matched_count == 0:
        raise HTTPException(status_code=404, detail="Failed to update postponement record")
    
    # Schedule new reminders
    schedule_interview_reminders(application_id, new_deadline)
    
    # Write audit event
    from app.rbac.audit import write_audit
    write_audit(
        action=C.AUDIT_TECHNICAL_INTERVIEW_REOPENED,
        actor_id=actor_id,
        target={"applicationId": application_id, "jobId": record.get("jobId")},
        before={"status": TechnicalInterviewStatus.NOT_ATTENDED},
        after={"status": TechnicalInterviewStatus.POSTPONED, "deadlineAt": new_deadline},
        module=C.MODULE_RECRUITMENT,
        permission_action=C.ACTION_TECHNICAL_INTERVIEW_REOPEN
    )
    
    # Refresh and return record
    return get_postponement_record(application_id)


def mark_not_attended(application_id: str) -> Dict[str, Any]:
    """Mark interview as not attended (deadline expiration).
    
    Returns:
        Updated postponement record
    """
    db = get_db()
    record = get_postponement_record(application_id)
    
    if not record:
        raise HTTPException(status_code=404, detail="Postponement record not found")
    
    now = datetime.now(timezone.utc)
    
    # Update record
    updated = db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].update_one(
        {"applicationId": application_id},
        {
            "$set": {
                "status": TechnicalInterviewStatus.NOT_ATTENDED,
                "notAttendedAt": now,
                "updatedAt": now
            }
        }
    )
    
    if updated.matched_count == 0:
        raise HTTPException(status_code=404, detail="Failed to update postponement record")
    
    # Cancel remaining reminders
    cancel_interview_reminders(application_id)
    
    # Check if auto-rejection is enabled
    job_id = record.get("jobId")
    config = get_job_postponement_config(job_id)
    if config.get("automaticRejectionAfterNotAttended"):
        auto_reject_candidate(application_id, job_id)
    
    # Refresh and return record
    return get_postponement_record(application_id)


def auto_reject_candidate(application_id: str, job_id: str) -> None:
    """Automatically reject candidate after not-attended if configured."""
    # This would integrate with your existing candidate rejection logic
    # For now, we'll just write an audit event
    from app.rbac.audit import write_audit
    
    write_audit(
        action=C.AUDIT_TECHNICAL_INTERVIEW_AUTO_REJECTED,
        target={"applicationId": application_id, "jobId": job_id},
        module=C.MODULE_RECRUITMENT,
        permission_action=C.ACTION_TECHNICAL_INTERVIEW_REJECT
    )


def schedule_interview_reminders(application_id: str, deadline: datetime) -> List[str]:
    """Schedule reminder jobs for an interview.
    
    Returns:
        List of scheduled reminder job IDs
    """
    db = get_db()
    record = get_postponement_record(application_id)
    
    if not record:
        return []
    
    reminder_config = record.get("reminderConfigurationSnapshot", {})
    interval_hours = reminder_config.get("intervalHours", C.DEFAULT_REMINDER_INTERVAL_HOURS)
    final_reminder_hours = reminder_config.get("finalReminderHours", C.DEFAULT_FINAL_REMINDER_HOURS)
    quiet_hours_start = reminder_config.get("quietHoursStart", C.DEFAULT_QUIET_HOURS_START)
    quiet_hours_end = reminder_config.get("quietHoursEnd", C.DEFAULT_QUIET_HOURS_END)
    
    now = datetime.now(timezone.utc)
    reminder_times = []
    
    # Calculate regular reminder times
    current_time = now + timedelta(hours=interval_hours)
    while current_time < deadline - timedelta(hours=final_reminder_hours):
        # Adjust for quiet hours
        adjusted_time = adjust_for_quiet_hours(current_time, quiet_hours_start, quiet_hours_end)
        
        # Don't schedule if adjusted time would be past deadline
        if adjusted_time < deadline:
            reminder_times.append(adjusted_time)
        
        current_time += timedelta(hours=interval_hours)
    
    # Add final reminder
    final_reminder_time = deadline - timedelta(hours=final_reminder_hours)
    reminder_times.append(final_reminder_time)
    
    # Create reminder jobs
    job_ids = []
    for reminder_time in reminder_times:
        if reminder_time > now:  # Only schedule future reminders
            reminder_job = {
                "applicationId": application_id,
                "candidateId": record.get("candidateId"),
                "jobId": record.get("jobId"),
                "scheduledAt": reminder_time,
                "reminderType": "final" if reminder_time == final_reminder_time else "regular",
                "status": "pending",
                "createdAt": now,
            }
            result = db[C.COL_INTERVIEW_REMINDERS].insert_one(reminder_job)
            job_ids.append(str(result.inserted_id))
    
    return job_ids


def adjust_for_quiet_hours(scheduled_time: datetime, quiet_start: int, quiet_end: int) -> datetime:
    """Adjust scheduled time to avoid quiet hours.
    
    Args:
        scheduled_time: Originally scheduled time
        quiet_start: Quiet hours start (hour in 24h format)
        quiet_end: Quiet hours end (hour in 24h format)
    
    Returns:
        Adjusted time that falls outside quiet hours
    """
    hour = scheduled_time.hour
    
    if quiet_end < quiet_start:
        # Quiet hours span midnight (e.g., 22:00 to 08:00)
        if hour >= quiet_start or hour < quiet_end:
            # Move to end of quiet hours
            if hour >= quiet_start:
                return scheduled_time.replace(day=scheduled_time.day + 1, hour=quiet_end, minute=0)
            else:
                return scheduled_time.replace(hour=quiet_end, minute=0)
    else:
        # Normal quiet hours (e.g., 01:00 to 06:00)
        if quiet_start <= hour < quiet_end:
            return scheduled_time.replace(hour=quiet_end, minute=0)
    
    return scheduled_time


def reschedule_interview_reminders(application_id: str, new_deadline: datetime) -> List[str]:
    """Reschedule reminders after deadline extension.
    
    Cancels existing reminders and creates new schedule.
    """
    cancel_interview_reminders(application_id)
    return schedule_interview_reminders(application_id, new_deadline)


def cancel_interview_reminders(application_id: str) -> int:
    """Cancel all pending reminders for an interview.
    
    Returns:
        Number of reminders cancelled
    """
    db = get_db()
    result = db[C.COL_INTERVIEW_REMINDERS].update_many(
        {
            "applicationId": application_id,
            "status": "pending"
        },
        {
            "$set": {
                "status": "cancelled",
                "cancelledAt": datetime.now(timezone.utc)
            }
        }
    )
    return result.modified_count


def get_interview_state(application_id: str) -> Dict[str, Any]:
    """Get complete interview state for API response.
    
    Returns:
        Interview state with all relevant information
    """
    db = get_db()
    record = get_postponement_record(application_id)
    
    if not record:
        # No postponement record exists, return default pending state
        return {
            "status": TechnicalInterviewStatus.PENDING,
            "canPostpone": True,
            "canStart": True,
        }
    
    status = record.get("status")
    deadline = record.get("deadlineAt")
    timezone_str = record.get("candidateTimezoneAtPostponement", "UTC")
    
    can_postpone, _ = can_postpone(application_id)
    can_start, _ = can_start_interview(application_id)
    
    response = {
        "status": status,
        "canPostpone": can_postpone,
        "canStart": can_start,
        "postponementNumber": record.get("postponementNumber"),
        "maxPostponements": record.get("maxPostponements"),
    }
    
    if deadline:
        response["deadlineAt"] = deadline.isoformat()
        response["deadlineLocal"] = format_deadline_local(deadline, timezone_str)
        response["timezone"] = timezone_str
    
    return response