"""Multi-provider email delivery with health tracking (Mailgun)."""

from __future__ import annotations

import base64
import logging
import re
import threading
from collections import deque
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests

from app.email.config import EmailConfig, get_email_config
from app.email.results import DeliveryResult
from app.email.templates import EmailTemplates

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_recipients(recipients: Optional[List[str]]) -> List[str]:
    if not recipients:
        return []
    cleaned: List[str] = []
    for raw in recipients:
        if not raw:
            continue
        email = str(raw).strip()
        if _EMAIL_RE.match(email):
            cleaned.append(email)
        else:
            logger.warning("Skipping invalid email recipient: %r", raw)
    # de-dupe preserving order
    seen = set()
    unique = []
    for email in cleaned:
        key = email.lower()
        if key not in seen:
            seen.add(key)
            unique.append(email)
    return unique


class EmailService:
    """Send notifications via Mailgun HTTP API."""

    def __init__(self, config: Optional[EmailConfig] = None):
        self.config = config or get_email_config()
        self._templates = EmailTemplates()
        self._lock = threading.RLock()
        self._consecutive_failures = 0
        self._recent: Deque[DeliveryResult] = deque(maxlen=50)
        self._last_success_at: Optional[str] = None
        self._last_failure_at: Optional[str] = None
        self._last_error: Optional[str] = None

    def health(self) -> dict:
        with self._lock:
            return {
                "email_enabled": self.config.email_enabled,
                "provider": "mailgun",
                "hr_recipient_count": len(self.config.hr_emails),
                "sender_configured": bool(self.config.sender_email),
                "consecutive_failures": self._consecutive_failures,
                "failure_alert_threshold": self.config.failure_alert_threshold,
                "unhealthy": self._consecutive_failures
                >= self.config.failure_alert_threshold,
                "last_success_at": self._last_success_at,
                "last_failure_at": self._last_failure_at,
                "last_error": self._last_error,
                "recent": [r.to_dict() for r in list(self._recent)[-10:]],
            }

    def _record(self, result: DeliveryResult) -> DeliveryResult:
        with self._lock:
            self._recent.append(result)
            if result.skipped:
                return result
            if result.success:
                self._consecutive_failures = 0
                self._last_success_at = result.timestamp.isoformat()
                self._last_error = None
            else:
                self._consecutive_failures += 1
                self._last_failure_at = result.timestamp.isoformat()
                self._last_error = result.error
                if self._consecutive_failures >= self.config.failure_alert_threshold:
                    logger.critical(
                        "EMAIL DELIVERY UNHEALTHY: %s consecutive failures. "
                        "Last error: %s | subject=%r provider=%s",
                        self._consecutive_failures,
                        result.error,
                        result.subject,
                        result.provider,
                    )
        return result

    def send_email(
        self,
        subject: str,
        body_html: str,
        body_text: str = None,
        recipients: List[str] = None,
        cc: List[str] = None,
        calendar_ics: str = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Backward-compatible bool API used by routers."""
        result = self.send_email_detailed(
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            recipients=recipients,
            cc=cc,
            calendar_ics=calendar_ics,
            attachments=attachments,
        )
        return bool(result.success and not result.skipped)

    def send_email_detailed(
        self,
        subject: str,
        body_html: str,
        body_text: str = None,
        recipients: List[str] = None,
        cc: List[str] = None,
        calendar_ics: str = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> DeliveryResult:
        if not self.config.email_enabled:
            result = DeliveryResult(
                success=False,
                provider="disabled",
                subject=subject,
                recipients=_normalize_recipients(recipients),
                skipped=True,
                error="Email delivery is disabled (EMAIL_ENABLED=false)",
            )
            logger.info("Email disabled; skipped send: %s", subject)
            return self._record(result)

        to_list = _normalize_recipients(
            recipients if recipients is not None else self.config.hr_emails
        )
        cc_list = _normalize_recipients(cc)
        if not to_list:
            result = DeliveryResult(
                success=False,
                provider="none",
                subject=subject,
                recipients=[],
                error="No valid recipients",
            )
            logger.error("Email aborted — no valid recipients for %r", subject)
            return self._record(result)

        if not self.config.sender_email:
            result = DeliveryResult(
                success=False,
                provider="none",
                subject=subject,
                recipients=to_list,
                error="SENDER_EMAIL not configured",
            )
            return self._record(result)

        # Use Mailgun as the sole delivery provider
        return self._record(
            self._send_via_mailgun(
                subject,
                body_html,
                body_text,
                to_list,
                cc_list,
                calendar_ics,
                attachments,
            )
        )

    # ------------------------------------------------------------------
    # SMTP send path has been removed — Mailgun is now the active provider.
    # Original SMTP code is preserved below as comments for reference.
    # ------------------------------------------------------------------
    # def _send_via_smtp(self, subject, body_html, body_text, recipients,
    #                    cc, calendar_ics, attachments=None) -> DeliveryResult:
    #     import smtplib
    #     try:
    #         if not self.config.smtp_username or not self.config.smtp_password:
    #             return DeliveryResult(success=False, provider="smtp", ...)
    #         msg = self._build_mime(...)
    #         all_recipients = list(recipients) + list(cc or [])
    #         with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port,
    #                           timeout=30) as server:
    #             server.starttls()
    #             server.login(self.config.smtp_username, self.config.smtp_password)
    #             server.sendmail(self.config.sender_email, all_recipients, msg.as_string())
    #         logger.info("Email sent via SMTP: %s", subject)
    #         return DeliveryResult(success=True, provider="smtp", ...)
    #     except Exception as e:
    #         logger.error("SMTP send failed for %r: %s", subject, e)
    #         return DeliveryResult(success=False, provider="smtp", error=str(e), ...)

    def _send_via_mailgun(
        self,
        subject: str,
        body_html: str,
        body_text: Optional[str],
        recipients: List[str],
        cc: Optional[List[str]],
        calendar_ics: Optional[str],
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> DeliveryResult:
        """Send email via the Mailgun Messages API."""
        try:
            if not self.config.mailgun_api_key or not self.config.mailgun_domain:
                return DeliveryResult(
                    success=False,
                    provider="mailgun",
                    subject=subject,
                    recipients=recipients,
                    error="Mailgun credentials not configured (MAILGUN_API_KEY / MAILGUN_DOMAIN)",
                )

            from_addr = (
                f"{self.config.sender_name} <{self.config.mailgun_from_email or self.config.sender_email}>"
            )

            data: Dict[str, Any] = {
                "from": from_addr,
                "to": recipients,
                "subject": subject,
                "html": body_html,
            }
            if body_text:
                data["text"] = body_text
            if cc:
                data["cc"] = cc

            files: List[Tuple[str, Any]] = []

            # Attach calendar ICS if provided
            if calendar_ics:
                files.append(
                    (
                        "attachment",
                        ("invite.ics", calendar_ics.encode("utf-8"), "text/calendar"),
                    )
                )

            # Attach any extra files
            for item in attachments or []:
                if not item:
                    continue
                filename = str(item.get("filename") or "attachment")
                content = item.get("content")
                mime_type = str(item.get("mime_type") or "application/octet-stream")
                if not content:
                    continue
                if isinstance(content, str):
                    payload = content.encode("utf-8")
                else:
                    payload = bytes(content)
                files.append(("attachment", (filename, payload, mime_type)))

            url = f"{self.config.mailgun_base_url}/v3/{self.config.mailgun_domain}/messages"
            response = requests.post(
                url,
                auth=("api", self.config.mailgun_api_key),
                data=data,
                files=files or None,
                timeout=30,
            )
            response.raise_for_status()
            logger.info("Email sent via Mailgun: %s", subject)
            return DeliveryResult(
                success=True,
                provider="mailgun",
                subject=subject,
                recipients=recipients,
            )
        except Exception as e:
            logger.error("Mailgun send failed for %r: %s", subject, e)
            return DeliveryResult(
                success=False,
                provider="mailgun",
                subject=subject,
                recipients=recipients,
                error=str(e),
            )

    def _build_mime(
        self,
        subject: str,
        body_html: str,
        body_text: Optional[str],
        recipients: List[str],
        cc: Optional[List[str]],
        calendar_ics: Optional[str],
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> MIMEMultipart:
        """Build MIME message (kept for potential future use)."""
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = f"{self.config.sender_name} <{self.config.sender_email}>"
        msg["To"] = ", ".join(recipients)
        if cc:
            msg["Cc"] = ", ".join(cc)

        alt = MIMEMultipart("alternative")
        if body_text:
            alt.attach(MIMEText(body_text, "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alt)

        if calendar_ics:
            part = MIMEBase("text", "calendar", method="REQUEST", name="invite.ics")
            part.set_payload(calendar_ics.encode("utf-8"))
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", 'attachment; filename="invite.ics"')
            part.add_header("Content-Class", "urn:content-classes:calendarmessage")
            msg.attach(part)
        self._attach_files(msg, attachments)
        return msg

    def _attach_files(
        self,
        msg: MIMEMultipart,
        attachments: Optional[List[Dict[str, Any]]],
    ) -> None:
        for item in attachments or []:
            if not item:
                continue
            filename = str(item.get("filename") or "attachment")
            content = item.get("content")
            mime_type = str(item.get("mime_type") or "application/octet-stream")
            if not content:
                continue
            if isinstance(content, str):
                payload = content.encode("utf-8")
            else:
                payload = bytes(content)
            maintype, _, subtype = mime_type.partition("/")
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(payload)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)

    def send_notification(
        self,
        action_type: str,
        data: dict,
        recipients: List[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        try:
            subject, body_html, body_text = self._templates.get_template(action_type, data)
        except Exception:
            logger.exception("Email template failed for action=%s", action_type)
            return False
        return self.send_email(
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            recipients=recipients,
            attachments=attachments,
        )


_email_service: Optional[EmailService] = None
_service_lock = threading.Lock()


def get_email_service() -> EmailService:
    global _email_service
    with _service_lock:
        if _email_service is None:
            _email_service = EmailService()
        return _email_service


# Lazy module-level proxy for backward compatibility (`from email_utils import email_service`)
class _EmailServiceProxy:
    def __getattr__(self, name):
        return getattr(get_email_service(), name)

    @property
    def config(self):
        return get_email_service().config


email_service = _EmailServiceProxy()


def notify_hr(
    action_type: str,
    data: dict,
    recipients: List[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    payload = dict(data or {})
    payload["is_candidate"] = False
    return get_email_service().send_notification(
        action_type,
        payload,
        recipients=recipients,
        attachments=attachments,
    )


def notify_candidate(
    action_type: str,
    data: dict,
    candidate_email: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    if not candidate_email:
        logger.warning("No candidate email provided; skipping candidate notification")
        return False
    payload = dict(data or {})
    payload["is_candidate"] = True
    payload["candidate_email"] = candidate_email
    payload.pop("l2_pdf_attached", None)
    payload.pop("is_fyi", None)
    return get_email_service().send_notification(
        action_type,
        payload,
        recipients=[candidate_email],
        attachments=attachments,
    )
