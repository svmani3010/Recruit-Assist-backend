"""Candidate mobile identity — store, duplicate-check, decrypt, release (KV05 / KV08).

When MongoDB Queryable Encryption is configured:
  - plaintext is never written to the candidates collection
  - equality checks use encrypt(query_type=EQUALITY) only
  - account deletion / release removes the QE doc so the number can be reused
  - phone_hash enables DB-level uniqueness for verified mobiles (KV08)

When QE is not configured, falls back to legacy plaintext phone field
(so local/dev without keys still works).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.database import get_db
from app.mobile_crypto import (
    MobileCryptoError,
    decrypt_mobile,
    encrypt_mobile_equality_query,
    encrypt_mobile_indexed,
    get_mobiles_collection,
    is_mobile_qe_ready,
    key_alt_name,
    key_version,
    mask_e164,
    mobiles_collection_name,
)
from app.mobile_phone import MobileValidationError, require_mobile_e164, split_e164

logger = logging.getLogger(__name__)

# AC2 — active-account duplicate (KV05). Never include PII.
DUPLICATE_DETAIL = (
    "An account already exists with this mobile number. "
    "Please sign in to that account, or use a different mobile number."
)
OWN_VERIFIED_DETAIL = "This number is already verified on your account."
REACTIVATION_DETAIL = (
    "This mobile number belongs to a deactivated account. "
    "You can reactivate that account to continue."
)
UNVERIFIED_STALE_HOURS = 24


class MobileClaimStatus(str, Enum):
    AVAILABLE = "available"
    OWN_VERIFIED = "own_verified"
    DUPLICATE_ACTIVE = "duplicate_active"
    REACTIVATION_AVAILABLE = "reactivation_available"


@dataclass
class MobileClaimResult:
    status: MobileClaimStatus
    message: str
    owner_candidate_id: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _candidates():
    return get_db()["candidates"]


def _mobiles():
    """Use QE client when configured so Indexed writes include __safeContent__."""
    if is_mobile_qe_ready():
        return get_mobiles_collection()
    return get_db()[mobiles_collection_name()]


def _oid(candidate_id: str) -> ObjectId:
    try:
        return ObjectId(candidate_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid candidate id.") from exc


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def phone_index_key(phone_e164: str) -> str:
    """HMAC of E.164 for indexes / audit (not reversible)."""
    from app.settings import get_settings

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


def mask_email_hint(email: str) -> str:
    """KV06-only masked hint, e.g. s••••m@f••••o.com — never return on KV05."""
    text = str(email or "").strip().lower()
    if "@" not in text:
        return "••••@••••.com"
    local, domain = text.split("@", 1)

    def _mask_token(part: str) -> str:
        chars = part.strip()
        if len(chars) <= 1:
            return "•" * max(1, len(chars) or 1)
        if len(chars) == 2:
            return f"{chars[0]}•"
        bullets = "•" * min(4, max(2, len(chars) - 2))
        return f"{chars[0]}{bullets}{chars[-1]}"

    if "." in domain:
        name, tld = domain.rsplit(".", 1)
        return f"{_mask_token(local)}@{_mask_token(name)}.{tld}"
    return f"{_mask_token(local)}@{_mask_token(domain)}"


def normalize_mobile(
    raw: str,
    *,
    region: Optional[str] = None,
    dial_code: Optional[str] = None,
) -> str:
    try:
        return require_mobile_e164(
            raw, region=region, dial_code=dial_code, national_only=True
        )
    except MobileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def is_candidate_inactive(doc: Optional[dict]) -> bool:
    """Soft-deleted / deactivated accounts → KV06 reactivation, not hard block."""
    if not doc:
        return True
    if doc.get("softDeleted") or doc.get("soft_deleted"):
        return True
    if doc.get("deactivated") or doc.get("isDeactivated"):
        return True
    if doc.get("deleted") or doc.get("isDeleted"):
        return True
    status = str(doc.get("accountStatus") or doc.get("status") or "").strip().lower()
    if status in {"deleted", "soft_deleted", "deactivated", "inactive", "disabled"}:
        return True
    return False


def _candidate_doc(candidate_id: str) -> Optional[dict]:
    try:
        return _candidates().find_one({"_id": _oid(candidate_id)})
    except HTTPException:
        return None


def _legacy_find_verified_owner(candidate_id: str, e164: str) -> Optional[str]:
    digits = _digits(e164)
    cursor = _candidates().find(
        {
            "_id": {"$ne": _oid(candidate_id)},
            "mobile_verified": True,
            "phone": {"$exists": True, "$nin": [None, ""]},
        },
        {"phone": 1, "softDeleted": 1, "soft_deleted": 1, "deactivated": 1,
         "deleted": 1, "accountStatus": 1, "status": 1, "isDeleted": 1, "isDeactivated": 1},
    )
    for doc in cursor:
        if _digits(doc.get("phone")) == digits:
            return str(doc["_id"])
    return None


def _qe_find_verified_owner(candidate_id: str, e164: str) -> Optional[str]:
    query_cipher = encrypt_mobile_equality_query(e164)
    hit = _mobiles().find_one(
        {
            "mobile": query_cipher,
            "verified": True,
            "candidate_id": {"$ne": _oid(candidate_id)},
        },
        {"candidate_id": 1},
    )
    if not hit:
        return None
    return str(hit.get("candidate_id") or "")


def find_verified_owner_id(candidate_id: str, e164: str) -> Optional[str]:
    if is_mobile_qe_ready():
        try:
            owner = _qe_find_verified_owner(candidate_id, e164)
            if owner:
                return owner
        except MobileCryptoError:
            logger.exception("QE duplicate check failed; refusing write")
            raise HTTPException(
                status_code=503,
                detail="Mobile encryption service temporarily unavailable.",
            )
        # Migration: also check leftover plaintext verified phones
        return _legacy_find_verified_owner(candidate_id, e164)
    return _legacy_find_verified_owner(candidate_id, e164)


def candidate_owns_verified_mobile(candidate_id: str, e164: str) -> bool:
    doc = _candidate_doc(candidate_id)
    if not doc or not (doc.get("mobile_verified") or doc.get("mobileVerified")):
        return False
    own = get_plaintext_mobile(candidate_id) or str(doc.get("phone") or "")
    if not own:
        return False
    return _digits(own) == _digits(e164)


def resolve_mobile_claim(candidate_id: str, e164: str) -> MobileClaimResult:
    """KV05 / AC1–AC6 — classify mobile ownership without leaking PII."""
    purge_expired_unverified_bindings()

    if candidate_owns_verified_mobile(candidate_id, e164):
        return MobileClaimResult(
            status=MobileClaimStatus.OWN_VERIFIED,
            message=OWN_VERIFIED_DETAIL,
            owner_candidate_id=candidate_id,
        )

    owner_id = find_verified_owner_id(candidate_id, e164)
    if not owner_id:
        return MobileClaimResult(
            status=MobileClaimStatus.AVAILABLE,
            message="Mobile number is available.",
        )

    owner = _candidate_doc(owner_id)
    if is_candidate_inactive(owner):
        return MobileClaimResult(
            status=MobileClaimStatus.REACTIVATION_AVAILABLE,
            message=REACTIVATION_DETAIL,
            owner_candidate_id=owner_id,
        )

    return MobileClaimResult(
        status=MobileClaimStatus.DUPLICATE_ACTIVE,
        message=DUPLICATE_DETAIL,
        owner_candidate_id=owner_id,
    )


def mobile_is_taken(candidate_id: str, e164: str) -> bool:
    """True when another *active* verified account already owns this number."""
    return resolve_mobile_claim(candidate_id, e164).status == MobileClaimStatus.DUPLICATE_ACTIVE


def audit_duplicate_attempt(
    *,
    candidate_id: str,
    e164: str,
    ip: str,
    outcome: str,
    owner_candidate_id: str = "",
) -> None:
    """AC9 — every duplicate / claim check for fraud review."""
    get_db()["mobile_duplicate_audit"].insert_one(
        {
            "phone_hash": phone_index_key(e164),
            "attempting_account_id": candidate_id,
            "owner_candidate_id": owner_candidate_id or "",
            "outcome": outcome,
            "ip": ip or "unknown",
            "createdAt": _now(),
        }
    )


def assert_unique_mobile(
    candidate_id: str,
    e164: str,
    *,
    ip: str = "",
    audit: bool = True,
) -> MobileClaimResult:
    """Constant-shaped duplicate check. Raises 409 for active duplicates only."""
    started = time.perf_counter()
    claim = resolve_mobile_claim(candidate_id, e164)
    elapsed = time.perf_counter() - started
    min_seconds = 0.05
    if elapsed < min_seconds:
        time.sleep(min_seconds - elapsed)

    if claim.status == MobileClaimStatus.DUPLICATE_ACTIVE:
        if audit:
            audit_duplicate_attempt(
                candidate_id=candidate_id,
                e164=e164,
                ip=ip,
                outcome="duplicate_active",
                owner_candidate_id=claim.owner_candidate_id,
            )
        raise HTTPException(
            status_code=409,
            detail={
                "message": DUPLICATE_DETAIL,
                "errorCode": "duplicate_active",
                "status": MobileClaimStatus.DUPLICATE_ACTIVE.value,
            },
        )

    if claim.status == MobileClaimStatus.REACTIVATION_AVAILABLE:
        if audit:
            audit_duplicate_attempt(
                candidate_id=candidate_id,
                e164=e164,
                ip=ip,
                outcome="reactivation_available",
                owner_candidate_id=claim.owner_candidate_id,
            )
        raise HTTPException(
            status_code=409,
            detail={
                "message": REACTIVATION_DETAIL,
                "errorCode": "reactivation_available",
                "status": MobileClaimStatus.REACTIVATION_AVAILABLE.value,
            },
        )

    return claim


def claim_status_payload(claim: MobileClaimResult) -> dict[str, Any]:
    """Safe API shape for check-mobile — never includes email/name (AC3)."""
    return {
        "status": claim.status.value,
        "available": claim.status
        in {MobileClaimStatus.AVAILABLE, MobileClaimStatus.OWN_VERIFIED},
        "ownVerified": claim.status == MobileClaimStatus.OWN_VERIFIED,
        "reactivationAvailable": claim.status
        == MobileClaimStatus.REACTIVATION_AVAILABLE,
        "detail": claim.message,
        "message": claim.message,
    }


def reactivation_preview_for_number(
    *,
    candidate_id: str,
    e164: str,
) -> dict[str, Any]:
    """KV06 — masked email hint only after claim is reactivation_available."""
    claim = resolve_mobile_claim(candidate_id, e164)
    if claim.status != MobileClaimStatus.REACTIVATION_AVAILABLE:
        raise HTTPException(
            status_code=400,
            detail="Account reactivation is not available for this number.",
        )
    owner = _candidate_doc(claim.owner_candidate_id) or {}
    email = str(owner.get("email") or "").strip()
    return {
        "status": MobileClaimStatus.REACTIVATION_AVAILABLE.value,
        "message": REACTIVATION_DETAIL,
        "registeredHint": mask_email_hint(email) if email else "••••@••••.com",
        # Never return full email / name
    }


def purge_expired_unverified_bindings() -> int:
    """AC5 — stale unverified bindings expire after 24 hours."""
    cutoff = _now() - timedelta(hours=UNVERIFIED_STALE_HOURS)
    released = 0
    if is_mobile_qe_ready():
        expired = list(
            _mobiles().find(
                {
                    "verified": False,
                    "$or": [
                        {"unverified_expires_at": {"$lte": _now()}},
                        {
                            "unverified_expires_at": {"$exists": False},
                            "created_at": {"$lte": cutoff},
                        },
                    ],
                },
                {"candidate_id": 1},
            )
        )
        for row in expired:
            cid = str(row.get("candidate_id") or "")
            if cid:
                release_candidate_mobile(cid)
                released += 1
    # Legacy plaintext: clear unverified phones older than 24h
    legacy = list(
        _candidates().find(
            {
                "mobile_verified": {"$ne": True},
                "phone": {"$exists": True, "$nin": [None, ""]},
                "mobile_unverified_at": {"$lte": cutoff},
            },
            {"_id": 1},
        )
    )
    for doc in legacy:
        release_candidate_mobile(str(doc["_id"]))
        released += 1
    return released


def release_other_unverified_claims(candidate_id: str, e164: str) -> None:
    """AC5 — on bind/verify, release stale unverified claims for the same E.164."""
    if is_mobile_qe_ready():
        try:
            query_cipher = encrypt_mobile_equality_query(e164)
            hits = list(
                _mobiles().find(
                    {
                        "mobile": query_cipher,
                        "verified": False,
                        "candidate_id": {"$ne": _oid(candidate_id)},
                    },
                    {"candidate_id": 1},
                )
            )
            for hit in hits:
                cid = str(hit.get("candidate_id") or "")
                if cid:
                    release_candidate_mobile(cid)
        except MobileCryptoError:
            logger.exception("Failed releasing unverified QE claims")
            return

    digits = _digits(e164)
    cursor = _candidates().find(
        {
            "_id": {"$ne": _oid(candidate_id)},
            "mobile_verified": {"$ne": True},
            "phone": {"$exists": True, "$nin": [None, ""]},
        },
        {"phone": 1},
    )
    for doc in cursor:
        if _digits(doc.get("phone")) == digits:
            release_candidate_mobile(str(doc["_id"]))


def upsert_encrypted_mobile(
    candidate_id: str,
    e164: str,
    *,
    verified: bool,
) -> dict[str, Any]:
    """Write / update QE mobile row and return candidate $set fields (no plaintext)."""
    region, dial, _ = split_e164(e164)
    masked = mask_e164(e164)
    phone_hash = phone_index_key(e164)
    fields: dict[str, Any] = {
        "phoneCountry": region,
        "phoneDialCode": dial,
        "phone_masked": masked,
        "phoneMasked": masked,
        "mobile_key_version": key_version(),
        "mobile_key_alt_name": key_alt_name(),
        "mobile_verified": bool(verified),
        "mobile_phone_hash": phone_hash,
    }

    # AC5 — unverified binding is free; release other unverified holds for this number
    release_other_unverified_claims(candidate_id, e164)

    if not is_mobile_qe_ready():
        fields["phone"] = e164
        if verified:
            fields["mobile_unverified_at"] = None
        else:
            fields["mobile_unverified_at"] = _now()
        return fields

    ciphertext = encrypt_mobile_indexed(e164)
    oid = _oid(candidate_id)
    coll = _mobiles()
    existing = coll.find_one({"candidate_id": oid})
    now = _now()
    row = {
        "candidate_id": oid,
        "mobile": ciphertext,
        "phone_hash": phone_hash,
        "verified": bool(verified),
        "key_version": key_version(),
        "key_alt_name": key_alt_name(),
        "updated_at": now,
        "unverified_expires_at": (
            None if verified else now + timedelta(hours=UNVERIFIED_STALE_HOURS)
        ),
    }
    if existing:
        row["_id"] = existing["_id"]
        row["created_at"] = existing.get("created_at") or now
        coll.replace_one({"_id": existing["_id"]}, row)
    else:
        row["created_at"] = now
        try:
            coll.insert_one(row)
        except Exception as exc:
            # KV08 — unique verified phone_hash collision
            if "duplicate key" in str(exc).lower() or getattr(exc, "code", None) == 11000:
                raise HTTPException(status_code=409, detail={
                    "message": DUPLICATE_DETAIL,
                    "errorCode": "duplicate_active",
                    "status": MobileClaimStatus.DUPLICATE_ACTIVE.value,
                }) from exc
            raise

    if verified:
        _reconcile_duplicate_mobile_claims(candidate_id, e164)

    fields["phone"] = ""
    return fields


def _reconcile_duplicate_mobile_claims(candidate_id: str, e164: str) -> None:
    """If a race created two verified rows for one number, keep earliest and release rest."""
    query_cipher = encrypt_mobile_equality_query(e164)
    hits = list(
        _mobiles()
        .find({"mobile": query_cipher, "verified": True})
        .sort([("created_at", 1)])
    )
    # Also drop any leftover unverified rows for the same number
    release_other_unverified_claims(candidate_id, e164)

    if len(hits) <= 1:
        return
    keeper = hits[0]
    for hit in hits[1:]:
        cid = str(hit.get("candidate_id") or "")
        if not cid:
            continue
        release_candidate_mobile(cid)
        logger.warning(
            "Released duplicate verified mobile claim candidate_id=%s keeper=%s",
            cid,
            keeper.get("candidate_id"),
        )
    if str(keeper.get("candidate_id")) != str(_oid(candidate_id)):
        raise HTTPException(
            status_code=409,
            detail={
                "message": DUPLICATE_DETAIL,
                "errorCode": "duplicate_active",
                "status": MobileClaimStatus.DUPLICATE_ACTIVE.value,
            },
        )


def mark_mobile_verified(candidate_id: str) -> None:
    if not is_mobile_qe_ready():
        return
    _mobiles().update_one(
        {"candidate_id": _oid(candidate_id)},
        {
            "$set": {
                "verified": True,
                "unverified_expires_at": None,
                "updated_at": _now(),
            }
        },
    )


def release_candidate_mobile(candidate_id: str) -> None:
    """Purge encrypted mobile so the number can be claimed again (account delete)."""
    if is_mobile_qe_ready():
        _mobiles().delete_one({"candidate_id": _oid(candidate_id)})
    _candidates().update_one(
        {"_id": _oid(candidate_id)},
        {
            "$unset": {
                "phone": "",
                "phone_masked": "",
                "phoneMasked": "",
                "mobile_key_version": "",
                "mobile_key_alt_name": "",
                "mobile_phone_hash": "",
                "mobile_unverified_at": "",
            },
            "$set": {"mobile_verified": False},
        },
    )


def get_plaintext_mobile(candidate_id: str) -> Optional[str]:
    """Decrypt for owner flows / HR reveal. Never log the return value."""
    oid = _oid(candidate_id)
    if is_mobile_qe_ready():
        row = _mobiles().find_one({"candidate_id": oid}, {"mobile": 1})
        if row and row.get("mobile") is not None:
            value = row["mobile"]
            if isinstance(value, str):
                return value.strip() or None
            try:
                return decrypt_mobile(value)
            except MobileCryptoError:
                logger.exception(
                    "Decrypt failed for candidate_id=%s", candidate_id
                )
                raise HTTPException(
                    status_code=503,
                    detail="Unable to decrypt mobile number.",
                )
    doc = _candidates().find_one({"_id": oid}, {"phone": 1})
    phone = (doc or {}).get("phone")
    return str(phone).strip() if phone else None


def get_masked_mobile(candidate_id: str, doc: Optional[dict] = None) -> str:
    candidate = doc or _candidates().find_one(
        {"_id": _oid(candidate_id)},
        {"phone_masked": 1, "phoneMasked": 1, "phone": 1},
    )
    if not candidate:
        return ""
    masked = candidate.get("phone_masked") or candidate.get("phoneMasked")
    if masked:
        return str(masked)
    phone = candidate.get("phone")
    if phone:
        return mask_e164(str(phone))
    return ""


def resolve_phone_for_candidate_self(candidate_id: str, doc: dict) -> str:
    """Candidate viewing own profile — return plaintext when available."""
    plaintext = get_plaintext_mobile(candidate_id)
    if plaintext:
        return plaintext
    return str(doc.get("phone") or "")


def audit_mobile_reveal(
    *,
    hr_subject: str,
    hr_email: str,
    candidate_id: str,
    job_id: Optional[str] = None,
) -> None:
    get_db()["mobile_reveal_audit"].insert_one(
        {
            "hr_subject": hr_subject,
            "hr_email": hr_email,
            "candidate_id": candidate_id,
            "job_id": job_id,
            "action": "reveal_mobile",
            "created_at": _now(),
        }
    )


def ensure_mobile_uniqueness_indexes() -> None:
    """KV08 — DB-level uniqueness for verified mobile hashes."""
    db = get_db()
    name = mobiles_collection_name()
    try:
        db[name].create_index("candidate_id", unique=True)
        db[name].create_index([("verified", 1), ("candidate_id", 1)])
        db[name].create_index(
            [("phone_hash", 1)],
            unique=True,
            partialFilterExpression={
                "verified": True,
                "phone_hash": {"$type": "string"},
            },
            name="uniq_verified_phone_hash",
        )
    except Exception:
        logger.exception("Failed creating mobile uniqueness indexes on %s", name)

    try:
        db["candidates"].create_index(
            [("mobile_phone_hash", 1)],
            unique=True,
            partialFilterExpression={
                "mobile_verified": True,
                "mobile_phone_hash": {"$type": "string"},
            },
            name="uniq_verified_candidate_mobile_hash",
        )
    except Exception:
        logger.exception("Failed creating candidate mobile_phone_hash unique index")
