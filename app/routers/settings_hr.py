"""Endpoints for HR Recruit Settings."""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from datetime import datetime, timedelta

from app.auth import AuthPrincipal, require_hr, require_hr_account
from app.branding_images import optimize_branding_image
from app.company_branding import propagate_hr_branding_to_jobs
from app.database import get_db, get_gridfs
from app.hr_tenant import is_company_name_locked, propagate_company_name_to_jobs
from app.email.service import get_email_service
from app.email.templates import EmailTemplates
from app.otp_config import send_sms_otp
from app.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


def _hr_users_collection():
    return get_db()["hr_users"]


def _hr_login_email(doc: dict) -> str:
    """OAuth sign-in email for this HR account (not onboarding work contact)."""
    return (doc.get("email") or doc.get("workEmail") or "").strip().lower()


def _generate_otp() -> str:
    return str(secrets.randbelow(900000) + 100000)


class VerifyOtpRequest(BaseModel):
    otp: str

class MobileOtpRequest(BaseModel):
    phone: str


def _otp_store_collection():
    return get_db()["otp_store"]


class AccountUpdateRequest(BaseModel):
    fullName: str | None = None
    email: str | None = None
    phone: str | None = None
    designation: str | None = None


class CompanyUpdateRequest(BaseModel):
    companyName: str | None = None
    companyType: str | None = None
    industry: str | None = None
    companySize: str | None = None
    website: str | None = None
    headquarters: str | None = None
    foundedYear: str | None = None
    linkedin: str | None = None
    aboutCompany: str | None = None
    companyLogo: str | None = None
    companyBanner: str | None = None


BRANDING_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
BRANDING_IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


class OnboardingQuestionItem(BaseModel):
    """Single onboarding question in the HR profile library."""

    model_config = ConfigDict(extra="ignore")

    id: str
    question: str
    type: str = "Text Input"
    requirement: str = "Optional"
    options: Optional[List[str]] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data):
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        if normalized.get("id") is not None:
            normalized["id"] = str(normalized["id"]).strip()

        question = normalized.get("question")
        if not question and normalized.get("text"):
            question = normalized.get("text")
        if question is not None and not isinstance(question, str):
            question = str(question)
        normalized["question"] = (question or "").strip()

        for key in ("type", "requirement"):
            value = normalized.get(key)
            if value is not None and not isinstance(value, str):
                normalized[key] = str(value)

        normalized.setdefault("type", "Text Input")
        normalized.setdefault("requirement", "Optional")
        return normalized

    @field_validator("question")
    @classmethod
    def question_not_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Question text cannot be empty")
        return cleaned

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Question id cannot be empty")
        return cleaned


class OnboardingQuestionsSaveRequest(BaseModel):
    questions: list[OnboardingQuestionItem]

    @field_validator("questions")
    @classmethod
    def require_at_least_one(cls, value: list[OnboardingQuestionItem]) -> list[OnboardingQuestionItem]:
        if not value:
            raise ValueError("At least one question is required")
        return value


class ApplicationPolicyRequest(BaseModel):
    reapply_cooldown_months: int = Field(ge=1, le=60, default=6)


