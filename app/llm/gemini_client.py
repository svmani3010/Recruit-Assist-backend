"""Gemini implementation of the ScreeningLLM protocol, built on the Interactions API."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional, Tuple, Type

from google import genai
from pydantic import BaseModel

from app.llm.errors import LLMConfigError, LLMError, LLMResponseError
from app.llm.prompt_safety import sanitize_interview_email_fields
from app.llm.retry import create_interaction, generate_json
from app.schemas import DEFAULT_DIFFICULTY_LEVEL, normalize_difficulty_level
from gemini_schemas import (
    CompleteScreeningResult,
    ExperienceYearsResult,
    InitialScreeningResult,
    InterviewTurnResponse,
    JdResponse,
    JobDescriptionGenerator,
    JobKeywordsResult,
    L2QuestionsResult,
    ResumeResponse,
)
from sp_manager import PromptNotFoundError, SystemPromptManager

logger = logging.getLogger(__name__)

# Valid public Gemini model IDs (as of 2026). Configurable via settings/env.
DEFAULT_MODEL_FLASH = "gemini-3.1-flash-lite"
DEFAULT_MODEL_PRO = "gemini-3.1-flash-lite"

# Cache keys = `prompts/<name>.md` stems. Must exist at GeminiAPI startup.
REQUIRED_PROMPTS = (
    "resume",
    "jd",
    "initial",
    "complete",
    "l1_v3",
    "L2",
    "job_description_prompt",
    "job_keywords",
    "experience_years",
    "interview_email",
)

# L1 uses Google Search grounding so the interviewer can verify technical
# claims. Tools are interaction-scoped, so this is re-sent on every turn.
_GOOGLE_SEARCH_TOOL = [{"type": "google_search"}]


def _thinking_level(budget: int) -> str:
    """Map a legacy numeric thinking budget to an Interactions API thinking level.

    The Interactions API replaced the raw token `thinking_budget` with a
    coarse `thinking_level` (`minimal`/`low`/`medium`/`high`). Settings still
    expose a budget for backward compatibility, so bucket it here.
    """
    if budget <= 0:
        return "minimal"
    if budget <= 1024:
        return "low"
    if budget <= 8192:
        return "medium"
    return "high"


def _response_format(schema: Type[BaseModel]) -> dict:
    return {
        "type": "text",
        "mime_type": "application/json",
        "schema": schema.model_json_schema(),
    }


class GeminiAPI:
    """
    Production Gemini client for screening workflows.

    - Uses a single SystemPromptManager instance (no per-call file I/O)
    - Structured outputs use Pydantic schemas from `gemini_schemas/` — field
      descriptions there carry the extraction instructions, so keep system
      prompts short and put detailed guidance on the schema fields instead.
    - Bounded thinking levels (never unbounded in production defaults)
    - Retries + timeouts on every Interactions API call
    - Prompt-injection hardening for untrusted interview email fields
    - Async wrappers via asyncio.to_thread for FastAPI routes
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        prompt_dir: str = "prompts",
        *,
        model_flash: str = DEFAULT_MODEL_FLASH,
        model_pro: str = DEFAULT_MODEL_PRO,
        thinking_budget: int = 2048,
        max_retries: int = 3,
        timeout_seconds: float = 90.0,
    ):
        if not api_key:
            raise LLMConfigError("GEMINI_API_KEY is required")
        # Keep key only for client construction — do not expose via repr/logs
        self._client = genai.Client(api_key=api_key)
        self.model_flash = model_flash
        self.model_pro = model_pro
        self.thinking_budget = max(0, int(thinking_budget))
        self.max_retries = max(1, int(max_retries))
        self.timeout_seconds = float(timeout_seconds)
        try:
            self.prompt_manager = SystemPromptManager(
                prompt_dir=prompt_dir,
                required=REQUIRED_PROMPTS,
            )
        except PromptNotFoundError as e:
            raise LLMConfigError(str(e)) from e

    @property
    def client(self):
        """Long-lived genai client (required for durable L1 interactions)."""
        return self._client

    def _require_prompt(self, name: str) -> str:
        try:
            return self.prompt_manager.require(name)
        except PromptNotFoundError as e:
            raise LLMConfigError(str(e)) from e

    def _generation_config(self, temperature: Optional[float] = None) -> dict:
        config: dict[str, Any] = {
            "thinking_level": _thinking_level(self.thinking_budget)
        }
        if temperature is not None:
            config["temperature"] = temperature
        return config

    def _generate_structured(
        self,
        *,
        model: str,
        input: Any,
        system_instruction: str,
        schema: Type[BaseModel],
        by_alias: bool = False,
        temperature: Optional[float] = None,
    ) -> dict:
        """Call the Interactions API with a Pydantic `response_format` schema,
        validate the result against that schema, and return it as a dict."""
        interaction = create_interaction(
            client=self._client,
            max_retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            model=model,
            input=input,
            system_instruction=system_instruction,
            response_format=_response_format(schema),
            generation_config=self._generation_config(temperature),
            store=False,
        )
        text = (getattr(interaction, "output_text", None) or "").strip()
        if not text:
            raise LLMResponseError("Empty response from Gemini")
        try:
            validated = schema.model_validate_json(text)
        except Exception as e:
            raise LLMResponseError(
                f"Gemini response did not match the {schema.__name__} schema: {e}"
            ) from e
        return validated.model_dump(by_alias=by_alias)

    def _generate_text(
        self,
        *,
        model: str,
        input: Any,
        system_instruction: str,
        temperature: Optional[float] = None,
    ) -> str:
        return generate_json(
            client=self._client,
            max_retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            parse_json=False,
            model=model,
            input=input,
            system_instruction=system_instruction,
            generation_config=self._generation_config(temperature),
            store=False,
        )

    # ---- Public sync API (also used from background threads) ----

    def resume_parser(self, resume_text: str) -> dict:
        sp_resume = self._require_prompt("resume")
        return self._generate_structured(
            model=self.model_flash,
            input=resume_text,
            system_instruction=sp_resume,
            schema=ResumeResponse,
            temperature=0.8,
        )

    def jd_parser(self, jd_text: str) -> str:
        sp_jd = self._require_prompt("jd")
        result = self._generate_structured(
            model=self.model_flash,
            input=jd_text,
            system_instruction=sp_jd,
            schema=JdResponse,
            temperature=0.8,
        )
        return json.dumps(result, indent=2)

    def initial_screening(self, lite_resume_data: dict, lite_jd_data: dict) -> dict:
        sp_initial = self._require_prompt("initial")
        current_datetime = datetime.now().strftime("%d-%m-%Y")
        prompt = (
            "Resume Data:\n"
            f"{json.dumps(lite_resume_data, indent=2)}\n\n"
            "---\n\n"
            "Job Description:\n"
            f"{json.dumps(lite_jd_data, indent=2)}\n\n"
            "---\n\n"
            f"Current Date: {current_datetime}\n"
        )
        return self._generate_structured(
            model=self.model_pro,
            input=prompt,
            system_instruction=sp_initial,
            schema=InitialScreeningResult,
            temperature=0,
        )

    def _validate_l1_turn(self, interaction: Any) -> InterviewTurnResponse:
        text = (getattr(interaction, "output_text", None) or "").strip()
        if not text:
            raise LLMResponseError("Empty response from Gemini")
        try:
            return InterviewTurnResponse.model_validate_json(text)
        except Exception as e:
            raise LLMResponseError(
                f"Gemini response did not match the InterviewTurnResponse schema: {e}"
            ) from e

    def live_l1(
        self,
        resume_data: dict,
        jd_data: dict,
        difficulty_level: str = DEFAULT_DIFFICULTY_LEVEL,
    ) -> Tuple[str, InterviewTurnResponse]:
        """Start a new multi-turn L1 interview interaction.

        Returns `(interaction_id, turn)`, where `turn` is a validated
        `InterviewTurnResponse` — `turn.public_response` is the only
        candidate-facing text; `turn.interview_state` / `turn.final_report`
        carry internal audit data and must never be shown to the candidate.
        Pass `interaction_id` to `continue_l1` to keep the conversation
        going — the Interactions API stores history server-side, so no
        local chat object is needed.

        `difficulty_level` is the HR-configured assessment floor
        (`Beginner` / `Intermediate` / `Senior`) injected into the bootstrap
        user context so the model can calibrate question complexity.
        """
        sp_live = self._require_prompt("l1_v3")
        current_datetime = datetime.now().strftime("%d-%m-%Y")
        calibrated_difficulty = normalize_difficulty_level(difficulty_level)
        initial_prompt = (
            "Resume Data:\n"
            f"{json.dumps(resume_data, indent=2)}\n\n"
            "---\n\n"
            "Job Description:\n"
            f"{json.dumps(jd_data, indent=2)}\n\n"
            "---\n\n"
            f"Current Date: {current_datetime}\n\n"
            "---\n\n"
            f"Difficulty Level: {calibrated_difficulty}\n"
        )
        try:
            interaction = create_interaction(
                client=self._client,
                max_retries=self.max_retries,
                timeout_seconds=self.timeout_seconds,
                model=self.model_pro,
                input=initial_prompt,
                system_instruction=sp_live,
                tools=_GOOGLE_SEARCH_TOOL,
                response_format=_response_format(InterviewTurnResponse),
                generation_config=self._generation_config(0.2),
                store=True,
            )
            turn = self._validate_l1_turn(interaction)
            logger.info(
                "L1 interaction started (id=%s, difficulty=%s)",
                interaction.id,
                calibrated_difficulty,
            )
            return interaction.id, turn
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"live_l1 failed: {e}") from e

    def continue_l1(
        self, previous_interaction_id: str, message: str
    ) -> Tuple[str, InterviewTurnResponse]:
        """Send the candidate's next message in an existing L1 interview.

        `system_instruction`, `tools`, and `response_format` are
        interaction-scoped, not carried over via `previous_interaction_id`,
        so all three are re-sent every turn. Returns `(new_interaction_id,
        turn)` — store the new id to continue the conversation on the next
        turn. See `live_l1` for the shape of `turn`.
        """
        sp_live = self._require_prompt("l1_v3")
        try:
            interaction = create_interaction(
                client=self._client,
                max_retries=self.max_retries,
                timeout_seconds=self.timeout_seconds,
                model=self.model_pro,
                input=message,
                previous_interaction_id=previous_interaction_id,
                system_instruction=sp_live,
                tools=_GOOGLE_SEARCH_TOOL,
                response_format=_response_format(InterviewTurnResponse),
                generation_config=self._generation_config(0.2),
                store=True,
            )
            return interaction.id, self._validate_l1_turn(interaction)
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"continue_l1 failed: {e}") from e

    def complete_screening(
        self, complete_resume: str, complete_jd: str, section_weightages: dict
    ) -> dict:
        sp_complete = self._require_prompt("complete")
        resume = json.loads(complete_resume) if isinstance(complete_resume, str) else complete_resume
        jd = json.loads(complete_jd) if isinstance(complete_jd, str) else complete_jd
        current_datetime = datetime.now().strftime("%d-%m-%Y")
        prompt = (
            "Resume Data (JSON):\n"
            f"{json.dumps(resume, indent=2)}\n\n"
            "---\n\n"
            "Job Description (JSON):\n"
            f"{json.dumps(jd, indent=2)}\n\n"
            "---\n\n"
            f"Section Weightages:\n{section_weightages}\n\n"
            "---\n\n"
            f"Current Date: {current_datetime}\n"
        )
        return self._generate_structured(
            model=self.model_pro,
            input=prompt,
            system_instruction=sp_complete,
            schema=CompleteScreeningResult,
        )

    def job_description_generator(self, job_title: str) -> dict:
        sp_jd = self._require_prompt("job_description_prompt")
        safe_title = str(job_title or "").strip()[:200]
        safe_title = safe_title.replace("{", "").replace("}", "")
        try:
            contents = sp_jd.format(job_name=safe_title)
        except (KeyError, ValueError) as e:
            raise LLMConfigError(
                "job_description_prompt must contain a single {job_name} placeholder"
            ) from e

        # Response keys are human-readable titles (e.g. "Job Description") to
        # match the existing frontend contract, so alias output is required.
        return self._generate_structured(
            model=self.model_flash,
            input=contents,
            system_instruction=contents,
            schema=JobDescriptionGenerator,
            by_alias=True,
            temperature=0.0,
        )

    def job_keywords_generator(self, payload: dict) -> dict:
        sp_keywords = self._require_prompt("job_keywords")
        manual = payload.get("manual_keywords") or []
        if isinstance(manual, str):
            manual = [k.strip() for k in manual.split(",") if k.strip()]
        manual_text = ", ".join(str(k) for k in manual if str(k).strip())
        contents = (
            f"Job Title: {payload.get('job_title', '')}\n"
            f"Job Description: {payload.get('job_description', '')}\n"
            f"Roles and Responsibilities: {payload.get('roles_responsibilities', '')}\n"
            f"Desired Skills: {payload.get('desired_skills', '')}\n"
            f"Experience Required: {payload.get('experience_required', '')}\n"
            f"Manual Keywords: {manual_text}\n"
        )
        return self._generate_structured(
            model=self.model_flash,
            input=contents,
            system_instruction=sp_keywords,
            schema=JobKeywordsResult,
            temperature=0.0,
        )

    def experience_years_parser(self, raw_text: str) -> dict:
        sp_years = self._require_prompt("experience_years")
        contents = str(raw_text or "").strip()
        return self._generate_structured(
            model=self.model_flash,
            input=contents,
            system_instruction=sp_years,
            schema=ExperienceYearsResult,
            temperature=0.0,
        )

    def generate_l2_questions(
        self,
        resume_json: dict,
        jd_json: dict,
        resume_score: Any,
        resume_reason: Any,
        l1_report: dict,
    ) -> dict:
        sp_l2 = self._require_prompt("L2")
        try:
            prompt = sp_l2.format(
                resume_json=json.dumps(resume_json, ensure_ascii=False),
                jd_json=json.dumps(jd_json, ensure_ascii=False),
                resume_score=resume_score,
                resume_reason=json.dumps(resume_reason, ensure_ascii=False),
                l1_report=json.dumps(l1_report, ensure_ascii=False),
            )
        except (KeyError, ValueError) as e:
            raise LLMConfigError("L2 prompt has invalid format placeholders") from e

        last_error: Optional[BaseException] = None
        for attempt in range(self.max_retries):
            try:
                result = self._generate_structured(
                    model=self.model_flash,
                    input=prompt,
                    system_instruction=prompt,
                    schema=L2QuestionsResult,
                    temperature=0.1,
                )
                questions = result.get("question")
                if isinstance(questions, list) and len(questions) > 0:
                    return result
                raise LLMResponseError("L2 questions list empty or missing")
            except Exception as e:
                last_error = e
                if attempt >= self.max_retries - 1:
                    break
                logger.warning("generate_l2_questions attempt %s failed: %s", attempt + 1, e)
        raise LLMResponseError(
            f"generate_l2_questions failed after {self.max_retries} attempts"
        ) from last_error

    def generate_interview_email(self, data: dict) -> str:
        sp_email = self._require_prompt("interview_email")
        safe = sanitize_interview_email_fields(data)
        interviewer_names = ", ".join(
            i.get("name", "") for i in safe.get("interviewers", []) if i.get("name")
        )
        # Keep untrusted fields in the user payload, not in the system prompt
        contents = (
            "<<<CANDIDATE_DATA>>>\n"
            f"Company: {safe['Company']}\n"
            f"Candidate Name: {safe['candidate_name']}\n"
            f"Position: {safe['position']}\n"
            f"Interview Level: {safe['interview_level']}\n"
            f"Date: {safe['date']}\n"
            f"Time: {safe['time']}\n"
            f"Duration: {safe['duration_minutes']} minutes\n"
            f"Timezone: {safe['timezone']}\n"
            f"Interviewers: {interviewer_names}\n"
            f"Meeting Link: {safe['meeting_link']}\n"
            "<<<END_CANDIDATE_DATA>>>\n"
        )
        return self._generate_text(
            model=self.model_flash,
            input=contents,
            system_instruction=sp_email,
            temperature=0.7,
        )

    # ---- Async wrappers (non-blocking for FastAPI event loop) ----

    async def aresume_parser(self, resume_text: str) -> dict:
        return await asyncio.to_thread(self.resume_parser, resume_text)

    async def ajd_parser(self, jd_text: str) -> str:
        return await asyncio.to_thread(self.jd_parser, jd_text)

    async def ainitial_screening(
        self, lite_resume_data: dict, lite_jd_data: dict
    ) -> dict:
        return await asyncio.to_thread(
            self.initial_screening, lite_resume_data, lite_jd_data
        )

    async def acomplete_screening(
        self, complete_resume: str, complete_jd: str, section_weightages: dict
    ) -> dict:
        return await asyncio.to_thread(
            self.complete_screening, complete_resume, complete_jd, section_weightages
        )

    async def ajob_description_generator(self, job_title: str) -> dict:
        return await asyncio.to_thread(self.job_description_generator, job_title)

    async def agenerate_l2_questions(self, *args, **kwargs) -> dict:
        return await asyncio.to_thread(self.generate_l2_questions, *args, **kwargs)

    async def agenerate_interview_email(self, data: dict) -> str:
        return await asyncio.to_thread(self.generate_interview_email, data)

    def __repr__(self) -> str:
        return (
            f"GeminiAPI(model_flash={self.model_flash!r}, model_pro={self.model_pro!r}, "
            f"thinking_budget={self.thinking_budget}, timeout={self.timeout_seconds})"
        )
