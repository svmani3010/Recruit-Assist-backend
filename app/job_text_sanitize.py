"""Sanitize AI/job narrative fields for storage and display.

Prose fields (About the Job / Experience Required) must not contain literal
or real newlines — collapse them to spaces.

Bullet-style fields (Roles and Responsibilities / Desired Skills) keep
line breaks so the UI can split them into list items.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, MutableMapping

_PROSE_DB_KEYS = ("jobDescription", "experienceRequired")
_LIST_DB_KEYS = ("rolesResponsibilities", "desiredSkills", "goodToHave")

_PROSE_AI_KEYS = ("Job Description", "Experience Required")
_LIST_AI_KEYS = (
    "Roles and Responsibilities",
    "Desired Skills",
    "Good to Have",
)

_MULTI_SPACE_RE = re.compile(r"[ \t\f\v]+")


def collapse_prose_newlines(text: Any) -> str:
    """Turn literal \\n and real newlines into spaces for paragraph fields."""
    if text is None:
        return ""
    if isinstance(text, (list, tuple)):
        text = " ".join(str(item or "") for item in text)
    s = str(text)
    s = (
        s.replace("\\r\\n", " ")
        .replace("\\n", " ")
        .replace("\\r", " ")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )
    return _MULTI_SPACE_RE.sub(" ", s).strip()


def normalize_list_newlines(text: Any) -> str:
    """Keep list separators; convert literal \\n into real newlines."""
    if text is None:
        return ""
    if isinstance(text, (list, tuple)):
        text = "\n".join(str(item or "") for item in text)
    s = str(text)
    s = (
        s.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    lines = [ln.strip(" \t•-–—") for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def sanitize_generated_job_description(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Clean AI generate-job-description response keys before returning to HR UI."""
    out = dict(payload or {})
    for key in _PROSE_AI_KEYS:
        if key in out and out[key] is not None:
            out[key] = collapse_prose_newlines(out[key])
    for key in _LIST_AI_KEYS:
        if key in out and out[key] is not None:
            out[key] = normalize_list_newlines(out[key])
    # Education is prose on the candidate job page as well when shown elsewhere
    if "Educational Qualifications" in out and out["Educational Qualifications"] is not None:
        out["Educational Qualifications"] = collapse_prose_newlines(
            out["Educational Qualifications"]
        )
    return out


def sanitize_job_narrative_fields(data: MutableMapping[str, Any] | None) -> MutableMapping[str, Any]:
    """Normalize narrative fields on a job document before save."""
    if not data:
        return data or {}
    for key in _PROSE_DB_KEYS:
        if key in data and data[key] is not None:
            data[key] = collapse_prose_newlines(data[key])
    for key in _LIST_DB_KEYS:
        if key in data and data[key] is not None:
            data[key] = normalize_list_newlines(data[key])
    if "educationalQualifications" in data and data["educationalQualifications"] is not None:
        data["educationalQualifications"] = collapse_prose_newlines(
            data["educationalQualifications"]
        )
    return data