DEFAULT_HR_ONBOARDING_QUESTIONS: list[dict] = [
    # 1. Notice Period & Availability
    {
        "id": "q1",
        "question": "Are you currently serving a notice period?",
        "type": "Yes / No",
        "requirement": "Required",
    },
    {
        "id": "q2",
        "question": "What is your official notice period (in days)?",
        "type": "Numeric",
        "requirement": "Required",
    },
    {
        "id": "q3",
        "question": "What is your earliest available joining date or Last Working Day (LWD)?",
        "type": "Text Input",
        "requirement": "Required",
    },
    # 2. Compensation
    {
        "id": "q4",
        "question": "What is your current annual total CTC in INR (LPA)?",
        "type": "Numeric",
        "requirement": "Required",
    },
    {
        "id": "q5",
        "question": "What is your expected annual total CTC in INR (LPA)?",
        "type": "Numeric",
        "requirement": "Required",
    },
    # 3. Location & Mobility
    {
        "id": "q6",
        "question": "What is your current city of residence?",
        "type": "Text Input",
        "requirement": "Required",
    },
    {
        "id": "q7",
        "question": "Are you willing to relocate to [Job Location] at your own expense?",
        "type": "Multiple Choice",
        "requirement": "Required",
        "options": ["Yes", "No", "Currently reside in [Job Location]"],
    },
    # 4. Education & Work Experience
    {
        "id": "q8",
        "question": "What is your highest level of completed education?",
        "type": "Multiple Choice",
        "requirement": "Required",
        "options": [
            "Doctorate / Ph.D.",
            "Master's Degree",
            "Bachelor's Degree",
            "Diploma",
            "High School",
        ],
    },
    {
        "id": "q9",
        "question": "How many years of relevant experience do you have in [Primary Skill]?",
        "type": "Numeric",
        "requirement": "Required",
    },
    {
        "id": "q10",
        "question": "Do you have any break in your career?",
        "type": "Yes / No",
        "requirement": "Required",
    },
    {
        "id": "q11",
        "question": "If yes, please describe your career break (duration and reason).",
        "type": "Text Input",
        "requirement": "Optional",
    },
]


# ── Profile endpoints ─────────────────────────────────────────────────

@router.get("/api/recruit/account")
async def get_account(principal: Annotated[AuthPrincipal, Depends(require_hr_account)]):
    doc = _hr_users_collection().find_one({"_id": ObjectId(principal.subject)})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "fullName": doc.get("name", ""),
        "email": doc.get("email", ""),
        "phone": doc.get("kyc_mobile", doc.get("phone", "")),
        "designation": doc.get("role", ""),
    }


@router.patch("/api/recruit/account")
async def update_account(
    payload: AccountUpdateRequest,
    principal: Annotated[AuthPrincipal, Depends(require_hr_account)]
):
    update_data = {}
    if payload.fullName is not None:
        update_data["name"] = payload.fullName
    if payload.email is not None:
        update_data["email"] = payload.email
    if payload.phone is not None:
        update_data["phone"] = payload.phone
    if payload.designation is not None:
        update_data["role"] = payload.designation

    if not update_data:
        return {}

    _hr_users_collection().update_one(
        {"_id": ObjectId(principal.subject)}, {"$set": update_data}
    )
    return payload.model_dump(exclude_unset=True)


@router.get("/api/recruit/company")
async def get_company(principal: Annotated[AuthPrincipal, Depends(require_hr_account)]):
    doc = _hr_users_collection().find_one({"_id": ObjectId(principal.subject)})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "companyName": doc.get("companyName", ""),
        "companyNameLocked": is_company_name_locked(doc),
        "companyType": doc.get("companyType", ""),
        "industry": doc.get("industry", ""),
        "companySize": doc.get("companySize", ""),
        "website": doc.get("website", ""),
        "headquarters": doc.get("hqLocation", doc.get("headquarters", "")),
        "foundedYear": doc.get("foundedYear", ""),
        "linkedin": doc.get("linkedinUrl", doc.get("linkedin", "")),
        "aboutCompany": doc.get("aboutCompany", ""),
        "companyLogo": doc.get("companyLogo", ""),
        "companyBanner": doc.get("companyBanner", ""),
    }


