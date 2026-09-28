"""
Backward-compatible email utilities.

Canonical implementation: app.email
"""

from app.email.config import EmailConfig, get_email_config
from app.email.results import DeliveryResult
from app.email.service import (
    EmailService,
    email_service,
    get_email_service,
    notify_candidate,
    notify_hr,
)

__all__ = [
    "EmailConfig",
    "EmailService",
    "DeliveryResult",
    "email_service",
    "get_email_config",
    "get_email_service",
    "notify_hr",
    "notify_candidate",
]
