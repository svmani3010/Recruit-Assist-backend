"""Filter public job listings to a selected country.

Callers pass an already-chosen country ISO code (resolvedRegion). This module
does not compute distances — it only keeps jobs whose location matches that
country.
"""

from __future__ import annotations

from typing import Any

from app.services.geo_dataset import lookup_country_code


def job_country_code(job: dict[str, Any]) -> str:
    """Resolve a job's country ISO code from structured fields or location text."""
    code = str(
        job.get("locationCountryCode")
        or job.get("location_country_code")
        or ""
    ).strip().upper()
    if len(code) == 2 and code.isalpha():
        return code

    name = str(
        job.get("locationCountry")
        or job.get("country")
        or ""
    ).strip()
    if name:
        found = lookup_country_code(name)
        if found:
            return found

    loc = job.get("location")
    if isinstance(loc, list):
        loc = ", ".join(str(part).strip() for part in loc if str(part).strip())
    text = str(loc or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if lowered in {"work from home", "remote", "wfh", "work from home (remote)"}:
        return ""
    # "Work From Home · Bengaluru, India" → parse country after last comma.
    if "·" in text:
        text = text.split("·", 1)[-1].strip()
    if "," in text:
        country_part = text.rsplit(",", 1)[-1].strip()
        found = lookup_country_code(country_part)
        if found:
            return found
    return lookup_country_code(text)


def filter_jobs_by_country(
    jobs: list[dict[str, Any]],
    *,
    country_code: str,
) -> list[dict[str, Any]]:
    """Return only jobs whose location country matches ``country_code`` (ISO alpha-2).

    Special code ``ALL`` (or empty) returns the full list unfiltered.
    """
    target = str(country_code or "").strip().upper()
    if not target or target == "ALL":
        return list(jobs or [])
    if len(target) != 2 or not target.isalpha():
        return list(jobs or [])

    matched: list[dict[str, Any]] = []
    for job in jobs or []:
        if job_country_code(job) == target:
            matched.append(dict(job))
    return matched


# Backward-compatible name used by the jobs router.
def sort_jobs_by_resolved_region(
    jobs: list[dict[str, Any]],
    *,
    resolved_region: str,
    region_type: str = "country",
) -> list[dict[str, Any]]:
    """Filter jobs to the selected country. ``region_type`` other than country is ignored."""
    _ = region_type
    return filter_jobs_by_country(jobs, country_code=resolved_region)
