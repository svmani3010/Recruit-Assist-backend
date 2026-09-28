"""MongoDB Queryable Encryption helpers for candidate mobile numbers.

Uses ClientEncryption with Algorithm.INDEXED + QueryType.EQUALITY so:
  - ciphertext differs per encrypt (DB dump cannot correlate duplicates)
  - equality search still works for duplicate checks
  - plaintext is recoverable server-side for owner / authorized HR reveal

Keys: KEK in KMS (local master key for dev, AWS KMS for prod) wraps DEKs
stored in the MongoDB key vault collection. Never commit master keys.
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache
from typing import Any, Optional

from bson.binary import STANDARD, Binary
from bson.codec_options import CodecOptions
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.encryption import Algorithm, ClientEncryption, QueryType
from pymongo.encryption_options import AutoEncryptionOpts
from pymongo.errors import EncryptionError

from app.database import get_db, get_mongo_client
from app.settings import get_settings

logger = logging.getLogger(__name__)

KEY_ALT_NAME_DEFAULT = "candidate-mobile-v1"
MOBILE_COLLECTION_DEFAULT = "candidate_mobiles"
META_COLLECTION = "mobile_qe_meta"
CONTENTION_FACTOR = 0


class MobileCryptoError(RuntimeError):
    """Raised when encryption / decryption / QE setup fails."""


def _codec_options() -> CodecOptions:
    return CodecOptions(uuid_representation=STANDARD)


def key_vault_namespace() -> str:
    settings = get_settings()
    if settings.mobile_qe_key_vault_namespace:
        return settings.mobile_qe_key_vault_namespace.strip()
    return f"{settings.mongodb_db}.__keyVault"


def key_alt_name() -> str:
    settings = get_settings()
    return (settings.mobile_qe_key_alt_name or KEY_ALT_NAME_DEFAULT).strip()


def mobiles_collection_name() -> str:
    settings = get_settings()
    return (settings.mobile_qe_collection or MOBILE_COLLECTION_DEFAULT).strip()


def is_mobile_qe_ready() -> bool:
    settings = get_settings()
    if not settings.mobile_qe_enabled:
        return False
    provider = (settings.mobile_qe_kms_provider or "local").strip().lower()
    if provider == "local":
        raw = (settings.mobile_qe_local_master_key or "").strip()
        if not raw:
            return False
        try:
            return len(base64.b64decode(raw)) == 96
        except Exception:
            return False
    if provider == "aws":
        return bool((settings.aws_kms_key_arn or "").strip())
    return False


def _kms_providers() -> dict[str, Any]:
    settings = get_settings()
    provider = (settings.mobile_qe_kms_provider or "local").strip().lower()
    if provider == "local":
        raw = (settings.mobile_qe_local_master_key or "").strip()
        try:
            key = base64.b64decode(raw)
        except Exception as exc:
            raise MobileCryptoError(
                "MOBILE_QE_LOCAL_MASTER_KEY is not valid base64."
            ) from exc
        if len(key) != 96:
            raise MobileCryptoError(
                "MOBILE_QE_LOCAL_MASTER_KEY must decode to exactly 96 bytes."
            )
        return {"local": {"key": key}}

    if provider == "aws":
        providers: dict[str, Any] = {"aws": {}}
        access = (settings.aws_access_key_id or "").strip()
        secret = (settings.aws_secret_access_key or "").strip()
        if access and secret:
            providers["aws"] = {
                "accessKeyId": access,
                "secretAccessKey": secret,
            }
            session = (settings.aws_session_token or "").strip()
            if session:
                providers["aws"]["sessionToken"] = session
        return providers

    raise MobileCryptoError(
        f"Unsupported MOBILE_QE_KMS_PROVIDER={provider!r}. Use 'local' or 'aws'."
    )


def _kms_provider_name() -> str:
    return (get_settings().mobile_qe_kms_provider or "local").strip().lower()


def _aws_master_key() -> dict[str, str]:
    settings = get_settings()
    arn = (settings.aws_kms_key_arn or "").strip()
    if not arn:
        raise MobileCryptoError("AWS_KMS_KEY_ARN is required for aws KMS provider.")
    region = (settings.aws_kms_region or "").strip()
    if not region and ":" in arn:
        # arn:aws:kms:REGION:...
        parts = arn.split(":")
        if len(parts) > 3:
            region = parts[3]
    if not region:
        raise MobileCryptoError("AWS_KMS_REGION is required for aws KMS provider.")
    return {"region": region, "key": arn}


@lru_cache
def get_client_encryption() -> ClientEncryption:
    if not is_mobile_qe_ready():
        raise MobileCryptoError(
            "Mobile Queryable Encryption is not configured "
            "(set MOBILE_QE_ENABLED and KMS master key)."
        )
    return ClientEncryption(
        _kms_providers(),
        key_vault_namespace(),
        get_mongo_client(),
        _codec_options(),
    )


def reset_client_encryption_cache() -> None:
    """Test helper — drop cached ClientEncryption / QE clients after settings changes."""
    get_client_encryption.cache_clear()
    get_qe_mongo_client.cache_clear()


@lru_cache
def get_qe_mongo_client() -> MongoClient:
    """MongoClient that can write Indexed ciphertext + __safeContent__ for QE.

    Uses bypass_query_analysis so explicit encryption works without crypt_shared /
    mongocryptd. Automatic decryption still applies on reads.
    """
    if not is_mobile_qe_ready():
        raise MobileCryptoError(
            "Mobile Queryable Encryption is not configured "
            "(set MOBILE_QE_ENABLED and KMS master key)."
        )
    settings = get_settings()
    opts = AutoEncryptionOpts(
        _kms_providers(),
        key_vault_namespace(),
        bypass_query_analysis=True,
        key_vault_client=get_mongo_client(),
    )
    return MongoClient(
        settings.mongodb_uri,
        auto_encryption_opts=opts,
        maxPoolSize=settings.mongodb_max_pool_size,
        minPoolSize=max(1, settings.mongodb_min_pool_size // 2),
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=10000,
        socketTimeoutMS=30000,
        retryWrites=True,
        appname="screening-assist-qe",
    )


def get_mobiles_collection() -> Collection:
    """QE-enabled candidate_mobiles collection (encrypt writes / decrypt reads)."""
    settings = get_settings()
    return get_qe_mongo_client()[settings.mongodb_db][mobiles_collection_name()]


def ensure_key_vault_index() -> None:
    db_name, coll_name = key_vault_namespace().split(".", 1)
    coll = get_mongo_client()[db_name][coll_name]
    try:
        coll.create_index(
            "keyAltNames",
            unique=True,
            partialFilterExpression={"keyAltNames": {"$exists": True}},
        )
    except Exception as exc:
        # Index may already exist with an equivalent/legacy partial filter.
        msg = str(exc)
        if "IndexKeySpecsConflict" in msg or "already exists" in msg.lower():
            logger.debug("key vault index already present: %s", type(exc).__name__)
            return
        raise


def ensure_data_key() -> Binary:
    """Return the DEK Binary _id for the configured keyAltName, creating if needed."""
    ensure_key_vault_index()
    ce = get_client_encryption()
    alt = key_alt_name()
    db_name, coll_name = key_vault_namespace().split(".", 1)
    existing = get_mongo_client()[db_name][coll_name].find_one({"keyAltNames": alt})
    if existing and existing.get("_id") is not None:
        return existing["_id"]

    provider = _kms_provider_name()
    master_key = _aws_master_key() if provider == "aws" else None
    try:
        key_id = ce.create_data_key(
            provider,
            key_alt_names=[alt],
            master_key=master_key,
        )
    except EncryptionError as exc:
        # Race: another process created it
        existing = get_mongo_client()[db_name][coll_name].find_one({"keyAltNames": alt})
        if existing and existing.get("_id") is not None:
            return existing["_id"]
        raise MobileCryptoError(f"Failed to create mobile data key: {exc}") from exc
    logger.info("Created mobile QE data key altName=%s version=%s", alt, key_version())
    return key_id


def key_version() -> int:
    return int(get_settings().mobile_qe_key_version or 1)


def get_mobile_field_key_id() -> Binary:
    """Prefer the keyId declared on the QE collection; else vault DEK by alt name."""
    db = get_db()
    name = mobiles_collection_name()
    try:
        info = db.command("listCollections", filter={"name": name})
        batch = info.get("cursor", {}).get("firstBatch") or []
        if batch:
            fields = (
                (batch[0].get("options") or {})
                .get("encryptedFields", {})
                .get("fields")
                or []
            )
            for field in fields:
                if field.get("path") == "mobile" and field.get("keyId") is not None:
                    return field["keyId"]
    except Exception:
        logger.debug("Could not read encryptedFields keyId; falling back to vault", exc_info=True)
    return ensure_data_key()


def encrypt_mobile_indexed(e164: str) -> Binary:
    """Encrypt E.164 for storage (randomized ciphertext, equality-queryable)."""
    if not e164 or not str(e164).startswith("+"):
        raise MobileCryptoError("encrypt_mobile_indexed requires E.164 input.")
    ce = get_client_encryption()
    key_id = get_mobile_field_key_id()
    try:
        return ce.encrypt(
            str(e164),
            Algorithm.INDEXED,
            key_id=key_id,
            contention_factor=CONTENTION_FACTOR,
        )
    except EncryptionError as exc:
        raise MobileCryptoError("Failed to encrypt mobile number.") from exc


def encrypt_mobile_equality_query(e164: str) -> Binary:
    """Encrypt E.164 for an equality find (duplicate check)."""
    if not e164 or not str(e164).startswith("+"):
        raise MobileCryptoError("encrypt_mobile_equality_query requires E.164 input.")
    ce = get_client_encryption()
    key_id = get_mobile_field_key_id()
    try:
        return ce.encrypt(
            str(e164),
            Algorithm.INDEXED,
            key_id=key_id,
            query_type=QueryType.EQUALITY,
            contention_factor=CONTENTION_FACTOR,
        )
    except EncryptionError as exc:
        raise MobileCryptoError("Failed to build mobile equality query.") from exc


def decrypt_mobile(ciphertext: Any) -> str:
    """Decrypt a stored QE Binary back to E.164 plaintext."""
    if ciphertext is None:
        raise MobileCryptoError("Missing encrypted mobile.")
    ce = get_client_encryption()
    try:
        value = ce.decrypt(ciphertext)
    except EncryptionError as exc:
        raise MobileCryptoError("Failed to decrypt mobile number.") from exc
    text = value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)
    return text


def ensure_encrypted_mobiles_collection() -> None:
    """Create the QE-enabled candidate_mobiles collection if it does not exist."""
    if not is_mobile_qe_ready():
        return

    ensure_key_vault_index()
    key_id = ensure_data_key()
    db = get_db()
    name = mobiles_collection_name()

    if name in db.list_collection_names():
        db[name].create_index("candidate_id", unique=True)
        db[name].create_index([("verified", 1), ("candidate_id", 1)])
        try:
            from app.services.candidate_mobile_identity import ensure_mobile_uniqueness_indexes

            ensure_mobile_uniqueness_indexes()
        except Exception:
            logger.exception("Deferred mobile uniqueness index setup failed")
        return

    encrypted_fields = {
        "fields": [
            {
                "path": "mobile",
                "bsonType": "string",
                "keyId": key_id,
                "queries": {"queryType": "equality", "contention": CONTENTION_FACTOR},
            }
        ]
    }
    ce = get_client_encryption()
    provider = _kms_provider_name()
    master_key = _aws_master_key() if provider == "aws" else None
    try:
        _coll, ef = ce.create_encrypted_collection(
            db,
            name,
            encrypted_fields=encrypted_fields,
            kms_provider=provider,
            master_key=master_key,
        )
        db[META_COLLECTION].update_one(
            {"_id": "candidate_mobiles"},
            {
                "$set": {
                    "collection": name,
                    "encryptedFields": ef,
                    "keyAltName": key_alt_name(),
                    "keyVersion": key_version(),
                }
            },
            upsert=True,
        )
        logger.info("Created QE collection %s", name)
    except Exception as exc:
        # Collection may have been created concurrently
        if name in db.list_collection_names():
            logger.warning(
                "QE collection create raced or partially existed: %s", type(exc).__name__
            )
        else:
            raise MobileCryptoError(
                f"Failed to create encrypted mobiles collection: {exc}"
            ) from exc

    db[name].create_index("candidate_id", unique=True)
    db[name].create_index([("verified", 1), ("candidate_id", 1)])
    try:
        from app.services.candidate_mobile_identity import ensure_mobile_uniqueness_indexes

        ensure_mobile_uniqueness_indexes()
    except Exception:
        logger.exception("Deferred mobile uniqueness index setup failed")


def mask_e164(e164: str) -> str:
    """UI-safe mask: keep country-ish prefix hint and last 4 digits."""
    digits = "".join(ch for ch in str(e164 or "") if ch.isdigit())
    if len(digits) < 4:
        return "••••"
    last4 = digits[-4:]
    return f"+••••••••{last4}"
