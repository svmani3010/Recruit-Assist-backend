"""Sanitize untrusted strings before embedding them in LLM prompts."""

from __future__ import annotations

import re
from typing import Any, Mapping

from app.settings import get_settings

# Patterns commonly used in prompt-injection / instruction-override attacks
_INJECTION_PATTERNS = re.compile(
    r"(?is)("
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?"
    r"|system\s*prompt"
    r"|you\s+are\s+now"
    r"|<\s*/?\s*system\s*>"
    r"|```+\s*system"
    r")"
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_untrusted_text(value: Any, *, max_len: int = 500, field_name: str = "field") -> str:
    """
    Normalize untrusted user/candidate content for safe prompt interpolation.

    - Coerces to string, strips control chars
    - Truncates length
    - Neutralizes common injection phrases by wrapping as quoted data
    """
    if value is None:
        return ""
    text = str(value)
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\r\n", "\n").strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    if _INJECTION_PATTERNS.search(text):
        # Keep content but mark it explicitly as untrusted data, not instructions
        text = f"[UNTRUSTED_{field_name.upper()}_DATA] {text}"
    # Escape curly braces so .format / f-string template fragments cannot break out
    text = text.replace("{", "{{").replace("}", "}}")
    return text


def sanitize_interview_email_fields(data: Mapping[str, Any]) -> dict:
    """Return a copy of interview email fields with untrusted values sanitized."""
    interviewers = data.get("interviewers") or []
    safe_interviewers = []
    for item in interviewers:
        if isinstance(item, Mapping):
            safe_interviewers.append(
                {
                    "name": sanitize_untrusted_text(
                        item.get("name"), max_len=120, field_name="interviewer_name"
                    ),
                    "email": sanitize_untrusted_text(
                        item.get("email"), max_len=200, field_name="interviewer_email"
                    ),
                }
            )
    return {
        "Company": sanitize_untrusted_text(
            data.get("Company", "Fanisko"), max_len=120, field_name="company"
        ),
        "candidate_name": sanitize_untrusted_text(
            data.get("candidate_name"), max_len=120, field_name="candidate_name"
        ),
        "position": sanitize_untrusted_text(
            data.get("position"), max_len=200, field_name="position"
        ),
        "interview_level": sanitize_untrusted_text(
            data.get("interview_level", "Technical Round"),
            max_len=80,
            field_name="interview_level",
        ),
        "date": sanitize_untrusted_text(data.get("date"), max_len=40, field_name="date"),
        "time": sanitize_untrusted_text(data.get("time"), max_len=40, field_name="time"),
        "duration_minutes": data.get("duration_minutes", 60),
        "timezone": sanitize_untrusted_text(
            data.get("timezone"), max_len=60, field_name="timezone"
        ),
        "meeting_link": sanitize_untrusted_text(
            data.get("meeting_link", get_settings().default_meeting_link),
            max_len=300,
            field_name="meeting_link",
        ),
        "interviewers": safe_interviewers,
    }
