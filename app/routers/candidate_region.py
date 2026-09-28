"""Candidate region resolve / override APIs (Phase 4)."""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth import (
    AuthPrincipal,
    Role,
    assert_candidate_access,
    get_current_principal_optional,
)
from app.rate_limit import limiter
from app.services.candidate_region import (
    apply_manual_override,
    resolve_region_from_signals,
)

router = APIRouter(tags=["candidate-region"])


class RegionResolveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    countryField: str = ""
    phoneCountryCode: str = ""
    # aliases accepted for convenience
    country_field: str = ""
    phone_country_code: str = ""

    def signals(self) -> tuple[str, str]:
        country = self.countryField or self.country_field
        phone = self.phoneCountryCode or self.phone_country_code
        return country, phone


class RegionOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidateId: Optional[str] = None
    sessionId: Optional[str] = None
    regionCode: str
    regionType: Literal["country", "cluster"] = "country"

    @model_validator(mode="after")
    def _require_target(self):
        if not (self.candidateId or self.sessionId):
            # Anonymous callers may omit sessionId — server will mint one.
            pass
        if not str(self.regionCode or "").strip():
            raise ValueError("regionCode is required")
        return self


@router.post("/api/candidate/region/resolve")
@limiter.limit("60/minute")
async def resolve_candidate_region(request: Request, payload: RegionResolveRequest):
    """Stateless region resolution from onboarding signals (pure function)."""
    _ = request
    country, phone = payload.signals()
    return resolve_region_from_signals(
        country_field=country,
        phone_country_code=phone,
    )


@router.post("/api/candidate/region/override")
@limiter.limit("60/minute")
async def override_candidate_region(
    request: Request,
    payload: RegionOverrideRequest,
    principal: Annotated[
        Optional[AuthPrincipal], Depends(get_current_principal_optional)
    ] = None,
):
    """Single write path for manual region override (profile or anonymous session)."""
    _ = request
    candidate_id = str(payload.candidateId or "").strip()
    session_id = str(payload.sessionId or "").strip()

    if candidate_id:
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        # Allow the owning candidate or service key; HR should not rewrite this.
        if principal.role == Role.CANDIDATE:
            assert_candidate_access(principal, candidate_id)
        elif principal.role != Role.SERVICE:
            raise HTTPException(
                status_code=403,
                detail="Only the candidate can override their region.",
            )

    try:
        return apply_manual_override(
            region_code=payload.regionCode,
            region_type=payload.regionType,
            candidate_id=candidate_id,
            session_id=session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
