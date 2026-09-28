"""KV42 — Candidate multi-channel notification dispatcher.

Channel priority when whatsappOptIn (KV13):
  email (always if present) + WhatsApp primary; SMS only if WA fails or
  undelivered after 10 minutes (no double notify).

Without WhatsApp opt-in: email + SMS (if verified mobile).
Without verified mobile: email only (AC5, no error).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urljoin

from bson import ObjectId

from app.database import get_db
from app.services.comms_catalog import get_copy
from app.services.short_links import create_short_link
from app.services.sms_notify import send_sms
from app.services.whatsapp_notify import send_whatsapp_text
from app.settings import get_settings
from app.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)

SMS_QUIET_START = 21  # 21:00 local
SMS_QUIET_END = 9  # 09:00 local
WA_SMS_FALLBACK_MINUTES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _logs():
    return get_db()["communication_logs"]


def _candidates():
    return get_db()["candidates"]


def _oid(candidate_id: str) -> Optional[ObjectId]:
    try:
        return ObjectId(candidate_id)
    except Exception:
        return None


def _write_log(**fields: Any) -> str:
    doc = {
        "createdAt": _now(),
        "updatedAt": _now(),
        **fields,
    }
    result = _logs().insert_one(doc)
    return str(result.inserted_id)


def _update_log(log_id: str, **fields: Any) -> None:
    try:
        _logs().update_one(
            {"_id": ObjectId(log_id)},
            {"$set": {**fields, "updatedAt": _now()}},
        )
    except Exception:
        logger.exception("Failed updating communication log %s", log_id)


def _candidate_profile(candidate_id: str) -> dict:
    oid = _oid(candidate_id)
    if not oid:
        return {}
    return _candidates().find_one({"_id": oid}) or {}


def _candidate_local_now(profile: dict) -> datetime:
    tz_name = (
        profile.get("timezone")
        or profile.get("timeZone")
        or get_settings().google_calendar_timezone
        or "Asia/Kolkata"
    )
    tz = resolve_timezone(tz_name)
    return datetime.now(timezone.utc).astimezone(tz)


def sms_allowed_now(
    *,
    event: str,
    profile: dict,
    interview_starts_at: Optional[datetime] = None,
) -> bool:
    """AC7 — no SMS 21:00–09:00 local except URGENT priority."""
    copy = get_copy(event)
    from app.services.comms_catalog import Priority
    if copy and copy.priority == Priority.URGENT:
        return True

    local = _candidate_local_now(profile)
    hour = local.hour
    in_quiet = hour >= SMS_QUIET_START or hour < SMS_QUIET_END
    if not in_quiet:
        return True
    
    # Legacy fallback for older reminder formats
    if event.startswith("interview_reminder") and interview_starts_at:
        start = interview_starts_at
        if start.tzinfo is not None:
            start = start.astimezone(timezone.utc).replace(tzinfo=None)
        delta = start - _now()
        if timedelta(0) <= delta <= timedelta(hours=3):
            return True
    return False


def _build_destination(
    *,
    path_template: str,
    job_id: str,
    candidate_id: str,
    token: str,
) -> str:
    settings = get_settings()
    base = (settings.frontend_url or "").rstrip("/") + "/"
    path = (
        path_template.replace("{job_id}", job_id or "")
        .replace("{candidate_id}", candidate_id or "")
        .replace("{token}", token or "")
    )
    return urljoin(base, path.lstrip("/"))


def notify_candidate_milestone(
    *,
    event: str,
    candidate_id: str,
    job_id: str = "",
    data: Optional[dict] = None,
    booking_token: str = "",
    interview_starts_at: Optional[datetime] = None,
    force_email_action: str = "",
) -> dict[str, Any]:
    """Dispatch a hiring milestone across email / WhatsApp / SMS (KV42)."""
    data = dict(data or {})
    copy = get_copy(event)
    if not copy:
        logger.warning("Unknown milestone event=%s", event)
        return {"ok": False, "error": "unknown_event"}

    profile = _candidate_profile(candidate_id)
    email = (profile.get("email") or data.get("candidate_email") or "").strip()
    mobile_verified = bool(
        profile.get("mobile_verified") or profile.get("mobileVerified")
    )
    whatsapp_opt_in = bool(profile.get("whatsappOptIn"))
    phone = ""
    try:
        from app.services.candidate_mobile_identity import get_plaintext_mobile

        phone = get_plaintext_mobile(candidate_id) or ""
    except Exception:
        phone = str(profile.get("phone") or "")

    destination = _build_destination(
        path_template=copy.path_template,
        job_id=job_id,
        candidate_id=candidate_id,
        token=booking_token,
    )
    short_url = create_short_link(
        destination_url=destination,
        candidate_id=candidate_id,
        event=event,
        job_id=job_id,
    )
    sms_body = copy.body
    wa_body = f"{copy.title}. {copy.body} {short_url}"

    result: dict[str, Any] = {
        "ok": True,
        "event": event,
        "channels": {},
        "shortUrl": short_url,
    }

    # ── Email (always when present) ────────────────────────────────────
    email_action = force_email_action or copy.email_action
    if email:
        try:
            from app.email.service import notify_candidate

            payload = {
                **data,
                "candidate_id": candidate_id,
                "job_id": job_id,
                "candidate_name": data.get("candidate_name")
                or profile.get("name")
                or "there",
                "job_role": data.get("job_role") or data.get("role") or "",
                "status": data.get("status") or copy.title,
                "message": data.get("message") or copy.body,
                "frontend_url": destination,
                "short_url": short_url,
            }
            sent = notify_candidate(email_action, payload, email)
            log_id = _write_log(
                candidate_id=candidate_id,
                job_id=job_id,
                event=event,
                channel="email",
                template_id=email_action,
                provider="mailgun",
                provider_message_id="",
                status="sent" if sent else "failed",
                short_url=short_url,
            )
            result["channels"]["email"] = {
                "status": "sent" if sent else "failed",
                "logId": log_id,
            }
        except Exception:
            logger.exception("Email milestone failed event=%s", event)
            log_id = _write_log(
                candidate_id=candidate_id,
                job_id=job_id,
                event=event,
                channel="email",
                template_id=email_action,
                provider="mailgun",
                status="failed",
            )
            result["channels"]["email"] = {"status": "failed", "logId": log_id}
    else:
        log_id = _write_log(
            candidate_id=candidate_id,
            job_id=job_id,
            event=event,
            channel="email",
            template_id=email_action,
            provider="mailgun",
            status="skipped",
            skip_reason="no_email",
        )
        result["channels"]["email"] = {"status": "skipped", "logId": log_id}

    # ── Mobile channels ────────────────────────────────────────────────
    if not mobile_verified or not phone:
        # AC5 — email-only, no error
        log_id = _write_log(
            candidate_id=candidate_id,
            job_id=job_id,
            event=event,
            channel="sms",
            template_id=copy.sms_template_id,
            provider="none",
            status="skipped",
            skip_reason="no_verified_mobile",
        )
        result["channels"]["sms"] = {"status": "skipped", "logId": log_id}
        result["channels"]["whatsapp"] = {"status": "skipped", "reason": "no_verified_mobile"}
        return result

    if whatsapp_opt_in:
        wa = send_whatsapp_text(
            phone_e164=phone,
            body=wa_body,
            template_name=copy.whatsapp_template,
        )
        wa_status = "sent" if wa.success else "failed"
        wa_log = _write_log(
            candidate_id=candidate_id,
            job_id=job_id,
            event=event,
            channel="whatsapp",
            template_id=copy.whatsapp_template,
            provider=wa.provider,
            provider_message_id=wa.provider_message_id,
            status=wa_status,
            short_url=short_url,
            dedupe_key=f"{candidate_id}:{job_id}:{event}:mobile",
        )
        result["channels"]["whatsapp"] = {
            "status": wa_status,
            "logId": wa_log,
            "providerMessageId": wa.provider_message_id,
        }
        # AC4 — queue SMS fallback only if WA failed OR pending delivery check
        if wa.success:
            _write_log(
                candidate_id=candidate_id,
                job_id=job_id,
                event=event,
                channel="sms",
                template_id=copy.sms_template_id,
                provider="pending",
                status="pending_fallback",
                short_url=short_url,
                dedupe_key=f"{candidate_id}:{job_id}:{event}:mobile",
                fallback_after=_now() + timedelta(minutes=WA_SMS_FALLBACK_MINUTES),
                parent_whatsapp_log_id=wa_log,
                phone_e164=phone,
                body=sms_body,
            )
            result["channels"]["sms"] = {"status": "pending_fallback"}
        else:
            # Immediate SMS fallback on WA send failure
            _dispatch_sms(
                result=result,
                event=event,
                candidate_id=candidate_id,
                job_id=job_id,
                profile=profile,
                phone=phone,
                sms_body=sms_body,
                template_id=copy.sms_template_id,
                short_url=short_url,
                interview_starts_at=interview_starts_at,
                dedupe_key=f"{candidate_id}:{job_id}:{event}:mobile",
            )
    else:
        # No WA opt-in → SMS (subject to quiet hours)
        _dispatch_sms(
            result=result,
            event=event,
            candidate_id=candidate_id,
            job_id=job_id,
            profile=profile,
            phone=phone,
            sms_body=sms_body,
            template_id=copy.sms_template_id,
            short_url=short_url,
            interview_starts_at=interview_starts_at,
            dedupe_key=f"{candidate_id}:{job_id}:{event}:mobile",
        )
        result["channels"]["whatsapp"] = {"status": "skipped", "reason": "not_opted_in"}

    return result


def _dispatch_sms(
    *,
    result: dict,
    event: str,
    candidate_id: str,
    job_id: str,
    profile: dict,
    phone: str,
    sms_body: str,
    template_id: str,
    short_url: str,
    interview_starts_at: Optional[datetime],
    dedupe_key: str,
) -> None:
    # Avoid double SMS if already sent for this dedupe key
    existing = _logs().find_one(
        {
            "dedupe_key": dedupe_key,
            "channel": "sms",
            "status": {"$in": ["sent", "delivered"]},
        }
    )
    if existing:
        result["channels"]["sms"] = {"status": "skipped", "reason": "already_sent"}
        return

    if not sms_allowed_now(
        event=event, profile=profile, interview_starts_at=interview_starts_at
    ):
        log_id = _write_log(
            candidate_id=candidate_id,
            job_id=job_id,
            event=event,
            channel="sms",
            template_id=template_id,
            provider="queued",
            status="queued_quiet_hours",
            short_url=short_url,
            dedupe_key=dedupe_key,
            phone_e164=phone,
            body=sms_body,
            deliver_after=_next_quiet_end(profile),
        )
        result["channels"]["sms"] = {"status": "queued_quiet_hours", "logId": log_id}
        return

    sms = send_sms(
        phone_e164=phone,
        body=sms_body,
        template_id=template_id,
        link=short_url,
    )
    status = "sent" if sms.success else "failed"
    log_id = _write_log(
        candidate_id=candidate_id,
        job_id=job_id,
        event=event,
        channel="sms",
        template_id=template_id,
        provider=sms.provider,
        provider_message_id=sms.provider_message_id,
        status=status,
        short_url=short_url,
        dedupe_key=dedupe_key,
    )
    result["channels"]["sms"] = {
        "status": status,
        "logId": log_id,
        "providerMessageId": sms.provider_message_id,
    }


def _next_quiet_end(profile: dict) -> datetime:
    local = _candidate_local_now(profile)
    tz = local.tzinfo
    if local.hour >= SMS_QUIET_START:
        nxt = local.replace(hour=SMS_QUIET_END, minute=0, second=0, microsecond=0)
        from datetime import timedelta as td

        nxt = nxt + td(days=1)
    else:
        nxt = local.replace(hour=SMS_QUIET_END, minute=0, second=0, microsecond=0)
    return nxt.astimezone(timezone.utc).replace(tzinfo=None)


def process_pending_sms_fallbacks(limit: int = 50) -> int:
    """AC4 — if WhatsApp not delivered within 10 minutes, send SMS once."""
    now = _now()
    pending = list(
        _logs()
        .find(
            {
                "channel": "sms",
                "status": "pending_fallback",
                "fallback_after": {"$lte": now},
            }
        )
        .limit(limit)
    )
    sent = 0
    for doc in pending:
        dedupe = doc.get("dedupe_key") or ""
        already = _logs().find_one(
            {
                "dedupe_key": dedupe,
                "channel": "sms",
                "status": {"$in": ["sent", "delivered"]},
            }
        )
        # Check parent WA delivery. Without delivery webhooks, treat provider
        # "sent" as success so we do not double-notify via SMS (AC4).
        parent_id = doc.get("parent_whatsapp_log_id")
        wa_ok = False
        if parent_id:
            try:
                parent = _logs().find_one({"_id": ObjectId(parent_id)})
                if parent and parent.get("status") in {
                    "sent",
                    "delivered",
                    "read",
                }:
                    wa_ok = True
                elif parent and parent.get("status") in {"failed", "undelivered"}:
                    wa_ok = False
                elif parent:
                    # Unknown status — wait / skip until webhook updates
                    wa_ok = True
            except Exception:
                pass
        if wa_ok or already:
            _update_log(str(doc["_id"]), status="cancelled", skip_reason="wa_ok_or_sent")
            continue

        phone = doc.get("phone_e164") or ""
        if not phone:
            _update_log(str(doc["_id"]), status="failed", skip_reason="no_phone")
            continue
        profile = _candidate_profile(str(doc.get("candidate_id") or ""))
        event = str(doc.get("event") or "")
        if not sms_allowed_now(event=event, profile=profile):
            _update_log(
                str(doc["_id"]),
                status="queued_quiet_hours",
                deliver_after=_next_quiet_end(profile),
            )
            continue
        sms = send_sms(
            phone_e164=phone,
            body=str(doc.get("body") or ""),
            template_id=str(doc.get("template_id") or ""),
            link=str(doc.get("short_url") or ""),
        )
        _update_log(
            str(doc["_id"]),
            status="sent" if sms.success else "failed",
            provider=sms.provider,
            provider_message_id=sms.provider_message_id,
        )
        if sms.success:
            sent += 1
    return sent


def process_queued_quiet_hour_sms(limit: int = 50) -> int:
    now = _now()
    queued = list(
        _logs()
        .find({"channel": "sms", "status": "queued_quiet_hours", "deliver_after": {"$lte": now}})
        .limit(limit)
    )
    sent = 0
    for doc in queued:
        phone = doc.get("phone_e164") or ""
        if not phone:
            continue
        sms = send_sms(
            phone_e164=phone,
            body=str(doc.get("body") or ""),
            template_id=str(doc.get("template_id") or ""),
            link=str(doc.get("short_url") or ""),
        )
        _update_log(
            str(doc["_id"]),
            status="sent" if sms.success else "failed",
            provider=sms.provider,
            provider_message_id=sms.provider_message_id,
        )
        if sms.success:
            sent += 1
    return sent
