"""Short-TTL role cache with immediate invalidation (Redis → Mongo)."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Optional

from app.rbac import constants as C
from app.rbac.models import utcnow

logger = logging.getLogger(__name__)


def _db():
    from app.database import get_db

    return get_db()


def _settings():
    from app.settings import get_settings

    return get_settings()


class RoleCache:
    def __init__(self) -> None:
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
        except Exception as exc:
            logger.warning("RBAC RoleCache: Redis unavailable (%s)", exc)
            self._redis = None

    def _ttl(self) -> int:
        settings = _settings()
        return int(
            getattr(settings, "rbac_role_cache_ttl_seconds", None)
            or C.DEFAULT_ROLE_CACHE_TTL_SECONDS
        )

    def _key(self, user_id: str, org_id: str, module: str) -> str:
        return f"{C.REDIS_ROLE_PREFIX}{user_id}:{org_id}:{module}"

    def get(self, user_id: str, org_id: str, module: str) -> Optional[str]:
        key = self._key(user_id, org_id, module)
        if self._redis is not None:
            try:
                val = self._redis.get(key)
                if val is not None:
                    return val
            except Exception as exc:
                logger.warning("Redis role cache get failed: %s", exc)

        doc = _db()[C.COL_RBAC_ROLE_CACHE].find_one(
            {"user_id": user_id, "org_id": org_id, "module": module}
        )
        if not doc:
            return None
        expires = doc.get("expires_at")
        if expires is not None:
            from datetime import datetime, timezone

            exp = expires
            if isinstance(exp, datetime) and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if isinstance(exp, datetime) and exp < utcnow():
                self.invalidate(user_id, org_id, module)
                return None
        return str(doc.get("role_id") or "")

    def set(self, user_id: str, org_id: str, module: str, role_id: str) -> None:
        key = self._key(user_id, org_id, module)
        ttl = self._ttl()
        role_id = role_id or ""
        if self._redis is not None:
            try:
                self._redis.setex(key, ttl, role_id)
            except Exception as exc:
                logger.warning("Redis role cache set failed: %s", exc)

        now = utcnow()
        _db()[C.COL_RBAC_ROLE_CACHE].update_one(
            {"user_id": user_id, "org_id": org_id, "module": module},
            {
                "$set": {
                    "role_id": role_id,
                    "expires_at": now + timedelta(seconds=ttl),
                    "updated_at": now,
                }
            },
            upsert=True,
        )

    def invalidate(
        self, user_id: str, org_id: str, module: Optional[str] = None
    ) -> None:
        modules = [module] if module else [C.MODULE_RECRUITMENT]
        for mod in modules:
            key = self._key(user_id, org_id, mod)
            if self._redis is not None:
                try:
                    self._redis.delete(key)
                except Exception as exc:
                    logger.warning("Redis role cache delete failed: %s", exc)
            _db()[C.COL_RBAC_ROLE_CACHE].delete_one(
                {"user_id": user_id, "org_id": org_id, "module": mod}
            )

    def invalidate_user_org(self, user_id: str, org_id: str) -> None:
        if self._redis is not None:
            try:
                pattern = f"{C.REDIS_ROLE_PREFIX}{user_id}:{org_id}:*"
                for key in self._redis.scan_iter(match=pattern, count=100):
                    self._redis.delete(key)
            except Exception as exc:
                logger.warning("Redis role cache scan delete failed: %s", exc)
        _db()[C.COL_RBAC_ROLE_CACHE].delete_many(
            {"user_id": user_id, "org_id": org_id}
        )


_cache: Optional[RoleCache] = None
_cache_lock = threading.Lock()


def get_role_cache() -> RoleCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = RoleCache()
    return _cache


def get_cached_role(user_id: str, org_id: str, module: str) -> Optional[str]:
    return get_role_cache().get(user_id, org_id, module)


def set_cached_role(
    user_id: str, org_id: str, module: str, role_id: str
) -> None:
    get_role_cache().set(user_id, org_id, module, role_id)


def invalidate_role(
    user_id: str, org_id: str, module: Optional[str] = None
) -> None:
    if module:
        get_role_cache().invalidate(user_id, org_id, module)
    else:
        get_role_cache().invalidate_user_org(user_id, org_id)


def reset_role_cache_for_tests() -> None:
    global _cache
    with _cache_lock:
        _cache = None
