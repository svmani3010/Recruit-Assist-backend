"""Pydantic schema for the job-keywords structured output.

Used by ``GeminiAPI.job_keywords_generator``. This call did not exist in the
legacy implementation, so the description below is carried over unchanged
from the current schema.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class JobKeywordsResult(BaseModel):
    jd_keywords_ai: List[str] = Field(
        description="Exactly 5 short, distinct search keywords for the job",
        min_length=5,
        max_length=5,
    )
