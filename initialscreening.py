"""
Deprecated standalone initial-screening entrypoint.

Prefer:
  from app.services.screening import run_initial_screening_and_store

This module remains as a thin compatibility wrapper so old CLI/scripts keep working.
It does not embed credentials and does not log resume/JD payloads.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any, Dict

from app.database import get_db
from app.helpers import (
    fetch_jd_json,
    prune_jd_fields,
    prune_resume_fields_all_except_exclusions,
)
from app.services.screening import get_gemini, run_initial_screening_and_store

logger = logging.getLogger(__name__)

_DEPRECATION = (
    "initialscreening.run_initial_screening is deprecated; "
    "use app.services.screening.run_initial_screening_and_store instead."
)


def run_initial_screening(job_id: str, candidate_id: str) -> Dict[str, Any]:
    """
    Run L0 screening for one candidate/job and persist via the canonical pipeline.

    Returns
    -------
    dict
        ``{"candidate_id": ..., "compatibility": <float>}``
    """
    warnings.warn(_DEPRECATION, DeprecationWarning, stacklevel=2)
    logger.info(
        "Deprecated initialscreening entry used for candidate=%s job=%s",
        candidate_id,
        job_id,
    )
    run_initial_screening_and_store(job_id, candidate_id)
    score_doc = get_db()["Scores"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id}
    ) or {}
    return {
        "candidate_id": candidate_id,
        "compatibility": float(score_doc.get("compatibility") or 0.0),
        "status": score_doc.get("status", "unknown"),
    }


def preview_compatibility(job_id: str, candidate_id: str) -> Dict[str, Any]:
    """
    Compute L0 compatibility without writing side effects beyond the Gemini call.

    Intended for debugging; still deprecated relative to the service layer.
    """
    warnings.warn(
        "initialscreening.preview_compatibility is deprecated; "
        "prefer get_gemini().initial_screening via the service layer.",
        DeprecationWarning,
        stacklevel=2,
    )
    db = get_db()
    jd_json = fetch_jd_json(job_id)
    resume_doc = db["ResumeV2"].find_one(
        {"job_id": job_id, "candidate_id": candidate_id}
    )
    if not resume_doc or not resume_doc.get("resume_json"):
        raise ValueError(
            f"Resume JSON missing for candidate={candidate_id} job={job_id}"
        )
    screening = get_gemini().initial_screening(
        prune_resume_fields_all_except_exclusions(resume_doc["resume_json"]),
        prune_jd_fields(jd_json),
    )
    raw = screening.get("compatibility", "0%")
    if isinstance(raw, str) and raw.endswith("%"):
        try:
            score = float(raw.rstrip("%").strip())
        except ValueError:
            score = 0.0
    elif isinstance(raw, (int, float)):
        score = float(raw)
    else:
        score = 0.0
    return {
        "candidate_id": candidate_id,
        "compatibility": score,
        "l0_reasons": screening.get("l0_reasons") or [],
    }
