"""WhatsApp milestone messages (reuse Meta Cloud API; not OTP)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class WhatsAppNotifyResult:
    success: bool
    provider: str
    provider_message_id: str = ""
    error: str = ""


def _mask(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return "****"
    return f"****{digits[-4:]}"


def send_whatsapp_text(*, phone_e164: str, body: str, template_name: str = "") -> WhatsAppNotifyResult:
    settings = get_settings()
    provider = (getattr(settings, "whatsapp_otp_provider", None) or "console").strip().lower()
    to = "".join(ch for ch in phone_e164 if ch.isdigit())
    text = (body or "").strip()

    if provider in {"", "console", "none"}:
        logger.info(
            "WhatsApp notify console to %s template=%s",
            _mask(phone_e164),
            template_name or "text",
        )
        return WhatsAppNotifyResult(
            success=True,
            provider="console",
            provider_message_id=f"wa-console-{to[-4:] if to else 'x'}",
        )

    token = (getattr(settings, "whatsapp_access_token", None) or "").strip()
    phone_number_id = (getattr(settings, "whatsapp_phone_number_id", None) or "").strip()
    api_version = (getattr(settings, "whatsapp_api_version", None) or "v21.0").strip()
    language = (getattr(settings, "whatsapp_template_language", None) or "en").strip()
    if not token or not phone_number_id:
        return WhatsAppNotifyResult(
            success=False, provider="meta", error="config_missing"
        )

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Prefer named template when configured mapping exists; else free-form text
    template_map_raw = getattr(settings, "whatsapp_notify_templates", None) or ""
    mapped = ""
    for part in str(template_map_raw).split(","):
        if "=" in part:
            key, val = part.split("=", 1)
            if key.strip() == template_name:
                mapped = val.strip()
                break

    if mapped:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": mapped,
                "language": {"code": language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": text[:500]}],
                    }
                ],
            },
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"preview_url": True, "body": text[:1000]},
        }

    try:
        with httpx.Client(timeout=12.0) as client:
            response = client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.error(
                "WhatsApp notify failed phone=%s status=%s",
                _mask(phone_e164),
                response.status_code,
            )
            return WhatsAppNotifyResult(
                success=False, provider="meta", error=f"http_{response.status_code}"
            )
        data = {}
        try:
            data = response.json()
        except Exception:
            data = {}
        messages = data.get("messages") if isinstance(data, dict) else None
        mid = ""
        if isinstance(messages, list) and messages:
            mid = str(messages[0].get("id") or "")
        return WhatsAppNotifyResult(
            success=True, provider="meta", provider_message_id=mid
        )
    except Exception as exc:
        logger.error(
            "WhatsApp notify exception phone=%s type=%s",
            _mask(phone_e164),
            type(exc).__name__,
        )
        return WhatsAppNotifyResult(
            success=False, provider="meta", error=type(exc).__name__
        )
