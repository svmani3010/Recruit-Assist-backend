"""Standard platform credits programme (fixed values).

Earning rules, catalogue prices, ceilings, and expiry are platform-wide
constants — not configurable per recruiter / org. A future Kovetted admin
panel may version these; HR portals must not edit them.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

TERMS_VERSION = 1
LEDGER_RETENTION_YEARS = 7
DEFAULT_ORG = "default"

DIRECTION_CREDIT = "credit"
DIRECTION_DEBIT = "debit"

# Quality markers on the candidate doc are unrelated to this ledger.
# Profile-score domain (15 items, apply gate 100) already exists and is the
# trigger for profile_score_100.

EARNING_RULES: dict[str, dict[str, Any]] = {
    "profile_score_100": {
        "label": "Reach a profile score of 100",
        "amount": 100,
        "capCount": 1,
        "capPeriod": "lifetime",
        "onceLifetime": True,
        "category": "profile",
        "immediateNotify": True,
        "enabled": True,
        "requires": "verified_event",
    },
    "employment_verified": {
        "label": "Add a verified employment record",
        "amount": 50,
        "capCount": 3,
        "capPeriod": "lifetime",
        "category": "profile",
        "immediateNotify": True,
        "enabled": True,
        "requires": "verification",
    },
    "skill_verified": {
        "label": "Verify a skill",
        "amount": 25,
        "capCount": 10,
        "capPeriod": "lifetime",
        "category": "profile",
        "immediateNotify": True,
        "enabled": True,
        "requires": "verification",
    },
    "resume_material_update": {
        "label": "Upload a materially updated resume",
        "amount": 50,
        "capCount": 1,
        "capPeriod": "90d",
        "category": "profile",
        "immediateNotify": True,
        "enabled": True,
        "requires": "material_change",
    },
    "assessment_submitted": {
        "label": "Complete a skill assessment",
        "amount": 100,
        "capCount": 5,
        "capPeriod": "rolling_12m",
        "category": "profile",
        "immediateNotify": True,
        "enabled": True,
        "awardOnSubmitRegardlessOfScore": True,
    },
    "apply_relevant": {
        "label": "Apply to a relevant job",
        "amount": 10,
        "capCount": 5,
        "capPeriod": "day",
        "secondaryCapCount": 30,
        "secondaryCapPeriod": "calendar_month",
        "category": "engagement",
        "immediateNotify": False,
        "enabled": True,
        "minMatchScore": 60,
    },
    "employer_view": {
        "label": "Employer views your profile",
        "amount": 10,
        "capCount": 3,
        "capPeriod": "day",
        "uniquePer": "employer_application_30d",
        "category": "engagement",
        "immediateNotify": False,
        "enabled": True,
    },
    "employer_shortlist": {
        "label": "Employer shortlists your profile",
        "amount": 30,
        "capCount": 50,
        "capPeriod": "rolling_12m",
        "category": "engagement",
        "immediateNotify": True,
        "enabled": True,
        "neverReverseOnReject": True,
    },
    "interview_attended": {
        "label": "Complete an interview",
        "amount": 50,
        "capCount": 20,
        "capPeriod": "rolling_12m",
        "category": "engagement",
        "immediateNotify": True,
        "enabled": True,
        "requiresInterviewerAttended": True,
    },
    "interview_feedback": {
        "label": "Provide interview feedback within 7 days",
        "amount": 20,
        "capCount": 1,
        "capPeriod": "per_interview",
        "windowDays": 7,
        "category": "engagement",
        "immediateNotify": True,
        "enabled": True,
    },
    "referral_profile_100": {
        "label": "Referred candidate reaches profile score 100",
        "amount": 50,
        "capCount": 20,
        "capPeriod": "rolling_12m",
        "category": "referral",
        "immediateNotify": True,
        "enabled": True,
    },
    "referral_shortlist": {
        "label": "Referred candidate is shortlisted",
        "amount": 100,
        "capCount": 10,
        "capPeriod": "rolling_12m",
        "category": "referral",
        "immediateNotify": True,
        "enabled": True,
        "oncePerReferredPerson": True,
    },
    "referral_hire_standard": {
        "label": "Referred candidate hired (standard role)",
        "amount": 500,
        "capCount": 5,
        "capPeriod": "rolling_12m",
        "category": "referral",
        "immediateNotify": True,
        "enabled": True,
        "clawbackDays": 90,
    },
    "referral_hire_premium": {
        "label": "Referred candidate hired (premium role)",
        "amount": 1000,
        "capCount": 5,
        "capPeriod": "rolling_12m",
        "category": "referral",
        "immediateNotify": True,
        "enabled": True,
        "clawbackDays": 90,
    },
}

CATALOGUE: dict[str, dict[str, Any]] = {
    "visibility_boost_7d": {
        "name": "7-day search visibility boost",
        "description": "Improves your position within already-relevant recruiter results. It never changes your match score, assessments, or hiring decisions.",
        "price": 200,
        "fulfilment": "automatic",
        "durationDays": 7,
        "benefitKey": "visibility_boost",
        "enabled": True,
        "manualSlaHours": None,
    },
    "resume_review": {
        "name": "Expert resume review",
        "description": "A recruiter-style review of your resume with written notes attached to the order.",
        "price": 400,
        "fulfilment": "manual",
        "durationDays": None,
        "benefitKey": "resume_review",
        "enabled": True,
        "manualSlaHours": 72,
    },
    "assessment_credit": {
        "name": "Free skill assessment",
        "description": "Unlock one additional skill assessment attempt.",
        "price": 150,
        "fulfilment": "automatic",
        "durationDays": 30,
        "benefitKey": "assessment_credit",
        "enabled": True,
        "manualSlaHours": None,
    },
}

SYSTEM_RULES = {
    "redeem": {"label": "Redemption", "category": "redeem"},
    "expire": {"label": "Expiry", "category": "expire"},
    "reverse": {"label": "Reversal", "category": "adjust"},
    "admin_award": {"label": "Administrator award", "category": "adjust"},
    "admin_adjust": {"label": "Administrator adjustment", "category": "adjust"},
    "refund": {"label": "Order refund", "category": "redeem"},
}

DEFAULT_CONFIG: dict[str, Any] = {
    "orgId": DEFAULT_ORG,
    "version": 1,
    "earningEnabled": True,
    "redemptionEnabled": True,
    "killSwitch": False,
    "indicativeCreditValue": 1.0,
    "currency": "INR",
    "expiryDays": 365,
    "expiryNoticeDays": [30, 7],
    "referralClickWindowDays": 30,
    "hireClawbackDays": 90,
    "premiumRolesDefined": False,
    "dailyEarningCeiling": 250,
    "monthlyEarningCeiling": 2000,
    "applyRelevanceThreshold": 60,
    "boostRankDelta": 5,
    "boostMaxPageShare": 0.2,
    "boostOptOut": False,
    "tierEnabled": False,
    "tierThresholds": {"bronze": 0, "silver": 500, "gold": 2000, "platinum": 5000},
    "abuse": {
        "burstAwardsPerHour": 20,
        "relatedReferralChainMax": 3,
        "employerViewFarmPerHour": 40,
        "assessmentMinSeconds": 30,
    },
    "issuanceAlertThreshold": 50000,
    "outstandingAlertCeiling": 500000,
    "termsVersion": TERMS_VERSION,
    "retentionYears": LEDGER_RETENTION_YEARS,
    "rules": deepcopy(EARNING_RULES),
    "catalogue": deepcopy(CATALOGUE),
    "readOnly": True,
    "configurableByOrg": False,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from app.database import get_db

    return get_db()


class CreditConfigError(Exception):
    def __init__(self, message: str, details: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


def validate_credit_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    rules = config.get("rules") or {}
    if not isinstance(rules, dict) or not rules:
        errors.append("At least one earning rule is required.")
        return errors
    for rule_id, rule in rules.items():
        if not isinstance(rule, dict):
            errors.append(f"{rule_id}: rule must be an object.")
            continue
        if int(rule.get("amount") or 0) <= 0 and rule_id not in SYSTEM_RULES:
            errors.append(f"{rule_id}: earning amount must be > 0.")
        cap_count = rule.get("capCount")
        cap_period = rule.get("capPeriod")
        if not cap_count or not cap_period:
            errors.append(
                f"{rule_id}: an earning rule without a cap cannot be configured."
            )
        if cap_count is not None and int(cap_count) < 1:
            errors.append(f"{rule_id}: capCount must be at least 1.")
    catalogue = config.get("catalogue") or {}
    for item_id, item in catalogue.items():
        if not isinstance(item, dict):
            errors.append(f"catalogue.{item_id}: must be an object.")
            continue
        if int(item.get("price") or 0) < 0:
            errors.append(f"catalogue.{item_id}: price cannot be negative.")
    if int(config.get("expiryDays") or 0) < 1:
        errors.append("expiryDays must be at least 1.")
    if int(config.get("dailyEarningCeiling") or 0) < 1:
        errors.append("dailyEarningCeiling is required.")
    if int(config.get("monthlyEarningCeiling") or 0) < 1:
        errors.append("monthlyEarningCeiling is required.")
    return errors


def create_default_credit_config(org_id: str = DEFAULT_ORG) -> dict[str, Any]:
    doc = deepcopy(DEFAULT_CONFIG)
    doc["orgId"] = org_id or DEFAULT_ORG
    return doc


def load_credit_config(org_id: str = DEFAULT_ORG) -> dict[str, Any]:
    """Always return the platform-standard programme (ignores per-org DB overrides)."""
    return create_default_credit_config(org_id or DEFAULT_ORG)


def save_credit_config(
    org_id: str,
    patch: dict[str, Any],
    *,
    actor_id: str,
) -> dict[str, Any]:
    """HR / org portals cannot change credit amounts. Reserved for future platform admin."""
    raise CreditConfigError(
        "Credits programme values are platform-standard and cannot be changed from the recruiter portal.",
        ["configurableByOrg=false"],
    )


def rule_for(config: dict[str, Any], rule_id: str) -> dict[str, Any]:
    return dict((config.get("rules") or {}).get(rule_id) or {})


def catalogue_item(config: dict[str, Any], item_id: str) -> dict[str, Any]:
    return dict((config.get("catalogue") or {}).get(item_id) or {})
