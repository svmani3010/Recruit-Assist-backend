"""Structured email delivery outcomes for observability."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class DeliveryResult:
    success: bool
    provider: str
    subject: str
    recipients: List[str]
    message_id: Optional[str] = None
    error: Optional[str] = None
    skipped: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "provider": self.provider,
            "subject": self.subject,
            "recipients_count": len(self.recipients),
            "message_id": self.message_id,
            "error": self.error,
            "skipped": self.skipped,
            "timestamp": self.timestamp.isoformat(),
        }
