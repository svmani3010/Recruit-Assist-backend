"""Pydantic schema for the resume-parsing structured output.

Used by ``GeminiAPI.resume_parser`` as the ``response_format`` schema for the
Interactions API. Field descriptions are read by the model at generation
time, so they carry the actual extraction instructions — keep them precise.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class PersonalInformation(BaseModel):
    """All personal contact information for the candidate. All fields are mandatory."""

    full_name: str = Field(description="The full name of the candidate.")
    email: str = Field(description="The email address of the candidate.")
    phone: str = Field(description="The phone number of the candidate.")
    location: str = Field(
        description="The city and state of the candidate, e.g., 'San Francisco, CA'."
    )
    linkedin: str = Field(description="URL to the candidate's LinkedIn profile.")
    portfolio_url: str = Field(
        description="URL to the candidate's personal portfolio or GitHub."
    )


class Education(BaseModel):
    degree: str = Field(description="The degree obtained, e.g., 'Bachelor of Science'.")
    field_of_study: str = Field(
        description="The major or field of study, e.g., 'Computer Science'."
    )
    institution: str = Field(description="The name of the university or institution.")
    start_date: str = Field(description="The start date of education.")
    end_date: str = Field(description="The end date or expected graduation date.")
    grade: str = Field(
        description="The grade or classification, e.g., 'Summa Cum Laude'."
    )
    gpa: float = Field(description="The Grade Point Average.")


class Skills(BaseModel):
    """All skills listed in the resume, categorized. All fields are mandatory."""

    technical_skills: List[str] = Field(
        description="List of technical skills, languages, and frameworks."
    )
    soft_skills: List[str] = Field(description="List of soft skills.")
    tools_and_technologies: List[str] = Field(
        description="List of specific tools and technologies."
    )


class WorkExperience(BaseModel):
    job_title: str = Field(description="The job title or position held.")
    organization: str = Field(description="The name of the company or organization.")
    location: str = Field(description="The location of the job.")
    start_date: str = Field(description="The start date of the employment.")
    end_date: str = Field(description="The end date of the employment (or 'Present').")
    description: List[str] = Field(
        description="A list of sentences describing the overall role."
    )
    responsibilities: List[str] = Field(
        description="A bulleted list of key responsibilities."
    )
    achievements: List[str] = Field(
        description="A bulleted list of notable achievements."
    )


class Project(BaseModel):
    project_name: str = Field(description="The name of the project.")
    description: str = Field(description="A brief description of the project.")
    technologies_used: List[str] = Field(
        description="List of technologies used in the project."
    )
    role: str = Field(description="The candidate's role in the project.")
    link: str = Field(description="A URL to the project repository or live version.")


class Certification(BaseModel):
    name: str = Field(description="The name of the certification.")
    issuer: str = Field(
        description="The organization that issued the certification."
    )
    date_issued: str = Field(description="The date the certification was issued.")
    expiration_date: str = Field(description="The expiration date, if applicable.")


class OtherInformationItem(BaseModel):
    key: str = Field(
        description="The label or topic of the miscellaneous information "
        "(e.g., 'Hobbies', 'Availability')."
    )
    value: str = Field(
        description="The details of the miscellaneous information "
        "(e.g., 'Hiking, Reading', 'Available Immediately')."
    )


class ResumeResponse(BaseModel):
    """Schema for a parsed resume from raw text. Extract information from the provided text to populate all fields. All fields are mandatory."""

    personal_information: PersonalInformation = Field(
        description="All personal contact information for the candidate. All fields are mandatory."
    )
    objective: str = Field(description="The career objective statement from the resume.")
    summary: str = Field(description="The professional summary section of the resume.")
    education: List[Education] = Field(
        description="A list of all educational qualifications. All fields are mandatory."
    )
    skills: Skills = Field(
        description="All skills listed in the resume, categorized. All fields are mandatory."
    )
    work_experience: List[WorkExperience] = Field(
        description="A list of all previous jobs and work experiences. "
        "All fields are mandatory."
    )
    projects: List[Project] = Field(
        description="A list of projects the candidate has worked on. "
        "All fields are mandatory."
    )
    certifications: List[Certification] = Field(
        description="A list of professional certifications. All fields are mandatory."
    )
    awards_and_honors: List[str] = Field(description="List of any awards or honors.")
    publications: List[str] = Field(description="List of any publications.")
    languages: List[str] = Field(
        description="List of languages spoken by the candidate."
    )
    referrals: List[str] = Field(description="Any referrals mentioned in the resume.")
    other_information: List[OtherInformationItem] = Field(
        description="A list of key-value pairs for any other miscellaneous "
        "information. All fields are mandatory."
    )
