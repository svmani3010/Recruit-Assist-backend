"""HR-facing candidate mobile endpoints (masked by default + reveal + audit)."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import AuthPrincipal, assert_candidate_access, require_hr
from app.rate_limit import limiter
from app.services.candidate_mobile_identity import (
    DUPLICATE_DETAIL,
    audit_mobile_reveal,
    claim_status_payload,
    get_plaintext_mobile,
    normalize_mobile,
    release_candidate_mobile,
    resolve_mobile_claim,
)

router = APIRouter(tags=["candidate-mobile"])


class MobileCheckPayload(BaseModel):
    phone: str
    phoneCountry: Optional[str] = "IN"
    phoneDialCode: Optional[str] = "91"
    candidateId: Optional[str] = None


class MobileCheckResponse(BaseModel):
    """Constant shape whether the number is free or taken."""

    available: bool
    detail: str


@router.post(
    "/api/candidate/mobile/check-duplicate",
    response_model=MobileCheckResponse,
)
@limiter.limit("6/minute")
async def check_mobile_duplicate(
    request: Request,
    payload: MobileCheckPayload,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    """HR / tooling duplicate probe — rate-limited like OTP, constant response shape."""
    _ = principal
    e164 = normalize_mobile(
        payload.phone,
        region=payload.phoneCountry,
        dial_code=payload.phoneDialCode,
    )
    candidate_id = (payload.candidateId or "").strip() or "000000000000000000000000"
    claim = resolve_mobile_claim(candidate_id, e164)
    body = claim_status_payload(claim)
    return MobileCheckResponse(
        available=bool(body["available"]),
        detail=str(body["detail"] or DUPLICATE_DETAIL),
    )


@router.get("/api/candidates/{candidate_id}/mobile")
async def get_candidate_mobile_for_hr(
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
    job_id: Optional[str] = None,
):
    """HR view: full mobile with country code (from QE), never resume plaintext."""
    if job_id:
        assert_candidate_access(principal, candidate_id, job_id)
    from app.helpers import resolve_candidate_contact

    contact = resolve_candidate_contact(candidate_id, audience="hr")
    if not contact.get("phone"):
        raise HTTPException(status_code=404, detail="Mobile number not found.")
    return {
        "candidateId": candidate_id,
        "phone": contact["phone"],
        "phoneDisplay": contact.get("phoneDisplay") or contact["phone"],
        "phoneCountry": contact.get("phoneCountry") or "",
        "phoneDialCode": contact.get("phoneDialCode") or "",
        "phoneNational": contact.get("phoneNational") or "",
    }


class RevealMobilePayload(BaseModel):
    jobId: Optional[str] = Field(default=None, alias="jobId")
    reason: Optional[str] = None

    model_config = {"populate_by_name": True}


@router.post("/api/candidates/{candidate_id}/mobile/reveal")
@limiter.limit("20/minute")
async def reveal_candidate_mobile(
    request: Request,
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
    payload: RevealMobilePayload | None = None,
):
    """All HR roles may reveal plaintext; every reveal is audit-logged."""
    body = payload or RevealMobilePayload()
    job_id = body.jobId
    if job_id:
        assert_candidate_access(principal, candidate_id, job_id)

    plaintext = get_plaintext_mobile(candidate_id)
    if not plaintext:
        raise HTTPException(status_code=404, detail="Mobile number not found.")

    from app.mobile_phone import format_mobile_display, split_e164

    region, dial, national = split_e164(plaintext)
    display = format_mobile_display(plaintext)

    audit_mobile_reveal(
        hr_subject=principal.subject,
        hr_email=principal.subject,
        candidate_id=candidate_id,
        job_id=job_id,
    )

    # Notify candidate that their contact was viewed (WhatsApp preferred when opted in)
    try:
        from app.services.candidate_comms import notify_candidate_milestone

        notify_candidate_milestone(
            event="recruiter_viewed_mobile",
            candidate_id=candidate_id,
            job_id=job_id or "",
            data={"status": "Contact viewed by recruiter"},
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Failed notifying candidate of mobile reveal"
        )

    return {
        "candidateId": candidate_id,
        "phone": display,
        "phoneDisplay": display,
        "phoneE164": plaintext,
        "phoneCountry": region,
        "phoneDialCode": dial,
        "phoneNational": national,
    }


@router.post("/api/candidates/{candidate_id}/mobile/release")
async def release_mobile_for_reuse(
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    """Purge encrypted mobile so the number can be registered again (account delete)."""
    _ = principal
    release_candidate_mobile(candidate_id)
    return {"success": True, "candidateId": candidate_id, "released": True}
