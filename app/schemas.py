"""Pydantic request/response schemas."""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.field_validation import (
    coerce_form_string,
    optional_email,
    optional_linkedin,
    optional_person_name,
    optional_text,
    optional_url,
    require_company_name,
    require_email,
    require_job_title,
    require_person_name,
    require_phone,
    require_place_name,
    require_text,
)


class DifficultyLevel(str, Enum):
    """HR-configured L1 interview difficulty benchmark for a job."""

    BEGINNER = "Beginner"
    INTERMEDIATE = "Intermediate"
    SENIOR = "Senior"


DIFFICULTY_LEVEL_VALUES = tuple(level.value for level in DifficultyLevel)
DEFAULT_DIFFICULTY_LEVEL = DifficultyLevel.INTERMEDIATE.value
DifficultyLevelLiteral = Literal["Beginner", "Intermediate", "Senior"]


def normalize_difficulty_level(value: Any) -> str:
    """Coerce arbitrary input to a valid difficulty_level; default Intermediate."""
    if isinstance(value, DifficultyLevel):
        return value.value
    if isinstance(value, str):
        cleaned = value.strip()
        # Accept exact labels and common casings (beginner / BEGINNER).
        for allowed in DIFFICULTY_LEVEL_VALUES:
            if cleaned.lower() == allowed.lower():
                return allowed
    return DEFAULT_DIFFICULTY_LEVEL


class DirectShareRequest(BaseModel):
    """Payload for sharing a job via direct email."""

    recipients: List[str] = Field(..., min_length=1, max_length=50)
    message: Optional[str] = None


class OnboardingRequest(BaseModel):
    """Candidate signup / first-time profile form (not job-application questions)."""

    model_config = ConfigDict(extra="allow")

    firstName: str
    lastName: str = ""
    role: str
    phone: str
    city: str
    country: str
    street: Optional[str] = ""
    email: Optional[str] = None
    # Phase 3 — structured region inputs (ISO alpha-2). Optional for backward compat.
    phone_country_code: Optional[str] = None
    country_field: Optional[str] = None
    profilePicture: Optional[str] = None
    picture: Optional[str] = None
    photo: Optional[str] = None
    avatar: Optional[str] = None

    @field_validator("firstName")
    @classmethod
    def _first_name(cls, value: str) -> str:
        return require_person_name(value, "First name")

    @field_validator("lastName")
    @classmethod
    def _last_name(cls, value: Optional[str]) -> str:
        return optional_person_name(value, "Last name")

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        text = require_text(value, "Role")
        if "@" in text:
            raise ValueError("Role must be a job title, not an email address")
        return text

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return require_phone(value, "Phone")

    @field_validator("city")
    @classmethod
    def _city(cls, value: str) -> str:
        return require_place_name(value, "City")

    @field_validator("country")
    @classmethod
    def _country(cls, value: str) -> str:
        return require_place_name(value, "Country")

    @field_validator("street")
    @classmethod
    def _street(cls, value: Optional[str]) -> str:
        return optional_text(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: Optional[str]) -> str:
        return optional_email(value, "Email")


class ProfileUpdateRequest(BaseModel):
    """Partial profile update for the candidate profile section."""

    model_config = ConfigDict(extra="allow")

    firstName: Optional[str] = None
    lastName: Optional[str] = None
    role: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    street: Optional[str] = None
    # Phase 3 region fields (ISO alpha-2). Optional until Phase 5 wiring.
    phone_country_code: Optional[str] = None
    country_field: Optional[str] = None
    profilePicture: Optional[str] = None
    picture: Optional[str] = None
    photo: Optional[str] = None
    avatar: Optional[str] = None


class ResolvedRegionModel(BaseModel):
    """Resolved reference point for proximity sorting."""

    model_config = ConfigDict(extra="ignore")

    regionType: Literal["country", "cluster"]
    regionCode: str
    lat: float
    lon: float


class CandidateRegionStateModel(BaseModel):
    """Candidate (or anonymous session) region persistence shape."""

    model_config = ConfigDict(extra="ignore")

    phone_country_code: str = ""
    country_field: str = ""
    resolved_region: Optional[ResolvedRegionModel] = None
    is_manual_override: bool = False
    region_source: Literal[
        "country_field",
        "phone_code",
        "none",
        "manual_override",
    ] = "none"


