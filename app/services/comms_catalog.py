"""Shared candidate notification copy (AC6 — same meaning across channels)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Priority(str, Enum):
    URGENT = "urgent"  # Bypasses quiet hours
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


@dataclass(frozen=True)
class MilestoneCopy:
    event: str
    email_action: str  # existing EmailTemplates action key when available
    sms_template_id: str
    whatsapp_template: str
    title: str
    body: str  # short semantic body for SMS/WA (≤160 when possible)
    path_template: str  # frontend path with {job_id} {candidate_id} {token}
    priority: Priority = Priority.NORMAL


# Path templates use FRONTEND_URL + path
CATALOG: dict[str, MilestoneCopy] = {
    "application_submitted": MilestoneCopy(
        event="application_submitted",
        email_action="status_update",
        sms_template_id="DLT_APP_SUBMITTED",
        whatsapp_template="application_submitted",
        title="Application received",
        body="We've received your application. Track status on Kovetted:",
        path_template="/applications",
    ),
    "screening_passed": MilestoneCopy(
        event="screening_passed",
        email_action="status_update",
        sms_template_id="DLT_SCREEN_PASS",
        whatsapp_template="screening_passed",
        title="Screening passed",
        body="Great news — you passed screening. Continue here:",
        path_template="/applications",
    ),
    "screening_failed": MilestoneCopy(
        event="screening_failed",
        email_action="candidate_rejected",
        sms_template_id="DLT_SCREEN_FAIL",
        whatsapp_template="screening_failed",
        title="Screening update",
        body="Your screening result is ready. View details:",
        path_template="/applications",
    ),
    "assessment_invitation": MilestoneCopy(
        event="assessment_invitation",
        email_action="status_update",
        sms_template_id="DLT_ASSESS_INVITE",
        whatsapp_template="assessment_invitation",
        title="Assessment invitation",
        body="You're invited to take an assessment. Start here:",
        path_template="/applications",
    ),
    "recruiter_viewed_profile": MilestoneCopy(
        event="recruiter_viewed_profile",
        email_action="status_update",
        sms_template_id="DLT_HR_VIEW_PROFILE",
        whatsapp_template="recruiter_viewed_profile",
        title="A recruiter viewed your profile",
        body="A recruiter viewed your profile on Kovetted:",
        path_template="/applications",
    ),
    "recruiter_downloaded_resume": MilestoneCopy(
        event="recruiter_downloaded_resume",
        email_action="status_update",
        sms_template_id="DLT_HR_DL_RESUME",
        whatsapp_template="recruiter_downloaded_resume",
        title="Resume downloaded",
        body="A recruiter downloaded your resume. See activity:",
        path_template="/applications",
    ),
    "recruiter_viewed_mobile": MilestoneCopy(
        event="recruiter_viewed_mobile",
        email_action="status_update",
        sms_template_id="DLT_HR_VIEW_MOBILE",
        whatsapp_template="recruiter_viewed_mobile",
        title="Contact details viewed",
        body="A recruiter viewed your mobile number. Details:",
        path_template="/applications",
    ),
    "shortlisted": MilestoneCopy(
        event="shortlisted",
        email_action="status_update",
        sms_template_id="DLT_SHORTLISTED",
        whatsapp_template="shortlisted",
        title="You've been shortlisted",
        body="You've been shortlisted. View next steps:",
        path_template="/applications",
    ),
    "interview_invitation": MilestoneCopy(
        event="interview_invitation",
        email_action="interview_schedule",
        sms_template_id="DLT_INTERVIEW_INVITE",
        whatsapp_template="interview_invitation",
        title="Interview invitation",
        body="You're invited to schedule an interview:",
        path_template="/interview/book/{token}",
        priority=Priority.HIGH,
    ),
    "interview_confirmed": MilestoneCopy(
        event="interview_confirmed",
        email_action="interview_confirmed",
        sms_template_id="DLT_INTERVIEW_CONF",
        whatsapp_template="interview_confirmed",
        title="Interview confirmed",
        body="Your interview is confirmed. Details:",
        path_template="/applications",
        priority=Priority.HIGH,
    ),
    "interview_reminder_24h": MilestoneCopy(
        event="interview_reminder_24h",
        email_action="interview_confirmed",
        sms_template_id="DLT_INTERVIEW_REM_24",
        whatsapp_template="interview_reminder_24h",
        title="Interview in 24 hours",
        body="Reminder: your interview is in 24 hours. Details:",
        path_template="/applications",
        priority=Priority.HIGH,
    ),
    "interview_reminder_2h": MilestoneCopy(
        event="interview_reminder_2h",
        email_action="interview_confirmed",
        sms_template_id="DLT_INTERVIEW_REM_2",
        whatsapp_template="interview_reminder_2h",
        title="Interview in 2 hours",
        body="Reminder: your interview starts in 2 hours. Details:",
        path_template="/applications",
        priority=Priority.URGENT,
    ),
    "selection_outcome": MilestoneCopy(
        event="selection_outcome",
        email_action="status_update",
        sms_template_id="DLT_SELECTION",
        whatsapp_template="selection_outcome",
        title="Selection update",
        body="There's an update on your application. View:",
        path_template="/applications",
    ),
}


def get_copy(event: str) -> Optional[MilestoneCopy]:
    return CATALOG.get(event)
