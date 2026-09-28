"""SMS notifications via DLT templates (AC2). Console provider for local/dev."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class SmsSendResult:
    success: bool
    provider: str
    provider_message_id: str = ""
    error: str = ""


def _mask(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return "****"
    return f"****{digits[-4:]}"


def trim_sms_body(body: str, link: str = "", limit: int = 160) -> str:
    """AC2 — stay within 160 when possible; else body + short link."""
    text = (body or "").strip()
    link = (link or "").strip()
    if link:
        combined = f"{text} {link}".strip()
        if len(combined) <= limit:
            return combined
        # Prefer link for long content
        prefix_budget = max(0, limit - len(link) - 1)
        if prefix_budget < 20:
            return link[:limit]
        return f"{text[:prefix_budget].rstrip()}… {link}".strip()
    return text[:limit]


def send_sms(
    *,
    phone_e164: str,
    body: str,
    template_id: str,
    link: str = "",
) -> SmsSendResult:
    settings = get_settings()
    provider = (getattr(settings, "sms_provider", None) or "console").strip().lower()
    message = trim_sms_body(body, link=link)
    to = "".join(ch for ch in phone_e164 if ch.isdigit())

    if provider in {"", "console", "none"}:
        logger.info(
            "SMS console dispatch to %s template=%s len=%s",
            _mask(phone_e164),
            template_id,
            len(message),
        )
        return SmsSendResult(
            success=True,
            provider="console",
            provider_message_id=f"console-{template_id}-{to[-4:] if to else 'x'}",
        )

    # Generic HTTP SMS gateway (MSG91-style optional)
    api_url = (getattr(settings, "sms_api_url", None) or "").strip()
    api_key = (getattr(settings, "sms_api_key", None) or "").strip()
    sender = (getattr(settings, "sms_sender_id", None) or "KOVETD").strip()
    if not api_url or not api_key:
        return SmsSendResult(
            success=False, provider=provider, error="sms_config_missing"
        )

    payload: dict[str, Any] = {
        "to": to,
        "sender": sender,
        "template_id": template_id,
        "message": message,
        "dlt_template_id": template_id,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=12.0) as client:
            response = client.post(api_url, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.error(
                "SMS send failed phone=%s status=%s",
                _mask(phone_e164),
                response.status_code,
            )
            return SmsSendResult(
                success=False,
                provider=provider,
                error=f"http_{response.status_code}",
            )
        data = {}
        try:
            data = response.json()
        except Exception:
            data = {}
        mid = str(
            data.get("messageId")
            or data.get("message_id")
            or data.get("request_id")
            or ""
        )
        return SmsSendResult(
            success=True, provider=provider, provider_message_id=mid
        )
    except Exception as exc:
        logger.error(
            "SMS exception phone=%s type=%s", _mask(phone_e164), type(exc).__name__
        )
        return SmsSendResult(
            success=False, provider=provider, error=type(exc).__name__
        )
