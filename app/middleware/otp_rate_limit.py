import os
import time
from datetime import datetime, timedelta
from typing import Tuple, Optional

# Simple in‑memory store for counters (fallback if no Redis)
# Structure: {
#   'phone:{phone}': [(timestamp1, purpose), (timestamp2, purpose), ...]
#   'ip:{ip}': [(timestamp, purpose), ...]
#   'resend:{phone}': {'count': int, 'next_allowed': datetime}
#   'lockout:{phone}': datetime (lockout until)
# }
_store: dict[str, list[Tuple[float, str]]] = {}
_resend_store: dict[str, dict] = {}
_lockout_store: dict[str, datetime] = {}

# Load limits from environment variables with defaults
OTP_PER_15MIN = int(os.getenv("OTP_PER_15MIN", "3"))
OTP_PER_24H = int(os.getenv("OTP_PER_24H", "10"))
MAX_INCORRECT = int(os.getenv("OTP_MAX_INCORRECT", "5"))
LOCKOUT_DURATION = int(os.getenv("OTP_LOCKOUT_DURATION", str(30 * 60)))  # seconds

RESEND_COOLDOWNS = [int(x) for x in os.getenv("OTP_RESEND_COOLDOWNS", "30,60,120").split(",")]
RESEND_LOCKOUT = int(os.getenv("OTP_RESEND_LOCKOUT", str(15 * 60)))  # seconds

def _now_ts() -> float:
    return time.time()

def _clean_old_entries(key: str, window_seconds: int):
    now = _now_ts()
    entries = _store.get(key, [])
    _store[key] = [(ts, purpose) for ts, purpose in entries if now - ts < window_seconds]

def _increment_counter(key: str, purpose: str):
    now = _now_ts()
    _store.setdefault(key, []).append((now, purpose))

def check_otp_limits(phone: str, ip: str, purpose: str = "otp") -> Tuple[bool, Optional[int]]:
    """Return (allowed, retry_after_seconds). Checks per‑phone and per‑ip limits.
    Limits are 15‑minute and 24‑hour windows.
    """
    # Clean up old entries
    _clean_old_entries(f"phone:{phone}", 24 * 60 * 60)
    _clean_old_entries(f"ip:{ip}", 24 * 60 * 60)

    # Count recent entries
    phone_entries = [ts for ts, _ in _store.get(f"phone:{phone}", [])]
    ip_entries = [ts for ts, _ in _store.get(f"ip:{ip}", [])]

    # 15‑minute window
    now = _now_ts()
    recent_15min_phone = len([ts for ts in phone_entries if now - ts < 15 * 60])
    recent_15min_ip = len([ts for ts in ip_entries if now - ts < 15 * 60])

    if recent_15min_phone >= OTP_PER_15MIN or recent_15min_ip >= OTP_PER_15MIN:
        # calculate when next slot becomes available
        earliest_phone = min([ts for ts in phone_entries if now - ts < 15 * 60], default=now)
        earliest_ip = min([ts for ts in ip_entries if now - ts < 15 * 60], default=now)
        retry_after = int(max(earliest_phone + 15 * 60, earliest_ip + 15 * 60) - now)
        return False, max(retry_after, 0)

    # 24‑hour window
    recent_24h_phone = len([ts for ts in phone_entries if now - ts < 24 * 60 * 60])
    recent_24h_ip = len([ts for ts in ip_entries if now - ts < 24 * 60 * 60])
    if recent_24h_phone >= OTP_PER_24H or recent_24h_ip >= OTP_PER_24H:
        earliest_phone = min([ts for ts in phone_entries if now - ts < 24 * 60 * 60], default=now)
        earliest_ip = min([ts for ts in ip_entries if now - ts < 24 * 60 * 60], default=now)
        retry_after = int(max(earliest_phone + 24 * 60 * 60, earliest_ip + 24 * 60 * 60) - now)
        return False, max(retry_after, 0)

    # Passed limits – record this attempt
    _increment_counter(f"phone:{phone}", purpose)
    _increment_counter(f"ip:{ip}", purpose)
    return True, None

def check_resend_cooldown(phone: str) -> Tuple[bool, Optional[int]]:
    """Return (allowed, wait_seconds). Implements exponential back‑off.
    After each successful resend, the wait time increases according to RESEND_COOLDOWNS.
    After the last step, a lockout of RESEND_LOCKOUT seconds is applied.
    """
    now = datetime.utcnow()
    data = _resend_store.get(phone)
    if not data:
        # first attempt – allow immediately
        _resend_store[phone] = {"count": 0, "next_allowed": now}
        return True, None
    if now < data["next_allowed"]:
        wait = int((data["next_allowed"] - now).total_seconds())
        return False, wait
    # allowed – update count and next_allowed
    count = data["count"] + 1
    if count <= len(RESEND_COOLDOWNS):
        wait_sec = RESEND_COOLDOWNS[count - 1]
    else:
        # lockout after max attempts
        wait_sec = RESEND_LOCKOUT
    data["count"] = count
    data["next_allowed"] = now + timedelta(seconds=wait_sec)
    return True, None

def reset_resend_state(phone: str):
    """Reset the resend counter after a successful verification.
    This allows the user to start fresh for the next OTP flow.
    """
    _resend_store.pop(phone, None)

def check_verify_attempts(phone: str) -> Tuple[bool, Optional[int]]:
    """Check whether the phone is currently locked out due to too many incorrect attempts.
    Returns (allowed, lockout_remaining_seconds).
    """
    lockout_until = _lockout_store.get(phone)
    now = datetime.utcnow()
    if lockout_until and now < lockout_until:
        remaining = int((lockout_until - now).total_seconds())
        return False, remaining
    return True, None

def record_failed_attempt(phone: str):
    """Increment failure count and apply lockout if limit exceeded.
    Uses a simple per‑phone counter stored in the in‑memory store.
    """
    key = f"fail:{phone}"
    now = _now_ts()
    _store.setdefault(key, []).append((now, "fail"))
    fails = len(_store[key])
    if fails >= MAX_INCORRECT:
        # lock out for LOCKOUT_DURATION seconds
        _lockout_store[phone] = datetime.utcnow() + timedelta(seconds=LOCKOUT_DURATION)
        # reset failure list
        _store[key] = []

def reset_failed_attempts(phone: str):
    _store.pop(f"fail:{phone}", None)
    _lockout_store.pop(phone, None)
