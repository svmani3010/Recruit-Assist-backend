"""Pydantic schema for the L2 interview-question generation structured output.

Used by ``GeminiAPI.generate_l2_questions``. No field-level descriptions
exist in the original implementation for this schema, so none are added here.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel


class L2QuestionsResult(BaseModel):
    question: List[str]
