"""Shared input checks for signup and job-posting forms.

Rejects empty * required fields and obvious type mismatches
(name in an email box, email in a name box, etc.).
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Allow initials and titles ("S. Kumar", "Dr. Smith") by accepting one or more
# separators between name parts, not exactly one.
_PERSON_NAME_RE = re.compile(
    r"^[A-Za-zÀ-ÿ]+(?:[ .'\-]+[A-Za-zÀ-ÿ]+)*\.?$"
)
_PLACE_NAME_RE = re.compile(
    r"^[A-Za-zÀ-ÿ]+(?:[ ,.'\-][A-Za-zÀ-ÿ]+)*$"
)
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)
_PHONE_RE = re.compile(r"^\+?[0-9](?:[0-9\s\-().]{5,18}[0-9])?$")
_PHONE_DIGITS_RE = re.compile(r"\d")
_URL_RE = re.compile(
    r"^(https?://)?([A-Za-z0-9\-]+\.)+[A-Za-z]{2,}([/?#].*)?$",
    re.IGNORECASE,
)
_LINKEDIN_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9\-_.]{3,100}$")


def coerce_form_string(value: Any) -> str:
    """Normalize UI form values into a plain string.

    React selects often post `{value, label}` objects, and empty inputs are
    sent as `null`. Pydantic v2 treats explicit null as a type error (422)
    unless we coerce first.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("value", "label", "url", "src", "name"):
            inner = value.get(key)
            if inner is not None and not isinstance(inner, (dict, list)):
                return str(inner)
        return ""
    if isinstance(value, (list, tuple)):
        return coerce_form_string(value[0]) if value else ""
    return str(value)


def _clean(value: Optional[str]) -> str:
    return str(value or "").strip()


def require_text(value: Optional[str], label: str, *, min_length: int = 2) -> str:
    text = _clean(value)
    if not text:
        raise ValueError(f"{label} is required")
    if len(text) < min_length:
        raise ValueError(f"{label} must be at least {min_length} characters")
    return text


def require_person_name(value: Optional[str], label: str = "Name") -> str:
    text = require_text(value, label, min_length=2)
    if "@" in text:
        raise ValueError(f"{label} must be a name, not an email address")
    if any(ch.isdigit() for ch in text):
        raise ValueError(f"{label} must contain letters only")
    if not _PERSON_NAME_RE.fullmatch(text):
        raise ValueError(f"{label} must be a valid name")
    return text


def optional_person_name(value: Optional[str], label: str = "Name") -> str:
    text = _clean(value)
    if not text:
        return ""
    return require_person_name(text, label)


def require_place_name(value: Optional[str], label: str) -> str:
    text = require_text(value, label, min_length=2)
    if "@" in text or any(ch.isdigit() for ch in text):
        raise ValueError(f"{label} must be a location name")
    if not _PLACE_NAME_RE.fullmatch(text):
        raise ValueError(f"{label} must be a valid location name")
    return text


def require_email(value: Optional[str], label: str = "Email") -> str:
    text = require_text(value, label, min_length=5).lower()
    if not _EMAIL_RE.fullmatch(text):
        raise ValueError(f"{label} must be a valid email address")
    return text


def optional_email(value: Optional[str], label: str = "Email") -> str:
    text = _clean(value)
    if not text:
        return ""
    return require_email(text, label)


def require_phone(value: Optional[str], label: str = "Phone") -> str:
    text = require_text(value, label, min_length=7)
    if "@" in text:
        raise ValueError(f"{label} must be a phone number, not an email address")
    digits = "".join(_PHONE_DIGITS_RE.findall(text))
    if len(digits) < 7 or len(digits) > 15:
        raise ValueError(f"{label} must be a valid phone number")
    if not _PHONE_RE.fullmatch(text):
        raise ValueError(f"{label} must be a valid phone number")
    return text


def require_company_name(value: Optional[str], label: str = "Company name") -> str:
    text = require_text(value, label, min_length=2)
    if "@" in text and "." in text.split("@")[-1]:
        raise ValueError(f"{label} must be a company name, not an email address")
    return text


def require_job_title(value: Optional[str], label: str = "Job title") -> str:
    text = require_text(value, label, min_length=2)
    if _EMAIL_RE.fullmatch(text):
        raise ValueError(f"{label} must be a job title, not an email address")
    return text


def optional_url(value: Optional[str], label: str = "Website") -> str:
    text = _clean(value)
    if not text:
        return ""
    if not _URL_RE.fullmatch(text):
        raise ValueError(f"{label} must be a valid website URL")
    return text


def optional_linkedin(value: Optional[str], label: str = "LinkedIn") -> str:
    text = _clean(value)
    if not text:
        return ""
    if text.lower().startswith("http") or "linkedin.com" in text.lower():
        return optional_url(text, label)
    if not _LINKEDIN_HANDLE_RE.fullmatch(text):
        raise ValueError(f"{label} must be a valid LinkedIn URL or handle")
    return text


def optional_text(value: Optional[str]) -> str:
    return _clean(value)
