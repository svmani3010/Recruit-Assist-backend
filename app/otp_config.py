"""Legacy SMS OTP shim — candidate verification uses WhatsApp (KV03).

HR KYC may still import this module. Prefer WhatsApp for candidate OTPs.
"""

from __future__ import annotations

import logging

from app.services.candidate_whatsapp_otp import (
    _mask_phone,
    send_whatsapp_otp,
)

logger = logging.getLogger(__name__)

SMS_PROVIDER = "whatsapp"  # SMS channel disabled; WhatsApp only


def send_sms_otp(phone_number: str, otp: str) -> bool:
    """Backward-compatible entry point — routes to WhatsApp OTP delivery."""
    result = send_whatsapp_otp(phone_number, otp)
    if not result.success:
        logger.error(
            "OTP delivery failed phone=%s provider=%s code=%s",
            _mask_phone(phone_number),
            result.provider,
            result.error_code,
        )
    return result.success
