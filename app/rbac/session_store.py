"""Redis-primary / Mongo-fallback opaque session store."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import timedelta
from typing import Any, Optional

from app.rbac import constants as C
from app.rbac.models import SessionPayload, utcnow


def _db():
    from app.database import get_db

    return get_db()


def _settings():
    from app.settings import get_settings

    return get_settings()

logger = logging.getLogger(__name__)


class SessionStore:
    """Opaque session:{id} → {user_id, org_id, issued_at, ...}."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._redis = None
        self._init_redis()

    def _init_redis(self) -> None:
        settings = _settings()
        if not settings.redis_url:
            self._redis = None
            return
        try:
            import redis

            client = redis.Redis.from_url(
                settings.redis_url, decode_responses=True
            )
            client.ping()
            self._redis = client
            logger.info("RBAC SessionStore: Redis connected")
        except Exception as exc:
            logger.warning(
                "RBAC SessionStore: Redis unavailable (%s); using Mongo fallback",
                exc,
            )
            self._redis = None

    def _ttl_seconds(self) -> int:
        settings = _settings()
        custom = int(getattr(settings, "rbac_session_ttl_seconds", 0) or 0)
        if custom > 0:
            return custom
        return int(settings.jwt_expire_minutes) * 60

    def _redis_key(self, session_id: str) -> str:
        return f"{C.REDIS_SESSION_PREFIX}{session_id}"

    def create(
        self,
        *,
        user_id: str,
        org_id: Optional[str] = None,
        membership_status: Optional[str] = None,
        email: Optional[str] = None,
    ) -> str:
        session_id = secrets.token_urlsafe(32)
        now = utcnow()
        payload: SessionPayload = {
            "user_id": str(user_id),
            "org_id": str(org_id) if org_id else None,
            "membership_status": membership_status,
            "issued_at": now.isoformat(),
            "email": (email or "").strip().lower() or None,
        }
        ttl = self._ttl_seconds()
        written = False
        if self._redis is not None:
            try:
                self._redis.setex(
                    self._redis_key(session_id), ttl, json.dumps(payload)
                )
                written = True
            except Exception as exc:
                logger.warning("Redis session setex failed: %s", exc)

        # Always mirror to Mongo so revoke/lookup works if Redis flaps.
        expires_at = now + timedelta(seconds=ttl)
        _db()[C.COL_RBAC_SESSIONS].update_one(
            {"session_id": session_id},
            {
                "$set": {
                    **payload,
                    "session_id": session_id,
                    "expires_at": expires_at,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        if not written:
            logger.debug("Session %s stored in Mongo fallback", session_id[:8])
        return session_id

    def get(self, session_id: str) -> Optional[dict[str, Any]]:
        if not session_id:
            return None
        if self._redis is not None:
            try:
                raw = self._redis.get(self._redis_key(session_id))
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        return data
            except Exception as exc:
                logger.warning("Redis session get failed: %s", exc)

        doc = _db()[C.COL_RBAC_SESSIONS].find_one({"session_id": session_id})
        if not doc:
            return None
        expires = doc.get("expires_at")
        if expires is not None:
            from datetime import datetime, timezone

            exp = expires
            if isinstance(exp, datetime) and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if isinstance(exp, datetime) and exp < utcnow():
                self.delete(session_id)
                return None
        return {
            "user_id": doc.get("user_id"),
            "org_id": doc.get("org_id"),
            "membership_status": doc.get("membership_status"),
            "issued_at": doc.get("issued_at"),
            "email": doc.get("email"),
        }

    def delete(self, session_id: str) -> bool:
        if not session_id:
            return False
        deleted = False
        if self._redis is not None:
            try:
                deleted = bool(self._redis.delete(self._redis_key(session_id)))
            except Exception as exc:
                logger.warning("Redis session delete failed: %s", exc)
        result = _db()[C.COL_RBAC_SESSIONS].delete_one(
            {"session_id": session_id}
        )
        return deleted or result.deleted_count > 0

    def backend(self) -> str:
        return "redis+mongo" if self._redis is not None else "mongo"


_store: Optional[SessionStore] = None
_store_lock = threading.Lock()


def get_session_store() -> SessionStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SessionStore()
    return _store


def reset_session_store_for_tests() -> None:
    """Clear singleton (unit tests)."""
    global _store
    with _store_lock:
        _store = None