@router.patch("/api/recruit/company")
async def update_company(
    payload: CompanyUpdateRequest,
    principal: Annotated[AuthPrincipal, Depends(require_hr_account)]
):
    update_data = {}
    db = get_db()
    existing = _hr_users_collection().find_one({"_id": ObjectId(principal.subject)})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.companyName is not None:
        incoming = str(payload.companyName).strip()
        current = str(existing.get("companyName") or "").strip()
        if is_company_name_locked(existing):
            if incoming and incoming != current:
                raise HTTPException(
                    status_code=400,
                    detail="Company name is locked after onboarding and cannot be changed.",
                )
        elif incoming:
            update_data["companyName"] = incoming
            update_data["company_name_locked"] = True
    if payload.companyType is not None:
        update_data["companyType"] = payload.companyType
    if payload.industry is not None:
        update_data["industry"] = payload.industry
    if payload.companySize is not None:
        update_data["companySize"] = payload.companySize
    if payload.website is not None:
        update_data["website"] = payload.website
    if payload.headquarters is not None:
        update_data["hqLocation"] = payload.headquarters
        update_data["headquarters"] = payload.headquarters
    if payload.foundedYear is not None:
        update_data["foundedYear"] = payload.foundedYear
    if payload.linkedin is not None:
        update_data["linkedinUrl"] = payload.linkedin
        update_data["linkedin"] = payload.linkedin
    if payload.aboutCompany is not None:
        update_data["aboutCompany"] = payload.aboutCompany
    if payload.companyLogo is not None:
        update_data["companyLogo"] = payload.companyLogo
    if payload.companyBanner is not None:
        update_data["companyBanner"] = payload.companyBanner

    if not update_data:
        return {}

    _hr_users_collection().update_one(
        {"_id": ObjectId(principal.subject)}, {"$set": update_data}
    )

    branding_fields = {"companyLogo", "companyBanner"}
    if branding_fields.intersection(update_data.keys()):
        propagate_hr_branding_to_jobs(
            principal.subject,
            db,
            logo=update_data.get("companyLogo"),
            banner=update_data.get("companyBanner"),
        )

    if update_data.get("company_name_locked") or update_data.get("companyName"):
        company_name = update_data.get("companyName") or existing.get("companyName", "")
        propagate_company_name_to_jobs(principal.subject, str(company_name).strip(), db)

    return payload.model_dump(exclude_unset=True)


@router.post("/api/hr/branding/upload")
async def upload_hr_branding(
    principal: Annotated[AuthPrincipal, Depends(require_hr_account)],
    file: UploadFile = File(...),
    kind: str = Form(...),
):
    """Upload company logo or job-details banner image for HR branding."""
    normalized_kind = (kind or "").strip().lower()
    if normalized_kind not in {"logo", "banner"}:
        raise HTTPException(status_code=400, detail="kind must be 'logo' or 'banner'")

    filename = file.filename or ""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    dotted_suffix = f".{suffix}" if suffix else ""
    if dotted_suffix not in BRANDING_IMAGE_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type. Allowed: {sorted(BRANDING_IMAGE_SUFFIXES)}",
        )

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type and content_type not in BRANDING_IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported image content type")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image must be 5 MB or smaller")

    optimized, content_type, ext = optimize_branding_image(
        raw, normalized_kind  # type: ignore[arg-type]
    )
    stored_name = f"company_{normalized_kind}{ext}"

    fs = get_gridfs()
    stored_id = fs.put(
        optimized,
        filename=stored_name,
        content_type=content_type,
        metadata={
            "kind": f"company_{normalized_kind}",
            "hr_user_id": principal.subject,
            "optimized": True,
            "original_bytes": len(raw),
            "stored_bytes": len(optimized),
        },
    )

    url = f"/api/files/{stored_id}"
    field_name = "companyLogo" if normalized_kind == "logo" else "companyBanner"
    _hr_users_collection().update_one(
        {"_id": ObjectId(principal.subject)},
        {"$set": {field_name: url}},
    )

    propagate_hr_branding_to_jobs(
        principal.subject,
        get_db(),
        logo=url if field_name == "companyLogo" else None,
        banner=url if field_name == "companyBanner" else None,
    )

    return {"url": url, "field": field_name, "optimized": True, "bytes": len(optimized)}


# ── HR onboarding question library ───────────────────────────────────

