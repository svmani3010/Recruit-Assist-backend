"""Generate / ensure MOBILE_QE_LOCAL_MASTER_KEY in .env (local KMS only).

Does not print the key. Safe to re-run — skips if already set.
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
KEY_NAME = "MOBILE_QE_LOCAL_MASTER_KEY"


def _env_has_key(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == KEY_NAME and value.strip():
            return True
    return False


def main() -> int:
    existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if _env_has_key(existing):
        print(f"{KEY_NAME} already present in .env — no change.")
        return 0

    key = base64.b64encode(secrets.token_bytes(96)).decode("ascii")
    block = (
        "\n# MongoDB Queryable Encryption — local KMS master key (96 bytes, base64)\n"
        "# DEV ONLY. Production: set MOBILE_QE_KMS_PROVIDER=aws and AWS_KMS_KEY_ARN.\n"
        "MOBILE_QE_ENABLED=true\n"
        "MOBILE_QE_KMS_PROVIDER=local\n"
        f"{KEY_NAME}={key}\n"
        "MOBILE_QE_KEY_ALT_NAME=candidate-mobile-v1\n"
        "MOBILE_QE_KEY_VERSION=1\n"
    )
    with ENV_PATH.open("a", encoding="utf-8") as fh:
        fh.write(block)
    # Also export for current process consumers
    os.environ[KEY_NAME] = key
    os.environ.setdefault("MOBILE_QE_ENABLED", "true")
    os.environ.setdefault("MOBILE_QE_KMS_PROVIDER", "local")
    print(f"Appended {KEY_NAME} and QE settings to .env (value not shown).")
    print("Restart the API process to load the new key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
