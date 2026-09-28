"""KV03 — Candidate mobile OTP via WhatsApp (no SMS).

OTP is generated with secrets, stored hashed (bcrypt), sent over WhatsApp,
rate-limited by phone + IP + account, and never logged in plaintext.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import httpx

from app.database import get_db
from app.mobile_phone import format_otp_masked_display
from app.settings import get_settings

logger = logging.getLogger(__name__)

PURPOSE_CANDIDATE_MOBILE = "candidate_mobile"
PURPOSE_MOBILE_DUP_CHECK = "candidate_mobile_dup_check"
OTP_TTL_MINUTES = 10
MAX_VERIFY_ATTEMPTS = 5
VERIFY_LOCKOUT_MINUTES = 30
# After initial send: 1st resend @30s, 2nd @60s, 3rd @120s; then 15 min lock.
RESEND_COOLDOWNS_SECONDS = (25, 60, 120)  # after initial send: 1st resend @25s, 2nd @60s, 3rd @120s; then 15 min lock.
RESEND_LOCK_MINUTES = 15
MAX_RESENDS_BEFORE_LOCK = 3  # third successful resend triggers 15m lock
RATE_LIMIT_15M = 5  # allow initial + 3 resends before hard cap (AC3 ladder)
RATE_LIMIT_15M_WINDOW = timedelta(minutes=15)
RATE_LIMIT_24H = 10
RATE_LIMIT_24H_WINDOW = timedelta(hours=24)
# Dup-check is a preflight before every OTP send — keep caps separate/higher so
# local testing and retries do not burn the WhatsApp send budget for ~24h.
DUP_CHECK_RATE_LIMIT_15M = 30
DUP_CHECK_RATE_LIMIT_24H = 100

ERR_SEND_FAILED = (
    "We couldn't send the code right now. Please try again, or contact support."
)
ERR_COUNTRY_UNSUPPORTED_WA = (
    "WhatsApp verification is not available for this country yet. Please contact support."
)
ERR_INVALID_OTP = "Incorrect code."
ERR_EXPIRED_OTP = "This code has expired. Please request a new one."
ERR_TOO_MANY_ATTEMPTS = (
    "Too many incorrect attempts. Please request a new code after 30 minutes."
)
ERR_RESEND_LOCKED = (
    "Still not received? Try again in 15 minutes or contact support."
)
STATE_MOBILE_VERIFIED = "MOBILE_VERIFIED"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _otp_store():
    return get_db()["otp_store"]


def _otp_send_events():
    return get_db()["otp_send_events"]


def _otp_flow_state():
    return get_db()["otp_flow_state"]


def _otp_verify_audit():
    return get_db()["otp_verify_audit"]

def _flow_key(candidate_id: str, phone: str) -> dict[str, Any]:
    """Return the MongoDB filter for OTP flow state documents.

    The flow state is keyed by ``subject`` (candidate ID), ``purpose`` and
    optionally ``phone_hash`` when a phone number is provided. This function
    centralises the key construction used by ``_advance_resend_ladder`` and
    other flow‑state updates.
    """
    key: dict[str, Any] = {
        "subject": candidate_id,
        "purpose": PURPOSE_CANDIDATE_MOBILE,
    }
    if phone:
        key["phone_hash"] = phone_index_key(phone)
    return key
def _mask_phone(phone: str) -> str:
    return format_otp_masked_display(phone)


def phone_index_key(phone_e164: str) -> str:
    """HMAC of E.164 for OTP rate-limit / store indexes (not reversible)."""
    import hashlib
    import hmac

    settings = get_settings()
    secret = (
        (getattr(settings, "otp_phone_hmac_key", None) or "").strip()
        or (settings.jwt_secret or "").strip()
        or "otp-phone-hmac-fallback"
    )
    return hmac.new(
        secret.encode("utf-8"),
        str(phone_e164 or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_otp_code() -> str:
    """Cryptographically secure 6-digit OTP."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(otp: str) -> str:
    """Bcrypt hash; never store plaintext."""
    return bcrypt.hashpw(otp.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_otp_hash(otp: str, otp_hash: str) -> bool:
    try:
        return bcrypt.checkpw(otp.encode("utf-8"), otp_hash.encode("utf-8"))
    except Exception:
        return False


def build_whatsapp_otp_body(otp: str) -> str:
    settings = get_settings()
    template = (
        getattr(settings, "whatsapp_otp_template", None)
        or "{OTP} is your Kovetted verification code. Valid for 10 minutes. Do not share this code with anyone."
    )
    return template.replace("{OTP}", otp)


def is_whatsapp_country_supported(phone_e164: str) -> bool:
    settings = get_settings()
    allowed = getattr(settings, "whatsapp_supported_country_codes", "") or ""
    codes = [c.strip().lstrip("+") for c in allowed.split(",") if c.strip()]
    if not codes:
        return True
    digits = "".join(ch for ch in phone_e164 if ch.isdigit())
    return any(digits.startswith(code) for code in codes)


@dataclass
class RateLimitResult:
    allowed: bool
    retry_after_minutes: int = 0
    message: str = ""


def _count_events(filter_query: dict, since: datetime) -> int:
    return _otp_send_events().count_documents(
        {**filter_query, "createdAt": {"$gte": since}}
    )


def check_otp_rate_limits(
    *,
    phone: str,
    ip: str,
    account_id: str,
    purpose: str = PURPOSE_CANDIDATE_MOBILE,
) -> RateLimitResult:
    # Bypass rate limiting if OTP rate limit feature is disabled
    from app.settings import get_settings
    if not get_settings().otp_rate_limit_enabled:
        return RateLimitResult(allowed=True)

    """Independent hard caps by number, IP, and account.

    Dup-check uses a higher ceiling because the UI calls it before every OTP send.
    """
    now = _now()
    phone_key = phone_index_key(phone)
    if purpose == PURPOSE_MOBILE_DUP_CHECK:
        windows = (
            (RATE_LIMIT_15M_WINDOW, DUP_CHECK_RATE_LIMIT_15M, 15),
            (RATE_LIMIT_24H_WINDOW, DUP_CHECK_RATE_LIMIT_24H, 24 * 60),
        )
    else:
        windows = (
            (RATE_LIMIT_15M_WINDOW, RATE_LIMIT_15M, 15),
            (RATE_LIMIT_24H_WINDOW, RATE_LIMIT_24H, 24 * 60),
        )
    dimensions = (
        ("phone_hash", phone_key),
        ("ip", ip or "unknown"),
        ("account_id", account_id),
    )

    for window, max_count, fallback_minutes in windows:
        since = now - window
        for field, value in dimensions:
            count = _count_events({field: value, "purpose": purpose}, since)
            if count >= max_count:
                oldest = _otp_send_events().find_one(
                    {
                        field: value,
                        "purpose": purpose,
                        "createdAt": {"$gte": since},
                    },
                    sort=[("createdAt", 1)],
                )
                wait = fallback_minutes
                if oldest and oldest.get("createdAt"):
                    created = oldest["createdAt"]
                    # Normalize tz-aware / naive timestamps so unlock math cannot explode.
                    if getattr(created, "tzinfo", None) is not None and created.tzinfo.utcoffset(created) is not None:
                        created = created.astimezone(timezone.utc).replace(tzinfo=None)
                    unlock_at = created + window
                    remaining = (unlock_at - now).total_seconds()
                    # Never report longer than the window itself.
                    wait = max(1, min(fallback_minutes, int(remaining // 60) + 1))
                return RateLimitResult(
                    allowed=False,
                    retry_after_minutes=wait,
                    message=_retry_message(wait),
                )
    return RateLimitResult(allowed=True)


def _retry_message(wait_minutes: int) -> str:
    if wait_minutes >= 60:
        hours = max(1, (wait_minutes + 59) // 60)
        return f"Too many requests. Please try again after about {hours} hour{'s' if hours != 1 else ''}."
    return f"Too many requests. Please try again after {wait_minutes} minutes."


def record_otp_send_event(
    *,
    phone: str,
    ip: str,
    account_id: str,
    channel: str,
    purpose: str = PURPOSE_CANDIDATE_MOBILE,
) -> None:
    _otp_send_events().insert_one(
        {
            "phone_hash": phone_index_key(phone),
            "ip": ip or "unknown",
            "account_id": account_id,
            "purpose": purpose,
            "channel": channel,
            "createdAt": _now(),
        }
    )


def _get_flow_state(candidate_id: str, phone: str | None = None) -> dict[str, Any]:
    """Retrieve OTP flow state.

    If ``phone`` is provided, the lookup includes the phone hash so that
    each phone number has its own independent state. This supports the
    per‑number OTP cooldown feature.
    """
    query: dict[str, Any] = {"subject": candidate_id, "purpose": PURPOSE_CANDIDATE_MOBILE}
    if phone:
        query["phone_hash"] = phone_index_key(phone)
    return _otp_flow_state().find_one(query) or {}


def _seconds_until(when: Optional[datetime]) -> int:
    if not when:
        return 0
    delta = (when - _now()).total_seconds()
    if delta <= 0:
        return 0
    return int(delta) if delta == int(delta) else int(delta) + 1


def _incorrect_message(attempts_remaining: int) -> str:
    return f"Incorrect code. {attempts_remaining} attempts remaining."


def record_otp_verify_audit(
    *,
    candidate_id: str,
    phone: str,
    outcome: str,
    ip: str,
    user_agent: str,
    error_code: str = "",
) -> None:
    """AC11 — every verify attempt (success/failure) with IP + UA."""
    _otp_verify_audit().insert_one(
        {
            "candidate_id": candidate_id,
            "phone_hash": phone_index_key(phone) if phone else "",
            "purpose": PURPOSE_CANDIDATE_MOBILE,
            "outcome": outcome,
            "error_code": error_code or "",
            "ip": ip or "unknown",
            "user_agent": (user_agent or "")[:500],
            "createdAt": _now(),
        }
    )


def invalidate_previous_otps(*, candidate_id: str, phone: str = "", purpose: str = PURPOSE_CANDIDATE_MOBILE) -> None:
    """Only the latest OTP remains valid; change-number invalidates pending OTP."""
    query: dict[str, Any] = {
        "purpose": purpose,
        "invalidated": {"$ne": True},
        "$or": [
            {"candidate_id": candidate_id},
            {"subject": candidate_id},
        ],
    }
    if phone:
        query["$or"].append({"phone_hash": phone_index_key(phone)})
    _otp_store().update_many(
        query,
        {"$set": {"invalidated": True, "invalidatedAt": _now()}},
    )


def invalidate_pending_otp_for_change_number(*, candidate_id: str) -> None:
    """AC1 — Change number returns to KV02 and invalidates the pending OTP."""
    invalidate_previous_otps(candidate_id=candidate_id)
    _otp_flow_state().update_one(
        {"subject": candidate_id, "purpose": PURPOSE_CANDIDATE_MOBILE},
        {
            "$set": {
                "send_count": 0,
                "resend_count": 0,
                "next_resend_at": None,
                "resend_locked_until": None,
                "updatedAt": _now(),
            }
        },
        upsert=True,
    )


def store_hashed_otp(
    *,
    candidate_id: str,
    phone: str,
    otp: str,
    purpose: str = PURPOSE_CANDIDATE_MOBILE,
    channel: str = "whatsapp",
) -> dict[str, Any]:
    now = _now()
    expires = now + timedelta(minutes=OTP_TTL_MINUTES)
    invalidate_previous_otps(candidate_id=candidate_id, phone=phone, purpose=purpose)
    doc = {
        "subject": candidate_id,
        "candidate_id": candidate_id,
        "purpose": purpose,
        "phone_hash": phone_index_key(phone),
        "otp_hash": hash_otp(otp),
        "created_at": now,
        "expires_at": expires,
        "attempt_count": 0,
        "channel": channel,
        "invalidated": False,
        "expiresAt": expires,
    }
    _otp_store().update_one(
        {"subject": candidate_id, "purpose": purpose},
        {"$set": doc, "$unset": {"phone": ""}},
        upsert=True,
    )
    return doc


@dataclass
class OtpVerifyResult:
    ok: bool
    error: str = ""
    error_code: str = ""
    attempts_remaining: Optional[int] = None
    locked_until: Optional[datetime] = None


def _set_verify_lockout(candidate_id: str) -> datetime:
    locked_until = _now() + timedelta(minutes=VERIFY_LOCKOUT_MINUTES)
    _otp_flow_state().update_one(
        {"subject": candidate_id, "purpose": PURPOSE_CANDIDATE_MOBILE},
        {
            "$set": {
                "verify_locked_until": locked_until,
                "updatedAt": _now(),
            }
        },
        upsert=True,
    )
    return locked_until


def verify_stored_otp(
    *,
    candidate_id: str,
    otp: str,
    purpose: str = PURPOSE_CANDIDATE_MOBILE,
    phone: str = "",
    ip: str = "",
    user_agent: str = "",
) -> OtpVerifyResult:
    now = _now()
    flow = _get_flow_state(candidate_id)
    verify_locked_until = flow.get("verify_locked_until")
    if verify_locked_until and verify_locked_until > now:
        result = OtpVerifyResult(
            ok=False,
            error=ERR_TOO_MANY_ATTEMPTS,
            error_code="locked",
            attempts_remaining=0,
            locked_until=verify_locked_until,
        )
        record_otp_verify_audit(
            candidate_id=candidate_id,
            phone=phone,
            outcome="failure",
            ip=ip,
            user_agent=user_agent,
            error_code="locked",
        )
        return result

    record = _otp_store().find_one(
        {
            "subject": candidate_id,
            "purpose": purpose,
            "invalidated": {"$ne": True},
        }
    )
    if not record:
        result = OtpVerifyResult(
            ok=False,
            error=ERR_EXPIRED_OTP,
            error_code="expired_or_missing",
        )
        record_otp_verify_audit(
            candidate_id=candidate_id,
            phone=phone,
            outcome="failure",
            ip=ip,
            user_agent=user_agent,
            error_code="expired_or_missing",
        )
        return result

    expires = record.get("expires_at") or record.get("expiresAt")
    if expires and expires < now:
        # AC7 — expired does not count toward incorrect-attempt limit
        _otp_store().update_one({"_id": record["_id"]}, {"$set": {"invalidated": True}})
        result = OtpVerifyResult(ok=False, error=ERR_EXPIRED_OTP, error_code="expired")
        record_otp_verify_audit(
            candidate_id=candidate_id,
            phone=phone,
            outcome="failure",
            ip=ip,
            user_agent=user_agent,
            error_code="expired",
        )
        return result

    attempt_count = int(record.get("attempt_count") or 0)
    if attempt_count >= MAX_VERIFY_ATTEMPTS:
        _otp_store().update_one({"_id": record["_id"]}, {"$set": {"invalidated": True}})
        locked_until = _set_verify_lockout(candidate_id)
        result = OtpVerifyResult(
            ok=False,
            error=ERR_TOO_MANY_ATTEMPTS,
            error_code="locked",
            attempts_remaining=0,
            locked_until=locked_until,
        )
        record_otp_verify_audit(
            candidate_id=candidate_id,
            phone=phone,
            outcome="failure",
            ip=ip,
            user_agent=user_agent,
            error_code="locked",
        )
        return result

    otp_hash = record.get("otp_hash") or ""
    if not otp_hash and record.get("otp"):
        _otp_store().update_one({"_id": record["_id"]}, {"$set": {"invalidated": True}})
        result = OtpVerifyResult(ok=False, error=ERR_EXPIRED_OTP, error_code="legacy")
        record_otp_verify_audit(
            candidate_id=candidate_id,
            phone=phone,
            outcome="failure",
            ip=ip,
            user_agent=user_agent,
            error_code="legacy",
        )
        return result

    if not verify_otp_hash(str(otp).strip(), otp_hash):
        new_count = attempt_count + 1
        _otp_store().update_one(
            {"_id": record["_id"]},
            {"$inc": {"attempt_count": 1}},
        )
        remaining = max(0, MAX_VERIFY_ATTEMPTS - new_count)
        if remaining <= 0:
            _otp_store().update_one(
                {"_id": record["_id"]},
                {"$set": {"invalidated": True}},
            )
            locked_until = _set_verify_lockout(candidate_id)
            result = OtpVerifyResult(
                ok=False,
                error=ERR_TOO_MANY_ATTEMPTS,
                error_code="locked",
                attempts_remaining=0,
                locked_until=locked_until,
            )
            record_otp_verify_audit(
                candidate_id=candidate_id,
                phone=phone,
                outcome="failure",
                ip=ip,
                user_agent=user_agent,
                error_code="locked",
            )
            return result

        # AC5 — remaining attempts (only returned from rate-limited verify route)
        result = OtpVerifyResult(
            ok=False,
            error=_incorrect_message(remaining),
            error_code="incorrect",
            attempts_remaining=remaining,
        )
        record_otp_verify_audit(
            candidate_id=candidate_id,
            phone=phone,
            outcome="failure",
            ip=ip,
            user_agent=user_agent,
            error_code="incorrect",
        )
        return result

    _otp_store().delete_one({"_id": record["_id"]})
    _otp_flow_state().update_one(
        {"subject": candidate_id, "purpose": PURPOSE_CANDIDATE_MOBILE},
        {
            "$set": {
                "send_count": 0,
                "resend_count": 0,
                "next_resend_at": None,
                "resend_locked_until": None,
                "verify_locked_until": None,
                "updatedAt": _now(),
            }
        },
        upsert=True,
    )
    record_otp_verify_audit(
        candidate_id=candidate_id,
        phone=phone,
        outcome="success",
        ip=ip,
        user_agent=user_agent,
        error_code="",
    )
    return OtpVerifyResult(ok=True)


# ── WhatsApp providers ─────────────────────────────────────────────────


@dataclass
class WhatsAppSendResult:
    success: bool
    provider: str
    error_code: str = ""
    error_message: str = ""


def _send_whatsapp_console(phone: str, otp: str) -> WhatsAppSendResult:
    """Dev provider — never log the OTP code itself."""
    logger.info(
        "WhatsApp OTP dispatched via console provider to %s (code redacted)",
        _mask_phone(phone),
    )
    _ = hash_otp(otp)[:8]
    return WhatsAppSendResult(success=True, provider="console")


def _send_whatsapp_meta(phone: str, otp: str) -> WhatsAppSendResult:
    """Meta Cloud API WhatsApp (primary production path)."""
    settings = get_settings()
    token = (getattr(settings, "whatsapp_access_token", None) or "").strip()
    phone_number_id = (getattr(settings, "whatsapp_phone_number_id", None) or "").strip()
    api_version = (getattr(settings, "whatsapp_api_version", None) or "v21.0").strip()
    template_name = (getattr(settings, "whatsapp_template_id", None) or "").strip()
    language = (getattr(settings, "whatsapp_template_language", None) or "en").strip()

    if not token or not phone_number_id:
        return WhatsAppSendResult(
            success=False,
            provider="meta",
            error_code="config_missing",
            error_message="WhatsApp credentials not configured",
        )

    to = "".join(ch for ch in phone if ch.isdigit())
    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    if template_name:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": otp}],
                    }
                ],
            },
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": build_whatsapp_otp_body(otp)},
        }

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            err_body = {}
            try:
                err_body = response.json()
            except Exception:
                err_body = {}
            err = (err_body.get("error") or {}) if isinstance(err_body, dict) else {}
            code = str(err.get("code") or response.status_code)
            logger.error(
                "WhatsApp Meta send failed phone=%s provider_error_code=%s status=%s",
                _mask_phone(phone),
                code,
                response.status_code,
            )
            return WhatsAppSendResult(
                success=False,
                provider="meta",
                error_code=code,
                error_message=str(err.get("message") or "provider_error")[:200],
            )
        logger.info(
            "WhatsApp OTP sent via meta to %s",
            _mask_phone(phone),
        )
        return WhatsAppSendResult(success=True, provider="meta")
    except Exception as exc:
        logger.error(
            "WhatsApp Meta send exception phone=%s error_type=%s",
            _mask_phone(phone),
            type(exc).__name__,
        )
        return WhatsAppSendResult(
            success=False,
            provider="meta",
            error_code="exception",
            error_message=type(exc).__name__,
        )


def _provider_chain() -> list[str]:
    settings = get_settings()
    primary = (getattr(settings, "whatsapp_otp_provider", None) or "console").strip().lower()
    fallback = (getattr(settings, "whatsapp_otp_fallback_provider", None) or "console").strip().lower()
    chain = [primary]
    if fallback and fallback != primary:
        chain.append(fallback)
    return chain


def _dispatch_provider(name: str, phone: str, otp: str) -> WhatsAppSendResult:
    if name == "meta":
        return _send_whatsapp_meta(phone, otp)
    return _send_whatsapp_console(phone, otp)


def send_whatsapp_otp(phone: str, otp: str) -> WhatsAppSendResult:
    """Send via WhatsApp; auto-retry once on fallback provider. No confirmation SMS."""
    last = WhatsAppSendResult(success=False, provider="none", error_code="no_provider")
    for index, name in enumerate(_provider_chain()):
        result = _dispatch_provider(name, phone, otp)
        if result.success:
            return result
        last = result
        if index == 0:
            logger.warning(
                "WhatsApp primary provider failed phone=%s provider=%s code=%s; trying fallback",
                _mask_phone(phone),
                result.provider,
                result.error_code,
            )
    return last


@dataclass
class IssueOtpResult:
    success: bool
    sent_to: str = ""
    sent_to_masked: str = ""
    channel: str = "whatsapp"
    error: str = ""
    error_code: str = ""
    retry_after_minutes: int = 0
    retry_after_seconds: int = 0
    resend_count: int = 0
    resend_locked: bool = False
    # Dev-only: returned when settings.debug so QA can verify without WhatsApp
    dev_code: Optional[str] = None


def _advance_resend_ladder(candidate_id: str, phone: str) -> tuple[int, int, bool]:
    """Update flow state after a successful send.
    Returns (resend_count, cooldown_seconds, locked).
    Respects the `OTP_RESEND_COOLDOWN_ENABLED` toggle.
    """
    now = _now()
    settings = get_settings()
    flow = _get_flow_state(candidate_id, phone)
    send_count = int(flow.get("send_count") or 0) + 1
    resend_count = max(0, send_count - 1)
    # Default values when cooldown is disabled
    next_resend_at: Optional[datetime] = None
    locked = False
    resend_locked_until = None

    if settings.otp_resend_cooldown_enabled:
        if resend_count >= MAX_RESENDS_BEFORE_LOCK:
            resend_locked_until = now + timedelta(minutes=RESEND_LOCK_MINUTES)
            next_resend_at = resend_locked_until
            locked = True
        elif resend_count < len(RESEND_COOLDOWNS_SECONDS):
            next_resend_at = now + timedelta(seconds=RESEND_COOLDOWNS_SECONDS[resend_count])
        else:
            next_resend_at = now + timedelta(seconds=RESEND_COOLDOWNS_SECONDS[-1])

    # Use subject+purpose as the unique key, phone_hash is just a field
    base_key = {"subject": candidate_id, "purpose": PURPOSE_CANDIDATE_MOBILE}
    
    try:
        _otp_flow_state().update_one(
            base_key,
            {
                "$set": {
                    "phone_hash": phone_index_key(phone),
                    "send_count": send_count,
                    "resend_count": resend_count,
                    "next_resend_at": next_resend_at,
                    "resend_locked_until": resend_locked_until if locked else None,
                    "updatedAt": now,
                }
            },
            upsert=True,
        )
    except Exception as e:
        # Handle race condition by retrying with find+update
        if "duplicate key" in str(e):
            existing = _otp_flow_state().find_one(base_key)
            if existing:
                _otp_flow_state().update_one(
                    {"_id": existing["_id"]},
                    {
                        "$set": {
                            "phone_hash": phone_index_key(phone),
                            "send_count": send_count,
                            "resend_count": resend_count,
                            "next_resend_at": next_resend_at,
                            "resend_locked_until": resend_locked_until if locked else None,
                            "updatedAt": now,
                        }
                    }
                )
            else:
                raise
        else:
            raise
    
    cooldown = _seconds_until(next_resend_at) if next_resend_at else 0
    return resend_count, cooldown, locked


def issue_candidate_whatsapp_otp(
    *,
    candidate_id: str,
    phone: str,
    ip: str,
) -> IssueOtpResult:
    if not is_whatsapp_country_supported(phone):
        return IssueOtpResult(success=False, error=ERR_COUNTRY_UNSUPPORTED_WA, error_code="country")

    now = _now()
    flow = _get_flow_state(candidate_id)

    verify_locked_until = flow.get("verify_locked_until")
    if verify_locked_until and verify_locked_until > now:
        wait_m = max(1, int((verify_locked_until - now).total_seconds() // 60) + 1)
        return IssueOtpResult(
            success=False,
            error=ERR_TOO_MANY_ATTEMPTS,
            error_code="verify_locked",
            retry_after_minutes=wait_m,
            retry_after_seconds=_seconds_until(verify_locked_until),
        )

    settings = get_settings()
    resend_locked_until = flow.get("resend_locked_until")
    if settings.otp_resend_cooldown_enabled:
        if resend_locked_until and resend_locked_until > now:
            return IssueOtpResult(
                success=False,
                error=ERR_RESEND_LOCKED,
                error_code="resend_locked",
                retry_after_minutes=max(
                    1, int((resend_locked_until - now).total_seconds() // 60) + 1
                ),
                retry_after_seconds=_seconds_until(resend_locked_until),
                resend_locked=True,
                resend_count=int(flow.get("resend_count") or 0),
            )
        # 15‑minute resend lock expired — reset ladder so the next send starts fresh.
        if resend_locked_until and resend_locked_until <= now:
            _otp_flow_state().update_one(
                _flow_key(candidate_id, phone),
                {
                    "$set": {
                        "send_count": 0,
                        "resend_count": 0,
                        "next_resend_at": None,
                        "resend_locked_until": None,
                        "updatedAt": now,
                    }
                },
                upsert=True,
            )
            flow = _get_flow_state(candidate_id, phone)

    

    limits = check_otp_rate_limits(phone=phone, ip=ip, account_id=candidate_id)
    if not limits.allowed:
        return IssueOtpResult(
            success=False,
            error=limits.message,
            error_code="rate_limit",
            retry_after_minutes=limits.retry_after_minutes,
            retry_after_seconds=limits.retry_after_minutes * 60,
        )

    otp = generate_otp_code()
    store_hashed_otp(candidate_id=candidate_id, phone=phone, otp=otp, channel="whatsapp")

    send_result = send_whatsapp_otp(phone, otp)
    if not send_result.success:
        invalidate_previous_otps(
            candidate_id=candidate_id, phone=phone, purpose=PURPOSE_CANDIDATE_MOBILE
        )
        logger.error(
            "WhatsApp OTP delivery failed phone=%s account=%s provider=%s code=%s",
            _mask_phone(phone),
            candidate_id,
            send_result.provider,
            send_result.error_code,
        )
        return IssueOtpResult(success=False, error=ERR_SEND_FAILED, error_code="send_failed")

    record_otp_send_event(
        phone=phone, ip=ip, account_id=candidate_id, channel="whatsapp"
    )
    resend_count, cooldown_s, locked = _advance_resend_ladder(candidate_id, phone)

    settings = get_settings()
    return IssueOtpResult(
        success=True,
        sent_to=phone,
        sent_to_masked=format_otp_masked_display(phone),
        channel="whatsapp",
        retry_after_seconds=cooldown_s,
        resend_count=resend_count,
        resend_locked=locked,
        # Never include in production responses
        dev_code=otp if settings.debug else None,
    )
