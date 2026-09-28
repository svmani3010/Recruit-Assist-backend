"""Email configuration from centralized app settings (no hardcoded secrets)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import List

from app.settings import get_settings


@dataclass(frozen=True)
class EmailConfig:
    # Mailgun (active email provider)
    mailgun_api_key: str
    mailgun_domain: str
    mailgun_base_url: str
    mailgun_from_email: str

    # Common email settings
    hr_emails: List[str]
    sender_email: str
    sender_name: str
    email_enabled: bool
    default_meeting_link: str
    failure_alert_threshold: int = 3

    # ------------------------------------------------------------------
    # Backward-compat aliases used by older call sites (email_api, etc.)
    # SMTP settings have been commented out — Mailgun is the active provider.
    # ------------------------------------------------------------------
    # @property
    # def smtp_mode(self) -> bool: ...
    # @property
    # def smtp_server(self) -> str: ...
    # @property
    # def smtp_port(self) -> int: ...
    # @property
    # def smtp_username(self) -> str: ...
    # @property
    # def smtp_password(self) -> str: ...

    @property
    def HR_EMAILS(self) -> List[str]:
        return list(self.hr_emails)

    @property
    def SENDER_EMAIL(self) -> str:
        return self.sender_email

    @property
    def SENDER_NAME(self) -> str:
        return self.sender_name

    @property
    def EMAIL_ENABLED(self) -> bool:
        return self.email_enabled

    @property
    def DEFAULT_MEETING_LINK(self) -> str:
        return self.default_meeting_link

    def __repr__(self) -> str:
        return (
            f"EmailConfig(enabled={self.email_enabled}, provider=mailgun, "
            f"domain={self.mailgun_domain!r}, sender={self.sender_email!r}, "
            f"hr_recipients={len(self.hr_emails)})"
        )


@lru_cache
def get_email_config() -> EmailConfig:
    s = get_settings()
    return EmailConfig(
        mailgun_api_key=s.mailgun_api_key,
        mailgun_domain=s.mailgun_domain,
        mailgun_base_url=s.mailgun_base_url,
        mailgun_from_email=s.mailgun_from_email,
        hr_emails=s.hr_email_list,
        sender_email=s.sender_email,
        sender_name=s.sender_name,
        email_enabled=s.email_enabled,
        default_meeting_link=s.default_meeting_link,
    )