@router.get("/api/hr/onboarding-questions")
async def get_hr_onboarding_questions(
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    """Return the HR user's saved onboarding library, or system defaults."""
    doc = _hr_users_collection().find_one({"_id": ObjectId(principal.subject)})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")

    saved = doc.get("onboarding_questions")
    if saved:
        return {"questions": saved, "isCustom": True}

    return {"questions": DEFAULT_HR_ONBOARDING_QUESTIONS, "isCustom": False}


@router.post("/api/hr/onboarding-questions")
async def save_hr_onboarding_questions(
    payload: OnboardingQuestionsSaveRequest,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    """Replace the HR user's saved onboarding question library."""
    questions = []
    for q in payload.questions:
        item = {
            "id": q.id,
            "question": q.question,
            "type": q.type,
            "requirement": q.requirement,
        }
        if q.options:
            item["options"] = [str(opt).strip() for opt in q.options if str(opt).strip()]
        questions.append(item)
    result = _hr_users_collection().update_one(
        {"_id": ObjectId(principal.subject)},
        {"$set": {"onboarding_questions": questions}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"success": True, "questions": questions}


@router.get("/api/recruit/application-policy")
async def get_application_policy(
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    from app.services.application_policy import get_application_policy

    return get_application_policy(principal.subject)


@router.patch("/api/recruit/application-policy")
async def update_application_policy(
    payload: ApplicationPolicyRequest,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    from app.services.application_policy import save_application_policy

    return save_application_policy(
        principal.subject,
        payload.reapply_cooldown_months,
    )


# ── KYC data endpoints ────────────────────────────────────────────────

@router.get("/api/recruit/kyc")
async def get_kyc_data(
    principal: Annotated[AuthPrincipal, Depends(require_hr_account)]
):
    """Return KYC verification state for the authenticated HR user."""
    doc = _hr_users_collection().find_one({"_id": ObjectId(principal.subject)})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")

    email_verified = doc.get("kyc_email_verified", False)
    mobile_verified = doc.get("kyc_mobile_verified", False)
    login_email = _hr_login_email(doc)
    stored_kyc_email = (doc.get("kyc_email") or "").strip().lower()
    # Ignore a stale kyc_email left over from an older work-email preference.
    display_email = login_email
    if email_verified and stored_kyc_email:
        display_email = stored_kyc_email

    if email_verified and mobile_verified:
        status = "verified"
    else:
        status = "pending"

    return {
        "status": status,
        "mobileNumber": doc.get("kyc_mobile", ""),
        "mobileVerified": mobile_verified,
        "loginEmail": login_email,
        "emailAddress": display_email,
        "emailVerified": email_verified,
    }


@router.get("/api/recruit/kyc/status")
async def get_kyc_status(
    principal: Annotated[AuthPrincipal, Depends(require_hr_account)]
):
    """Lightweight status check used by the tab indicator."""
    doc = _hr_users_collection().find_one(
        {"_id": ObjectId(principal.subject)},
        {"kyc_email_verified": 1, "kyc_mobile_verified": 1},
    )
    if not doc:
        return {"status": "pending"}

    email_ok = doc.get("kyc_email_verified", False)
    mobile_ok = doc.get("kyc_mobile_verified", False)
    return {"status": "verified" if (email_ok and mobile_ok) else "pending"}


# ── Email OTP endpoints ───────────────────────────────────────────────

@router.post("/api/recruit/kyc/send-email-otp")
@limiter.limit("3/minute")
async def send_email_otp(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(require_hr_account)]
):
    doc = _hr_users_collection().find_one({"_id": ObjectId(principal.subject)})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")

    # Always use the OAuth login email — not onboarding workEmail.
    email = _hr_login_email(doc)
    if not email:
        raise HTTPException(
            status_code=400,
            detail="No registered email found on this account.",
        )

    otp = _generate_otp()

    _otp_store_collection().update_one(
        {"subject": principal.subject, "purpose": "email"},
        {
            "$set": {
                "otp": otp,
                "attempts": 0,
                "createdAt": datetime.utcnow(),
                "expiresAt": datetime.utcnow() + timedelta(minutes=5),
            }
        },
        upsert=True,
    )

    _hr_users_collection().update_one(
        {"_id": ObjectId(principal.subject)},
        {"$set": {"kyc_email": email, "kyc_email_verified": False}},
    )

    email_service = get_email_service()
    subject, html_body, text_body = EmailTemplates.kyc_verification_otp_template(otp)

    success = email_service.send_email(
        subject=subject,
        body_html=html_body,
        body_text=text_body,
        recipients=[email],
    )

    if not success:
        # Don't leave the OTP live if nothing was actually sent
        _otp_store_collection().delete_one({"subject": principal.subject, "purpose": "email"})
        raise HTTPException(
            status_code=502,
            detail="Failed to send verification email. Please try again shortly.",
        )

    return {"success": True, "sentTo": email}


@router.post("/api/recruit/kyc/verify-email-otp")
async def verify_email_otp(
    payload: VerifyOtpRequest, principal: Annotated[AuthPrincipal, Depends(require_hr_account)]
):
    record = _otp_store_collection().find_one({"subject": principal.subject, "purpose": "email"})
    if not record:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    if record.get("attempts", 0) >= 5:
        _otp_store_collection().delete_one({"_id": record["_id"]})
        raise HTTPException(status_code=429, detail="Too many attempts. Please request a new OTP.")

    if record["otp"] != payload.otp:
        _otp_store_collection().update_one({"_id": record["_id"]}, {"$inc": {"attempts": 1}})
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    _otp_store_collection().delete_one({"_id": record["_id"]})  # one-time use

    _hr_users_collection().update_one(
        {"_id": ObjectId(principal.subject)},
        {"$set": {"kyc_email_verified": True}},
    )
    return {"success": True}


# ── Mobile OTP endpoints (dummy / console) ─────────────────────────────

@router.post("/api/recruit/kyc/send-mobile-otp")
@limiter.limit("3/minute")
async def send_mobile_otp(
    request: Request,
    payload: MobileOtpRequest,
    principal: Annotated[AuthPrincipal, Depends(require_hr_account)],
):
    otp = _generate_otp()

    _otp_store_collection().update_one(
        {"subject": principal.subject, "purpose": "mobile"},
        {
            "$set": {
                "otp": otp,
                "attempts": 0,
                "createdAt": datetime.utcnow(),
                "expiresAt": datetime.utcnow() + timedelta(minutes=5),
            }
        },
        upsert=True,
    )

    _hr_users_collection().update_one(
        {"_id": ObjectId(principal.subject)},
        {"$set": {"kyc_mobile": payload.phone, "kyc_mobile_verified": False}},
    )

    # Send via dummy console provider (logs OTP to terminal)
    success = send_sms_otp(payload.phone, otp)
    if not success:
        _otp_store_collection().delete_one({"subject": principal.subject, "purpose": "mobile"})
        raise HTTPException(status_code=502, detail="Failed to send mobile OTP.")

    return {"success": True, "sentTo": payload.phone, "resend_cooldown_enabled": get_settings().otp_resend_cooldown_enabled, "retry_after_seconds": 0}


@router.post("/api/recruit/kyc/verify-mobile-otp")
async def verify_mobile_otp(
    payload: VerifyOtpRequest, principal: Annotated[AuthPrincipal, Depends(require_hr_account)]
):
    record = _otp_store_collection().find_one({"subject": principal.subject, "purpose": "mobile"})
    if not record:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    if record.get("attempts", 0) >= 5:
        _otp_store_collection().delete_one({"_id": record["_id"]})
        raise HTTPException(status_code=429, detail="Too many attempts. Please request a new OTP.")

    if record["otp"] != payload.otp:
        _otp_store_collection().update_one({"_id": record["_id"]}, {"$inc": {"attempts": 1}})
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    _otp_store_collection().delete_one({"_id": record["_id"]})  # one-time use

    _hr_users_collection().update_one(
        {"_id": ObjectId(principal.subject)},
        {"$set": {"kyc_mobile_verified": True}},
    )
    return {"success": True}