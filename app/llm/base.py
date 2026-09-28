"""Provider-agnostic protocol for screening LLM operations."""

from __future__ import annotations

from typing import Any, Protocol, Tuple, runtime_checkable

from gemini_schemas import InterviewTurnResponse


@runtime_checkable
class ScreeningLLM(Protocol):
    """Interface implemented by GeminiAPI (and future providers)."""

    def resume_parser(self, resume_text: str) -> dict: ...

    def jd_parser(self, jd_text: str) -> str: ...

    def initial_screening(self, lite_resume_data: dict, lite_jd_data: dict) -> dict: ...

    def live_l1(
        self,
        resume_data: dict,
        jd_data: dict,
        difficulty_level: str = "Intermediate",
    ) -> Tuple[str, InterviewTurnResponse]: ...

    def continue_l1(
        self, previous_interaction_id: str, message: str
    ) -> Tuple[str, InterviewTurnResponse]: ...

    def complete_screening(
        self, complete_resume: str, complete_jd: str, section_weightages: dict
    ) -> dict: ...

    def job_description_generator(self, job_title: str) -> dict: ...

    def job_keywords_generator(self, payload: dict) -> dict: ...

    def generate_l2_questions(
        self,
        resume_json: dict,
        jd_json: dict,
        resume_score: Any,
        resume_reason: Any,
        l1_report: dict,
    ) -> dict: ...

    def generate_interview_email(self, data: dict) -> str: ...
