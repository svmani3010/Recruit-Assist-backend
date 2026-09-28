"""Parse HR-entered experience year requirements into filterable year tokens."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_YEAR = 40
_PLUS_CAP = 15


def _format_year_label(year: int) -> str:
    return f"{year} year" if year == 1 else f"{year} years"


def _clamp_years(years: List[int]) -> List[int]:
    cleaned = sorted({int(y) for y in years if 0 <= int(y) <= MAX_YEAR})
    return cleaned


def parse_experience_years_rule(raw: str) -> List[int]:
    """Deterministic parser for common HR phrasing."""
    text = str(raw or "").strip().lower()
    if not text:
        return []

    up_to = re.search(
        r"(?:up\s*to|upto|maximum|max|at\s+most|≤|<=)\s*(\d+(?:\.\d+)?)",
        text,
    )
    if up_to:
        end = int(float(up_to.group(1)))
        return [min(end, MAX_YEAR)]

    range_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:-|to|–|—)\s*(\d+(?:\.\d+)?)\s*(?:year|yr|yrs)?",
        text,
    )
    if range_match:
        lo = int(float(range_match.group(1)))
        hi = int(float(range_match.group(2)))
        return list(range(min(lo, hi), min(max(lo, hi), MAX_YEAR) + 1))

    plus_match = re.search(r"(\d+(?:\.\d+)?)\s*\+", text)
    if plus_match:
        start = int(float(plus_match.group(1)))
        return list(range(start, min(start + _PLUS_CAP, MAX_YEAR) + 1))

    min_match = re.search(
        r"(?:minimum|min|at\s+least|least)\s*(\d+(?:\.\d+)?)",
        text,
    )
    if min_match:
        start = int(float(min_match.group(1)))
        return list(range(start, min(start + _PLUS_CAP, MAX_YEAR) + 1))

    numbers = re.findall(r"(\d+(?:\.\d+)?)", text)
    if numbers:
        if len(numbers) >= 2 and not range_match:
            lo = int(float(numbers[0]))
            hi = int(float(numbers[1]))
            return list(range(min(lo, hi), min(max(lo, hi), MAX_YEAR) + 1))
        return [int(float(numbers[0]))]

    return []


def build_experience_years_payload(raw: str, years: List[int]) -> Dict[str, Any]:
    cleaned = _clamp_years(years)
    labels = [_format_year_label(year) for year in cleaned]
    return {
        "experienceYearsRequired": str(raw or "").strip(),
        "experienceYears": labels,
        "experienceYearsMin": min(cleaned) if cleaned else None,
        "experienceYearsMax": max(cleaned) if cleaned else None,
    }


def resolve_experience_years_sync(raw: str) -> Dict[str, Any]:
    years = parse_experience_years_rule(raw)
    return build_experience_years_payload(raw, years)


async def resolve_experience_years(raw: str) -> Dict[str, Any]:
    """Rule-based parse first; Gemini fallback when rules cannot extract years."""
    text = str(raw or "").strip()
    if not text:
        return build_experience_years_payload("", [])

    years = parse_experience_years_rule(text)
    if years:
        return build_experience_years_payload(text, years)

    try:
        from app.services.screening import get_gemini, run_in_thread

        ai_result = await run_in_thread(get_gemini().experience_years_parser, text)
        ai_years = ai_result.get("years") if isinstance(ai_result, dict) else []
        if isinstance(ai_years, list) and ai_years:
            parsed = [int(float(y)) for y in ai_years]
            return build_experience_years_payload(text, parsed)
    except Exception:
        logger.warning("AI experience years parse failed for %r", text, exc_info=True)

    return build_experience_years_payload(text, [])


async def attach_experience_years_fields(data: dict) -> dict:
    raw = str(data.get("experienceYearsRequired") or "").strip()
    if not raw:
        return data
    resolved = await resolve_experience_years(raw)
    data.update(resolved)
    return data


def enrich_job_experience_years(job: dict) -> dict:
    """Backfill filter tokens for legacy jobs missing experienceYears."""
    if job.get("experienceYears"):
        return job
    raw = str(
        job.get("experienceYearsRequired") or job.get("experienceRequired") or ""
    ).strip()
    if not raw:
        return job
    resolved = resolve_experience_years_sync(raw)
    if resolved.get("experienceYears"):
        job.update(resolved)
    return job
