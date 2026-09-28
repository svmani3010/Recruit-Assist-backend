"""Canonical hiring Metric Definitions (KV140 E19).

Every analytics chart, number and export MUST use these definitions.
Product / client sign-off is recorded on each metric (AC7).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


NOT_ENOUGH_DATA = "Not enough data"

# Bumped when Metric Definitions change (KV148 AC6).
DEFINITION_VERSION = "2026.09.22.1"

# Shared exclusion language referenced by application-funnel metrics (AC5).
APPLICATION_EXCLUSIONS = (
    "Withdrawn within 24 hours of submission, duplicate (superseded) applications, "
    "and test data (is_test=true)."
)


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    name: str
    formula: str
    excluded: str
    time_basis: str
    plain_language: str
    unit: str  # count | rate | days | breakdown
    uses_median: bool = False
    signed_off_by_product: bool = True
    signed_off_by_client: bool = True
    sheet_ref: str = "Metric Definitions"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


METRIC_DEFINITIONS: List[MetricDefinition] = [
    MetricDefinition(
        key="total_jobs_posted",
        name="Total jobs posted",
        formula="Count of jobs published during the period.",
        excluded="Drafts, pending approval, and deleted jobs.",
        time_basis="Job publish date",
        plain_language="How many jobs went live in this period.",
        unit="count",
    ),
    MetricDefinition(
        key="active_jobs",
        name="Active jobs",
        formula="Count of jobs published and not closed at the end of the period.",
        excluded="Scheduled jobs not yet live.",
        time_basis="Point in time",
        plain_language="Jobs that were live (published and not closed) at the end of the period.",
        unit="count",
    ),
    MetricDefinition(
        key="job_views",
        name="Job views",
        formula="Count of job detail page views lasting at least 5 seconds.",
        excluded="Views by the organisation's own users, and bots.",
        time_basis="View date",
        plain_language="Meaningful visits to a job page (5 seconds or more), excluding staff and bots.",
        unit="count",
    ),
    MetricDefinition(
        key="apply_rate",
        name="Apply rate",
        formula="Applications ÷ job views.",
        excluded="As for job views and total applications.",
        time_basis="Period",
        plain_language="Share of job viewers who submitted an application.",
        unit="rate",
    ),
    MetricDefinition(
        key="total_applications",
        name="Total applications",
        formula="Count of submitted applications.",
        excluded=APPLICATION_EXCLUSIONS,
        time_basis="Application date",
        plain_language="Submitted applications after removing early withdrawals, duplicates and test data.",
        unit="count",
    ),
    MetricDefinition(
        key="qualified_candidates",
        name="Qualified candidates",
        formula="Applications that meet or exceed the job's screening threshold.",
        excluded=APPLICATION_EXCLUSIONS,
        time_basis="Screening date",
        plain_language="Applicants who passed the automated screening threshold for the job.",
        unit="count",
    ),
    MetricDefinition(
        key="screening_rate",
        name="Screening rate",
        formula="Qualified candidates ÷ total applications.",
        excluded=APPLICATION_EXCLUSIONS,
        time_basis="Period",
        plain_language="Share of applications that passed screening.",
        unit="rate",
    ),
    MetricDefinition(
        key="technical_interview_completion_rate",
        name="Technical interview completion rate",
        formula="Technical interviews completed ÷ technical interviews invited.",
        excluded="Jobs with the Technical Interview toggle Off.",
        time_basis="Invitation date",
        plain_language="Share of technical interview invites that were completed.",
        unit="rate",
    ),
    MetricDefinition(
        key="approval_rate",
        name="Approval rate",
        formula="Candidates approved by a recruiter ÷ qualified candidates.",
        excluded=APPLICATION_EXCLUSIONS,
        time_basis="Decision date",
        plain_language="Share of qualified candidates a recruiter approved to progress.",
        unit="rate",
    ),
    MetricDefinition(
        key="interview_conversion_rate",
        name="Interview conversion rate",
        formula="Candidates who completed at least one interview ÷ candidates approved.",
        excluded=APPLICATION_EXCLUSIONS,
        time_basis="Interview date",
        plain_language="Share of approved candidates who completed an interview.",
        unit="rate",
    ),
    MetricDefinition(
        key="interview_to_offer_rate",
        name="Interview-to-offer rate",
        formula="Offers made ÷ candidates interviewed.",
        excluded=APPLICATION_EXCLUSIONS,
        time_basis="Offer date",
        plain_language="Share of interviewed candidates who received an offer.",
        unit="rate",
    ),
    MetricDefinition(
        key="offer_acceptance_rate",
        name="Offer acceptance rate",
        formula="Offers accepted ÷ offers made.",
        excluded="Offers still awaiting a decision.",
        time_basis="Decision date",
        plain_language="Share of decided offers that were accepted.",
        unit="rate",
    ),
    MetricDefinition(
        key="hires",
        name="Hires",
        formula="Count of candidates marked hired.",
        excluded=APPLICATION_EXCLUSIONS,
        time_basis="Hire date",
        plain_language="Candidates marked as hired in the period.",
        unit="count",
    ),
    MetricDefinition(
        key="offers_made",
        name="Offers made",
        formula="Count of offers extended to candidates.",
        excluded=APPLICATION_EXCLUSIONS,
        time_basis="Offer date",
        plain_language="How many offers were sent in the period.",
        unit="count",
    ),
    MetricDefinition(
        key="time_to_hire",
        name="Time-to-hire",
        formula="Median days from application submitted to offer accepted, per hire.",
        excluded="Hires with missing dates. " + APPLICATION_EXCLUSIONS,
        time_basis="Hire date",
        plain_language="Typical number of days from apply to accepted offer (median; average shown alongside).",
        unit="days",
        uses_median=True,
    ),
    MetricDefinition(
        key="time_to_fill",
        name="Time-to-fill",
        formula="Median days from job published to the first offer accepted for that job.",
        excluded="Jobs with no accepted offer.",
        time_basis="Offer acceptance date",
        plain_language="Typical days from publishing a job to its first accepted offer (median).",
        unit="days",
        uses_median=True,
    ),
    MetricDefinition(
        key="stage_duration",
        name="Stage duration",
        formula="Median days candidates spend in each stage before moving on.",
        excluded="Candidates still in the stage (shown separately as 'waiting').",
        time_basis="Stage exit date",
        plain_language="How long candidates typically stay in each hiring stage before moving on.",
        unit="breakdown",
        uses_median=True,
    ),
    MetricDefinition(
        key="source_performance",
        name="Source performance",
        formula=(
            "Applications, qualified, approved and hires per attributed source, "
            "with conversion at each step."
        ),
        excluded=APPLICATION_EXCLUSIONS,
        time_basis="Application date",
        plain_language="Which sources bring applications and how they convert through the funnel.",
        unit="breakdown",
    ),
    MetricDefinition(
        key="featured_impressions",
        name="Featured impressions",
        formula="Count of times a featured job was displayed in a featured position.",
        excluded="Bot traffic where detectable.",
        time_basis="Display date",
        plain_language="How often featured jobs appeared in a featured placement.",
        unit="count",
    ),
    MetricDefinition(
        key="featured_click_through_rate",
        name="Featured click-through rate",
        formula="Clicks on featured position ÷ featured impressions.",
        excluded="Bot traffic where detectable.",
        time_basis="Period",
        plain_language="Share of featured impressions that led to a click.",
        unit="rate",
    ),
]


_BY_KEY: Dict[str, MetricDefinition] = {m.key: m for m in METRIC_DEFINITIONS}


def get_definition(key: str) -> Optional[MetricDefinition]:
    return _BY_KEY.get(key)


def list_definitions() -> List[Dict[str, Any]]:
    return [m.to_dict() for m in METRIC_DEFINITIONS]


def definitions_sign_off_status() -> Dict[str, Any]:
    """AC7 — definitions reviewed and signed off by Product and client before build."""
    all_product = all(m.signed_off_by_product for m in METRIC_DEFINITIONS)
    all_client = all(m.signed_off_by_client for m in METRIC_DEFINITIONS)
    return {
        "productSignedOff": all_product,
        "clientSignedOff": all_client,
        "readyForBuild": all_product and all_client,
        "metricCount": len(METRIC_DEFINITIONS),
        "definitionVersion": DEFINITION_VERSION,
        "sourceSheet": (
            "https://docs.google.com/spreadsheets/d/"
            "1fs4BTSEl8u0zxP-yEgqagaGhZPE2ivaFndBqlp2YnQw"
        ),
    }
