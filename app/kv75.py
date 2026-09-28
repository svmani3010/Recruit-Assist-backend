"""KV75 AC9 — platform-wide field rules. Copy and codes match the frontend."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

FIELD_RULES = {
    "name": {
        "min": 2,
        "max": 100,
        "message": "Please enter a valid name using letters only.",
        "requiredMessage": "Full name is required.",
        "tooShortMessage": "Full name must be at least 2 characters.",
        "tooLongMessage": "Full name cannot exceed 100 characters.",
    },
    "mobile": {
        "message": "Please enter a valid mobile number using digits only.",
        "requiredMessage": "Please select a country code",
    },
    "email": {
        "message": "Please enter a valid email address, for example name@company.com.",
        "requiredMessage": "Email is required.",
        "typoSuggestions": {
            "gmial.com": "gmail.com",
            "gmal.com": "gmail.com",
            "gmail.co": "gmail.com",
            "gmail.con": "gmail.com",
            "yaho.com": "yahoo.com",
            "yahooo.com": "yahoo.com",
            "hotmial.com": "hotmail.com",
            "outllok.com": "outlook.com",
        },
    },
    "numeric": {
        "message": "Enter a valid number.",
        "negativeMessage": "Negative values are not allowed.",
        "caps": {"experienceYears": 60, "noticeDays": 365, "vacancyCount": 9999},
    },
    "date": {
        "pastOnlyMessage": "Please choose a date in the past.",
        "futureOnlyMessage": "Please choose a date in the future.",
    },
    "url": {
        "linkedinMessage": "Please enter a valid LinkedIn profile URL.",
        "genericMessage": "Please enter a valid URL.",
    },
    "freetext": {
        "scriptMessage": "Remove script tags and control characters before continuing.",
    },
    "enum": {
        "message": "Please select a value from the list.",
    },
}

NAME_PATTERN = re.compile(r"^[A-Za-zÀ-ÿ]+(?:[ .'\-]+[A-Za-zÀ-ÿ]+)*\.?$")
NAME_ALLOWED_CHAR = re.compile(r"[A-Za-zÀ-ÿ .'\-]")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
LINKEDIN_PROFILE_RE = re.compile(
    r"^(https?:\/\/)?(www\.)?linkedin\.com\/in\/[\w%-]+\/?$",
    re.IGNORECASE,
)

NAME_PLACEHOLDER_DENYLIST = {
    "test",
    "abc",
    "asdf",
    "n/a",
    "na",
    "none",
    "xx",
    "xxx",
    ".",
    "foo",
    "bar",
    "asdfg",
    "qwerty",
}

JOB_TITLE_LIMITS = {"min": 3, "max": 100}
SUMMARY_MIN_REAL_CHARS = 100
SKILLS_MIN_COUNT = 5
EXPERIENCE_MAX_YEARS = 60
NOTICE_MAX_DAYS = 365

JOB_TITLE_FRESHER_SENTINELS = ("Student", "Seeking first role")
EMPLOYER_FRESHER_SENTINEL = "Not applicable"
NOTICE_PERIOD_OPTIONS = ("Immediate", "15 days", "30 days", "60 days", "90 days")
OPEN_TO_REMOTE = "Open to remote"
SALARY_NEGOTIABLE = "Negotiable"
SALARY_CURRENCIES = ("INR", "USD", "EUR", "GBP", "AED", "SGD")


def count_real_chars(value: Any) -> int:
    text = str(value or "")
    text = re.sub(r"[\u200B-\u200D\uFEFF]", "", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text)


def has_script_or_control(value: Any) -> bool:
    text = str(value or "")
    if re.search(r"<\s*script\b", text, re.IGNORECASE) or re.search(
        r"</\s*script\s*>", text, re.IGNORECASE
    ):
        return True
    return bool(re.search(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", text))


def is_placeholder_name(value: Any) -> bool:
    normalized = re.sub(r"[.']", "", str(value or "").strip().lower())
    if not normalized or len(normalized) == 1:
        return True
    return normalized in NAME_PLACEHOLDER_DENYLIST


def validate_name(value: Any, *, required: bool = True) -> dict[str, Any]:
    trimmed = str(value or "").strip()
    rules = FIELD_RULES["name"]
    if not trimmed:
        if required:
            return {"ok": False, "code": "name_required", "message": rules["requiredMessage"]}
        return {"ok": True, "value": ""}
    if len(trimmed) < rules["min"]:
        return {"ok": False, "code": "name_too_short", "message": rules["tooShortMessage"]}
    if len(trimmed) > rules["max"]:
        return {"ok": False, "code": "name_too_long", "message": rules["tooLongMessage"]}
    if not NAME_PATTERN.fullmatch(trimmed):
        return {"ok": False, "code": "name_invalid", "message": rules["message"]}
    return {
        "ok": True,
        "value": trimmed,
        "placeholder": is_placeholder_name(trimmed),
    }


def email_typo_suggestion(email: str) -> Optional[str]:
    trimmed = str(email or "").strip().lower()
    at = trimmed.rfind("@")
    if at < 0:
        return None
    domain = trimmed[at + 1 :]
    suggested = FIELD_RULES["email"]["typoSuggestions"].get(domain)
    if not suggested:
        return None
    return f"{trimmed[: at + 1]}{suggested}"


def validate_email(value: Any, *, required: bool = True) -> dict[str, Any]:
    trimmed = str(value or "").strip()
    rules = FIELD_RULES["email"]
    if not trimmed:
        if required:
            return {"ok": False, "code": "email_required", "message": rules["requiredMessage"]}
        return {"ok": True, "value": ""}
    if re.search(r"\s", trimmed) or not EMAIL_PATTERN.fullmatch(trimmed):
        return {"ok": False, "code": "email_invalid", "message": rules["message"]}
    suggestion = email_typo_suggestion(trimmed)
    return {
        "ok": True,
        "value": trimmed,
        "suggestion": suggestion if suggestion and suggestion != trimmed.lower() else None,
    }


def validate_numeric(
    value: Any,
    *,
    required: bool = True,
    min_value: float = 0,
    max_value: Optional[float] = None,
    allow_decimal: bool = True,
) -> dict[str, Any]:
    raw = str(value if value is not None else "").strip()
    rules = FIELD_RULES["numeric"]
    if raw == "":
        if required:
            return {"ok": False, "code": "numeric_required", "message": rules["message"]}
        return {"ok": True, "value": None}
    if not allow_decimal and "." in raw:
        return {"ok": False, "code": "numeric_invalid", "message": rules["message"]}
    if not re.fullmatch(r"\d+(\.\d+)?", raw):
        return {"ok": False, "code": "numeric_invalid", "message": rules["message"]}
    num = float(raw)
    if num < 0:
        return {"ok": False, "code": "numeric_negative", "message": rules["negativeMessage"]}
    if min_value is not None and num < min_value:
        return {"ok": False, "code": "numeric_min", "message": rules["message"]}
    if max_value is not None and num > max_value:
        return {
            "ok": False,
            "code": "numeric_max",
            "message": f"Enter a value of {int(max_value) if float(max_value).is_integer() else max_value} or less.",
        }
    return {"ok": True, "value": num}


def validate_experience_years(value: Any, *, required: bool = True) -> dict[str, Any]:
    return validate_numeric(
        value,
        required=required,
        min_value=0,
        max_value=EXPERIENCE_MAX_YEARS,
        allow_decimal=True,
    )


def validate_notice_days(value: Any, *, required: bool = True) -> dict[str, Any]:
    return validate_numeric(
        value,
        required=required,
        min_value=0,
        max_value=NOTICE_MAX_DAYS,
        allow_decimal=False,
    )


def validate_date(value: Any, *, required: bool = True, mode: str = "any") -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        if required:
            return {"ok": False, "code": "date_required", "message": "Please select a date."}
        return {"ok": True, "value": ""}
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return {"ok": False, "code": "date_invalid", "message": "Please select a valid date."}
    today = datetime.now(timezone.utc).date()
    cmp = parsed.date()
    if mode == "past" and cmp > today:
        return {"ok": False, "code": "date_future", "message": FIELD_RULES["date"]["pastOnlyMessage"]}
    if mode == "future" and cmp < today:
        return {"ok": False, "code": "date_past", "message": FIELD_RULES["date"]["futureOnlyMessage"]}
    return {"ok": True, "value": raw}


def validate_url(value: Any, *, required: bool = False, kind: str = "generic") -> dict[str, Any]:
    trimmed = str(value or "").strip()
    if not trimmed:
        if required:
            return {"ok": False, "code": "url_required", "message": FIELD_RULES["url"]["genericMessage"]}
        return {"ok": True, "value": ""}
    if kind == "linkedin":
        if not LINKEDIN_PROFILE_RE.fullmatch(trimmed):
            return {
                "ok": False,
                "code": "url_linkedin",
                "message": FIELD_RULES["url"]["linkedinMessage"],
            }
        return {"ok": True, "value": trimmed}
    try:
        parsed = urlparse(trimmed if "://" in trimmed else f"https://{trimmed}")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "code": "url_invalid", "message": FIELD_RULES["url"]["genericMessage"]}
        return {"ok": True, "value": trimmed}
    except Exception:
        return {"ok": False, "code": "url_invalid", "message": FIELD_RULES["url"]["genericMessage"]}


def validate_free_text(
    value: Any,
    *,
    required: bool = False,
    min_chars: int = 0,
    max_chars: int = 5000,
) -> dict[str, Any]:
    text = str(value or "")
    if has_script_or_control(text):
        return {
            "ok": False,
            "code": "freetext_unsafe",
            "message": FIELD_RULES["freetext"]["scriptMessage"],
        }
    trimmed = text.strip()
    if not trimmed:
        if required:
            return {"ok": False, "code": "freetext_required", "message": "This field is required."}
        return {"ok": True, "value": "", "realChars": 0}
    real = count_real_chars(trimmed)
    if min_chars and real < min_chars:
        return {
            "ok": False,
            "code": "freetext_min",
            "message": f"Enter at least {min_chars} characters.",
            "realChars": real,
            "value": trimmed,
        }
    if max_chars and len(trimmed) > max_chars:
        return {
            "ok": False,
            "code": "freetext_max",
            "message": f"Keep this under {max_chars} characters.",
            "realChars": real,
        }
    return {"ok": True, "value": trimmed, "realChars": real}


def validate_enum(value: Any, allowed: Iterable[str], *, required: bool = True) -> dict[str, Any]:
    trimmed = str(value or "").strip()
    allowed_list = [str(item) for item in allowed]
    if not trimmed:
        if required:
            return {"ok": False, "code": "enum_required", "message": FIELD_RULES["enum"]["message"]}
        return {"ok": True, "value": ""}
    if trimmed not in allowed_list:
        return {"ok": False, "code": "enum_invalid", "message": FIELD_RULES["enum"]["message"]}
    return {"ok": True, "value": trimmed}


def validate_job_title(
    value: Any,
    *,
    required: bool = True,
    fresher_sentinels: Iterable[str] = JOB_TITLE_FRESHER_SENTINELS,
) -> dict[str, Any]:
    trimmed = str(value or "").strip()
    sentinels = list(fresher_sentinels)
    if trimmed in sentinels:
        return {"ok": True, "value": trimmed, "sentinel": True}
    if not trimmed:
        if required:
            return {"ok": False, "code": "job_title_required", "message": "Job title is required."}
        return {"ok": True, "value": ""}
    if len(trimmed) < JOB_TITLE_LIMITS["min"] or len(trimmed) > JOB_TITLE_LIMITS["max"]:
        return {
            "ok": False,
            "code": "job_title_length",
            "message": (
                f"Job title must be {JOB_TITLE_LIMITS['min']}–{JOB_TITLE_LIMITS['max']} characters."
            ),
        }
    return {"ok": True, "value": trimmed}


def api_field_error(field: str, result: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    if not result or result.get("ok"):
        return None
    return {
        "field": field,
        "code": str(result.get("code") or ""),
        "message": str(result.get("message") or "Invalid value."),
    }
