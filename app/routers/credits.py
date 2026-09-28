"""Candidate and admin APIs for the E16 credits ledger."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.auth import require_candidate, require_hr
from app.credits.config import load_credit_config
from app.credits.ledger import derived_balance, history, reconcile
from app.credits.service import (
    accept_terms,
    admin_candidate_ledger,
    attribute_referral,
    ensure_referral_identity,
    expire_due_lots,
    has_active_benefit,
    list_orders,
    redeem,
    refund_order,
    reporting,
    resume_earning,
    reverse_entry,
    snapshot,
    suspend_earning,
)

router = APIRouter(tags=["credits"])


class TermsBody(BaseModel):
    version: Optional[int] = None


class RedeemBody(BaseModel):
    itemId: str
    confirm: bool = False


class ReverseBody(BaseModel):
    entryId: str
    reason: str = Field(min_length=3)


class AdminAwardBody(BaseModel):
    candidateId: str
    amount: int
    reason: str = Field(min_length=3)


class OrderStatusBody(BaseModel):
    status: str
    attachment: Optional[str] = None
    reason: Optional[str] = None


class ReferralConsentBody(BaseModel):
    consent: bool = True


class SuspendBody(BaseModel):
    candidateId: str
    reason: str
    resume: bool = False


def _cid(principal) -> str:
    return str(principal.candidate_id or principal.subject)


def _csv_cell(value: Any) -> str:
    text = str(value or "")
    if any(ch in text for ch in (",", '"', "\n", "\r")) or text[:1] in ("=", "+", "-", "@"):
        return '"' + text.replace('"', '""') + '"'
    return text


@router.get("/api/credits/me")
async def get_my_credits(principal=Depends(require_candidate)):
    return snapshot(_cid(principal))


@router.get("/api/credits/balance")
async def get_my_balance(principal=Depends(require_candidate)):
    cid = _cid(principal)
    expire_due_lots(cid)
    return {"balance": derived_balance(cid)}


@router.get("/api/credits/history")
async def get_my_history(
    principal=Depends(require_candidate),
    ruleId: Optional[str] = None,
    direction: Optional[str] = None,
    export: bool = False,
):
    rows = history(_cid(principal), rule_id=ruleId, direction=direction, limit=500, for_candidate=True)
    if not export:
        return {"entries": rows}
    lines = ["date,action,amount,balance"]
    for row in rows:
        lines.append(
            ",".join(
                [
                    _csv_cell(row.get("timestamp")),
                    _csv_cell(row.get("label")),
                    _csv_cell(row.get("amount")),
                    _csv_cell(row.get("resultingBalance")),
                ]
            )
        )
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=credits-history.csv"},
    )


@router.post("/api/credits/terms")
async def post_terms(body: TermsBody, principal=Depends(require_candidate)):
    result = accept_terms(_cid(principal), body.version)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or "Cannot accept terms.")
    return result


@router.post("/api/credits/redeem")
async def post_redeem(body: RedeemBody, principal=Depends(require_candidate)):
    result = redeem(
        candidate_id=_cid(principal),
        item_id=body.itemId,
        confirm=body.confirm,
        actor={"type": "candidate", "id": _cid(principal)},
    )
    if not result.get("ok") and not result.get("requiresConfirm"):
        raise HTTPException(status_code=400, detail=result.get("message") or "Redemption refused.")
    return result


@router.get("/api/credits/orders")
async def get_orders(principal=Depends(require_candidate)):
    return {"orders": list_orders(_cid(principal))}


@router.get("/api/credits/referral")
async def get_referral(principal=Depends(require_candidate)):
    identity = ensure_referral_identity(_cid(principal))
    snap = snapshot(_cid(principal))
    return {**identity, "referrals": snap.get("referrals") or []}


@router.post("/api/credits/referral/consent")
async def post_referral_consent(body: ReferralConsentBody, principal=Depends(require_candidate)):
    from app.database import get_db

    get_db()["credit_referrals"].update_many(
        {"kind": "attribution", "referredId": _cid(principal)},
        {"$set": {"consentName": bool(body.consent)}},
    )
    return {"ok": True}


@router.patch("/api/credits/prefs")
async def patch_prefs(body: dict, principal=Depends(require_candidate)):
    from bson import ObjectId
    from app.database import get_db

    enabled = body.get("creditNotifications")
    if enabled is None:
        raise HTTPException(status_code=400, detail="creditNotifications is required.")
    try:
        oid = ObjectId(_cid(principal))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid candidate.")
    get_db()["candidates"].update_one(
        {"_id": oid},
        {"$set": {"creditNotifications": bool(enabled)}},
    )
    return {"ok": True, "creditNotifications": bool(enabled)}


@router.get("/api/credits/boost-active")
async def boost_active(principal=Depends(require_candidate)):
    return {"active": has_active_benefit(_cid(principal), "visibility_boost")}


@router.get("/api/credits/admin/config")
async def admin_config(principal=Depends(require_hr), orgId: str = "default"):
    """Read-only platform-standard programme (not editable by recruiters)."""
    return load_credit_config(orgId)


@router.put("/api/credits/admin/config")
async def admin_put_config(body: dict[str, Any], principal=Depends(require_hr), orgId: str = "default"):
    raise HTTPException(
        status_code=403,
        detail={
            "message": "Credits programme values are platform-standard and cannot be changed from the recruiter portal.",
            "errors": ["configurableByOrg=false"],
        },
    )

@router.get("/api/credits/admin/report")
async def admin_report(
    principal=Depends(require_hr),
    orgId: str = "default",
    days: int = Query(default=30, ge=1, le=365),
    export: bool = False,
):
    data = reporting(orgId, days=days)
    if not export:
        return data
    lines = [
        "metric,value",
        f"issued,{_csv_cell(data['issued'])}",
        f"redeemed,{_csv_cell(data['redeemed'])}",
        f"expired,{_csv_cell(data['expired'])}",
        f"outstanding,{_csv_cell(data['outstanding'])}",
    ]
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=credits-report.csv"},
    )


@router.get("/api/credits/admin/candidate/{candidate_id}")
async def admin_candidate(candidate_id: str, principal=Depends(require_hr)):
    return admin_candidate_ledger(candidate_id)


@router.post("/api/credits/admin/reconcile")
async def admin_reconcile(principal=Depends(require_hr), orgId: Optional[str] = None):
    expire_due_lots()
    return reconcile(orgId)


@router.post("/api/credits/admin/reverse")
async def admin_reverse(body: ReverseBody, principal=Depends(require_hr)):
    result = reverse_entry(
        body.entryId,
        actor={"type": "admin", "id": str(principal.subject)},
        reason=body.reason,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/api/credits/admin/award")
async def admin_award(body: AdminAwardBody, principal=Depends(require_hr)):
    if body.amount == 0:
        raise HTTPException(status_code=400, detail="Amount cannot be zero.")
    from app.credits.ledger import post_entry
    from app.credits.config import load_credit_config as _load

    config = _load()
    now = datetime.now(timezone.utc)
    event_id = f"admin:{body.candidateId}:{now.timestamp()}"
    try:
        entry = post_entry(
            candidate_id=body.candidateId,
            rule_id="admin_award" if body.amount > 0 else "admin_adjust",
            amount=body.amount,
            event_id=event_id,
            idempotency_key=event_id,
            actor={"type": "admin", "id": str(principal.subject)},
            label="Administrator adjustment",
            reason=body.reason,
            expires_at=now + timedelta(days=int(config.get("expiryDays") or 365))
            if body.amount > 0
            else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    from app.credits.ledger import serialize_entry
    from app.credits.service import _notify_candidate

    _notify_candidate(
        body.candidateId,
        f"An administrator adjusted your credits by {body.amount}. {body.reason}",
        "credits_admin",
    )
    return {
        "ok": True,
        "entry": serialize_entry(entry),
        "balance": derived_balance(body.candidateId),
    }


@router.post("/api/credits/admin/orders/{order_id}")
async def admin_order(order_id: str, body: OrderStatusBody, principal=Depends(require_hr)):
    from bson import ObjectId
    from app.database import get_db

    db = get_db()
    try:
        oid = ObjectId(order_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Order not found.")
    order = db["credit_orders"].find_one({"_id": oid})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    status = body.status.strip().lower()
    allowed = {"confirmed", "in_progress", "delivered", "cancelled"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Unknown order status.")
    if status == "cancelled":
        return refund_order(
            order_id,
            actor={"type": "admin", "id": str(principal.subject)},
            reason=body.reason or "Could not be fulfilled.",
        )
    updates: dict[str, Any] = {"status": status}
    if body.attachment:
        att = body.attachment.strip()
        if not att.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Attachment must be an http(s) URL.")
        updates["attachment"] = att
    if status == "delivered":
        updates["deliveredAt"] = datetime.now(timezone.utc)
    db["credit_orders"].update_one({"_id": order["_id"]}, {"$set": updates})
    return {"ok": True}


@router.get("/api/credits/admin/queue")
async def admin_queue(principal=Depends(require_hr)):
    from app.database import get_db

    docs = list(
        get_db()["credit_orders"]
        .find({"fulfilment": "manual", "status": {"$in": ["confirmed", "in_progress"]}})
        .sort("slaDueAt", 1)
    )
    from app.credits.service import _serialize_order

    return {"orders": [_serialize_order(d, for_admin=True) for d in docs]}


@router.post("/api/credits/admin/suspend")
async def admin_suspend(body: SuspendBody, principal=Depends(require_hr)):
    if body.resume:
        resume_earning(body.candidateId, actor_id=str(principal.subject))
    else:
        suspend_earning(body.candidateId, body.reason, actor_id=str(principal.subject))
    return {"ok": True}


def apply_referral_on_register(candidate_id: str, code: Optional[str], email: Optional[str] = None) -> None:
    if not code:
        return
    attribute_referral(candidate_id, code=code, email=email)
