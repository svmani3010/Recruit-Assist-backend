"""Pydantic schema for the L0 (initial) resume/JD compatibility structured output.

Used by ``GeminiAPI.initial_screening``.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class InitialScreeningResult(BaseModel):
    """Represents the compatibility score between a candidate's resume and a job description."""

    compatibility: str = Field(
        description="The calculated compatibility score, expressed as a "
        "percentage string (e.g., '82%')."
    )
    l0_reasons: List[str] = Field(
        description="A list of key reasons explaining the screening result "
        "(pass or fail), highlighting the most important factors."
    )
