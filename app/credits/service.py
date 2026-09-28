"""Award, redeem, reverse, expire, refer — all via the ledger service."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.credits.config import (
    DEFAULT_ORG,
    DIRECTION_CREDIT,
    TERMS_VERSION,
    catalogue_item,
    load_credit_config,
    rule_for,
)
from app.credits.ledger import (
    COL_FLAGS,
    COL_LEDGER,
    allocate_fifo,
    count_rule_awards,
    credits_earned_since,
    derived_balance,
    earning_suspended,
    expiring_lots,
    find_by_idempotency,
    history,
    period_start,
    post_entry,
    serialize_entry,
    _as_utc,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from app.database import get_db

    return get_db()


def _candidate(candidate_id: str) -> Optional[dict[str, Any]]:
    try:
        oid = ObjectId(candidate_id)
    except Exception:
        return None
    return _db()["candidates"].find_one({"_id": oid})


def _notify_candidate(candidate_id: str, message: str, ntype: str = "credits") -> None:
    try:
        doc = _candidate(candidate_id) or {}
        if doc.get("creditNotifications") is False:
            return
        from app.services.notifications import create_notification

        create_notification(
            "candidate",
            ntype,
            message,
            candidate_id=str(candidate_id),
            candidate_email=doc.get("email"),
        )
    except Exception:
        logger.exception("Credit notification failed")


def _cap_reached(
    candidate_id: str,
    rule_id: str,
    rule: dict[str, Any],
    extra: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[datetime], str]:
    cap_count = int(rule.get("capCount") or 0)
    cap_period = str(rule.get("capPeriod") or "lifetime")
    since = period_start(cap_period)
    used = count_rule_awards(candidate_id, rule_id, since=since, extra=extra)
    if cap_count and used >= cap_count:
        nxt = None
        if since:
            if cap_period == "day":
                nxt = since + timedelta(days=1)
            elif cap_period == "calendar_month":
                month = since.month + 1 if since.month < 12 else 1
                year = since.year if since.month < 12 else since.year + 1
                nxt = since.replace(year=year, month=month)
            else:
                nxt = _now() + timedelta(days=1)
        return True, nxt, f"You have reached the cap for {rule.get('label') or rule_id}."
    secondary = rule.get("secondaryCapCount")
    secondary_period = rule.get("secondaryCapPeriod")
    if secondary and secondary_period:
        used2 = count_rule_awards(
            candidate_id, rule_id, since=period_start(str(secondary_period)), extra=extra
        )
        if used2 >= int(secondary):
            return True, None, f"Monthly cap reached for {rule.get('label') or rule_id}."
    return False, None, ""


def _ceiling_blocks(candidate_id: str, amount: int, config: dict[str, Any]) -> Optional[str]:
    day_start = period_start("day")
    month_start = period_start("calendar_month")
    if day_start and credits_earned_since(candidate_id, day_start) + amount > int(
        config.get("dailyEarningCeiling") or 250
    ):
        return "Daily earning ceiling reached."
    if month_start and credits_earned_since(candidate_id, month_start) + amount > int(
        config.get("monthlyEarningCeiling") or 2000
    ):
        return "Monthly earning ceiling reached."
    return None


def award(
    *,
    candidate_id: str,
    rule_id: str,
    event_id: str,
    actor: dict[str, Any],
    org_id: str = DEFAULT_ORG,
    metadata: Optional[dict[str, Any]] = None,
    skip_if_no_terms: bool = True,
) -> dict[str, Any]:
    config = load_credit_config(org_id)
    if config.get("killSwitch") or not config.get("earningEnabled", True):
        return {"awarded": False, "reason": "paused", "message": "Credit earning is paused."}
    if earning_suspended(candidate_id):
        return {
            "awarded": False,
            "reason": "earning_suspended",
            "message": "Earning is paused while your account is reviewed. Your balance is unchanged.",
        }
    if skip_if_no_terms and not has_accepted_terms(candidate_id, config):
        return {
            "awarded": False,
            "reason": "terms",
            "message": "Accept the credits terms to start earning.",
            "requiresTerms": True,
            "termsVersion": config.get("termsVersion") or TERMS_VERSION,
        }
    rule = rule_for(config, rule_id)
    if not rule or not rule.get("enabled", True):
        return {"awarded": False, "reason": "disabled", "message": "This earning action is not available."}
    amount = int(rule.get("amount") or 0)
    if amount <= 0:
        return {"awarded": False, "reason": "invalid_rule"}
    idem = f"{rule_id}:{event_id}"
    existing = find_by_idempotency(candidate_id, idem)
    if existing:
        return {"awarded": True, "duplicate": True, "entry": serialize_entry(existing, for_candidate=True)}
    blocked, next_at, cap_msg = _cap_reached(candidate_id, rule_id, rule)
    if blocked:
        return {
            "awarded": False,
            "reason": "cap_reached",
            "message": cap_msg,
            "nextAvailableAt": next_at.isoformat().replace("+00:00", "Z") if next_at else None,
        }
    ceiling = _ceiling_blocks(candidate_id, amount, config)
    if ceiling:
        return {"awarded": False, "reason": "ceiling", "message": ceiling}

    expiry_days = int(config.get("expiryDays") or 365)
    expires_at = _now() + timedelta(days=expiry_days)
    try:
        entry = post_entry(
            candidate_id=candidate_id,
            rule_id=rule_id,
            amount=amount,
            event_id=event_id,
            idempotency_key=idem,
            actor=actor,
            org_id=org_id,
            label=rule.get("label") or rule_id,
            metadata=metadata,
            expires_at=expires_at,
        )
    except ValueError as exc:
        return {"awarded": False, "reason": str(exc)}

    maybe_flag_burst(candidate_id, config)
    if rule.get("immediateNotify"):
        _notify_candidate(
            candidate_id,
            f"You earned {amount} credits for {rule.get('label')}.",
            "credits_award",
        )
    maybe_notify_milestone(candidate_id)
    return {
        "awarded": True,
        "duplicate": False,
        "entry": serialize_entry(entry, for_candidate=True),
        "balance": derived_balance(candidate_id),
        "toast": {
            "action": rule.get("label"),
            "amount": amount,
        },
    }


def redeem(
    *,
    candidate_id: str,
    item_id: str,
    actor: dict[str, Any],
    org_id: str = DEFAULT_ORG,
    confirm: bool = False,
) -> dict[str, Any]:
    config = load_credit_config(org_id)
    if config.get("killSwitch") or not config.get("redemptionEnabled", True):
        return {"ok": False, "reason": "paused", "message": "Redemptions are paused."}
    if not has_accepted_terms(candidate_id, config):
        return {
            "ok": False,
            "reason": "terms",
            "message": "Accept the credits terms before redeeming.",
            "requiresTerms": True,
        }
    item = catalogue_item(config, item_id)
    if not item or not item.get("enabled", True):
        return {"ok": False, "reason": "unavailable", "message": "This reward is not available."}
    price = int(item.get("price") or 0)
    balance = derived_balance(candidate_id)
    if balance < price:
        return {
            "ok": False,
            "reason": "insufficient",
            "message": "Your balance changed. This redemption was refused.",
            "shortfall": price - balance,
            "balance": balance,
        }
    benefit_key = item.get("benefitKey") or item_id
    if item.get("durationDays") and has_active_benefit(candidate_id, benefit_key):
        return {
            "ok": False,
            "reason": "overlap",
            "message": "You already have this benefit. It cannot overlap.",
        }
    if not confirm:
        return {
            "ok": True,
            "requiresConfirm": True,
            "item": item_id,
            "name": item.get("name"),
            "price": price,
            "balanceAfter": balance - price,
            "disclaimer": (
                "Credits have no cash value, cannot be transferred, and cannot be withdrawn as money."
            ),
        }
    try:
        allocations = allocate_fifo(candidate_id, price)
    except ValueError:
        return {
            "ok": False,
            "reason": "insufficient",
            "message": "Your balance changed. This redemption was refused.",
            "balance": derived_balance(candidate_id),
        }
    event_id = f"redeem:{item_id}:{secrets.token_hex(8)}"
    try:
        entry = post_entry(
            candidate_id=candidate_id,
            rule_id="redeem",
            amount=-price,
            event_id=event_id,
            idempotency_key=event_id,
            actor=actor,
            org_id=org_id,
            label=f"Redeemed {item.get('name')}",
            metadata={"itemId": item_id, "lotAllocations": allocations, "priceAtRedeem": price},
            reason="redemption",
        )
    except ValueError:
        return {
            "ok": False,
            "reason": "insufficient",
            "message": "Your balance changed. This redemption was refused.",
            "balance": derived_balance(candidate_id),
        }

    fulfilment = item.get("fulfilment") or "automatic"
    status = "delivered" if fulfilment == "automatic" else "confirmed"
    sla_hours = item.get("manualSlaHours")
    sla_due = _now() + timedelta(hours=int(sla_hours)) if sla_hours else None
    expires = None
    if item.get("durationDays"):
        expires = _now() + timedelta(days=int(item["durationDays"]))
        _db()["credit_benefits"].insert_one(
            {
                "candidateId": str(candidate_id),
                "benefitKey": benefit_key,
                "itemId": item_id,
                "startsAt": _now(),
                "expiresAt": expires,
                "active": True,
            }
        )
    order = {
        "candidateId": str(candidate_id),
        "itemId": item_id,
        "name": item.get("name"),
        "price": price,
        "status": status if fulfilment == "automatic" else "in_progress",
        "fulfilment": fulfilment,
        "ledgerEntryId": str(entry["_id"]),
        "createdAt": _now(),
        "slaDueAt": sla_due,
        "deliveredAt": _now() if fulfilment == "automatic" else None,
        "expiresAt": expires,
        "benefitKey": benefit_key,
    }
    inserted = _db()["credit_orders"].insert_one(order)
    order["id"] = str(inserted.inserted_id)
    _notify_candidate(
        candidate_id,
        f"Redemption confirmed: {item.get('name')} for {price} credits.",
        "credits_redeem",
    )
    return {
        "ok": True,
        "order": _serialize_order(order),
        "balance": derived_balance(candidate_id),
        "entry": serialize_entry(entry, for_candidate=True),
    }


def refund_order(order_id: str, *, actor: dict[str, Any], reason: str) -> dict[str, Any]:
    try:
        oid = ObjectId(order_id)
    except Exception:
        return {"ok": False, "message": "Order not found."}
    order = _db()["credit_orders"].find_one({"_id": oid})
    if not order:
        return {"ok": False, "message": "Order not found."}
    if order.get("status") == "cancelled":
        return {"ok": False, "message": "Order already cancelled."}
    price = int(order.get("price") or 0)
    candidate_id = str(order["candidateId"])
    event_id = f"refund:{order_id}"
    config = load_credit_config(order.get("orgId") or DEFAULT_ORG)
    expiry_days = int(config.get("expiryDays") or 365)
    entry = post_entry(
        candidate_id=candidate_id,
        rule_id="refund",
        amount=price,
        event_id=event_id,
        idempotency_key=event_id,
        actor=actor,
        label=f"Refund: {order.get('name')}",
        metadata={"orderId": order_id, "originalLedgerEntryId": order.get("ledgerEntryId")},
        expires_at=_now() + timedelta(days=expiry_days),
        reason=reason,
    )
    _db()["credit_orders"].update_one(
        {"_id": order["_id"]},
        {"$set": {"status": "cancelled", "cancelledAt": _now(), "cancelReason": reason}},
    )
    _db()["credit_benefits"].update_many(
        {"candidateId": candidate_id, "itemId": order.get("itemId"), "active": True},
        {"$set": {"active": False}},
    )
    _notify_candidate(
        candidate_id,
        f"{price} credits were returned because {reason}",
        "credits_refund",
    )
    return {"ok": True, "entry": serialize_entry(entry), "balance": derived_balance(candidate_id)}


def reverse_entry(
    entry_id: str,
    *,
    actor: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """AC3 — corrections are reversing entries, never edits."""
    try:
        oid = ObjectId(entry_id)
    except Exception:
        return {"ok": False, "message": "Entry not found."}
    original = _db()[COL_LEDGER].find_one({"_id": oid})
    if not original:
        return {"ok": False, "message": "Entry not found."}
    amount = int(original.get("amount") or 0)
    reverse_amount = -amount
    candidate_id = str(original["candidateId"])
    balance = derived_balance(candidate_id)
    shortfall = 0
    if reverse_amount < 0 and balance + reverse_amount < 0:
        shortfall = abs(balance + reverse_amount)
        reverse_amount = -balance
    metadata: dict[str, Any] = {"originalEntryId": entry_id, "shortfall": shortfall}
    if reverse_amount < 0:
        try:
            metadata["lotAllocations"] = allocate_fifo(candidate_id, abs(reverse_amount))
        except ValueError:
            return {"ok": False, "message": "Balance changed; reversal refused."}
    event_id = f"reverse:{entry_id}"
    entry = post_entry(
        candidate_id=candidate_id,
        rule_id="reverse",
        amount=reverse_amount,
        event_id=event_id,
        idempotency_key=event_id,
        actor=actor,
        label=f"Reversal of {original.get('label')}",
        metadata=metadata,
        reverses_entry_id=entry_id,
        reason=reason,
        allow_zero=reverse_amount == 0,
    )
    _notify_candidate(candidate_id, f"A credit adjustment was made: {reason}", "credits_reverse")
    return {
        "ok": True,
        "entry": serialize_entry(entry),
        "shortfall": shortfall,
        "balance": derived_balance(candidate_id),
        "benefitWithdrawn": False,
    }


def expire_due_lots(candidate_id: Optional[str] = None) -> int:
    query: dict[str, Any] = {"direction": DIRECTION_CREDIT, "expiresAt": {"$lte": _now()}}
    if candidate_id:
        query["candidateId"] = str(candidate_id)
    expired = 0
    from app.credits.ledger import _fifo_lots

    ids = {str(candidate_id)} if candidate_id else {
        d["candidateId"] for d in _db()[COL_LEDGER].find(query, {"candidateId": 1})
    }
    for cid in ids:
        for lot in _fifo_lots(cid, include_expired=True):
            expires = lot.get("expiresAt")
            if not isinstance(expires, datetime) or _as_utc(expires) > _now():
                continue
            remaining = int(lot["remaining"])
            if remaining <= 0:
                continue
            event_id = f"expire:{lot['lotId']}"
            try:
                post_entry(
                    candidate_id=cid,
                    rule_id="expire",
                    amount=-remaining,
                    event_id=event_id,
                    idempotency_key=event_id,
                    actor={"type": "system", "id": "expiry"},
                    label="Credits expired",
                    metadata={"lotAllocations": [{"lotId": lot["lotId"], "amount": remaining}]},
                    reason="expiry",
                )
                expired += 1
                _notify_candidate(
                    cid,
                    f"{remaining} credits expired.",
                    "credits_expire",
                )
            except ValueError:
                continue
    return expired


def has_active_benefit(candidate_id: str, benefit_key: str) -> bool:
    doc = _db()["credit_benefits"].find_one(
        {
            "candidateId": str(candidate_id),
            "benefitKey": benefit_key,
            "active": True,
            "expiresAt": {"$gt": _now()},
        }
    )
    return bool(doc)


def active_benefits(candidate_id: str) -> list[dict[str, Any]]:
    config = load_credit_config()
    names: dict[str, str] = {}
    for item_id, item in (config.get("catalogue") or {}).items():
        label = item.get("name") or item_id
        names[item_id] = label
        if item.get("benefitKey"):
            names[item["benefitKey"]] = label
    out = []
    for doc in _db()["credit_benefits"].find(
        {"candidateId": str(candidate_id), "active": True, "expiresAt": {"$gt": _now()}}
    ):
        exp = doc.get("expiresAt")
        out.append(
            {
                "benefitKey": doc.get("benefitKey"),
                "itemId": doc.get("itemId"),
                "name": names.get(doc.get("benefitKey")) or names.get(doc.get("itemId")) or doc.get("benefitKey"),
                "expiresAt": exp.isoformat().replace("+00:00", "Z") if isinstance(exp, datetime) else exp,
            }
        )
    return out


def has_accepted_terms(candidate_id: str, config: Optional[dict[str, Any]] = None) -> bool:
    version = int((config or load_credit_config()).get("termsVersion") or TERMS_VERSION)
    return bool(
        _db()["credit_terms_acceptances"].find_one(
            {"candidateId": str(candidate_id), "version": version}
        )
    )


def accept_terms(candidate_id: str, version: Optional[int] = None) -> dict[str, Any]:
    config = load_credit_config()
    current = int(config.get("termsVersion") or TERMS_VERSION)
    if version is not None and int(version) != current:
        return {
            "ok": False,
            "message": "Terms have been updated. Please accept the current version.",
            "version": current,
        }
    try:
        _db()["credit_terms_acceptances"].insert_one(
            {"candidateId": str(candidate_id), "version": current, "acceptedAt": _now()}
        )
    except DuplicateKeyError:
        pass
    grants = grant_pending_candidate_credits(candidate_id)
    newly = [g for g in grants if g.get("awarded") and not g.get("duplicate") and not g.get("requiresTerms")]
    return {
        "ok": True,
        "version": current,
        "balance": derived_balance(candidate_id),
        "grants": newly,
        "toast": newly[0].get("toast") if newly else None,
    }


def grant_pending_candidate_credits(candidate_id: str) -> list[dict[str, Any]]:
    """Catch up ledger awards that were skipped (no terms yet) or missed on unlock.

    Safe to call repeatedly — each rule uses an idempotent event_id.
    """
    if not has_accepted_terms(candidate_id):
        return []
    results: list[dict[str, Any]] = []
    try:
        oid = ObjectId(candidate_id)
    except Exception:
        return results
    doc = _db()["candidates"].find_one({"_id": oid}) or {}
    score = doc.get("profileScore") if isinstance(doc.get("profileScore"), dict) else {}
    at_100 = bool(
        score.get("applyEligible")
        or doc.get("profileScoreReached100At")
        or int(score.get("total") or 0) >= 100
    )
    if at_100:
        from app.credits.hooks import on_profile_score_100

        result = on_profile_score_100(candidate_id, first_time=True)
        if result:
            results.append(result)

    fingerprint = doc.get("resumeFingerprint")
    if fingerprint:
        from app.credits.hooks import on_resume_uploaded

        # previous=None so catch-up is not treated as an unchanged re-upload.
        result = on_resume_uploaded(candidate_id, str(fingerprint), None)
        if result:
            results.append(result)
    return results


def ensure_referral_identity(candidate_id: str) -> dict[str, Any]:
    coll = _db()["credit_referrals"]
    existing = coll.find_one({"referrerId": str(candidate_id), "kind": "identity"})
    if existing:
        return {"code": existing["code"], "link": _referral_link(existing["code"])}
    code = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10].upper()
    while coll.find_one({"code": code}):
        code = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10].upper()
    try:
        coll.insert_one(
            {
                "kind": "identity",
                "referrerId": str(candidate_id),
                "code": code,
                "createdAt": _now(),
            }
        )
    except DuplicateKeyError:
        existing = coll.find_one({"referrerId": str(candidate_id), "kind": "identity"})
        if existing:
            return {"code": existing["code"], "link": _referral_link(existing["code"])}
        raise
    try:
        oid = ObjectId(candidate_id)
        _db()["candidates"].update_one(
            {"_id": oid, "referralCode": {"$exists": False}},
            {"$set": {"referralCode": code}},
        )
    except Exception:
        pass
    return {"code": code, "link": _referral_link(code)}


def _referral_link(code: str) -> str:
    from app.settings import get_settings

    base = get_settings().frontend_url.rstrip("/")
    return f"{base}/register?ref={code}"


def attribute_referral(
    referred_id: str,
    *,
    code: Optional[str] = None,
    email: Optional[str] = None,
    mobile: Optional[str] = None,
) -> dict[str, Any]:
    if not code:
        return {"ok": False, "reason": "missing_code"}
    identity = _db()["credit_referrals"].find_one({"kind": "identity", "code": str(code).upper()})
    if not identity:
        return {"ok": False, "reason": "unknown_code"}
    referrer_id = str(identity["referrerId"])
    if referrer_id == str(referred_id):
        _log_abuse(referred_id, "self_referral", {"code": code})
        return {"ok": False, "reason": "self", "message": "You cannot refer yourself."}
    referred = _candidate(referred_id) or {}
    referrer = _candidate(referrer_id) or {}
    if email and referrer.get("email") and str(email).lower() == str(referrer.get("email")).lower():
        _log_abuse(referred_id, "shared_email_referral", {"code": code})
        return {"ok": False, "reason": "shared_identity", "message": "This account cannot be referred."}
    if mobile and referrer.get("mobile") and str(mobile) == str(referrer.get("mobile")):
        _log_abuse(referred_id, "shared_mobile_referral", {"code": code})
        return {"ok": False, "reason": "shared_identity", "message": "This account cannot be referred."}
    existing = _db()["credit_referrals"].find_one({"kind": "attribution", "referredId": str(referred_id)})
    if existing:
        return {"ok": True, "alreadyAttributed": True}
    try:
        _db()["credit_referrals"].insert_one(
            {
                "kind": "attribution",
                "referrerId": referrer_id,
                "referredId": str(referred_id),
                "code": str(code).upper(),
                "consentName": False,
                "createdAt": _now(),
                "stage": "registered",
            }
        )
    except DuplicateKeyError:
        return {"ok": True, "alreadyAttributed": True}
    return {"ok": True, "referrerId": referrer_id}


def referral_progress(referred_id: str, stage: str, *, org_id: str = DEFAULT_ORG) -> Optional[dict[str, Any]]:
    attr = _db()["credit_referrals"].find_one({"kind": "attribution", "referredId": str(referred_id)})
    if not attr:
        return None
    rule_map = {
        "profile_100": "referral_profile_100",
        "shortlist": "referral_shortlist",
        "hire_standard": "referral_hire_standard",
        "hire_premium": "referral_hire_premium",
    }
    rule_id = rule_map.get(stage)
    if not rule_id:
        _db()["credit_referrals"].update_one({"_id": attr["_id"]}, {"$set": {"stage": stage}})
        return None
    result = award(
        candidate_id=str(attr["referrerId"]),
        rule_id=rule_id,
        event_id=f"referral:{stage}:{referred_id}",
        actor={"type": "system", "id": "referral"},
        org_id=org_id,
        metadata={"referredId": str(referred_id), "stage": stage},
        skip_if_no_terms=True,
    )
    _db()["credit_referrals"].update_one({"_id": attr["_id"]}, {"$set": {"stage": stage}})
    return result


def list_referrals(referrer_id: str) -> list[dict[str, Any]]:
    out = []
    for doc in _db()["credit_referrals"].find(
        {"kind": "attribution", "referrerId": str(referrer_id)}
    ).sort("createdAt", -1):
        referred = _candidate(str(doc["referredId"])) or {}
        consent = bool(doc.get("consentName"))
        referred_key = hashlib.sha256(
            f"{referrer_id}:{doc.get('referredId')}".encode("utf-8")
        ).hexdigest()[:12]
        out.append(
            {
                "referralKey": referred_key,
                "display": referred.get("name") if consent else "Referred candidate",
                "masked": not consent,
                "stage": doc.get("stage") or "registered",
                "earned": _referral_earned(str(referrer_id), str(doc["referredId"])),
                "possible": 50 + 100 + 500,
            }
        )
    return out


def _referral_earned(referrer_id: str, referred_id: str) -> int:
    pipeline = [
        {
            "$match": {
                "candidateId": referrer_id,
                "direction": DIRECTION_CREDIT,
                "metadata.referredId": referred_id,
            }
        },
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    rows = list(_db()[COL_LEDGER].aggregate(pipeline))
    return int(rows[0]["total"]) if rows else 0


def resume_fingerprint(content: bytes | str) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def maybe_flag_burst(candidate_id: str, config: dict[str, Any]) -> None:
    hour_ago = _now() - timedelta(hours=1)
    count = _db()[COL_LEDGER].count_documents(
        {
            "candidateId": str(candidate_id),
            "direction": DIRECTION_CREDIT,
            "timestamp": {"$gte": hour_ago},
        }
    )
    threshold = int((config.get("abuse") or {}).get("burstAwardsPerHour") or 20)
    if count >= threshold:
        _log_abuse(candidate_id, "burst_awards", {"count": count})
        suspend_earning(candidate_id, "Unusual burst of credit events.")


def _log_abuse(candidate_id: str, kind: str, meta: dict[str, Any]) -> None:
    _db()["credit_abuse_audit"].insert_one(
        {
            "candidateId": str(candidate_id),
            "kind": kind,
            "metadata": meta,
            "createdAt": _now(),
        }
    )


def suspend_earning(candidate_id: str, reason: str, *, actor_id: str = "system") -> None:
    _db()[COL_FLAGS].update_one(
        {"candidateId": str(candidate_id)},
        {
            "$set": {
                "status": "suspended",
                "reason": reason,
                "updatedAt": _now(),
                "actorId": actor_id,
            }
        },
        upsert=True,
    )


def resume_earning(candidate_id: str, *, actor_id: str) -> None:
    _db()[COL_FLAGS].update_one(
        {"candidateId": str(candidate_id)},
        {"$set": {"status": "clear", "updatedAt": _now(), "actorId": actor_id}},
        upsert=True,
    )


def maybe_notify_milestone(candidate_id: str) -> None:
    balance = derived_balance(candidate_id)
    for mark in (100, 250, 500, 1000, 2500):
        if balance < mark:
            continue
        key = f"milestone:{candidate_id}:{mark}"
        if find_by_idempotency(candidate_id, f"note-{key}"):
            continue
        # Marker-only: zero-amount not allowed; store a notification audit instead.
        if _db()["credit_milestones"].find_one({"_id": key}):
            continue
        _db()["credit_milestones"].insert_one({"_id": key, "at": _now()})
        _notify_candidate(candidate_id, f"You reached {mark} credits.", "credits_milestone")


def lifetime_earned(candidate_id: str) -> int:
    pipeline = [
        {
            "$match": {
                "candidateId": str(candidate_id),
                "direction": DIRECTION_CREDIT,
            }
        },
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    rows = list(_db()[COL_LEDGER].aggregate(pipeline))
    return int(rows[0]["total"]) if rows else 0


def lifetime_redeemed(candidate_id: str) -> int:
    pipeline = [
        {
            "$match": {
                "candidateId": str(candidate_id),
                "ruleId": {"$in": ["redeem"]},
            }
        },
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    rows = list(_db()[COL_LEDGER].aggregate(pipeline))
    return abs(int(rows[0]["total"])) if rows else 0


RULE_HREFS = {
    "profile_score_100": "/profile",
    "employment_verified": "/profile",
    "skill_verified": "/profile",
    "resume_material_update": "/profile",
    "assessment_submitted": "/profile",
    "apply_relevant": "/search",
    "employer_view": "/credits",
    "employer_shortlist": "/credits",
    "interview_attended": "/credits",
    "interview_feedback": "/credits",
    "referral_profile_100": "/credits",
    "referral_shortlist": "/credits",
    "referral_hire_standard": "/credits",
    "referral_hire_premium": "/credits",
}


def ways_to_earn(candidate_id: str, org_id: str = DEFAULT_ORG) -> list[dict[str, Any]]:
    config = load_credit_config(org_id)
    out = []
    for rule_id, rule in (config.get("rules") or {}).items():
        if not rule.get("enabled", True):
            continue
        used = count_rule_awards(
            candidate_id, rule_id, since=period_start(str(rule.get("capPeriod") or "lifetime"))
        )
        cap = int(rule.get("capCount") or 0)
        exhausted, next_at, msg = _cap_reached(candidate_id, rule_id, rule)
        out.append(
            {
                "ruleId": rule_id,
                "label": rule.get("label"),
                "amount": int(rule.get("amount") or 0),
                "capCount": cap,
                "capPeriod": rule.get("capPeriod"),
                "used": used,
                "exhausted": exhausted,
                "nextAvailableAt": next_at.isoformat().replace("+00:00", "Z") if next_at else None,
                "message": msg if exhausted else "",
                "requiresVerification": rule.get("requires") in ("verification", "verified_event"),
                "category": rule.get("category") or "profile",
                "href": RULE_HREFS.get(rule_id),
            }
        )
    return out


def snapshot(candidate_id: str, org_id: str = DEFAULT_ORG) -> dict[str, Any]:
    expire_due_lots(candidate_id)
    # Catch up profile/resume awards that were blocked (no terms) or missed on unlock.
    if has_accepted_terms(candidate_id):
        try:
            grant_pending_candidate_credits(candidate_id)
        except Exception:
            logger.exception("Pending credit grant failed for %s", candidate_id)
    config = load_credit_config(org_id)
    identity = ensure_referral_identity(candidate_id)
    as_referred = _db()["credit_referrals"].find_one(
        {"kind": "attribution", "referredId": str(candidate_id)}
    )
    return {
        "balance": derived_balance(candidate_id),
        "earnedToDate": lifetime_earned(candidate_id),
        "redeemedToDate": lifetime_redeemed(candidate_id),
        "expiring": expiring_lots(candidate_id, 30),
        "history": history(candidate_id, limit=50, for_candidate=True),
        "waysToEarn": ways_to_earn(candidate_id, org_id),
        "catalogue": _catalogue_view(candidate_id, config),
        "orders": list_orders(candidate_id),
        "referrals": list_referrals(candidate_id),
        "referral": identity,
        "referralNameConsent": bool((as_referred or {}).get("consentName")),
        "wasReferred": bool(as_referred),
        "benefits": active_benefits(candidate_id),
        "termsAccepted": has_accepted_terms(candidate_id, config),
        "termsVersion": config.get("termsVersion") or TERMS_VERSION,
        "disclaimer": (
            "Credits have no cash value, cannot be transferred, and cannot be withdrawn."
        ),
        "earningEnabled": bool(config.get("earningEnabled") and not config.get("killSwitch")),
        "redemptionEnabled": bool(config.get("redemptionEnabled") and not config.get("killSwitch")),
        "earningSuspended": earning_suspended(candidate_id),
        "creditNotifications": (_candidate(candidate_id) or {}).get("creditNotifications", True) is not False,
        "tierEnabled": bool(config.get("tierEnabled")),
        "lifetimeEarned": lifetime_earned(candidate_id),
    }


def admin_candidate_ledger(candidate_id: str) -> dict[str, Any]:
    expire_due_lots(candidate_id)
    cand = _candidate(candidate_id)
    return {
        "found": bool(cand),
        "candidateId": str(candidate_id),
        "name": (cand or {}).get("name"),
        "email": (cand or {}).get("email"),
        "balance": derived_balance(candidate_id),
        "earnedToDate": lifetime_earned(candidate_id),
        "redeemedToDate": lifetime_redeemed(candidate_id),
        "history": history(candidate_id, limit=100, for_candidate=False),
        "orders": list_orders(candidate_id),
        "benefits": active_benefits(candidate_id),
        "earningSuspended": earning_suspended(candidate_id),
        "termsAccepted": has_accepted_terms(candidate_id),
    }


def _catalogue_view(candidate_id: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    balance = derived_balance(candidate_id)
    items = []
    for item_id, item in (config.get("catalogue") or {}).items():
        if not item.get("enabled", True):
            continue
        price = int(item.get("price") or 0)
        items.append(
            {
                "id": item_id,
                **item,
                "affordable": balance >= price,
                "shortfall": max(0, price - balance),
                "overlapBlocked": bool(
                    item.get("durationDays")
                    and has_active_benefit(candidate_id, item.get("benefitKey") or item_id)
                ),
            }
        )
    return items


def list_orders(candidate_id: str) -> list[dict[str, Any]]:
    docs = _db()["credit_orders"].find({"candidateId": str(candidate_id)}).sort("createdAt", -1)
    return [_serialize_order(d) for d in docs]


def _serialize_order(doc: dict[str, Any], *, for_admin: bool = False) -> dict[str, Any]:
    def iso(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat().replace("+00:00", "Z")
        return value

    attachment = doc.get("attachment")
    if not isinstance(attachment, str) or not attachment.startswith(("http://", "https://")):
        attachment = None
    payload = {
        "id": str(doc.get("id") or doc.get("_id")),
        "itemId": doc.get("itemId"),
        "name": doc.get("name"),
        "price": doc.get("price"),
        "status": doc.get("status"),
        "fulfilment": doc.get("fulfilment"),
        "createdAt": iso(doc.get("createdAt")),
        "slaDueAt": iso(doc.get("slaDueAt")),
        "deliveredAt": iso(doc.get("deliveredAt")),
        "expiresAt": iso(doc.get("expiresAt")),
        "attachment": attachment,
        "cancelReason": doc.get("cancelReason"),
    }
    if for_admin:
        payload["candidateId"] = doc.get("candidateId")
    return payload


def reporting(org_id: str = DEFAULT_ORG, *, days: int = 30) -> dict[str, Any]:
    since = _now() - timedelta(days=days)
    match_org: dict[str, Any] = {}
    if org_id:
        match_org["orgId"] = org_id
    issued = list(
        _db()[COL_LEDGER].aggregate(
            [
                {
                    "$match": {
                        **match_org,
                        "direction": DIRECTION_CREDIT,
                        "timestamp": {"$gte": since},
                    }
                },
                {"$group": {"_id": "$ruleId", "credits": {"$sum": "$amount"}, "count": {"$sum": 1}}},
            ]
        )
    )
    redeemed = list(
        _db()[COL_LEDGER].aggregate(
            [
                {
                    "$match": {
                        **match_org,
                        "ruleId": "redeem",
                        "timestamp": {"$gte": since},
                    }
                },
                {
                    "$group": {
                        "_id": "$metadata.itemId",
                        "credits": {"$sum": "$amount"},
                        "count": {"$sum": 1},
                    }
                },
            ]
        )
    )
    expired = list(
        _db()[COL_LEDGER].aggregate(
            [
                {"$match": {**match_org, "ruleId": "expire", "timestamp": {"$gte": since}}},
                {"$group": {"_id": None, "credits": {"$sum": "$amount"}}},
            ]
        )
    )
    outstanding = list(
        _db()[COL_LEDGER].aggregate(
            [{"$match": match_org}, {"$group": {"_id": None, "credits": {"$sum": "$amount"}}}]
        )
    )
    issued_total = sum(int(r["credits"]) for r in issued)
    redeemed_total = abs(sum(int(r["credits"]) for r in redeemed))
    config = load_credit_config(org_id)
    value = float(config.get("indicativeCreditValue") or 1)
    outstanding_credits = max(int(outstanding[0]["credits"]) if outstanding else 0, 0)
    referral_rules = [
        "referral_profile_100",
        "referral_shortlist",
        "referral_hire_standard",
        "referral_hire_premium",
    ]
    referral_cost = sum(int(r["credits"]) for r in issued if r["_id"] in referral_rules)
    return {
        "periodDays": days,
        "issued": issued_total,
        "redeemed": redeemed_total,
        "expired": abs(int(expired[0]["credits"])) if expired else 0,
        "outstanding": outstanding_credits,
        "outstandingValue": outstanding_credits * value,
        "redemptionRate": (redeemed_total / issued_total) if issued_total else 0,
        "byRule": issued,
        "byItem": redeemed,
        "referralCost": referral_cost,
        "currency": config.get("currency") or "INR",
    }
