"""Append-only credit ledger. Balances are derived; entries are never edited."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError

from app.credits.config import (
    DEFAULT_ORG,
    DIRECTION_CREDIT,
    DIRECTION_DEBIT,
    LEDGER_RETENTION_YEARS,
)

logger = logging.getLogger(__name__)

COL_LEDGER = "credit_ledger"
COL_FLAGS = "credit_abuse_flags"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from app.database import get_db

    return get_db()


def _drop_index(coll, name: str) -> None:
    try:
        coll.drop_index(name)
    except Exception:
        pass


def ensure_credit_indexes() -> None:
    db = _db()
    ledger = db[COL_LEDGER]
    ledger.create_index(
        [("candidateId", 1), ("idempotencyKey", 1)],
        unique=True,
        name="credit_idempotency",
    )
    ledger.create_index([("candidateId", 1), ("timestamp", -1)])
    ledger.create_index([("candidateId", 1), ("ruleId", 1), ("timestamp", -1)])
    ledger.create_index("timestamp")
    ledger.create_index("expiresAt")
    db["credit_config"].create_index("orgId")
    db["credit_orders"].create_index([("candidateId", 1), ("createdAt", -1)])
    db["credit_orders"].create_index([("status", 1), ("slaDueAt", 1)])
    # Sparse so pre-seq documents can coexist; new writes always set seq.
    # Drop mismatched same-name index (e.g. non-sparse from an earlier deploy).
    _drop_index(ledger, "credit_seq")
    ledger.create_index(
        [("candidateId", 1), ("seq", 1)],
        unique=True,
        sparse=True,
        name="credit_seq",
    )
    referrals = db["credit_referrals"]
    # Attribution rows reuse `code` of the referrer; uniqueness is identity-only.
    _drop_index(referrals, "code_1")
    _drop_index(referrals, "referredId_1")
    referrals.create_index(
        "code",
        unique=True,
        partialFilterExpression={"kind": "identity"},
        name="credit_referral_identity_code",
    )
    referrals.create_index(
        [("kind", 1), ("referrerId", 1)],
        unique=True,
        partialFilterExpression={"kind": "identity"},
        name="credit_referral_identity_person",
    )
    referrals.create_index("referrerId")
    referrals.create_index(
        [("kind", 1), ("referredId", 1)],
        unique=True,
        partialFilterExpression={"kind": "attribution"},
        name="credit_referral_attribution_person",
    )
    db[COL_FLAGS].create_index("candidateId")
    db["credit_terms_acceptances"].create_index(
        [("candidateId", 1), ("version", 1)], unique=True
    )
    db["credit_benefits"].create_index(
        [("candidateId", 1), ("benefitKey", 1), ("expiresAt", 1)]
    )


_PUBLIC_META_KEYS = {
    "jobId",
    "itemId",
    "stage",
    "shortfall",
    "priceAtRedeem",
    "orderId",
}


def _as_utc(value: Any) -> Any:
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def serialize_entry(doc: dict[str, Any], *, for_candidate: bool = False) -> dict[str, Any]:
    ts = _as_utc(doc.get("timestamp"))
    exp = _as_utc(doc.get("expiresAt"))
    meta = dict(doc.get("metadata") or {})
    actor = dict(doc.get("actor") or {})
    if for_candidate:
        meta = {k: v for k, v in meta.items() if k in _PUBLIC_META_KEYS}
        actor = {"type": actor.get("type") or "system"}
    return {
        "id": str(doc.get("_id")),
        "ruleId": doc.get("ruleId"),
        "label": doc.get("label"),
        "amount": int(doc.get("amount") or 0),
        "direction": doc.get("direction"),
        "resultingBalance": int(doc.get("resultingBalance") or 0),
        "timestamp": ts.isoformat().replace("+00:00", "Z") if isinstance(ts, datetime) else ts,
        "expiresAt": exp.isoformat().replace("+00:00", "Z") if isinstance(exp, datetime) else exp,
        "actor": actor,
        "metadata": meta,
        "reason": doc.get("reason"),
    }


def derived_balance(candidate_id: str) -> int:
    """AC2 — balance is always the sum of ledger amounts, never an editable field."""
    pipeline = [
        {"$match": {"candidateId": str(candidate_id)}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    rows = list(_db()[COL_LEDGER].aggregate(pipeline))
    return int(rows[0]["total"]) if rows else 0


def last_resulting_balance(candidate_id: str) -> int:
    doc = _db()[COL_LEDGER].find_one(
        {"candidateId": str(candidate_id)}, sort=[("seq", -1), ("timestamp", -1), ("_id", -1)]
    )
    if not doc:
        return 0
    return int(doc.get("resultingBalance") or 0)


def _next_seq(candidate_id: str) -> int:
    last = _db()[COL_LEDGER].find_one(
        {"candidateId": str(candidate_id), "seq": {"$exists": True}},
        sort=[("seq", -1)],
    )
    return int((last or {}).get("seq") or 0) + 1


def earning_suspended(candidate_id: str) -> bool:
    flag = _db()[COL_FLAGS].find_one(
        {"candidateId": str(candidate_id), "status": "suspended"}
    )
    return bool(flag)


def find_by_idempotency(candidate_id: str, key: str) -> Optional[dict[str, Any]]:
    return _db()[COL_LEDGER].find_one(
        {"candidateId": str(candidate_id), "idempotencyKey": key}
    )


def count_rule_awards(
    candidate_id: str,
    rule_id: str,
    *,
    since: Optional[datetime] = None,
    extra: Optional[dict[str, Any]] = None,
) -> int:
    query: dict[str, Any] = {
        "candidateId": str(candidate_id),
        "ruleId": rule_id,
        "direction": DIRECTION_CREDIT,
    }
    if since is not None:
        query["timestamp"] = {"$gte": since}
    if extra:
        query.update(extra)
    return _db()[COL_LEDGER].count_documents(query)


def period_start(period: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or _now()
    if period in ("lifetime", "per_interview"):
        return None
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "calendar_month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "90d":
        return now - timedelta(days=90)
    if period in ("rolling_12m", "year"):
        return now - timedelta(days=365)
    if period.endswith("d") and period[:-1].isdigit():
        return now - timedelta(days=int(period[:-1]))
    return None


def credits_earned_since(candidate_id: str, since: datetime) -> int:
    pipeline = [
        {
            "$match": {
                "candidateId": str(candidate_id),
                "direction": DIRECTION_CREDIT,
                "timestamp": {"$gte": since},
            }
        },
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    rows = list(_db()[COL_LEDGER].aggregate(pipeline))
    return int(rows[0]["total"]) if rows else 0


def _fifo_lots(candidate_id: str, *, include_expired: bool = False) -> list[dict[str, Any]]:
    """Unspent award lots, oldest first. Remaining is derived, never edited."""
    coll = _db()[COL_LEDGER]
    awards = list(
        coll.find(
            {"candidateId": str(candidate_id), "direction": DIRECTION_CREDIT}
        ).sort([("timestamp", 1), ("_id", 1)])
    )
    consumed: dict[str, int] = {}
    for debit in coll.find(
        {
            "candidateId": str(candidate_id),
            "direction": DIRECTION_DEBIT,
            "metadata.lotAllocations": {"$exists": True},
        }
    ):
        for alloc in (debit.get("metadata") or {}).get("lotAllocations") or []:
            lot_id = str(alloc.get("lotId") or "")
            consumed[lot_id] = consumed.get(lot_id, 0) + int(alloc.get("amount") or 0)
    lots = []
    now = _now()
    for award in awards:
        lot_id = str(award["_id"])
        remaining = int(award.get("amount") or 0) - consumed.get(lot_id, 0)
        expires = award.get("expiresAt")
        if remaining <= 0:
            continue
        expired = isinstance(expires, datetime) and _as_utc(expires) <= now
        if expired and not include_expired:
            continue
        lots.append(
            {**award, "lotId": lot_id, "remaining": remaining, "expired": expired}
        )
    return lots


def allocate_fifo(candidate_id: str, spend: int) -> list[dict[str, int | str]]:
    remaining = int(spend)
    allocations: list[dict[str, int | str]] = []
    for lot in _fifo_lots(candidate_id):
        if remaining <= 0:
            break
        take = min(remaining, int(lot["remaining"]))
        allocations.append({"lotId": lot["lotId"], "amount": take})
        remaining -= take
    if remaining > 0:
        raise ValueError("INSUFFICIENT_BALANCE")
    return allocations


def post_entry(
    *,
    candidate_id: str,
    rule_id: str,
    amount: int,
    event_id: str,
    idempotency_key: str,
    actor: dict[str, Any],
    org_id: str = DEFAULT_ORG,
    label: str = "",
    metadata: Optional[dict[str, Any]] = None,
    expires_at: Optional[datetime] = None,
    reverses_entry_id: Optional[str] = None,
    reason: Optional[str] = None,
    allow_zero: bool = False,
) -> dict[str, Any]:
    """Single write path for every award and redemption (AC8)."""
    if amount == 0 and not allow_zero:
        raise ValueError("Ledger amount cannot be zero.")
    existing = find_by_idempotency(candidate_id, idempotency_key)
    if existing:
        return existing

    direction = DIRECTION_CREDIT if amount > 0 else DIRECTION_DEBIT
    last_error: Optional[Exception] = None
    for _ in range(8):
        current = derived_balance(candidate_id)
        nxt = current + int(amount)
        if nxt < 0:
            raise ValueError("INSUFFICIENT_BALANCE")
        seq = _next_seq(candidate_id)
        doc = {
            "candidateId": str(candidate_id),
            "orgId": org_id or DEFAULT_ORG,
            "ruleId": rule_id,
            "label": label or rule_id,
            "amount": int(amount),
            "direction": direction,
            "eventId": event_id,
            "idempotencyKey": idempotency_key,
            "seq": seq,
            "resultingBalance": nxt,
            "timestamp": _now(),
            "actor": actor,
            "metadata": metadata or {},
            "reversesEntryId": reverses_entry_id,
            "reason": reason,
            "expiresAt": expires_at,
            "retainUntil": _now() + timedelta(days=365 * LEDGER_RETENTION_YEARS),
        }
        try:
            result = _db()[COL_LEDGER].insert_one(doc)
            doc["_id"] = result.inserted_id
            return doc
        except DuplicateKeyError as exc:
            last_error = exc
            replayed = find_by_idempotency(candidate_id, idempotency_key)
            if replayed:
                return replayed
            continue
    raise last_error or ValueError("LEDGER_BUSY")


def reconcile(org_id: Optional[str] = None) -> dict[str, Any]:
    """AC7 — sum of entries must equal every candidate's derived balance."""
    match: dict[str, Any] = {}
    if org_id:
        match["orgId"] = org_id
    pipeline = [
        {"$match": match} if match else {"$match": {}},
        {"$sort": {"seq": 1, "timestamp": 1, "_id": 1}},
        {
            "$group": {
                "_id": "$candidateId",
                "sumAmount": {"$sum": "$amount"},
                "lastBalance": {"$last": "$resultingBalance"},
            }
        },
    ]
    discrepancies = []
    for row in _db()[COL_LEDGER].aggregate(pipeline):
        summed = int(row.get("sumAmount") or 0)
        last = int(row.get("lastBalance") or 0)
        if summed != last:
            discrepancies.append(
                {
                    "candidateId": row["_id"],
                    "sumAmount": summed,
                    "lastResultingBalance": last,
                }
            )
    report = {
        "ok": not discrepancies,
        "checkedAt": _now(),
        "discrepancyCount": len(discrepancies),
        "discrepancies": discrepancies[:50],
    }
    _db()["credit_reconcile_runs"].insert_one(report)
    if discrepancies:
        logger.error("Credit ledger reconcile failed: %s", discrepancies[:5])
    return {
        "ok": report["ok"],
        "checkedAt": report["checkedAt"].isoformat().replace("+00:00", "Z"),
        "discrepancyCount": report["discrepancyCount"],
        "discrepancies": report["discrepancies"],
    }


def history(
    candidate_id: str,
    *,
    rule_id: Optional[str] = None,
    direction: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 100,
    for_candidate: bool = True,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"candidateId": str(candidate_id)}
    if rule_id:
        query["ruleId"] = rule_id
    if direction:
        query["direction"] = direction
    if since or until:
        query["timestamp"] = {}
        if since:
            query["timestamp"]["$gte"] = since
        if until:
            query["timestamp"]["$lte"] = until
    docs = (
        _db()[COL_LEDGER]
        .find(query)
        .sort([("timestamp", -1), ("_id", -1)])
        .limit(max(1, min(limit, 500)))
    )
    return [serialize_entry(d, for_candidate=for_candidate) for d in docs]


def expiring_lots(candidate_id: str, within_days: int) -> list[dict[str, Any]]:
    horizon = _now() + timedelta(days=within_days)
    out = []
    for lot in _fifo_lots(candidate_id):
        expires = lot.get("expiresAt")
        if isinstance(expires, datetime) and _now() < _as_utc(expires) <= horizon:
            out.append(
                {
                    "amount": int(lot["remaining"]),
                    "expiresAt": expires.isoformat().replace("+00:00", "Z"),
                }
            )
    return out
