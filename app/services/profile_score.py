"""Server-side Profile Score engine (all-or-nothing, 100-point apply gate)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.kv75 import (
    EMPLOYER_FRESHER_SENTINEL,
    JOB_TITLE_FRESHER_SENTINELS,
    NOTICE_PERIOD_OPTIONS,
    OPEN_TO_REMOTE,
    SALARY_NEGOTIABLE,
    SKILLS_MIN_COUNT,
    SUMMARY_MIN_REAL_CHARS,
    count_real_chars,
    is_placeholder_name,
    validate_experience_years,
    validate_job_title,
    validate_name,
)

logger = logging.getLogger(__name__)


class ProfileScoreError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = "PROFILE_SCORE_ERROR",
        details: Optional[list[str]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details or []


APPLY_GATE_SCORE = 100
STALENESS_AFFECTS_APPLY_GATE = False
PROFILE_SCORE_CONFIG_VERSION = 1
AUDIT_CAP = 50

FRESHNESS_WINDOWS_MONTHS = {
    "resume_uploaded": 12,
    "notice_period": 6,
    "expected_salary": 6,
    "employer": 12,
}

QUALITY_RULE_ITEM_IDS = {
    "full_name",
    "total_experience_years",
    "job_title",
    "resume_uploaded",
    "skills",
    "education",
    "professional_summary",
}

PROFILE_SCORE_ITEMS = [
    {
        "id": "email_verified",
        "row": 1,
        "section": "Account",
        "label": "Email address verified",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "email_verified"},
        "completionHref": "/settings",
        "freshness": None,
    },
    {
        "id": "full_name",
        "row": 2,
        "section": "Basic details",
        "label": "Full name",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "name_kv75"},
        "completionHref": "/profile#basic-details",
        "freshness": None,
    },
    {
        "id": "mobile_otp_verified",
        "row": 3,
        "section": "Account",
        "label": "Mobile number verified by OTP",
        "weight": 10,
        "mandatory": True,
        "qualityRule": {"type": "mobile_otp"},
        "completionHref": "/profile/setup",
        "freshness": None,
    },
    {
        "id": "whatsapp_choice",
        "row": 4,
        "section": "Account",
        "label": "WhatsApp number confirmed or explicitly declined",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "whatsapp_choice"},
        "completionHref": "/profile/setup",
        "freshness": None,
    },
    {
        "id": "current_location",
        "row": 5,
        "section": "Basic details",
        "label": "Current location (country and city)",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "structured_location"},
        "completionHref": "/profile#basic-details",
        "freshness": None,
    },
    {
        "id": "total_experience_years",
        "row": 6,
        "section": "Experience",
        "label": "Total years of experience",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "experience_years"},
        "completionHref": "/profile#basic-details",
        "freshness": None,
    },
    {
        "id": "job_title",
        "row": 7,
        "section": "Experience",
        "label": "Current or most recent job title",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "job_title"},
        "completionHref": "/profile#basic-details",
        "freshness": None,
    },
    {
        "id": "employer",
        "row": 8,
        "section": "Experience",
        "label": "Current or most recent employer",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "employer"},
        "completionHref": "/profile#basic-details",
        "freshness": {"windowMonths": 12, "affectsApplyGate": False},
    },
    {
        "id": "resume_uploaded",
        "row": 9,
        "section": "Documents",
        "label": "Resume uploaded",
        "weight": 15,
        "mandatory": True,
        "qualityRule": {"type": "resume_parsed", "formats": ["pdf", "doc", "docx"], "maxMb": 5},
        "completionHref": "/profile#resume",
        "freshness": {"windowMonths": 12, "affectsApplyGate": False},
    },
    {
        "id": "skills",
        "row": 10,
        "section": "Skills",
        "label": "Key skills added (minimum 5)",
        "weight": 10,
        "mandatory": True,
        "qualityRule": {"type": "skills_min", "minCount": 5},
        "completionHref": "/profile#skills",
        "freshness": None,
    },
    {
        "id": "education",
        "row": 11,
        "section": "Education",
        "label": "Highest qualification",
        "weight": 10,
        "mandatory": True,
        "qualityRule": {"type": "education_triple"},
        "completionHref": "/profile#education",
        "freshness": None,
    },
    {
        "id": "notice_period",
        "row": 12,
        "section": "Availability",
        "label": "Notice period or availability to join",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "notice_enum"},
        "completionHref": "/profile#basic-details",
        "freshness": {"windowMonths": 6, "affectsApplyGate": False},
    },
    {
        "id": "preferred_locations",
        "row": 13,
        "section": "Preferences",
        "label": "Preferred job locations",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "preferred_locations"},
        "completionHref": "/profile#basic-details",
        "freshness": None,
    },
    {
        "id": "expected_salary",
        "row": 14,
        "section": "Preferences",
        "label": "Expected salary range",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "salary_range"},
        "completionHref": "/profile#basic-details",
        "freshness": {"windowMonths": 6, "affectsApplyGate": False},
    },
    {
        "id": "professional_summary",
        "row": 15,
        "section": "Profile",
        "label": "Professional summary",
        "weight": 5,
        "mandatory": True,
        "qualityRule": {"type": "summary_min_chars", "minRealChars": 100},
        "completionHref": "/profile#basic-details",
        "freshness": None,
    },
]

_EXPERIENCE_BUCKETS = {
    "fresher / 0 years": 0,
    "fresher": 0,
    "0 years": 0,
    "0-1 years": 0.5,
    "0–1 years": 0.5,
    "1-3 years": 2,
    "1–3 years": 2,
    "3-5 years": 4,
    "3–5 years": 4,
    "5-8 years": 6.5,
    "5–8 years": 6.5,
    "8-12 years": 10,
    "8–12 years": 10,
    "12+ years": 12,
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    return bool(str(value).strip())


def create_default_profile_score_config(org_id: str = "default") -> dict[str, Any]:
    return {
        "orgId": org_id,
        "version": PROFILE_SCORE_CONFIG_VERSION,
        "applyGateScore": APPLY_GATE_SCORE,
        "stalenessAffectsApplyGate": STALENESS_AFFECTS_APPLY_GATE,
        "items": [dict(item) for item in PROFILE_SCORE_ITEMS],
        "freshnessDefaults": dict(FRESHNESS_WINDOWS_MONTHS),
        "qualityRules": {
            "full_name": {
                "min": 2,
                "max": 100,
                "placeholderDenylist": sorted(list({
                    "test", "abc", "asdf", "n/a", "xx", "xxx", "na", "none", ".",
                })),
            },
            "professional_summary": {"minRealChars": SUMMARY_MIN_REAL_CHARS},
            "skills": {"minCount": SKILLS_MIN_COUNT, "source": "taxonomy"},
            "job_title": {
                "min": 3,
                "max": 100,
                "fresherSentinels": list(JOB_TITLE_FRESHER_SENTINELS),
            },
            "employer": {"fresherSentinel": EMPLOYER_FRESHER_SENTINEL},
        },
    }


def item_weights_sum(items: list[dict[str, Any]]) -> int:
    return int(sum(Number(item.get("weight") or 0) for item in (items or [])))


def Number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def validate_profile_score_config(config: Any) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(config, dict):
        return {"ok": False, "errors": ["Config is required."], "weightSum": 0}
    if config.get("stalenessAffectsApplyGate") is True:
        errors.append(
            "stalenessAffectsApplyGate must be false — freshness does not affect the apply gate."
        )
    items = config.get("items") if isinstance(config.get("items"), list) else []
    if not items:
        errors.append("Config must include at least one scoring item.")
    weight_sum = int(sum(Number(item.get("weight")) for item in items))
    if weight_sum != APPLY_GATE_SCORE:
        errors.append(f"Item weights must sum to {APPLY_GATE_SCORE} (got {weight_sum}).")
    seen: set[str] = set()
    for index, item in enumerate(items):
        item_id = (item or {}).get("id")
        if not item_id:
            errors.append(f"Item {index + 1} is missing an id.")
        if item_id in seen:
            errors.append(f"Duplicate item id: {item_id}")
        seen.add(item_id)
        weight = Number((item or {}).get("weight"))
        if weight < 0:
            errors.append(f"Item {item_id or index + 1} has an invalid weight.")
    gate = Number(config.get("applyGateScore", APPLY_GATE_SCORE))
    if int(gate) != APPLY_GATE_SCORE:
        errors.append(f"applyGateScore must be {APPLY_GATE_SCORE}.")
    return {"ok": not errors, "errors": errors, "weightSum": weight_sum}


def assert_profile_score_config(config: dict[str, Any]) -> dict[str, Any]:
    result = validate_profile_score_config(config)
    if not result["ok"]:
        raise ProfileScoreError(
            " ".join(result["errors"]),
            code="PROFILE_SCORE_CONFIG_INVALID",
            details=result["errors"],
        )
    return config


def is_item_stale(item: dict[str, Any], profile: dict[str, Any]) -> bool:
    freshness = item.get("freshness") or {}
    window_months = freshness.get("windowMonths") if isinstance(freshness, dict) else None
    if not window_months:
        return False
    stamps = profile.get("freshness") or {}
    at = stamps.get(item.get("id"))
    if not at:
        return False
    try:
        then = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    elapsed = datetime.now(timezone.utc) - then
    return elapsed.total_seconds() > float(window_months) * 30 * 24 * 60 * 60


def _missing_reason(item_id: str, profile: dict[str, Any]) -> str:
    resume = profile.get("resume") or {}
    reasons = {
        "email_verified": "Follow the confirmation link sent to your email.",
        "full_name": "Enter a valid full name using letters only.",
        "mobile_otp_verified": "Verify your mobile number with the WhatsApp OTP.",
        "whatsapp_choice": "Confirm WhatsApp notifications or decline them.",
        "current_location": "Select your country and city from the list.",
        "total_experience_years": "Enter total years of experience (0–60).",
        "job_title": "Add your current or most recent job title.",
        "employer": "Add your current or most recent employer.",
        "skills": "Add at least 5 skills from the list.",
        "education": "Add qualification, institution, and year of completion.",
        "notice_period": "Select your notice period.",
        "preferred_locations": "Add a preferred location or choose Open to remote.",
        "expected_salary": "Add an expected salary range or choose Negotiable.",
        "professional_summary": "Write a professional summary of at least 100 characters.",
    }
    if item_id == "resume_uploaded":
        if resume.get("uploaded") and not resume.get("parsed"):
            return str(
                resume.get("parseError")
                or "We could not read that resume. Upload a readable PDF or Word file."
            )
        return "Upload a PDF, DOC, or DOCX resume (max 5 MB)."
    return reasons.get(item_id, f"Complete {item_id}.")


def _evaluate_item(item: dict[str, Any], profile: dict[str, Any]) -> bool:
    item_id = item.get("id")
    if item_id == "email_verified":
        return bool(profile.get("emailVerified"))
    if item_id == "full_name":
        result = validate_name(profile.get("fullName"), required=True)
        return bool(
            result.get("ok")
            and not result.get("placeholder")
            and not is_placeholder_name(profile.get("fullName"))
        )
    if item_id == "mobile_otp_verified":
        return bool(profile.get("mobileOtpVerified"))
    if item_id == "whatsapp_choice":
        return profile.get("whatsappChoice") in {"confirmed", "declined"}
    if item_id == "current_location":
        return (
            profile.get("locationStatus") == "confirmed"
            and _filled(profile.get("countryId"))
            and _filled(profile.get("cityId"))
        )
    if item_id == "total_experience_years":
        return bool(validate_experience_years(profile.get("totalExperienceYears"), required=True).get("ok"))
    if item_id == "job_title":
        return bool(
            validate_job_title(
                profile.get("jobTitle"),
                required=True,
                fresher_sentinels=JOB_TITLE_FRESHER_SENTINELS,
            ).get("ok")
        )
    if item_id == "employer":
        value = str(profile.get("employer") or "").strip()
        if value == EMPLOYER_FRESHER_SENTINEL:
            return True
        return len(value) >= 2
    if item_id == "resume_uploaded":
        resume = profile.get("resume") or {}
        return bool(resume.get("uploaded") and resume.get("parsed"))
    if item_id == "skills":
        skills = profile.get("skills") if isinstance(profile.get("skills"), list) else []
        return len([s for s in skills if s]) >= SKILLS_MIN_COUNT
    if item_id == "education":
        edu = profile.get("education") or {}
        return (
            _filled(edu.get("qualification"))
            and _filled(edu.get("institution"))
            and _filled(edu.get("year"))
        )
    if item_id == "notice_period":
        return str(profile.get("noticePeriod") or "").strip() in NOTICE_PERIOD_OPTIONS
    if item_id == "preferred_locations":
        if profile.get("openToRemote") is True or profile.get("preferredLocations") == OPEN_TO_REMOTE:
            return True
        locs = profile.get("preferredLocations") if isinstance(profile.get("preferredLocations"), list) else []
        return any(
            isinstance(loc, dict) and loc.get("countryId") and loc.get("cityId")
            for loc in locs
        )
    if item_id == "expected_salary":
        if profile.get("salaryNegotiable") is True or profile.get("expectedSalary") == SALARY_NEGOTIABLE:
            return True
        salary = profile.get("expectedSalary") or {}
        if not isinstance(salary, dict):
            return False
        try:
            min_v = float(salary.get("min"))
            max_v = float(salary.get("max"))
        except (TypeError, ValueError):
            return False
        return _filled(salary.get("currency")) and min_v >= 0 and max_v > 0 and max_v >= min_v
    if item_id == "professional_summary":
        return count_real_chars(profile.get("professionalSummary")) >= SUMMARY_MIN_REAL_CHARS
    return False


def evaluate_profile_score(
    profile: Optional[dict[str, Any]] = None,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    profile = profile or {}
    config = config or create_default_profile_score_config()
    check = validate_profile_score_config(config)
    if not check["ok"]:
        raise ProfileScoreError(
            " ".join(check["errors"]),
            code="PROFILE_SCORE_CONFIG_INVALID",
            details=check["errors"],
        )

    items: list[dict[str, Any]] = []
    for item in config.get("items") or []:
        earned = _evaluate_item(item, profile)
        stale = is_item_stale(item, profile)
        items.append(
            {
                "id": item.get("id"),
                "row": item.get("row"),
                "section": item.get("section"),
                "label": item.get("label"),
                "weight": item.get("weight"),
                "earned": earned,
                "points": item.get("weight") if earned else 0,
                "qualityRule": item.get("id") in QUALITY_RULE_ITEM_IDS,
                "missingReason": None if earned else _missing_reason(str(item.get("id")), profile),
                "completionHref": item.get("completionHref"),
                "stale": stale,
                "staleAffectsApply": bool(stale and config.get("stalenessAffectsApplyGate")),
            }
        )

    total = int(sum(Number(item.get("points")) for item in items))
    gate = int(Number(config.get("applyGateScore") or APPLY_GATE_SCORE))
    apply_eligible = total >= gate and (
        not items
        or not config.get("stalenessAffectsApplyGate")
        or not any(item.get("staleAffectsApply") for item in items)
    )
    missing = sorted(
        [item for item in items if not item.get("earned")],
        key=lambda row: Number(row.get("weight")),
        reverse=True,
    )
    block_rates = {
        item["id"]: item.get("missingReason")
        for item in items
        if item.get("id") in QUALITY_RULE_ITEM_IDS and not item.get("earned")
    }
    return {
        "total": total,
        "applyEligible": apply_eligible,
        "applyGateScore": gate,
        "stalenessAffectsApplyGate": STALENESS_AFFECTS_APPLY_GATE,
        "configVersion": config.get("version"),
        "items": items,
        "missingOrderedByWeight": missing,
        "source": "server",
        "metrics": {
            "reached_100": bool(apply_eligible and total >= APPLY_GATE_SCORE),
            "score_total": total,
            "quality_rule_block_ids": ",".join(block_rates.keys()),
            "quality_rule_block_rates": block_rates,
        },
    }


def explain_score_decrease(
    previous: Optional[dict[str, Any]], next_score: Optional[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    if not previous or not next_score:
        return None
    if Number(next_score.get("total")) >= Number(previous.get("total")):
        return None
    prev_items = {row.get("id"): row for row in (previous.get("items") or [])}
    dropped = []
    for item in next_score.get("items") or []:
        before = prev_items.get(item.get("id"))
        if before and before.get("earned") and not item.get("earned"):
            dropped.append(
                {
                    "id": item.get("id"),
                    "label": item.get("label"),
                    "weight": item.get("weight"),
                    "why": item.get("missingReason"),
                }
            )
    return {
        "previousTotal": previous.get("total"),
        "newTotal": next_score.get("total"),
        "dropped": dropped,
        "why": " ".join(f"{row['label']}: {row['why']}" for row in dropped),
        "at": _iso_now(),
    }


def full_name_from_doc(doc: dict[str, Any]) -> str:
    first = str(doc.get("firstName") or "").strip()
    last = str(doc.get("lastName") or "").strip()
    if first or last:
        return " ".join(part for part in (first, last) if part).strip()
    return str(doc.get("name") or "").strip()


def parse_experience_years(doc: dict[str, Any]) -> Any:
    raw = doc.get("totalExperienceYears")
    if raw is not None and str(raw).strip() != "":
        return raw
    text = str(doc.get("totalExperience") or doc.get("total_experience") or "").strip()
    if not text:
        return None
    mapped = _EXPERIENCE_BUCKETS.get(text.lower())
    if mapped is not None:
        return mapped
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else text


def normalize_skills(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def normalize_education(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {
            "qualification": str(value.get("qualification") or value.get("degree") or "").strip(),
            "institution": str(value.get("institution") or "").strip(),
            "year": str(value.get("year") or value.get("end_date") or "").strip(),
        }
    if isinstance(value, list) and value:
        return normalize_education(value[0])
    return {"qualification": "", "institution": "", "year": ""}


def location_status_from_doc(doc: dict[str, Any]) -> str:
    explicit = str(doc.get("locationStatus") or "").strip()
    if doc.get("countryId") and doc.get("cityId") and explicit != "unconfirmed":
        return "confirmed"
    if str(doc.get("location") or doc.get("city") or doc.get("country") or "").strip():
        return "unconfirmed"
    return "missing"


def whatsapp_choice_from_doc(doc: dict[str, Any]) -> Optional[str]:
    explicit = doc.get("whatsappChoice")
    if explicit in {"confirmed", "declined"}:
        return explicit
    if doc.get("whatsappChoiceMade") or doc.get("whatsappDeclined") or "whatsappOptIn" in doc:
        if doc.get("whatsappOptIn") is True:
            return "confirmed"
        if doc.get("whatsappChoiceMade") or doc.get("whatsappDeclined"):
            return "declined"
    return None


def snapshot_from_candidate(
    doc: dict[str, Any],
    *,
    has_resume: bool = False,
    resume_parsed: Optional[bool] = None,
    parse_error: Optional[str] = None,
) -> dict[str, Any]:
    uploaded = bool(has_resume or doc.get("resumeUploaded") or doc.get("resume_file_id"))
    parsed = resume_parsed if resume_parsed is not None else bool(doc.get("resumeParsed"))
    if uploaded and resume_parsed is None and doc.get("resumeParsed") is None:
        parsed = uploaded
    skills = normalize_skills(doc.get("skills"))
    education = normalize_education(doc.get("education"))
    preferred = doc.get("preferredLocations")
    if doc.get("openToRemote"):
        preferred = OPEN_TO_REMOTE
    elif not isinstance(preferred, list):
        if doc.get("preferredCountryId") and doc.get("preferredCityId"):
            preferred = [
                {
                    "countryId": doc.get("preferredCountryId"),
                    "cityId": doc.get("preferredCityId"),
                }
            ]
        else:
            preferred = []
    salary: Any
    if doc.get("salaryNegotiable"):
        salary = SALARY_NEGOTIABLE
    else:
        salary = {
            "min": doc.get("salaryMin"),
            "max": doc.get("salaryMax"),
            "currency": doc.get("salaryCurrency"),
        }
    email_verified = bool(
        doc.get("emailVerified")
        or doc.get("email_verified")
        or (doc.get("auth_provider") in {"google", "microsoft"} and doc.get("email"))
    )
    return {
        "emailVerified": email_verified,
        "fullName": full_name_from_doc(doc),
        "mobileOtpVerified": bool(doc.get("mobile_verified") or doc.get("mobileVerified")),
        "whatsappChoice": whatsapp_choice_from_doc(doc),
        "countryId": doc.get("countryId"),
        "cityId": doc.get("cityId"),
        "locationStatus": location_status_from_doc(doc),
        "totalExperienceYears": parse_experience_years(doc),
        "jobTitle": doc.get("jobTitle") or doc.get("role"),
        "employer": doc.get("employer") or doc.get("currentCompany") or doc.get("current_company"),
        "resume": {
            "uploaded": uploaded,
            "parsed": bool(parsed) if uploaded else False,
            "parseError": parse_error or doc.get("resumeParseError"),
        },
        "skills": skills,
        "education": education,
        "noticePeriod": doc.get("noticePeriod"),
        "openToRemote": bool(doc.get("openToRemote")),
        "preferredLocations": preferred,
        "salaryNegotiable": bool(doc.get("salaryNegotiable")),
        "expectedSalary": salary,
        "professionalSummary": doc.get("professionalSummary") or doc.get("summary") or "",
        "freshness": doc.get("freshness") or {},
        "userEditedFields": doc.get("userEditedFields") or [],
        "credits": doc.get("credits") or [],
    }


def personal_info_from_doc(doc: dict[str, Any], *, phone: str = "") -> dict[str, Any]:
    first = str(doc.get("firstName") or "").strip()
    last = str(doc.get("lastName") or "").strip()
    education = normalize_education(doc.get("education"))
    return {
        "fullName": full_name_from_doc(doc),
        "firstName": first,
        "lastName": last,
        "email": doc.get("email") or "",
        "emailVerified": bool(
            doc.get("emailVerified")
            or doc.get("email_verified")
            or (doc.get("auth_provider") in {"google", "microsoft"} and doc.get("email"))
        ),
        "phone": phone or doc.get("phone") or "",
        "location": str(doc.get("location") or "").strip()
        or ", ".join(
            part
            for part in (str(doc.get("city") or "").strip(), str(doc.get("country") or "").strip())
            if part and part.lower() != "not specified"
        ),
        "city": doc.get("city") or "",
        "country": doc.get("country") or "",
        "countryId": doc.get("countryId") or "",
        "cityId": doc.get("cityId") or "",
        "countryName": doc.get("country") or "",
        "cityName": doc.get("city") or "",
        "locationStatus": location_status_from_doc(doc),
        "totalExperience": doc.get("totalExperience") or doc.get("total_experience") or "",
        "totalExperienceYears": parse_experience_years(doc),
        "linkedinUrl": doc.get("linkedinUrl") or doc.get("linkedin") or "",
        "currentCompany": doc.get("currentCompany") or doc.get("current_company") or "",
        "employer": doc.get("employer") or doc.get("currentCompany") or "",
        "jobTitle": doc.get("jobTitle") or doc.get("role") or "",
        "isFresher": bool(doc.get("isFresher")),
        "noticePeriod": doc.get("noticePeriod") or "",
        "preferredCountryId": doc.get("preferredCountryId") or "",
        "preferredCityId": doc.get("preferredCityId") or "",
        "openToRemote": bool(doc.get("openToRemote")),
        "salaryNegotiable": bool(doc.get("salaryNegotiable")),
        "salaryCurrency": doc.get("salaryCurrency") or "INR",
        "salaryMin": doc.get("salaryMin"),
        "salaryMax": doc.get("salaryMax"),
        "professionalSummary": doc.get("professionalSummary") or "",
        "whatsappOptIn": bool(doc.get("whatsappOptIn", False)),
        "whatsappChoiceMade": bool(doc.get("whatsappChoiceMade")),
        "whatsappChoice": whatsapp_choice_from_doc(doc),
        "skills": normalize_skills(doc.get("skills")),
        "education": education,
        "qualification": education.get("qualification") or "",
        "institution": education.get("institution") or "",
        "educationYear": education.get("year") or "",
        "freshness": doc.get("freshness") or {},
        "userEditedFields": doc.get("userEditedFields") or [],
        "credits": doc.get("credits") or [],
        "resume": {
            "parsed": bool(doc.get("resumeParsed")),
            "parseError": doc.get("resumeParseError") or "",
        },
    }


def _db():
    from app.database import get_db

    return get_db()


def load_org_config(org_id: str = "default") -> dict[str, Any]:
    key = str(org_id or "default").strip() or "default"
    stored = _db()["org_profile_score_config"].find_one({"_id": key}) or _db()[
        "org_profile_score_config"
    ].find_one({"orgId": key})
    if not stored:
        return create_default_profile_score_config(key)
    stored.pop("_id", None)
    check = validate_profile_score_config(stored)
    if not check["ok"]:
        logger.warning("Invalid stored profile-score config for %s: %s", key, check["errors"])
        return create_default_profile_score_config(key)
    stored.setdefault("orgId", key)
    return stored


def save_org_config(org_id: str, config: dict[str, Any]) -> dict[str, Any]:
    key = str(org_id or "default").strip() or "default"
    merged = {**create_default_profile_score_config(key), **config, "orgId": key}
    merged["stalenessAffectsApplyGate"] = False
    merged["applyGateScore"] = APPLY_GATE_SCORE
    assert_profile_score_config(merged)
    merged["updatedAt"] = _now()
    _db()["org_profile_score_config"].update_one(
        {"_id": key},
        {"$set": merged},
        upsert=True,
    )
    merged.pop("_id", None)
    return merged


def _active_resume(candidate_id: str) -> Optional[dict[str, Any]]:
    return _db()["ProfileLibrary"].find_one(
        {"candidate_id": str(candidate_id), "is_active": True}
    )


def _record_rollout_metrics(score: dict[str, Any]) -> None:
    try:
        inc: dict[str, int] = {"evaluations": 1}
        if score.get("applyEligible"):
            inc["at_100"] = 1
        for item_id in (score.get("metrics") or {}).get("quality_rule_block_rates") or {}:
            inc[f"block_{item_id}"] = 1
        _db()["profile_score_rollout"].update_one(
            {"_id": "global"},
            {"$inc": inc, "$set": {"updatedAt": _now()}},
            upsert=True,
        )
    except Exception:
        logger.exception("Failed recording profile-score rollout metrics")


def recalculate_and_persist(
    candidate_id: str,
    *,
    org_id: str = "default",
) -> dict[str, Any]:
    from bson import ObjectId

    db = _db()
    try:
        oid = ObjectId(candidate_id)
    except Exception as exc:
        raise ProfileScoreError("Invalid candidate id.") from exc
    doc = db["candidates"].find_one({"_id": oid})
    if not doc:
        raise ProfileScoreError("Candidate not found.", status_code=404)
    resume = _active_resume(candidate_id)
    snapshot = snapshot_from_candidate(
        doc,
        has_resume=bool(resume),
        resume_parsed=bool(doc.get("resumeParsed")) if resume else False,
        parse_error=doc.get("resumeParseError"),
    )
    if resume:
        snapshot["resume"]["uploaded"] = True
        if doc.get("resumeParsed") is None:
            snapshot["resume"]["parsed"] = bool(resume.get("structured_resume"))
    config = load_org_config(org_id or doc.get("orgId") or "default")
    score = evaluate_profile_score(snapshot, config)
    previous = doc.get("profileScore") if isinstance(doc.get("profileScore"), dict) else None
    explanation = explain_score_decrease(previous, score)
    updates: dict[str, Any] = {
        "profileScore": score,
        "profileScoreUpdatedAt": _now(),
    }
    if score.get("applyEligible") and not doc.get("profileScoreReached100At"):
        updates["profileScoreReached100At"] = _now()
    if explanation:
        trail = list(doc.get("profileScoreAudit") or [])
        trail.insert(0, explanation)
        updates["profileScoreAudit"] = trail[:AUDIT_CAP]
        score["decreaseAudit"] = explanation
    db["candidates"].update_one({"_id": oid}, {"$set": updates})
    _record_rollout_metrics(score)
    score["audit"] = (updates.get("profileScoreAudit") or doc.get("profileScoreAudit") or [])[:10]
    if score.get("applyEligible"):
        try:
            from app.credits.hooks import on_profile_score_100

            # Always attempt; ledger idempotency prevents double-pay. Retries
            # cover terms-blocked first unlocks and pre-credits migrations.
            award = on_profile_score_100(candidate_id, first_time=True)
            if award:
                score["creditAward"] = {
                    "awarded": bool(award.get("awarded")),
                    "duplicate": bool(award.get("duplicate")),
                    "toast": award.get("toast"),
                    "balance": award.get("balance"),
                    "reason": award.get("reason"),
                    "requiresTerms": award.get("requiresTerms"),
                }
        except Exception:
            logger.exception("Credit award on profile 100 failed")
    return score


def candidate_score_payload(candidate_id: str, *, org_id: str = "default") -> dict[str, Any]:
    return recalculate_and_persist(candidate_id, org_id=org_id)


def apply_gate_block(candidate_id: str) -> Optional[dict[str, Any]]:
    score = recalculate_and_persist(candidate_id)
    if score.get("applyEligible"):
        return None
    missing = score.get("missingOrderedByWeight") or []
    top = missing[0]["missingReason"] if missing else "Complete your profile to apply."
    return {
        "eligible": False,
        "reason": "profile_score",
        "profile_score_incomplete": True,
        "message": top,
        "profile_score": {
            "total": score.get("total"),
            "applyEligible": False,
            "applyGateScore": score.get("applyGateScore"),
            "missingOrderedByWeight": missing,
        },
    }


HAND_EDIT_KEYS = {
    "fullName",
    "jobTitle",
    "employer",
    "currentCompany",
    "professionalSummary",
    "noticePeriod",
    "skills",
    "education",
    "countryId",
    "cityId",
    "location",
    "totalExperienceYears",
    "preferredCountryId",
    "preferredCityId",
    "openToRemote",
    "salaryNegotiable",
    "salaryMin",
    "salaryMax",
    "salaryCurrency",
    "linkedinUrl",
}

LOCKED_IF_VERIFIED = {"phone", "email"}


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = str(full_name or "").strip().split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else first
    return first, last


def location_persist_fields(payload: dict[str, Any], existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    existing = existing or {}
    country_id = str(payload.get("countryId") or existing.get("countryId") or "").strip()
    city_id = str(payload.get("cityId") or existing.get("cityId") or "").strip()
    country = str(
        payload.get("country")
        or payload.get("countryName")
        or existing.get("country")
        or ""
    ).strip()
    city = str(
        payload.get("city") or payload.get("cityName") or existing.get("city") or ""
    ).strip()
    location = str(payload.get("location") or "").strip()
    if not location:
        location = ", ".join(part for part in (city, country) if part)
    status = payload.get("locationStatus")
    if country_id and city_id and status != "unconfirmed":
        status = "confirmed"
    elif location or city or country:
        status = status or "unconfirmed"
    else:
        status = "missing"
    fields: dict[str, Any] = {
        "countryId": country_id,
        "cityId": city_id,
        "country": country,
        "city": city,
        "location": location,
        "locationStatus": status,
    }
    return fields


def stamp_freshness(existing: dict[str, Any], *item_ids: str) -> dict[str, Any]:
    stamps = dict(existing.get("freshness") or {})
    now = _iso_now()
    for item_id in item_ids:
        if item_id:
            stamps[item_id] = now
    return stamps


def fields_to_candidate_updates(
    fields: dict[str, Any],
    existing: dict[str, Any],
    *,
    source: str = "profile",
) -> dict[str, Any]:
    """Map PATCH /profile-fields onto candidate document keys."""
    updates: dict[str, Any] = {}
    edited = list(existing.get("userEditedFields") or [])
    verified_email = bool(existing.get("emailVerified") or existing.get("email"))
    verified_mobile = bool(existing.get("mobile_verified") or existing.get("mobileVerified"))

    if fields.get("confirmFreshness"):
        updates["freshness"] = stamp_freshness(existing, str(fields.get("confirmFreshness")))

    for key, value in fields.items():
        if key in {"confirmFreshness", "overwriteHandEdit"}:
            continue
        if key in LOCKED_IF_VERIFIED:
            if key == "email" and verified_email:
                continue
            if key == "phone" and verified_mobile:
                continue
        if key in edited and source == "resume" and not fields.get("overwriteHandEdit"):
            continue
        if key == "fullName":
            result = validate_name(value, required=False)
            if value and not result.get("ok"):
                raise ProfileScoreError(result["message"])
            if result.get("placeholder"):
                raise ProfileScoreError("Please enter a valid name using letters only.")
            name = result.get("value") or ""
            if name:
                first, last = split_full_name(name)
                updates["name"] = name
                updates["firstName"] = first
                updates["lastName"] = last
        elif key == "email":
            from app.kv75 import validate_email

            result = validate_email(value, required=False)
            if value and not result.get("ok"):
                raise ProfileScoreError(result["message"])
            if result.get("value"):
                updates["email"] = result["value"]
        elif key == "jobTitle":
            result = validate_job_title(value, required=False)
            if value and not result.get("ok"):
                raise ProfileScoreError(result["message"])
            updates["jobTitle"] = result.get("value") or ""
        elif key in {"employer", "currentCompany"}:
            text = str(value or "").strip()
            updates["employer"] = text
            updates["currentCompany"] = text
            updates["freshness"] = stamp_freshness(
                {**existing, **updates}, "employer"
            )
        elif key == "professionalSummary":
            from app.kv75 import validate_free_text

            result = validate_free_text(value, required=False, max_chars=2000)
            if not result.get("ok"):
                raise ProfileScoreError(result["message"])
            updates["professionalSummary"] = result.get("value") or ""
        elif key == "noticePeriod":
            from app.kv75 import validate_enum

            result = validate_enum(value, NOTICE_PERIOD_OPTIONS, required=False)
            if value and not result.get("ok"):
                raise ProfileScoreError(result["message"])
            updates["noticePeriod"] = result.get("value") or ""
            if updates["noticePeriod"]:
                updates["freshness"] = stamp_freshness(
                    {**existing, **updates}, "notice_period"
                )
        elif key == "skills":
            updates["skills"] = normalize_skills(value)
        elif key == "education":
            updates["education"] = normalize_education(value)
        elif key == "totalExperienceYears":
            result = validate_experience_years(value, required=False)
            if value not in (None, "") and not result.get("ok"):
                raise ProfileScoreError(result["message"])
            if result.get("value") is not None:
                updates["totalExperienceYears"] = result["value"]
                updates["totalExperience"] = str(result["value"])
        elif key == "credits":
            if isinstance(value, list):
                updates["credits"] = [str(item) for item in value if str(item).strip()]
        elif key in {
            "countryId",
            "cityId",
            "country",
            "city",
            "countryName",
            "cityName",
            "location",
            "locationStatus",
        }:
            updates.update(location_persist_fields({**fields, **updates}, existing))
        elif key in {
            "preferredCountryId",
            "preferredCityId",
            "openToRemote",
            "salaryNegotiable",
            "salaryCurrency",
            "salaryMin",
            "salaryMax",
            "linkedinUrl",
            "isFresher",
        }:
            if key == "linkedinUrl":
                from app.kv75 import validate_url

                result = validate_url(value, required=False, kind="linkedin")
                if value and not result.get("ok"):
                    raise ProfileScoreError(result["message"])
                updates["linkedinUrl"] = result.get("value") or ""
            elif key in {"openToRemote", "salaryNegotiable", "isFresher"}:
                updates[key] = bool(value)
            elif key in {"salaryMin", "salaryMax"}:
                try:
                    updates[key] = float(value) if value not in (None, "") else None
                except (TypeError, ValueError) as exc:
                    raise ProfileScoreError("Enter a valid number.") from exc
                updates["freshness"] = stamp_freshness(
                    {**existing, **updates}, "expected_salary"
                )
            else:
                updates[key] = value
        elif key == "phone":
            continue

        if key in HAND_EDIT_KEYS and source != "resume":
            if key not in edited:
                edited.append(key)
        if source == "resume" and key in HAND_EDIT_KEYS and key not in edited:
            edited.append(key)

    if edited:
        updates["userEditedFields"] = edited
    return updates


def extraction_from_structured_resume(
    structured: Any,
    *,
    mobile_verified: bool = False,
    email_verified: bool = False,
) -> dict[str, Any]:
    if not isinstance(structured, dict) or not structured:
        return {
            "fields": [],
            "parsed": False,
            "parseError": "We could not read that resume. Upload a readable PDF or Word file.",
        }

    personal = structured.get("personal_information") or structured.get("personalInformation") or {}
    work = structured.get("work_experience") or structured.get("workExperience") or []
    latest = work[0] if isinstance(work, list) and work else {}
    skills_block = structured.get("skills") or {}
    skill_list: list[str] = []
    if isinstance(skills_block, dict):
        for key in ("technical_skills", "tools_and_technologies", "soft_skills"):
            skill_list.extend(normalize_skills(skills_block.get(key)))
    elif isinstance(skills_block, list):
        skill_list = normalize_skills(skills_block)
    seen: set[str] = set()
    unique_skills = []
    for skill in skill_list:
        key = skill.lower()
        if key not in seen:
            seen.add(key)
            unique_skills.append(skill)

    education = structured.get("education") or []
    edu = normalize_education(education[0] if isinstance(education, list) and education else education)
    summary = structured.get("summary") or structured.get("objective") or ""

    def field(key: str, label: str, value: Any, *, verified: bool = False, confidence: float = 0.0) -> dict[str, Any]:
        filled = _filled(value)
        return {
            "key": key,
            "label": label,
            "value": value if value is not None else "",
            "confidence": confidence if confidence else (0.8 if filled else 0.2),
            "verified": verified,
        }

    fields = [
        field("fullName", "Full name", personal.get("full_name") or personal.get("fullName")),
        field("email", "Email", personal.get("email"), verified=email_verified),
        field("phone", "Phone", personal.get("phone"), verified=mobile_verified),
        field("jobTitle", "Job title", latest.get("job_title") or latest.get("jobTitle") or personal.get("title")),
        field("employer", "Employer", latest.get("organization") or latest.get("company")),
        field("professionalSummary", "Professional summary", summary),
        field("linkedinUrl", "LinkedIn", personal.get("linkedin")),
        field("skills", "Skills", unique_skills, confidence=0.7 if unique_skills else 0.2),
        field("education", "Education", edu, confidence=0.7 if edu.get("qualification") else 0.2),
        field("location", "Location", personal.get("location")),
    ]
    return {"fields": fields, "parsed": True, "parseError": None}
