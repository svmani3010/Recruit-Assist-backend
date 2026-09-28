"""Pydantic schema for the live L1 interview structured output.

Used by ``GeminiAPI.live_l1`` / ``GeminiAPI.continue_l1``. The schema itself
lives in ``prompts/l1_v3_schema.py`` (co-located with its system prompt,
``prompts/l1_v3.md``); this module re-exports it so callers can import L1's
schema the same way as every other structured-output schema in this
package (``from gemini_schemas import InterviewTurnResponse``).
"""

from __future__ import annotations

from prompts.l1_v3_schema import (
    FinalReport,
    InterviewState,
    InterviewTurnResponse,
    QuestionHistoryItem,
)

__all__ = [
    "FinalReport",
    "InterviewState",
    "InterviewTurnResponse",
    "QuestionHistoryItem",
]
