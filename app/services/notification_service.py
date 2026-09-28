"""Facade for the centralized Notification Engine (KV42)."""

from typing import Any, Optional
from datetime import datetime
from app.services.candidate_comms import notify_candidate_milestone


class NotificationService:
    """
    Centralized Notification Dispatcher for Candidate Milestones (KV42).
    
    This service resolves channels based on Candidate preferences (WhatsApp Opt-In),
    handles priority-based quiet-hour bypassing (URGENT), and schedules fallbacks.
    """
    
    @classmethod
    def notify(
        cls,
        candidate_id: str,
        event_type: str,
        payload: dict,
        booking_token: str = "",
        interview_starts_at: Optional[datetime] = None,
        force_email_action: str = ""
    ) -> dict[str, Any]:
        """
        Primary interface for dispatching notifications across WhatsApp -> SMS -> Email.
        """
        return notify_candidate_milestone(
            event=event_type,
            candidate_id=candidate_id,
            data=payload,
            booking_token=booking_token,
            interview_starts_at=interview_starts_at,
            force_email_action=force_email_action
        )