class ProfilePictureUpdateRequest(BaseModel):
    profilePicture: Optional[str] = None
    picture: Optional[str] = None
    jobId: Optional[str] = None
    applicationId: Optional[str] = None

class InterviewerModel(BaseModel):
    name: str
    email: str


class SaveAnswersRequest(BaseModel):
    candidate_id: str
    job_id: str
    questions: list
    answers: list
    # Mid-interview transcript saves must NOT finalize L1. Only forced/early
    # finishes (and send_message's natural completion) should set this.
    finalize: bool = False


class SaveL2QuestionsRequest(BaseModel):
    candidate_id: str
    job_id: str
    questions: List[str]


class SendMessageRequest(BaseModel):
    candidate_id: str
    job_id: str
    message: str


class NoteRequest(BaseModel):
    candidate_id: str
    candidate_name: str
    job_id: str
    note: str
    level: str
    author: Optional[str] = "HR Manager"


class DraftL2FeedbackRequest(BaseModel):
    candidate_id: str
    job_id: str
    feedback: str


class SubmitFeedbackRequest(BaseModel):
    candidate_id: str
    job_id: str
    feedback: str
    author: Optional[str] = "HR Manager"
    level: Optional[str] = "L2"


class UpdateL1StatusRequest(BaseModel):
    candidate_id: str
    job_id: str
    status: str
    candidate_name: Optional[str] = None
    candidate_email: Optional[str] = None
    position: Optional[str] = None
    interview_level: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    duration_minutes: Optional[int] = None
    timezone: Optional[str] = None
    emails: Optional[List[str]] = None
    main_reviews: Optional[Any] = None
    interviewers: Optional[List[InterviewerModel]] = None


class JobUpdateRequest(BaseModel):
    """Allow-listed fields for PATCH /api/jobview/{jobid} — prevents NoSQL injection."""

    model_config = ConfigDict(extra="forbid")

    jobrole: Optional[str] = None
    jobDescription: Optional[str] = None
    educationalQualifications: Optional[str] = None
    experienceRequired: Optional[str] = None
    experienceYearsRequired: Optional[str] = None
    desiredSkills: Optional[str] = None
    requiredCertifications: Optional[str] = None
    goodToHave: Optional[str] = None
    employmentType: Optional[str] = None
    workMode: Optional[str] = None
    location: Optional[str] = None
    locationCountry: Optional[str] = None
    locationCountryCode: Optional[str] = None
    locationCity: Optional[str] = None
    joiningRequirement: Optional[str] = None
    salaryPackage: Optional[str] = None
    salaryUndisclosed: Optional[bool] = None
    sectionWeightage: Optional[dict] = None
    onboarding: Optional[Any] = None
    show: Optional[str] = Field(default=None, pattern="^(on|off|closed)$")
    jd_category: Optional[List[str]] = None
    jd_keywords: Optional[List[str]] = None
    jd_keywords_ai: Optional[List[str]] = None
    category: Optional[str] = None
    rolesResponsibilities: Optional[str] = None
    draftWeightageSections: Optional[Any] = None
    draftQuestions: Optional[Any] = None
    difficulty_level: Optional[DifficultyLevelLiteral] = None
    technicalInterviewEnabled: Optional[bool] = None
    technicalQuestions: Optional[List[str]] = None
    technicalWeightage: Optional[int] = None
    technicalInterviewPostponeEnabled: Optional[bool] = None

    @field_validator("difficulty_level", mode="before")
    @classmethod
    def _normalize_optional_difficulty(cls, value):
        if value is None or value == "":
            return None
        return normalize_difficulty_level(value)


