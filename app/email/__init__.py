"""Email / notification subsystem."""

from app.email.config import EmailConfig, get_email_config
from app.email.results import DeliveryResult
from app.email.templates import EmailTemplates

__all__ = [
    "EmailConfig",
    "EmailService",
    "EmailTemplates",
    "DeliveryResult",
    "email_service",
    "get_email_config",
    "get_email_service",
    "notify_hr",
    "notify_candidate",
]


def __getattr__(name: str):
    if name in {
        "EmailService",
        "email_service",
        "get_email_service",
        "notify_hr",
        "notify_candidate",
    }:
        from app.email import service as _service

        mapping = {
            "EmailService": _service.EmailService,
            "email_service": _service.email_service,
            "get_email_service": _service.get_email_service,
            "notify_hr": _service.notify_hr,
            "notify_candidate": _service.notify_candidate,
        }
        return mapping[name]
    raise AttributeError(f"module 'app.email' has no attribute {name!r}")
