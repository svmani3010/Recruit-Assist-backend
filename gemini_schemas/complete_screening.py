"""Pydantic schema for the L0 final (complete) screening structured output.

Used by ``GeminiAPI.complete_screening``. No field-level descriptions exist
in the original implementation for this schema, so none are added here.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel


class ScoreBreakdownItem(BaseModel):
    category: str
    score: str


class CompleteScreeningResult(BaseModel):
    llm_score: float
    llm_compatibility: str
    score_breakdown: List[ScoreBreakdownItem]
    reasons: List[str]
