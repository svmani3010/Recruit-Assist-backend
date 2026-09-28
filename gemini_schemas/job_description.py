"""Pydantic schema for the AI-drafted job description structured output.

Used by ``GeminiAPI.job_description_generator``. Output keys are
human-readable titles (e.g. "Job Description") to match the existing API
response shape, so fields use aliases. No field-level descriptions exist in
the original implementation for this schema, so none are added here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JobDescriptionGenerator(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    job_description: str = Field(alias="Job Description")
    roles_and_responsibilities: str = Field(alias="Roles and Responsibilities")
    educational_qualifications: str = Field(alias="Educational Qualifications")
    experience_required: str = Field(alias="Experience Required")
    desired_skills: str = Field(alias="Desired Skills")
    good_to_have: str = Field(alias="Good to Have")
