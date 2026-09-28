"""Pydantic schema for the job-description-parsing structured output.

Used by ``GeminiAPI.jd_parser`` as the ``response_format`` schema for the
Interactions API.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class JobInformation(BaseModel):
    """Core details about the job role and its context."""

    job_title: str = Field(description="The official title of the job.")
    department: str = Field(description="The department or team the role belongs to.")
    employment_type: str = Field(
        description="The type of employment (e.g., 'Full-time', 'Contract', 'Internship')."
    )
    location: str = Field(
        description="The physical location of the job (e.g., 'San Francisco, CA', 'London, UK')."
    )
    remote_policy: str = Field(
        description="The policy on remote work (e.g., 'Fully Remote', 'Hybrid', 'On-site')."
    )


class RequiredEducation(BaseModel):
    degree: str = Field(description="The required degree (e.g., 'Bachelor of Science').")
    field_of_study: str = Field(
        description="The required field of study (e.g., 'Computer Science')."
    )


class PreferredEducation(BaseModel):
    degree: str = Field(description="The preferred degree.")
    field_of_study: str = Field(description="The preferred field of study.")


class Requirements(BaseModel):
    """The mandatory qualifications and skills required for the job."""

    education: List[RequiredEducation] = Field(
        description="Required educational background."
    )
    technical_skills: List[str] = Field(
        description="List of required technical skills."
    )
    soft_skills: List[str] = Field(description="List of required soft skills.")
    tools_and_technologies: List[str] = Field(
        description="List of required tools and technologies."
    )
    certifications_required: List[str] = Field(
        description="List of mandatory certifications."
    )


class PreferredQualifications(BaseModel):
    """The 'nice-to-have' qualifications and skills that are preferred but not mandatory."""

    education: List[PreferredEducation] = Field(
        description="Preferred educational background."
    )
    technical_skills: List[str] = Field(
        description="List of preferred technical skills."
    )
    soft_skills: List[str] = Field(description="List of preferred soft skills.")
    tools_and_technologies: List[str] = Field(
        description="List of preferred tools and technologies."
    )
    certifications_preferred: List[str] = Field(
        description="List of preferred certifications."
    )


class ExperienceRequired(BaseModel):
    """Details about the required level and type of professional experience."""

    total_years: str = Field(
        description="The total years of professional experience required (e.g., '5+ years')."
    )
    relevant_experience_years: str = Field(
        description="The years of experience required in a specific, relevant field "
        "(e.g., '3+ years in data engineering')."
    )
    domains: List[str] = Field(
        description="Specific industries or domains where experience is required."
    )


class SalaryRange(BaseModel):
    """The salary range for the position."""

    min: float = Field(description="The minimum salary for the role.")
    max: float = Field(description="The maximum salary for the role.")
    currency: str = Field(description="The currency of the salary (e.g., 'USD', 'EUR').")


class JdOtherInformationItem(BaseModel):
    key: str = Field(
        description="The label or topic of the information (e.g., 'Travel Requirements')."
    )
    value: str = Field(
        description="The details of the information (e.g., '10% travel required')."
    )


class JdResponse(BaseModel):
    """Schema for parsing a job description from raw text. Extract information to populate all fields. All fields are mandatory."""

    job_information: JobInformation = Field(
        description="Core details about the job role and its context."
    )
    summary: str = Field(description="A brief summary or overview of the job role.")
    responsibilities: List[str] = Field(
        description="A list of the key responsibilities and day-to-day tasks for this role."
    )
    requirements: Requirements = Field(
        description="The mandatory qualifications and skills required for the job."
    )
    preferred_qualifications: PreferredQualifications = Field(
        description="The 'nice-to-have' qualifications and skills that are preferred but not mandatory."
    )
    experience_required: ExperienceRequired = Field(
        description="Details about the required level and type of professional experience."
    )
    benefits: List[str] = Field(description="A list of general company benefits.")
    salary_range: SalaryRange = Field(description="The salary range for the position.")
    employment_benefits: List[str] = Field(
        description="Specific benefits tied to employment, such as health "
        "insurance or retirement plans."
    )
    application_deadline: str = Field(
        description="The closing date for applications.",
        json_schema_extra={"format": "date-time"},
    )
    other_information: List[JdOtherInformationItem] = Field(
        description="A list of key-value pairs for any other miscellaneous information."
    )
    jd_keywords_ai: List[str] = Field(
        description="Analyze the provided Job Description and output exactly 5 "
        "highly relevant keywords that best explain the core requirements and "
        "nature of the JD.",
        min_length=5,
        max_length=5,
    )
