"""Pydantic schemas for Gemini Interactions API structured outputs.

Each module maps 1:1 to a structured-output call in
``app.llm.gemini_client.GeminiAPI``. Field descriptions are sent to the model
as part of the JSON schema, so they double as extraction instructions —
prefer editing descriptions here over adding instructions to system prompts.
"""

from __future__ import annotations

from gemini_schemas.complete_screening import CompleteScreeningResult, ScoreBreakdownItem
from gemini_schemas.experience_years import ExperienceYearsResult
from gemini_schemas.initial_screening import InitialScreeningResult
from gemini_schemas.jd import JdResponse
from gemini_schemas.job_description import JobDescriptionGenerator
from gemini_schemas.job_keywords import JobKeywordsResult
from gemini_schemas.l1_interview import (
    FinalReport,
    InterviewState,
    InterviewTurnResponse,
    QuestionHistoryItem,
)
from gemini_schemas.l2_questions import L2QuestionsResult
from gemini_schemas.resume import ResumeResponse

__all__ = [
    "CompleteScreeningResult",
    "ScoreBreakdownItem",
    "ExperienceYearsResult",
    "InitialScreeningResult",
    "JdResponse",
    "JobDescriptionGenerator",
    "JobKeywordsResult",
    "FinalReport",
    "InterviewState",
    "InterviewTurnResponse",
    "QuestionHistoryItem",
    "L2QuestionsResult",
    "ResumeResponse",
]
