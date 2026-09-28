"""Service for handling technical interview toggle logic."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.database import get_db
from app.rbac import constants as C

logger = logging.getLogger(__name__)


def count_candidates_affected_by_toggle(
    job_id: str, 
    current_enabled: bool, 
    new_enabled: bool
) -> Dict[str, int]:
    """Count candidates affected by technical interview toggle change.
    
    Returns dict with counts for different candidate states.
    """
    db = get_db()
    
    # Off → On: candidates who passed resume screening without technical stage
    if not current_enabled and new_enabled:
        count = db["candidateInformation"].count_documents({
            "job_id": job_id,
            "application_status": {"$in": ["applied", "in_progress"]},
            # Candidates who passed L0 but don't have technical interview stage
            "l0_passed": True,
            "l1_status": {"$in": [None, "not_started", "skipped"]}
        })
        return {"candidates_without_technical": count}
    
    # On → Off: candidates who started or completed technical interview
    if current_enabled and not new_enabled:
        in_progress = db["candidateInformation"].count_documents({
            "job_id": job_id,
            "l1_status": "in_progress"
        })
        completed = db["candidateInformation"].count_documents({
            "job_id": job_id,
            "l1_status": "completed"
        })
        return {"candidates_in_progress": in_progress, "candidates_completed": completed}
    
    return {"candidates_affected": 0}


def rebalance_weightage_with_technical(
    current_weightage: Dict[str, int],
    technical_weightage: int,
    technical_enabled: bool
) -> Dict[str, int]:
    """Rebalance weightage when technical interview toggle changes.
    
    When technical is disabled, redistribute its weightage proportionally.
    When technical is enabled, reduce other components proportionally.
    """
    if not current_weightage:
        current_weightage = {}
    
    # Default sections if not present
    default_sections = {
        "education": 20,
        "experience": 30,
        "skills": 30,
        "certifications": 10,
        "other": 10
    }
    
    # Use current weightage or defaults
    weightage = {**default_sections, **current_weightage}
    
    if technical_enabled:
        # Enable technical: reduce other components proportionally
        total_current = sum(weightage.values())
        if total_current == 0:
            return weightage
        
        # Calculate reduction factor for existing components
        reduction_factor = (total_current - technical_weightage) / total_current
        
        rebalanced = {}
        for key, value in weightage.items():
            rebalanced[key] = max(0, int(value * reduction_factor))
        
        # Add technical component
        rebalanced["technical"] = technical_weightage
        
        # Ensure sum is exactly 100
        total_rebalanced = sum(rebalanced.values())
        if total_rebalanced != 100:
            # Adjust the largest component to fix rounding
            largest_key = max(rebalanced, key=rebalanced.get)
            rebalanced[largest_key] += (100 - total_rebalanced)
        
        return rebalanced
    else:
        # Disable technical: redistribute to other components
        if "technical" not in weightage:
            return weightage
        
        technical_value = weightage.pop("technical")
        remaining_total = sum(weightage.values())
        
        if remaining_total == 0:
            # Distribute equally among non-technical sections
            non_technical_keys = [k for k in weightage.keys() if k != "technical"]
            if non_technical_keys:
                per_section = technical_value // len(non_technical_keys)
                for key in non_technical_keys:
                    weightage[key] = per_section
            return weightage
        
        # Redistribute proportionally
        rebalanced = {}
        for key, value in weightage.items():
            if key != "technical":
                rebalanced[key] = int(value + (value / remaining_total) * technical_value)
        
        # Ensure sum is exactly 100
        total_rebalanced = sum(rebalanced.values())
        if total_rebalanced != 100:
            largest_key = max(rebalanced, key=rebalanced.get)
            rebalanced[largest_key] += (100 - total_rebalanced)
        
        return rebalanced


def create_technical_interview_stages(
    job_id: str,
    candidate_ids: List[str]
) -> int:
    """Create technical interview stage records for candidates when toggle goes Off→On.
    
    Returns count of candidates processed.
    """
    db = get_db()
    processed = 0
    
    for candidate_id in candidate_ids:
        try:
            # Update candidate to include technical interview stage
            db["candidateInformation"].update_one(
                {
                    "candidate_id": candidate_id,
                    "job_id": job_id
                },
                {
                    "$set": {
                        "l1_status": "not_started",
                        "l1_invited_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            processed += 1
        except Exception as e:
            logger.error(f"Failed to create technical stage for {candidate_id}: {e}")
    
    return processed


def advance_candidates_without_technical(
    job_id: str,
    candidate_ids: List[str]
) -> int:
    """Advance candidates whose technical interview was in progress when toggle goes On→Off.
    
    Returns count of candidates processed.
    """
    db = get_db()
    processed = 0
    
    for candidate_id in candidate_ids:
        try:
            # Get current candidate status
            candidate = db["candidateInformation"].find_one({
                "candidate_id": candidate_id,
                "job_id": job_id
            })
            
            if not candidate:
                continue
            
            # Mark technical as skipped/Cancelled and advance to next stage
            db["candidateInformation"].update_one(
                {
                    "candidate_id": candidate_id,
                    "job_id": job_id
                },
                {
                    "$set": {
                        "l1_status": "cancelled",
                        "l1_cancelled_at": datetime.now(timezone.utc),
                        "l1_cancel_reason": "technical_interview_disabled",
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            
            # TODO: Advance to next configured stage based on job's workflow
            # This would need to be implemented based on the job's stage configuration
            
            processed += 1
        except Exception as e:
            logger.error(f"Failed to advance candidate {candidate_id}: {e}")
    
    return processed


def cancel_pending_technical_reminders(job_id: str) -> int:
    """Cancel any scheduled/pending technical interview reminders for a job.
    
    Returns count of reminders cancelled.
    """
    db = get_db()
    
    # Cancel reminders from the new postponement system
    result = db[C.COL_INTERVIEW_REMINDERS].update_many(
        {
            "jobId": job_id,
            "status": "pending"
        },
        {
            "$set": {
                "status": "cancelled",
                "cancelledAt": datetime.now(timezone.utc),
                "cancelReason": "technical_interview_disabled"
            }
        }
    )
    
    # Also clean up any postponement records for this job
    db[C.COL_TECHNICAL_INTERVIEW_POSTPONEMENTS].update_many(
        {
            "jobId": job_id,
            "status": "POSTPONED"
        },
        {
            "$set": {
                "status": "CANCELLED_JOB_CLOSED",
                "updatedAt": datetime.now(timezone.utc)
            }
        }
    )
    
    return result.modified_count


def validate_technical_interview_config(
    technical_enabled: bool,
    technical_questions: Optional[List[str]],
    difficulty_level: Optional[str]
) -> List[str]:
    """Validate technical interview configuration.
    
    Returns list of validation error messages (empty if valid).
    """
    errors = []
    
    if technical_enabled:
        if not technical_questions or len(technical_questions) == 0:
            errors.append("Technical questions are required when technical interview is enabled")
        
        if not difficulty_level:
            errors.append("Difficulty level is required when technical interview is enabled")
    
    return errors


def get_technical_interview_config(job_id: str) -> Dict[str, Any]:
    """Get current technical interview configuration for a job."""
    db = get_db()
    job = db["jobview"].find_one({"jobid": job_id})
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return {
        "technicalInterviewEnabled": job.get("technicalInterviewEnabled", False),
        "technicalQuestions": job.get("technicalQuestions", []),
        "technicalWeightage": job.get("technicalWeightage", 0),
        "difficulty_level": job.get("difficulty_level", "Intermediate")
    }


def write_technical_toggle_analytics(
    job_id: str,
    old_value: bool,
    new_value: bool,
    actor_id: str,
    org_id: Optional[str],
    candidates_affected: Optional[int] = None,
    option_chosen: Optional[str] = None
) -> None:
    """Write analytics event for technical interview toggle change."""
    db = get_db()
    
    event = {
        "event_type": "technical_toggle_changed",
        "job_id": job_id,
        "old_value": old_value,
        "new_value": new_value,
        "actor_id": actor_id,
        "org_id": org_id,
        "candidates_affected": candidates_affected,
        "option_chosen": option_chosen,
        "timestamp": datetime.now(timezone.utc)
    }
    
    db[C.COL_ANALYTICS_EVENTS].insert_one(event)


def write_technical_toggle_audit(
    job_id: str,
    old_value: bool,
    new_value: bool,
    actor_id: str,
    org_id: Optional[str],
    candidates_affected: Optional[int] = None,
    option_chosen: Optional[str] = None
) -> None:
    """Write audit log entry for technical interview toggle change."""
    from app.rbac.audit import write_audit
    
    write_audit(
        action=C.AUDIT_TECHNICAL_INTERVIEW_TOGGLE,
        actor_id=actor_id,
        org_id=org_id,
        target={"job_id": job_id},
        before={"technicalInterviewEnabled": old_value},
        after={
            "technicalInterviewEnabled": new_value,
            "candidates_affected": candidates_affected,
            "option_chosen": option_chosen
        },
        module=C.MODULE_RECRUITMENT,
        permission_action=C.ACTION_JOBS_CONFIG_TECHNICAL_INTERVIEW
    )