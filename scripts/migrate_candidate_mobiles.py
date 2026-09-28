"""One-time migration: plaintext candidate.phone → MongoDB Queryable Encryption.

Usage (staging first):
  python -m scripts.migrate_candidate_mobiles --dry-run
  python -m scripts.migrate_candidate_mobiles --apply
  python -m scripts.migrate_candidate_mobiles --apply --unset-plaintext

Rollback: restore from backup; encrypted rows live in candidate_mobiles and
can be deleted with --rollback-delete-qe (does not restore plaintext).
"""

from __future__ import annotations

import argparse
import logging
import sys

from bson import ObjectId

from app.database import get_db
from app.mobile_crypto import (
    ensure_encrypted_mobiles_collection,
    is_mobile_qe_ready,
    mask_e164,
)
from app.mobile_phone import MobileValidationError, require_mobile_e164
from app.services.candidate_mobile_identity import upsert_encrypted_mobile

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_candidate_mobiles")


def _iter_plaintext_candidates():
    return get_db()["candidates"].find(
        {
            "phone": {"$exists": True, "$type": "string", "$nin": [None, ""]},
        },
        {"phone": 1, "phoneCountry": 1, "phoneDialCode": 1, "mobile_verified": 1},
    )


def migrate(*, apply: bool, unset_plaintext: bool) -> int:
    if not is_mobile_qe_ready():
        logger.error("Mobile QE is not configured. Set MOBILE_QE_LOCAL_MASTER_KEY or AWS KMS.")
        return 2

    ensure_encrypted_mobiles_collection()
    migrated = 0
    skipped = 0
    errors = 0

    for doc in _iter_plaintext_candidates():
        cid = str(doc["_id"])
        raw = doc.get("phone") or ""
        try:
            e164 = require_mobile_e164(
                raw,
                region=doc.get("phoneCountry"),
                dial_code=doc.get("phoneDialCode"),
                national_only=False,
            )
        except MobileValidationError:
            logger.warning("skip invalid phone candidate_id=%s", cid)
            skipped += 1
            continue

        verified = bool(doc.get("mobile_verified") or doc.get("mobileVerified"))
        if not apply:
            logger.info(
                "dry-run candidate_id=%s verified=%s masked=%s",
                cid,
                verified,
                mask_e164(e164),
            )
            migrated += 1
            continue

        try:
            fields = upsert_encrypted_mobile(cid, e164, verified=verified)
            update: dict = {"$set": fields}
            if unset_plaintext:
                update["$unset"] = {"phone": ""}
                update["$set"].pop("phone", None)
            get_db()["candidates"].update_one({"_id": ObjectId(cid)}, update)
            migrated += 1
        except Exception:
            logger.exception("failed candidate_id=%s", cid)
            errors += 1

    logger.info(
        "done migrated=%s skipped=%s errors=%s apply=%s unset_plaintext=%s",
        migrated,
        skipped,
        errors,
        apply,
        unset_plaintext,
    )
    return 1 if errors else 0


def rollback_delete_qe() -> int:
    from app.mobile_crypto import mobiles_collection_name

    result = get_db()[mobiles_collection_name()].delete_many({})
    logger.warning(
        "Deleted %s QE mobile rows (plaintext not restored)", result.deleted_count
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only (default)")
    parser.add_argument("--apply", action="store_true", help="Write encrypted fields")
    parser.add_argument(
        "--unset-plaintext",
        action="store_true",
        help="Remove candidates.phone after encrypt (only with --apply)",
    )
    parser.add_argument(
        "--rollback-delete-qe",
        action="store_true",
        help="Delete all rows in candidate_mobiles (no plaintext restore)",
    )
    args = parser.parse_args(argv)

    if args.rollback_delete_qe:
        return rollback_delete_qe()

    apply = bool(args.apply)
    if not apply:
        args.dry_run = True
    return migrate(apply=apply, unset_plaintext=bool(args.unset_plaintext and apply))


if __name__ == "__main__":
    sys.exit(main())
