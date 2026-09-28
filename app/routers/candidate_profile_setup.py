"""KV01 — Candidate mandatory profile setup (PROFILE_INCOMPLETE).

After email signup, candidates must complete:
  1. Basic details → 2. Mobile OTP → 3. WhatsApp preference → 4. Done

Partial progress is persisted on every step transition so session timeouts
and mid-flow logouts do not lose entered data.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth import AuthPrincipal, require_candidate
from app.database import get_db
from app.field_validation import optional_linkedin, require_person_name
from app.mobile_phone import (
    MobileValidationError,
    format_mobile_display,
    format_otp_masked_display,
    require_mobile_e164,
    split_e164,
)
from app.rate_limit import limiter
from app.services.candidate_region import (
    extract_region_state,
    onboarding_region_persist_fields,
)
from app.services.candidate_mobile_identity import (
    MobileClaimStatus,
    assert_unique_mobile,
    claim_status_payload,
    get_plaintext_mobile,
    mark_mobile_verified,
    normalize_mobile,
    reactivation_preview_for_number,
    resolve_mobile_claim,
    resolve_phone_for_candidate_self,
    upsert_encrypted_mobile,
)
from app.services.candidate_whatsapp_otp import (
    STATE_MOBILE_VERIFIED,
    check_otp_rate_limits,
    invalidate_pending_otp_for_change_number,
    issue_candidate_whatsapp_otp,
    record_otp_send_event,
    verify_stored_otp,
)

router = APIRouter(tags=["candidate-profile-setup"])

PROFILE_INCOMPLETE = "PROFILE_INCOMPLETE"
PROFILE_COMPLETE = "PROFILE_COMPLETE"
SETUP_BLOCK_MESSAGE = (
    "Please complete your profile and verify your mobile number to continue."
)

STEP_BASIC = 1
STEP_MOBILE = 2
STEP_WHATSAPP = 3
STEP_DONE = 4


def _candidates():
    return get_db()["candidates"]


def _oid(candidate_id: str) -> ObjectId:
    try:
        return ObjectId(candidate_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid candidate id.") from exc


def _assert_self(principal: AuthPrincipal, candidate_id: str) -> None:
    if principal.candidate_id != candidate_id:
        raise HTTPException(status_code=403, detail="Not authorized for this candidate.")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _recalc(candidate_id: str) -> None:
    try:
        from app.services.profile_score import recalculate_and_persist

        recalculate_and_persist(candidate_id)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Profile score recalc failed for candidate %s", candidate_id
        )


def _normalize_phone(phone: str) -> str:
    try:
        return require_mobile_e164(phone)
    except MobileValidationError:
        return re.sub(r"[^\d+]", "", str(phone or "").strip())


def _profile_state(doc: dict) -> str:
    if doc.get("onboardingCompleted"):
        return PROFILE_COMPLETE
    return str(doc.get("profileState") or PROFILE_INCOMPLETE)


def _setup_step(doc: dict) -> int:
    if doc.get("onboardingCompleted"):
        return STEP_DONE
    raw = doc.get("profileSetupStep", STEP_BASIC)
    try:
        step = int(raw)
    except (TypeError, ValueError):
        step = STEP_BASIC
    return max(STEP_BASIC, min(STEP_DONE, step))


def _full_name(doc: dict) -> str:
    first = str(doc.get("firstName") or "").strip()
    last = str(doc.get("lastName") or "").strip()
    if first or last:
        return " ".join(part for part in (first, last) if part).strip()
    return str(doc.get("name") or "").strip()


def _location(doc: dict) -> str:
    loc = str(doc.get("location") or "").strip()
    if loc:
        return loc
    city = str(doc.get("city") or "").strip()
    country = str(doc.get("country") or "").strip()
    if city and country and country.lower() != "not specified":
        return f"{city}, {country}"
    return city or country or ""


def serialize_setup_status(doc: dict) -> dict[str, Any]:
    state = _profile_state(doc)
    candidate_id = str(doc.get("_id") or "")
    phone = ""
    if candidate_id:
        try:
            phone = resolve_phone_for_candidate_self(candidate_id, doc)
        except HTTPException:
            phone = str(doc.get("phone") or "")
    else:
        phone = str(doc.get("phone") or "")
    region, dial, national = split_e164(phone) if phone else ("IN", "91", "")
    if not phone:
        region = doc.get("phoneCountry") or region
        dial = doc.get("phoneDialCode") or dial
        national = ""
    region_state = extract_region_state(doc)
    resolved = (
        region_state.resolved_region.model_dump()
        if region_state.resolved_region is not None
        else None
    )
    return {
        "profileState": state,
        "mobileVerificationState": (
            STATE_MOBILE_VERIFIED
            if bool(doc.get("mobile_verified") or doc.get("mobileVerified"))
            else None
        ),
        "profileSetupStep": _setup_step(doc),
        "onboardingCompleted": bool(doc.get("onboardingCompleted")),
        "mobileVerified": bool(doc.get("mobile_verified") or doc.get("mobileVerified")),
        "blockMessage": SETUP_BLOCK_MESSAGE if state == PROFILE_INCOMPLETE else None,
        "resolvedRegion": resolved,
        "regionSource": region_state.region_source,
        "isManualOverride": region_state.is_manual_override,
        "countryField": region_state.country_field,
        "phoneCountryCode": region_state.phone_country_code,
        "personal_info": {
            "fullName": _full_name(doc),
            "firstName": doc.get("firstName") or "",
            "lastName": doc.get("lastName") or "",
            "email": doc.get("email") or "",
            "phone": phone,
            "phoneDisplay": format_mobile_display(phone) if phone else "",
            "phoneMasked": (
                doc.get("phone_masked")
                or doc.get("phoneMasked")
                or (format_otp_masked_display(phone) if phone else "")
            ),
            "phoneCountry": doc.get("phoneCountry") or region,
            "phoneDialCode": doc.get("phoneDialCode") or dial,
            "phoneNational": national,
            "location": _location(doc),
            "city": doc.get("city") or "",
            "country": doc.get("country") or "",
            "totalExperience": doc.get("totalExperience") or doc.get("total_experience") or "",
            "linkedinUrl": doc.get("linkedinUrl") or doc.get("linkedin") or "",
            "currentCompany": doc.get("currentCompany") or doc.get("current_company") or "",
            "whatsappOptIn": bool(doc.get("whatsappOptIn", False)),
        },
    }


def ensure_profile_incomplete_defaults(candidate_id: str) -> None:
    """Backfill PROFILE_INCOMPLETE for legacy incomplete candidates."""
    _candidates().update_one(
        {
            "_id": _oid(candidate_id),
            "onboardingCompleted": {"$ne": True},
            "$or": [
                {"profileState": {"$exists": False}},
                {"profileState": None},
                {"profileState": ""},
            ],
        },
        {
            "$set": {
                "profileState": PROFILE_INCOMPLETE,
                "profileSetupStep": STEP_BASIC,
            }
        },
    )


class BasicDetailsPayload(BaseModel):
    """Accepts legacy Fanisko fields and newer KV75 profile-setup payloads."""

    model_config = ConfigDict(extra="ignore")

    fullName: str
    phone: str = ""
    phoneCountry: Optional[str] = "IN"
    phoneDialCode: Optional[str] = "91"
    location: str = ""
    totalExperience: str = ""
    totalExperienceYears: Optional[float] = None
    linkedinUrl: Optional[str] = ""
    currentCompany: Optional[str] = ""
    # Newer frontend fields (ignored by legacy persist, kept for compatibility)
    countryId: Optional[str] = None
    cityId: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    jobTitle: Optional[str] = None
    employer: Optional[str] = None
    noticePeriod: Optional[str] = None
    preferredCountryId: Optional[str] = None
    preferredCityId: Optional[str] = None
    openToRemote: Optional[bool] = None
    salaryNegotiable: Optional[bool] = None
    salaryCurrency: Optional[str] = None
    salaryMin: Optional[float] = None
    salaryMax: Optional[float] = None
    professionalSummary: Optional[str] = None
    skip: bool = False

    @field_validator("fullName")
    @classmethod
    def _name(cls, value: str) -> str:
        parts = str(value or "").strip().split()
        if not parts:
            raise ValueError("Full name is required.")
        require_person_name(parts[0], "Full name")
        return " ".join(parts)

    @model_validator(mode="after")
    def _normalize(self):
        # Prefer explicit location; otherwise compose from city/country fields.
        if not str(self.location or "").strip():
            composed = ", ".join(
                part
                for part in (str(self.city or "").strip(), str(self.country or "").strip())
                if part
            )
            self.location = composed
        if not str(self.totalExperience or "").strip() and self.totalExperienceYears is not None:
            self.totalExperience = str(self.totalExperienceYears)
        if not str(self.currentCompany or "").strip() and self.employer:
            self.currentCompany = str(self.employer).strip()

        phone = str(self.phone or "").strip()
        if phone:
            try:
                if phone.startswith("+"):
                    self.phone = require_mobile_e164(
                        phone,
                        region=self.phoneCountry,
                        dial_code=self.phoneDialCode,
                        national_only=False,
                    )
                else:
                    self.phone = require_mobile_e164(
                        phone,
                        region=self.phoneCountry,
                        dial_code=self.phoneDialCode,
                        national_only=True,
                    )
            except MobileValidationError as exc:
                raise ValueError(str(exc)) from exc
            region, dial, _ = split_e164(self.phone)
            self.phoneCountry = region
            self.phoneDialCode = dial
        return self

    @field_validator("location")
    @classmethod
    def _location_req(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("totalExperience")
    @classmethod
    def _experience(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("linkedinUrl")
    @classmethod
    def _linkedin(cls, value: Optional[str]) -> str:
        return optional_linkedin(value or "") or ""

    @field_validator("currentCompany")
    @classmethod
    def _company(cls, value: Optional[str]) -> str:
        return str(value or "").strip()


class WhatsAppPreferencePayload(BaseModel):
    whatsappOptIn: bool = False


class CompleteSetupPayload(BaseModel):
    confirm: bool = True


class MobileOtpSendPayload(BaseModel):
    phone: Optional[str] = None
    phoneCountry: Optional[str] = None
    phoneDialCode: Optional[str] = None


class MobileOtpVerifyPayload(BaseModel):
    otp: str = Field(min_length=4, max_length=8)


class MobileDuplicateCheckPayload(BaseModel):
    phone: str
    phoneCountry: Optional[str] = "IN"
    phoneDialCode: Optional[str] = "91"


@router.post("/api/candidate/profile-setup/{candidate_id}/check-mobile")
@limiter.limit("6/minute")
async def check_candidate_mobile_available(
    request: Request,
    candidate_id: str,
    payload: MobileDuplicateCheckPayload,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    """KV05 — Duplicate check (rate-limited identically to WhatsApp OTP send)."""
    from app.services.candidate_whatsapp_otp import PURPOSE_MOBILE_DUP_CHECK

    _assert_self(principal, candidate_id)
    e164 = _coerce_mobile_e164(
        payload.phone,
        region=payload.phoneCountry,
        dial_code=payload.phoneDialCode,
    )
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
        or "unknown"
    )

    limits = check_otp_rate_limits(
        phone=e164,
        ip=client_ip,
        account_id=candidate_id,
        purpose=PURPOSE_MOBILE_DUP_CHECK,
    )
    if not limits.allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "message": limits.message,
                "errorCode": "rate_limit",
                "retryAfterMinutes": limits.retry_after_minutes,
            },
        )

    # Count toward dup-check windows (AC8). In development, skip writing an event
    # for every preflight so retries do not lock the account for ~24h.
    from app.settings import get_settings

    if get_settings().environment != "development":
        record_otp_send_event(
            phone=e164,
            ip=client_ip,
            account_id=candidate_id,
            channel="check",
            purpose=PURPOSE_MOBILE_DUP_CHECK,
        )

    claim = resolve_mobile_claim(candidate_id, e164)
    if claim.status in {
        MobileClaimStatus.DUPLICATE_ACTIVE,
        MobileClaimStatus.REACTIVATION_AVAILABLE,
    }:
        from app.services.candidate_mobile_identity import audit_duplicate_attempt

        audit_duplicate_attempt(
            candidate_id=candidate_id,
            e164=e164,
            ip=client_ip,
            outcome=claim.status.value,
            owner_candidate_id=claim.owner_candidate_id,
        )

    payload_out = claim_status_payload(claim)
    # AC3 — never attach email / name / owner id on this response
    return payload_out


@router.post("/api/candidate/profile-setup/{candidate_id}/mobile-reactivation/preview")
@limiter.limit("6/minute")
async def preview_mobile_reactivation(
    request: Request,
    candidate_id: str,
    payload: MobileDuplicateCheckPayload,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    """KV06 — masked account hint only (not shown on KV05 duplicate screen)."""
    _assert_self(principal, candidate_id)
    e164 = _coerce_mobile_e164(
        payload.phone,
        region=payload.phoneCountry,
        dial_code=payload.phoneDialCode,
    )
    return reactivation_preview_for_number(candidate_id=candidate_id, e164=e164)


class ProfileSetupStepPatch(BaseModel):
    """Generic step persist — used when advancing between steps."""

    step: Literal[1, 2, 3, 4]
    fullName: Optional[str] = None
    phone: Optional[str] = None
    phoneCountry: Optional[str] = None
    phoneDialCode: Optional[str] = None
    location: Optional[str] = None
    totalExperience: Optional[str] = None
    linkedinUrl: Optional[str] = None
    currentCompany: Optional[str] = None
    whatsappOptIn: Optional[bool] = None


def _coerce_mobile_e164(
    phone: str,
    *,
    region: Optional[str] = None,
    dial_code: Optional[str] = None,
) -> str:
    """Accept national digits (preferred) or legacy E.164 matching selected country."""
    raw = str(phone or "").strip()
    try:
        if raw.startswith("+"):
            e164 = require_mobile_e164(
                raw, region=region, dial_code=dial_code, national_only=False
            )
            parsed_region, _, _ = split_e164(e164)
            if region and parsed_region and parsed_region.upper() != str(region).upper():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Enter the mobile number without the country code. "
                        "Select the country from the dropdown."
                    ),
                )
            return e164
        return normalize_mobile(raw, region=region, dial_code=dial_code)
    except MobileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _split_name(full_name: str) -> tuple[str, str]:
    parts = str(full_name or "").strip().split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else first
    return first, last


def _parse_location(location: str) -> tuple[str, str, str]:
    trimmed = str(location or "").strip()
    if not trimmed:
        return "Not specified", "Not specified", ""
    parts = [p.strip() for p in trimmed.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[-1], trimmed
    return trimmed, "Not specified", trimmed


def _apply_mobile_fields(
    updates: dict[str, Any],
    candidate_id: str,
    e164: str,
    *,
    verified: bool,
    previous_phone: Optional[str] = None,
) -> None:
    assert_unique_mobile(candidate_id, e164)
    mobile_fields = upsert_encrypted_mobile(
        candidate_id, e164, verified=verified
    )
    updates.update(mobile_fields)
    if previous_phone and previous_phone != e164 and not verified:
        updates["mobile_verified"] = False


@router.get("/auth/candidate/status")
async def candidate_status(principal: Annotated[AuthPrincipal, Depends(require_candidate)]):
    ensure_profile_incomplete_defaults(principal.candidate_id)
    doc = _candidates().find_one({"_id": _oid(principal.candidate_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    return serialize_setup_status(doc)


@router.get("/api/candidate/profile-setup/{candidate_id}")
async def get_profile_setup(
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    _assert_self(principal, candidate_id)
    ensure_profile_incomplete_defaults(candidate_id)
    doc = _candidates().find_one({"_id": _oid(candidate_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    return serialize_setup_status(doc)


@router.patch("/api/candidate/profile-setup/{candidate_id}")
async def patch_profile_setup(
    candidate_id: str,
    payload: ProfileSetupStepPatch,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    """Persist partial profile on each step transition (AC7 / AC9).

    Never flips onboardingCompleted — that only happens on final complete.
    """
    _assert_self(principal, candidate_id)
    ensure_profile_incomplete_defaults(candidate_id)
    doc = _candidates().find_one({"_id": _oid(candidate_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    if doc.get("onboardingCompleted"):
        raise HTTPException(status_code=400, detail="Profile setup is already complete.")

    updates: dict[str, Any] = {
        "profileState": PROFILE_INCOMPLETE,
        "profileSetupStep": int(payload.step),
        "profileSetupUpdatedAt": _now(),
    }

    if payload.fullName is not None:
        full_name = str(payload.fullName).strip()
        if not full_name:
            raise HTTPException(status_code=400, detail="Full name is required.")
        parts = full_name.split()
        require_person_name(parts[0], "Full name")
        first, last = _split_name(full_name)
        updates["name"] = full_name
        updates["firstName"] = first
        updates["lastName"] = last

    if payload.phone is not None:
        phone = _coerce_mobile_e164(
            payload.phone,
            region=payload.phoneCountry,
            dial_code=payload.phoneDialCode,
        )
        previous = resolve_phone_for_candidate_self(candidate_id, doc) or doc.get("phone")
        _apply_mobile_fields(
            updates,
            candidate_id,
            phone,
            verified=False,
            previous_phone=previous,
        )

    if payload.location is not None:
        loc = str(payload.location).strip()
        if not loc:
            raise HTTPException(status_code=400, detail="Current location is required.")
        city, country, location_text = _parse_location(loc)
        updates["city"] = city
        updates["country"] = country
        updates["location"] = location_text

    if payload.totalExperience is not None:
        exp = str(payload.totalExperience).strip()
        if not exp:
            raise HTTPException(status_code=400, detail="Total experience is required.")
        updates["totalExperience"] = exp

    if payload.linkedinUrl is not None:
        updates["linkedinUrl"] = optional_linkedin(payload.linkedinUrl) or ""

    if payload.currentCompany is not None:
        updates["currentCompany"] = str(payload.currentCompany).strip()

    if payload.whatsappOptIn is not None:
        updates["whatsappOptIn"] = bool(payload.whatsappOptIn)

    if payload.location is not None or payload.phone is not None:
        updates.update(
            onboarding_region_persist_fields(
                country_name_or_code=str(
                    updates.get("country") or doc.get("country") or ""
                ),
                phone_country_code=str(
                    updates.get("phoneCountry")
                    or payload.phoneCountry
                    or doc.get("phoneCountry")
                    or ""
                ),
                existing_doc=doc,
            )
        )

    _candidates().update_one({"_id": _oid(candidate_id)}, {"$set": updates})
    refreshed = _candidates().find_one({"_id": _oid(candidate_id)})
    return serialize_setup_status(refreshed or doc)


@router.post("/api/candidate/profile-setup/{candidate_id}/basic-details")
async def save_basic_details(
    candidate_id: str,
    payload: BasicDetailsPayload,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    _assert_self(principal, candidate_id)
    ensure_profile_incomplete_defaults(candidate_id)
    doc = _candidates().find_one({"_id": _oid(candidate_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found.")

    previous = resolve_phone_for_candidate_self(candidate_id, doc) or doc.get("phone")
    phone = str(payload.phone or previous or "").strip()
    if not phone:
        raise HTTPException(
            status_code=400,
            detail="Mobile number is required. Verify your phone first.",
        )

    first, last = _split_name(payload.fullName)
    city, country, location_text = _parse_location(payload.location)
    if payload.city:
        city = str(payload.city).strip() or city
    if payload.country:
        country = str(payload.country).strip() or country
    if not location_text:
        location_text = ", ".join(part for part in (city, country) if part)
    if not location_text:
        raise HTTPException(status_code=400, detail="Current location is required.")
    if not str(payload.totalExperience or "").strip():
        raise HTTPException(status_code=400, detail="Total experience is required.")

    mobile_verified = bool(doc.get("mobile_verified") or doc.get("mobileVerified"))
    already_complete = bool(doc.get("onboardingCompleted"))
    # Frontend v2 order is Mobile → WhatsApp → Basic → Done. Do not send users
    # back to the mobile step after they already verified.
    next_step = STEP_DONE if mobile_verified else STEP_MOBILE

    updates = {
        "name": payload.fullName,
        "firstName": first,
        "lastName": last,
        "city": city,
        "country": country,
        "location": location_text,
        "totalExperience": payload.totalExperience,
        "linkedinUrl": payload.linkedinUrl or "",
        "currentCompany": payload.currentCompany or payload.employer or "",
        "profileSetupUpdatedAt": _now(),
    }
    if not already_complete:
        updates["profileState"] = PROFILE_INCOMPLETE
        updates["profileSetupStep"] = next_step
    if payload.countryId:
        updates["countryId"] = str(payload.countryId).strip().upper()
    if payload.cityId:
        updates["cityId"] = str(payload.cityId).strip()
    if payload.jobTitle:
        updates["jobTitle"] = str(payload.jobTitle).strip()
    if payload.totalExperienceYears is not None:
        updates["totalExperienceYears"] = float(payload.totalExperienceYears)
    if payload.noticePeriod:
        updates["noticePeriod"] = str(payload.noticePeriod).strip()
    if payload.preferredCountryId is not None:
        updates["preferredCountryId"] = str(payload.preferredCountryId or "").strip()
    if payload.preferredCityId is not None:
        updates["preferredCityId"] = str(payload.preferredCityId or "").strip()
    if payload.openToRemote is not None:
        updates["openToRemote"] = bool(payload.openToRemote)
    if payload.salaryNegotiable is not None:
        updates["salaryNegotiable"] = bool(payload.salaryNegotiable)
    if payload.salaryCurrency:
        updates["salaryCurrency"] = str(payload.salaryCurrency).strip()
    if payload.salaryMin is not None:
        updates["salaryMin"] = float(payload.salaryMin)
    if payload.salaryMax is not None:
        updates["salaryMax"] = float(payload.salaryMax)
    if payload.professionalSummary:
        updates["professionalSummary"] = str(payload.professionalSummary).strip()

    if phone:
        _apply_mobile_fields(
            updates,
            candidate_id,
            phone,
            verified=mobile_verified,
            previous_phone=previous,
        )
    updates.update(
        onboarding_region_persist_fields(
            country_name_or_code=country or str(payload.countryId or ""),
            phone_country_code=str(
                updates.get("phoneCountry") or payload.phoneCountry or ""
            ),
            existing_doc=doc,
        )
    )

    _candidates().update_one({"_id": _oid(candidate_id)}, {"$set": updates})
    _recalc(candidate_id)
    refreshed = _candidates().find_one({"_id": _oid(candidate_id)})
    return serialize_setup_status(refreshed or doc)


@router.post("/api/candidate/profile-setup/{candidate_id}/send-mobile-otp")
@limiter.limit("6/minute")
async def send_candidate_mobile_otp(
    request: Request,
    candidate_id: str,
    payload: MobileOtpSendPayload,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    """KV03 — Dispatch a WhatsApp OTP (hashed at rest, rate-limited)."""
    _assert_self(principal, candidate_id)
    doc = _candidates().find_one({"_id": _oid(candidate_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found.")

    phone = _coerce_mobile_e164(
        payload.phone or resolve_phone_for_candidate_self(candidate_id, doc) or "",
        region=payload.phoneCountry or doc.get("phoneCountry"),
        dial_code=payload.phoneDialCode or doc.get("phoneDialCode"),
    )
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
        or "unknown"
    )

    claim = resolve_mobile_claim(candidate_id, phone)
    # AC6 — own already-verified number: no OTP, advance flow
    if claim.status == MobileClaimStatus.OWN_VERIFIED:
        updates = {
            "mobile_verified": True,
            "mobileVerificationState": STATE_MOBILE_VERIFIED,
            "profileState": PROFILE_INCOMPLETE,
            "profileSetupStep": STEP_WHATSAPP,
            "profileSetupUpdatedAt": _now(),
        }
        _candidates().update_one({"_id": _oid(candidate_id)}, {"$set": updates})
        refreshed = _candidates().find_one({"_id": _oid(candidate_id)})
        status_payload = serialize_setup_status(refreshed or doc)
        status_payload["success"] = True
        status_payload["alreadyVerified"] = True
        status_payload["message"] = claim.message
        status_payload["mobileVerificationState"] = STATE_MOBILE_VERIFIED
        return status_payload

    try:
        assert_unique_mobile(candidate_id, phone, ip=client_ip, audit=True)
    except HTTPException:
        raise

    result = issue_candidate_whatsapp_otp(
        candidate_id=candidate_id,
        phone=phone,
        ip=client_ip,
    )
    if not result.success:
        status = 429 if result.retry_after_seconds or result.retry_after_minutes else 502
        if result.error_code in {"country"}:
            status = 400
        raise HTTPException(
            status_code=status,
            detail={
                "message": result.error,
                "errorCode": result.error_code,
                "retryAfterSeconds": result.retry_after_seconds,
                "retryAfterMinutes": result.retry_after_minutes,
                "resendLocked": result.resend_locked,
                "resendCount": result.resend_count,
            },
        )

    updates: dict[str, Any] = {
        "profileState": PROFILE_INCOMPLETE,
        "profileSetupStep": max(_setup_step(doc), STEP_MOBILE),
        "profileSetupUpdatedAt": _now(),
    }
    _apply_mobile_fields(
        updates,
        candidate_id,
        phone,
        verified=False,
        previous_phone=resolve_phone_for_candidate_self(candidate_id, doc),
    )
    _candidates().update_one({"_id": _oid(candidate_id)}, {"$set": updates})

    response: dict = {
        "success": True,
        "sentTo": format_otp_masked_display(phone),
        "sentToMasked": result.sent_to_masked or format_otp_masked_display(phone),
        "channel": "whatsapp",
        "expiresInMinutes": 10,
        "resendAfterSeconds": result.retry_after_seconds,
        "resendCount": result.resend_count,
        "resendLocked": result.resend_locked,
        "message": "Verification code sent on WhatsApp.",
    }
    # Dev-only hint — never logged; only returned when DEBUG=true
    if result.dev_code:
        response["devCode"] = result.dev_code
    return response


@router.post("/api/candidate/profile-setup/{candidate_id}/invalidate-mobile-otp")
@limiter.limit("10/minute")
async def invalidate_candidate_mobile_otp(
    request: Request,
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    """AC1 — Change number: invalidate pending OTP and return to KV02."""
    _assert_self(principal, candidate_id)
    invalidate_pending_otp_for_change_number(candidate_id=candidate_id)
    return {"success": True, "message": "Pending verification code cleared."}


@router.post("/api/candidate/profile-setup/{candidate_id}/verify-mobile-otp")
@limiter.limit("10/minute")
async def verify_candidate_mobile_otp(
    request: Request,
    candidate_id: str,
    payload: MobileOtpVerifyPayload,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    """Verify WhatsApp OTP. Rate-limited so remaining attempts may be exposed (dev note)."""
    _assert_self(principal, candidate_id)
    doc = _candidates().find_one({"_id": _oid(candidate_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found.")

    phone = get_plaintext_mobile(candidate_id) or doc.get("phone") or ""
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
        or "unknown"
    )
    user_agent = request.headers.get("user-agent", "") or ""

    verified = verify_stored_otp(
        candidate_id=candidate_id,
        otp=payload.otp,
        phone=str(phone or ""),
        ip=client_ip,
        user_agent=user_agent,
    )
    if not verified.ok:
        status = 429 if verified.error_code == "locked" else 400
        detail: dict[str, Any] = {
            "message": verified.error,
            "errorCode": verified.error_code,
        }
        # Remaining attempts only on this rate-limited route
        if verified.attempts_remaining is not None:
            detail["attemptsRemaining"] = verified.attempts_remaining
        raise HTTPException(status_code=status, detail=detail)

    if phone:
        try:
            assert_unique_mobile(candidate_id, phone)
        except HTTPException as exc:
            # AC8 — race: number verified on another account → KV05 duplicate flow
            if exc.status_code == 409:
                raise
            raise
        updates = {
            "mobile_verified": True,
            "mobileVerificationState": STATE_MOBILE_VERIFIED,
            "profileState": PROFILE_INCOMPLETE,
            "profileSetupStep": STEP_WHATSAPP,  # KV11
            "profileSetupUpdatedAt": _now(),
        }
        updates.update(upsert_encrypted_mobile(candidate_id, phone, verified=True))
        mark_mobile_verified(candidate_id)
        _candidates().update_one({"_id": _oid(candidate_id)}, {"$set": updates})
    else:
        _candidates().update_one(
            {"_id": _oid(candidate_id)},
            {
                "$set": {
                    "mobile_verified": True,
                    "mobileVerificationState": STATE_MOBILE_VERIFIED,
                    "profileState": PROFILE_INCOMPLETE,
                    "profileSetupStep": STEP_WHATSAPP,
                    "profileSetupUpdatedAt": _now(),
                }
            },
        )

    # AC10 — no confirmation SMS; email confirmation only
    try:
        email = (doc.get("email") or "").strip()
        if email:
            from app.email.service import email_service
            from app.email.templates import EmailTemplates

            subject, html_body, text_body = EmailTemplates.mobile_verified_confirmation_template(
                name=_full_name(doc),
                phone_masked=format_otp_masked_display(str(phone or "")),
            )
            email_service.send_email(
                subject=subject,
                body_html=html_body,
                body_text=text_body,
                recipients=[email],
            )
    except Exception:
        # Verification already succeeded — email is best-effort
        import logging

        logging.getLogger(__name__).exception(
            "Mobile verified confirmation email failed for candidate %s", candidate_id
        )

    refreshed = _candidates().find_one({"_id": _oid(candidate_id)})
    status_payload = serialize_setup_status(refreshed or doc)
    status_payload["success"] = True
    status_payload["message"] = "Mobile number verified successfully."
    status_payload["mobileVerificationState"] = STATE_MOBILE_VERIFIED
    return status_payload


@router.post("/api/candidate/profile-setup/{candidate_id}/whatsapp")
async def save_whatsapp_preference(
    candidate_id: str,
    payload: WhatsAppPreferencePayload,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    _assert_self(principal, candidate_id)
    doc = _candidates().find_one({"_id": _oid(candidate_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    if not (doc.get("mobile_verified") or doc.get("mobileVerified")):
        raise HTTPException(
            status_code=400,
            detail="Verify your mobile number before continuing.",
        )

    _candidates().update_one(
        {"_id": _oid(candidate_id)},
        {
            "$set": {
                "whatsappOptIn": bool(payload.whatsappOptIn),
                "profileState": PROFILE_INCOMPLETE,
                "profileSetupStep": STEP_DONE,
                "profileSetupUpdatedAt": _now(),
            }
        },
    )
    refreshed = _candidates().find_one({"_id": _oid(candidate_id)})
    return serialize_setup_status(refreshed or doc)


@router.post("/api/candidate/profile-setup/{candidate_id}/complete")
async def complete_profile_setup(
    candidate_id: str,
    payload: CompleteSetupPayload,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    _assert_self(principal, candidate_id)
    doc = _candidates().find_one({"_id": _oid(candidate_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found.")

    if not (doc.get("mobile_verified") or doc.get("mobileVerified")):
        raise HTTPException(
            status_code=400,
            detail="Verify your mobile number before finishing setup.",
        )
    if not doc.get("firstName") and not doc.get("name"):
        raise HTTPException(status_code=400, detail="Complete basic details first.")
    has_mobile = bool(
        get_plaintext_mobile(candidate_id) or doc.get("phone") or doc.get("phone_masked")
    )
    if not has_mobile:
        raise HTTPException(status_code=400, detail="Mobile number is required.")
    if not (doc.get("location") or doc.get("city")):
        raise HTTPException(status_code=400, detail="Current location is required.")
    if not (doc.get("totalExperience") or doc.get("total_experience")):
        raise HTTPException(status_code=400, detail="Total experience is required.")

    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required.")

    region_fields = onboarding_region_persist_fields(
        country_name_or_code=str(doc.get("country") or ""),
        phone_country_code=str(
            doc.get("phone_country_code") or doc.get("phoneCountry") or ""
        ),
        explicit_country_field=str(doc.get("country_field") or ""),
        existing_doc=doc,
    )

    _candidates().update_one(
        {"_id": _oid(candidate_id)},
        {
            "$set": {
                "onboardingCompleted": True,
                "profileState": PROFILE_COMPLETE,
                "profileSetupStep": STEP_DONE,
                "role": doc.get("role") or "Candidate",
                "profileSetupCompletedAt": _now(),
                "profileSetupUpdatedAt": _now(),
                **region_fields,
            }
        },
    )
    _recalc(candidate_id)
    refreshed = _candidates().find_one({"_id": _oid(candidate_id)})
    return serialize_setup_status(refreshed or doc)