class JobCreateRequest(BaseModel):
    """Live job posting / publish payload from the HR dashboard.

    Drafts use a separate unvalidated endpoint. This model enforces the
    starred fields on the post-job form before a job can go live.
    """

    model_config = ConfigDict(extra="allow")

    jobrole: Optional[str] = None
    jobDescription: Optional[str] = None
    educationalQualifications: Optional[str] = None
    experienceRequired: Optional[str] = None
    experienceYearsRequired: Optional[str] = None
    desiredSkills: Optional[str] = None
    requiredCertifications: Optional[str] = None
    goodToHave: Optional[str] = None
    employmentType: Optional[str] = None
    workMode: Optional[str] = None
    location: Optional[str] = None
    locationCountry: Optional[str] = None
    locationCountryCode: Optional[str] = None
    locationCity: Optional[str] = None
    joiningRequirement: Optional[str] = None
    salaryPackage: Optional[str] = None
    salaryUndisclosed: Optional[bool] = None
    companyName: Optional[str] = None
    sectionWeightage: Optional[dict] = None
    onboarding: Optional[Any] = None
    show: Literal["on", "off", "closed"] = "on"
    jd_category: List[str] = Field(default_factory=list)
    jd_keywords: List[str] = Field(default_factory=list)
    jd_keywords_ai: List[str] = Field(default_factory=list)
    category: Optional[str] = None
    rolesResponsibilities: Optional[str] = None
    difficulty_level: DifficultyLevelLiteral = DEFAULT_DIFFICULTY_LEVEL
    technicalInterviewEnabled: Optional[bool] = None
    technicalQuestions: Optional[List[str]] = None
    technicalWeightage: Optional[int] = None
    technicalInterviewPostponeEnabled: Optional[bool] = None

    @field_validator("difficulty_level", mode="before")
    @classmethod
    def _normalize_difficulty(cls, value):
        return normalize_difficulty_level(value)

    @field_validator(
        "jobrole",
        "jobDescription",
        "educationalQualifications",
        "experienceRequired",
        "experienceYearsRequired",
        "desiredSkills",
        "requiredCertifications",
        "goodToHave",
        "employmentType",
        "workMode",
        "location",
        "locationCountry",
        "locationCountryCode",
        "locationCity",
        "joiningRequirement",
        "salaryPackage",
        "companyName",
        "category",
        "rolesResponsibilities",
        mode="before",
    )
    @classmethod
    def _strip_optional_text(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def _require_starred_job_fields(self):
        missing: list[str] = []

        def filled(*values: Optional[str]) -> bool:
            return any(str(value or "").strip() for value in values)

        if not filled(self.jobrole):
            missing.append("Job title")
        elif self.jobrole:
            self.jobrole = require_job_title(self.jobrole, "Job title")

        if not filled(self.jobDescription):
            missing.append("Job description")
        elif len(str(self.jobDescription).strip()) < 20:
            raise ValueError("Job description must be at least 20 characters")

        if not filled(self.employmentType):
            missing.append("Employment type")
        if not filled(self.workMode):
            missing.append("Work mode")
        if not filled(self.location):
            missing.append("Location")
        if not filled(self.locationCountryCode):
            missing.append("Location country")
        if not filled(self.experienceRequired, self.experienceYearsRequired):
            missing.append("Experience required")
        if not filled(self.desiredSkills):
            missing.append("Desired skills")

        # Blank salary is allowed: treat it as undisclosed rather than blocking publish.
        if not filled(self.salaryPackage):
            self.salaryUndisclosed = True
            self.salaryPackage = ""

        if missing:
            raise ValueError(
                "Please fill all required fields: " + ", ".join(missing)
            )
        return self


class GenerateJobKeywordsRequest(BaseModel):
    job_title: str = ""
    job_description: Optional[str] = None
    roles_responsibilities: Optional[str] = None
    desired_skills: Optional[str] = None
    experience_required: Optional[str] = None
    manual_keywords: List[str] = Field(default_factory=list)


class JobShowToggleRequest(BaseModel):
    show: str = Field(..., pattern="^(on|off|closed)$")


class TechnicalInterviewToggleRequest(BaseModel):
    """Request to toggle technical interview requirement for a job."""

    technicalInterviewEnabled: bool = Field(..., description="Whether technical interview is enabled")
    optionChosen: Optional[str] = Field(
        None,
        description="For live jobs: 'leave_existing' or 'invite_existing' when enabling, 'keep_completed' or 'advance_in_progress' when disabling"
    )


class TechnicalInterviewToggleResponse(BaseModel):
    """Response when toggling technical interview requirement."""

    message: str
    jobId: str
    technicalInterviewEnabled: bool
    candidatesAffected: Optional[int] = None
    optionChosen: Optional[str] = None
    weightageRebalanced: Optional[dict] = None


# Technical Interview Postponement Schemas

class TechnicalInterviewStatus(str, Enum):
    """Technical interview lifecycle states."""

    PENDING = "TECHNICAL_INTERVIEW_PENDING"
    IN_PROGRESS = "TECHNICAL_INTERVIEW_IN_PROGRESS"
    COMPLETED = "TECHNICAL_INTERVIEW_COMPLETED"
    POSTPONED = "TECHNICAL_INTERVIEW_POSTPONED"
    WITHDRAWN = "TECHNICAL_INTERVIEW_WITHDRAWN"
    CANCELLED_JOB_CLOSED = "TECHNICAL_INTERVIEW_CANCELLED_JOB_CLOSED"
    NEXT_STAGE = "TECHNICAL_INTERVIEW_NEXT_STAGE"
    NOT_ATTENDED = "TECHNICAL_INTERVIEW_NOT_ATTENDED"
    REJECTED = "TECHNICAL_INTERVIEW_REJECTED"


class PostponeTechnicalInterviewRequest(BaseModel):
    """Request to postpone technical interview."""

    candidateTimezone: str = Field(..., description="Candidate's timezone at postponement (e.g., 'Asia/Kolkata')")


class PostponeTechnicalInterviewResponse(BaseModel):
    """Response when postponing technical interview."""

    message: str
    applicationId: str
    status: TechnicalInterviewStatus
    deadlineAt: str
    deadlineLocal: str
    timezone: str
    canPostpone: bool
    canStart: bool
    postponementNumber: int
    maxPostponements: int


class StartTechnicalInterviewRequest(BaseModel):
    """Request to start technical interview."""

    applicationId: str = Field(..., description="Application ID")


class StartTechnicalInterviewResponse(BaseModel):
    """Response when starting technical interview."""

    message: str
    applicationId: str
    status: TechnicalInterviewStatus
    interviewId: str


class TechnicalInterviewStateResponse(BaseModel):
    """Response for technical interview state query."""

    status: TechnicalInterviewStatus
    jobTitle: str
    company: str
    deadlineAt: Optional[str] = None
    deadlineLocal: Optional[str] = None
    timezone: Optional[str] = None
    canPostpone: bool
    canStart: bool
    postponementNumber: Optional[int] = None
    maxPostponements: Optional[int] = None
    interviewConfiguration: Optional[dict] = None


class ExtendTechnicalInterviewRequest(BaseModel):
    """Request to extend technical interview deadline (recruiter)."""

    extensionHours: int = Field(..., ge=1, le=72, description="Extension hours (max 72)")


class ExtendTechnicalInterviewResponse(BaseModel):
    """Response when extending technical interview deadline."""

    message: str
    applicationId: str
    oldDeadline: str
    newDeadline: str
    extensionHours: int


class ReopenTechnicalInterviewRequest(BaseModel):
    """Request to reopen missed technical interview (recruiter)."""

    newDeadlineHours: int = Field(..., ge=1, le=168, description="New deadline in hours (max 7 days)")


class ReopenTechnicalInterviewResponse(BaseModel):
    """Response when reopening technical interview."""

    message: str
    applicationId: str
    newDeadline: str
    status: TechnicalInterviewStatus


class GenerateJobDescriptionRequest(BaseModel):
    job_title: str


class TokenRequest(BaseModel):
    api_key: str
    role: str = "hr"
    subject: str = "hr-user"
    candidate_id: Optional[str] = None


class SendEmailRequest(BaseModel):
    Company: Optional[str] = "Fanisko"
    candidate_id: Optional[str] = None
    job_id: Optional[str] = None
    candidate_name: Optional[str] = None
    candidate_email: Optional[str] = None
    position: Optional[str] = None
    interview_level: Optional[str] = "Technical L3"
    date: Optional[str] = None
    time: Optional[str] = None
    duration_minutes: Optional[int] = 60
    timezone: Optional[str] = "Asia/Kolkata"
    meeting_link: Optional[str] = None
    interviewers: Optional[List[InterviewerModel]] = None


class ListEventsRequest(BaseModel):
    email_id: str
    days: int = Field(ge=0, le=90)
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class CreateInterviewInviteRequest(BaseModel):
    candidate_id: str
    job_id: str
    candidate_name: Optional[str] = None
    candidate_email: Optional[str] = None
    position: Optional[str] = None
    interview_level: Optional[str] = "Technical L2"
    duration_minutes: Optional[int] = 60
    timezone: Optional[str] = "Asia/Kolkata"
    date_range_start: str
    date_range_end: str
    include_saturday: Optional[bool] = False
    include_sunday: Optional[bool] = False
    working_hours_start: Optional[int] = 9
    working_hours_end: Optional[int] = 21
    slot_increment_minutes: Optional[int] = 30
    interviewers: Optional[List[InterviewerModel]] = None
    share_emails: Optional[List[str]] = None
    hr_calendar_email: Optional[str] = None
    frontend_base_url: Optional[str] = None
    interview_mode: Optional[str] = "remote"
    location_address: Optional[str] = None
    maps_url: Optional[str] = None


class RescheduleInterviewInviteRequest(CreateInterviewInviteRequest):
    """HR picks a new date/time or a new candidate booking window."""

    invite_id: Optional[str] = None
    token: Optional[str] = None
    date_range_start: Optional[str] = None
    date_range_end: Optional[str] = None
    include_saturday: Optional[bool] = None
    include_sunday: Optional[bool] = None
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = None


class InterviewOutcomeRequest(BaseModel):
    status: Literal["completed", "cancelled"]


class ConfirmInterviewBookingRequest(BaseModel):
    date: str
    time: str


class ShareInterviewInviteRequest(BaseModel):
    emails: Optional[List[str]] = None


class CandidateApplyNotificationRequest(BaseModel):
    job_id: str = Field(min_length=1)
    job_role: str = "N/A"
    candidate_name: Optional[str] = None
    submission_date: Optional[str] = None


class GoogleAuthRequest(BaseModel):
    """Body posted by the frontend after Google Identity Services sign-in."""

    credential: str = Field(min_length=10)

    @field_validator("credential")
    @classmethod
    def _credential(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Google sign-in credential is required")
        return text


class HrOnboardingRequest(BaseModel):
    """HR company-onboarding form, submitted once after first Google login."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    companyName: str
    industry: str = ""
    companySize: str = ""
    contactName: str = Field(
        default="",
        validation_alias=AliasChoices("contactName", "fullName", "name"),
    )
    workEmail: str = Field(
        default="",
        validation_alias=AliasChoices("workEmail", "email"),
    )
    role: str = Field(
        default="",
        validation_alias=AliasChoices("role", "designation"),
    )
    website: str = ""
    hqLocation: str = Field(
        default="",
        validation_alias=AliasChoices("hqLocation", "headquarters"),
    )
    aboutCompany: str = ""
    linkedinUrl: str = Field(
        default="",
        validation_alias=AliasChoices("linkedinUrl", "linkedin"),
    )
    linkedinHandle: str = ""
    companyLogo: str = ""
    companyBanner: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _coerce_strings(cls, value: Any) -> Any:
        return coerce_form_string(value)

    @field_validator("companyName")
    @classmethod
    def _company_name(cls, value: str) -> str:
        return require_company_name(value, "Company name")

    @field_validator("industry")
    @classmethod
    def _industry(cls, value: str) -> str:
        text = optional_text(value)
        return require_text(text, "Industry") if text else ""

    @field_validator("companySize")
    @classmethod
    def _company_size(cls, value: str) -> str:
        text = optional_text(value)
        return require_text(text, "Company size", min_length=1) if text else ""

    @field_validator("contactName")
    @classmethod
    def _contact_name(cls, value: str) -> str:
        text = optional_text(value)
        return require_person_name(text, "Contact name") if text else ""

    @field_validator("workEmail")
    @classmethod
    def _work_email(cls, value: str) -> str:
        return optional_email(value, "Work email")

    @field_validator("role")
    @classmethod
    def _hr_role(cls, value: str) -> str:
        text = optional_text(value)
        if not text:
            return ""
        text = require_text(text, "Role")
        if "@" in text:
            raise ValueError("Role must be a job title, not an email address")
        return text

    @field_validator("website")
    @classmethod
    def _website(cls, value: str) -> str:
        return optional_url(value, "Website")

    @field_validator("hqLocation")
    @classmethod
    def _hq(cls, value: str) -> str:
        return optional_text(value)

    @field_validator("aboutCompany")
    @classmethod
    def _about(cls, value: str) -> str:
        return optional_text(value)

    @field_validator("linkedinUrl")
    @classmethod
    def _linkedin_url(cls, value: str) -> str:
        return optional_linkedin(value, "LinkedIn URL")

    @field_validator("linkedinHandle")
    @classmethod
    def _linkedin_handle(cls, value: str) -> str:
        return optional_linkedin(value, "LinkedIn")

    @field_validator("companyLogo", "companyBanner")
    @classmethod
    def _branding(cls, value: str) -> str:
        return optional_text(value)
