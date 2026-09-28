"""Pydantic schema for the experience-years-parsing structured output.

Used by ``GeminiAPI.experience_years_parser``. This call did not exist in the
legacy implementation, so the description below is carried over unchanged
from the current schema.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class ExperienceYearsResult(BaseModel):
    years: List[int] = Field(
        description="Discrete years of experience included in this requirement"
    )
