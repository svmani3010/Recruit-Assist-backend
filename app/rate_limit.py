# Shared SlowAPI limiter instance.

from slowapi import Limiter
from slowapi.util import get_remote_address

# Load settings (including global and OTP flags) at runtime
from app.settings import get_settings

_settings = get_settings()
OTP_FEATURES_ENABLED = _settings.otp_features_enabled
OTP_RATE_LIMIT_ENABLED = _settings.otp_rate_limit_enabled
# Global toggle for any rate limiting (covers all @limiter.limit decorators)
RATE_LIMIT_ENABLED = _settings.rate_limit_enabled

limiter = Limiter(key_func=get_remote_address, default_limits=[])

# If global rate limiting is disabled, replace the `limit` decorator with a no‑op.
if not RATE_LIMIT_ENABLED:
    def _noop_limit(*_args, **_kwargs):
        def decorator(func):
            return func
        return decorator
    limiter.limit = _noop_limit
# Otherwise keep existing OTP‑specific bypass for backward compatibility.
elif not (OTP_FEATURES_ENABLED and OTP_RATE_LIMIT_ENABLED):
    def _noop_limit(*_args, **_kwargs):
        def decorator(func):
            return func
        return decorator
    limiter.limit = _noop_limit




