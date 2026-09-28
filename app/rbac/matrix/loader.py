"""Load Yes/No permission sheets from ``app/rbac/matrix/``."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MATRIX_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def load_roles_catalog() -> dict[str, Any]:
    path = MATRIX_DIR / "roles.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def load_recruitment_actions() -> dict[str, Any]:
    path = MATRIX_DIR / "recruitment_actions.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def matrix_denied_message() -> str:
    data = load_recruitment_actions()
    return str(
        data.get("denied_message")
        or "You don't have access to this action. "
        "Contact a Super Admin, Content Admin, or Manager."
    )


def iter_yes_no_rows() -> list[tuple[str, str, str, str]]:
    """Yield ``(module, action, role_id, yes|no)`` from the sheet files."""
    data = load_recruitment_actions()
    module = str(data.get("module") or "recruitment")
    rows: list[tuple[str, str, str, str]] = []
    for action, spec in (data.get("actions") or {}).items():
        roles = (spec or {}).get("roles") or {}
        for role_id, value in roles.items():
            flag = str(value or "no").strip().lower()
            if flag not in {"yes", "no"}:
                # Collapse any legacy scope word to yes (except explicit no).
                flag = "no" if flag in {"no", "none", "n", "false", "0"} else "yes"
            rows.append((module, str(action), str(role_id), flag))
    return rows


def file_allows(role_id: str, action: str, *, module: str = "recruitment") -> bool | None:
    """Direct file lookup (used before / without Mongo). Returns None if action not found."""
    data = load_recruitment_actions()
    if str(data.get("module") or "recruitment") != module:
        return False
    spec = (data.get("actions") or {}).get(action)
    if spec is None:
        # Action not mentioned in matrix - return None to indicate default behavior
        return None
    flag = str((spec.get("roles") or {}).get(role_id) or "no").strip().lower()
    return flag == "yes"


def all_role_ids() -> list[str]:
    catalog = load_roles_catalog()
    return [str(r["role_id"]) for r in catalog.get("roles") or [] if r.get("role_id")]


def clear_matrix_caches() -> None:
    load_roles_catalog.cache_clear()
    load_recruitment_actions.cache_clear()
