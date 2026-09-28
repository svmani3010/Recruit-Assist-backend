"""L1 chat session store: local chat objects + Redis-serialized transcript for multi-instance rebuild."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from app.settings import get_settings

logger = logging.getLogger(__name__)


class ChatSessionStore:
    """
    Hybrid session store for live Gemini L1 interviews.

    Sessions hold a `last_interaction_id` (Interactions API history is kept
    server-side under that id — no local chat object to manage) plus the
    resume/JD context and a local transcript for the HR-facing Q&A view.

    - Process-local cache holds the current session dict (LRU + TTL)
    - When REDIS_URL is set, the same JSON-serializable session is also
      stored so another instance can resume the interaction after a cache
      miss (no sticky sessions required)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._redis = None
        settings = get_settings()
        if settings.redis_url:
            try:
                import redis

                self._redis = redis.Redis.from_url(
                    settings.redis_url, decode_responses=True
                )
                self._redis.ping()
                logger.info("ChatSessionStore: Redis connected for L1 transcripts")
            except Exception as exc:
                logger.warning(
                    "ChatSessionStore: Redis unavailable (%s); in-memory only "
                    "(sticky sessions required for multi-instance L1)",
                    exc,
                )
                self._redis = None

    def _ttl(self) -> int:
        return get_settings().chat_session_ttl_seconds

    def _max_entries(self) -> int:
        return get_settings().chat_session_max_entries

    def _redis_key(self, key: str) -> str:
        return f"l1session:{key}"

    def _purge_expired_unlocked(self) -> None:
        now = time.time()
        expired = [k for k, (exp, _) in self._store.items() if exp <= now]
        for key in expired:
            del self._store[key]

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            self._purge_expired_unlocked()
            item = self._store.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at <= time.time():
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._purge_expired_unlocked()
            while len(self._store) >= self._max_entries():
                self._store.popitem(last=False)
            self._store[key] = (time.time() + self._ttl(), value)
            self._store.move_to_end(key)
        self.persist_transcript(key, value)

    def persist_transcript(self, key: str, session: Any) -> None:
        """Write serializable session fields to Redis (best-effort)."""
        if self._redis is None or not isinstance(session, dict):
            return
        payload = {
            "first_question": session.get("first_question", ""),
            "resume_data": session.get("resume_data"),
            "jd_data": session.get("jd_data"),
            "history": session.get("history") or [],
            "last_interaction_id": session.get("last_interaction_id"),
            # Internal audit data from the last structured L1 turn
            # (InterviewTurnResponse.interview_state / .final_report) —
            # never shown to the candidate, kept so another worker can
            # resume with the same audit trail after a cache miss.
            "interview_state": session.get("interview_state"),
            "final_report": session.get("final_report"),
            # HR-configured L1 difficulty floor — needed when a worker
            # rebuilds a session without a valid last_interaction_id.
            "difficulty_level": session.get("difficulty_level"),
        }
        try:
            self._redis.setex(
                self._redis_key(key),
                self._ttl(),
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception as exc:
            logger.warning("Redis transcript setex failed: %s", exc)

    def load_transcript(self, key: str) -> Optional[Dict[str, Any]]:
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(self._redis_key(key))
            if not raw:
                return None
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return data
        except Exception as exc:
            logger.warning("Redis transcript get failed: %s", exc)
            return None

    def append_history(self, key: str, role: str, text: str) -> None:
        session = self.get(key)
        if not isinstance(session, dict):
            return
        history: List[dict] = list(session.get("history") or [])
        history.append({"role": role, "text": text})
        # Cap history to control rebuild prompt size
        if len(history) > 40:
            history = history[-40:]
        session["history"] = history
        self.set(key, session)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)
        if self._redis is not None:
            try:
                self._redis.delete(self._redis_key(key))
            except Exception as exc:
                logger.warning("Redis delete failed: %s", exc)

    def backend(self) -> str:
        return "redis+memory" if self._redis is not None else "memory"

    def __contains__(self, key: str) -> bool:
        if self.get(key) is not None:
            return True
        return self.load_transcript(key) is not None


chat_sessions = ChatSessionStore()
