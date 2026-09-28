"""Profile Score APIs — GET score, PATCH fields, org config, rollout metrics."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthPrincipal, Role, require_candidate, require_hr
from app.database import get_db
from app.services.profile_score import (
    APPLY_GATE_SCORE,
    ProfileScoreError,
    candidate_score_payload,
    create_default_profile_score_config,
    fields_to_candidate_updates,
    load_org_config,
    personal_info_from_doc,
    recalculate_and_persist,
    save_org_config,
)

router = APIRouter(tags=["profile-score"])


def _assert_self(principal: AuthPrincipal, candidate_id: str) -> None:
    if principal.role == Role.SERVICE:
        return
    if principal.role == Role.HR:
        return
    if principal.candidate_id != candidate_id:
        raise HTTPException(status_code=403, detail="Not authorized for this candidate.")


class ProfileFieldsPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    fullName: Optional[str] = None
    jobTitle: Optional[str] = None
    employer: Optional[str] = None
    currentCompany: Optional[str] = None
    professionalSummary: Optional[str] = None
    noticePeriod: Optional[str] = None
    skills: Optional[list[str]] = None
    education: Optional[dict[str, Any]] = None
    countryId: Optional[str] = None
    cityId: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    location: Optional[str] = None
    totalExperienceYears: Optional[float] = None
    preferredCountryId: Optional[str] = None
    preferredCityId: Optional[str] = None
    openToRemote: Optional[bool] = None
    salaryNegotiable: Optional[bool] = None
    salaryCurrency: Optional[str] = None
    salaryMin: Optional[float] = None
    salaryMax: Optional[float] = None
    linkedinUrl: Optional[str] = None
    isFresher: Optional[bool] = None
    confirmFreshness: Optional[str] = None
    overwriteHandEdit: Optional[bool] = None
    credits: Optional[list[str]] = None


class OrgProfileScoreConfigPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    orgId: Optional[str] = None
    version: Optional[int] = None
    applyGateScore: int = Field(default=APPLY_GATE_SCORE)
    stalenessAffectsApplyGate: bool = False
    items: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/api/candidate/{candidate_id}/profile-score")
async def get_profile_score(
    candidate_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    _assert_self(principal, candidate_id)
    try:
        return candidate_score_payload(candidate_id)
    except ProfileScoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.patch("/api/candidate/{candidate_id}/profile-fields")
async def patch_profile_fields(
    candidate_id: str,
    payload: ProfileFieldsPatch,
    principal: Annotated[AuthPrincipal, Depends(require_candidate)],
):
    _assert_self(principal, candidate_id)
    db = get_db()
    try:
        oid = ObjectId(candidate_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid candidate id.") from exc
    doc = db["candidates"].find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Candidate not found.")

    raw = payload.model_dump(exclude_unset=True)
    keys = set(raw) - {"overwriteHandEdit"}
    if keys <= {"skills", "education", "confirmFreshness"} or "confirmFreshness" in keys:
        source = "profile"
    else:
        source = "resume"

    try:
        updates = fields_to_candidate_updates(raw, doc, source=source)
    except ProfileScoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if updates:
        db["candidates"].update_one({"_id": oid}, {"$set": updates})
    try:
        score = recalculate_and_persist(candidate_id)
    except ProfileScoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    refreshed = db["candidates"].find_one({"_id": oid}) or doc
    return {
        "status": "success",
        "personal_info": personal_info_from_doc(refreshed),
        "skills": refreshed.get("skills") or [],
        "education": refreshed.get("education") or {},
        "userEditedFields": refreshed.get("userEditedFields") or [],
        "freshness": refreshed.get("freshness") or {},
        "credits": refreshed.get("credits") or [],
        "profileScore": score,
        "total": score.get("total"),
        "applyEligible": score.get("applyEligible"),
        "items": score.get("items"),
        "missingOrderedByWeight": score.get("missingOrderedByWeight"),
        "applyGateScore": score.get("applyGateScore"),
        "source": "server",
    }


@router.get("/api/orgs/{org_id}/profile-score-config")
async def get_org_profile_score_config(org_id: str):
    return load_org_config(org_id)


@router.put("/api/orgs/{org_id}/profile-score-config")
async def put_org_profile_score_config(
    org_id: str,
    payload: OrgProfileScoreConfigPayload,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    _ = principal
    body = payload.model_dump(exclude_unset=True)
    body["orgId"] = org_id
    body["stalenessAffectsApplyGate"] = False
    body["applyGateScore"] = APPLY_GATE_SCORE
    if not body.get("items"):
        body["items"] = create_default_profile_score_config(org_id)["items"]
    try:
        return save_org_config(org_id, body)
    except ProfileScoreError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message, "errors": exc.details},
        ) from exc


@router.get("/api/orgs/{org_id}/profile-score-metrics")
async def get_org_profile_score_metrics(
    org_id: str,
    principal: Annotated[AuthPrincipal, Depends(require_hr)],
):
    _ = org_id
    _ = principal
    doc = get_db()["profile_score_rollout"].find_one({"_id": "global"}) or {}
    doc.pop("_id", None)
    evaluations = int(doc.get("evaluations") or 0)
    at_100 = int(doc.get("at_100") or 0)
    return {
        "orgId": org_id,
        "evaluations": evaluations,
        "at_100": at_100,
        "pct_at_100": round((at_100 / evaluations) * 100, 2) if evaluations else 0,
        "quality_rule_blocks": {
            key.replace("block_", ""): value
            for key, value in doc.items()
            if str(key).startswith("block_")
        },
        "stalenessAffectsApplyGate": False,
        "applyGateScore": APPLY_GATE_SCORE,
    }
