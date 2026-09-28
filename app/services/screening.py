"""Screening pipeline services (background-safe, null-guarded)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import HTTPException

from app.chat_sessions import chat_sessions
from app.database import get_db
from app.helpers import (
    build_fallback_first_question,
    extract_jd_fields,
    extract_resume_fields,
    fetch_jd_json,
    get_job_role_helper,
    prune_jd_fields,
    prune_resume_fields_all_except_exclusions,
    resolve_job_difficulty_level,
    sanitize_l1_message,
)
from app.llm import GeminiAPI, ScreeningLLM
from app.schemas import DEFAULT_DIFFICULTY_LEVEL, normalize_difficulty_level
from app.settings import get_settings
from gemini_schemas import InterviewTurnResponse

logger = logging.getLogger(__name__)

# Matches L0_PASS_THRESHOLD in JobApply.jsx and Applicantions_view.jsx -
# keep these in sync. Score at/above this is an L0 pass and is the only
# case that triggers the heavier final-screening LLM call.
INITIAL_SCREENING_FINAL_PASS_THRESHOLD = 50
L0_IN_PROGRESS_STATUSES = {"processing", "pending"}
L0_FAILED_STATUSES = {"failed"}
# Pipeline statuses that mean L0 itself is finished (candidate may proceed
# based on passed/l0_passed). Final screening overwrites status after L0.
L0_DONE_PIPELINE_STATUSES = {
    "completed",
    "final_completed",
    "final_failed",
    "hr_final_completed",
}


def _l0_status_key(value: Any) -> str:
    return str(value or "").strip().lower()


def l0_passed_from_score_doc(score_doc: dict) -> bool:
    if score_doc.get("l0_passed") is not None:
        return bool(score_doc.get("l0_passed"))
    try:
        return float(score_doc.get("compatibility") or 0) >= INITIAL_SCREENING_FINAL_PASS_THRESHOLD
    except (TypeError, ValueError):
        return False


def serialize_l0_result(score_doc: dict) -> dict:
    """Build the candidate-facing L0 payload.

    A Scores row is created as status=processing with compatibility=0. That
    placeholder must not be treated as a real fail until screening finishes.

    After an L0 pass, chained final screening may overwrite status to
    final_completed / final_failed. Candidates still need a stable
    terminal status of "completed" so the apply UI can open the L1 gate.
    """
    status = score_doc.get("status") or "completed"
    status_key = _l0_status_key(status)
    job_id = score_doc.get("job_id")
    if status_key in L0_IN_PROGRESS_STATUSES:
        return {
            "status": status,
            "score": None,
            "passed": None,
            "l0_passed": None,
            "ready": False,
            "job_id": job_id,
            "message": "Initial screening is still in progress.",
        }
    if status_key in L0_FAILED_STATUSES:
        return {
            "status": "failed",
            "score": 0,
            "passed": False,
            "l0_passed": False,
            "ready": True,
            "job_id": job_id,
        }
    # Treat any non-in-progress / non-failed row as L0-ready (including
    # final_* pipeline statuses written after a successful L0 pass).
    passed = l0_passed_from_score_doc(score_doc)
    return {
        "status": "completed",
        "score": score_doc.get("compatibility", 0),
        "passed": passed,
        "l0_passed": passed,
        "ready": True,
        "job_id": job_id,
        "pipeline_status": status if status_key not in {"completed"} else None,
    }


def assert_l0_passed_for_interview(candidate_id: str, job_id: str) -> None:
    from app.services.application_policy import _job_id_variants

    score_doc = get_db()["Scores"].find_one(
        {"candidate_id": candidate_id, "job_id": {"$in": _job_id_variants(job_id)}}
    )
    if not score_doc:
        raise HTTPException(
            status_code=409,
            detail="Initial screening has not finished yet.",
        )
    result = serialize_l0_result(score_doc)
    if not result.get("ready"):
        raise HTTPException(
            status_code=409,
            detail="Initial screening is still in progress.",
        )
    if not result.get("l0_passed"):
        raise HTTPException(
            status_code=403,
            detail="Candidate did not pass initial screening.",
        )

_gemini: Optional[GeminiAPI] = None


def get_gemini() -> ScreeningLLM:
    """Process-wide singleton Gemini client (implements ScreeningLLM protocol)."""
    global _gemini
    if _gemini is None:
        settings = get_settings()
        client = GeminiAPI(
            api_key=settings.gemini_api_key,
            prompt_dir=settings.prompt_dir,
            model_flash=settings.gemini_model_flash,
            model_pro=settings.gemini_model_pro,
            thinking_budget=settings.gemini_thinking_budget,
            max_retries=settings.gemini_max_retries,
            timeout_seconds=settings.gemini_timeout_seconds,
        )
        client.prompt_manager.auto_reload = settings.prompt_auto_reload
        _gemini = client
    return _gemini


async def run_in_thread(func, *args, **kwargs):
    """Run blocking Gemini / IO work off the event loop with a concurrency cap."""
    settings = get_settings()
    sem = _get_llm_semaphore(settings.gemini_max_concurrency)
    async with sem:
        return await asyncio.to_thread(func, *args, **kwargs)


_llm_semaphore: Optional[asyncio.Semaphore] = None
_llm_semaphore_size: Optional[int] = None


def _get_llm_semaphore(size: int) -> asyncio.Semaphore:
    global _llm_semaphore, _llm_semaphore_size
    size = max(1, int(size))
    if _llm_semaphore is None or _llm_semaphore_size != size:
        _llm_semaphore = asyncio.Semaphore(size)
        _llm_semaphore_size = size
    return _llm_semaphore


def run_initial_screening_and_store(jobid: str, candidate_id: str) -> None:
    db = get_db()
    try:
        jd_json = fetch_jd_json(jobid)
        resume_doc = db["ResumeV2"].find_one(
            {"job_id": jobid, "candidate_id": candidate_id}
        )
        if not resume_doc or not resume_doc.get("resume_json"):
            raise ValueError(
                f"Resume JSON missing for candidate={candidate_id} job={jobid}"
            )
        resume_json = resume_doc["resume_json"]

        lite_jd_data = prune_jd_fields(jd_json)
        lite_resume_data = prune_resume_fields_all_except_exclusions(resume_json)
        screening_json = get_gemini().initial_screening(lite_resume_data, lite_jd_data)
        raw_score = screening_json.get("compatibility", 0)

        # BUGFIX: the old parser only accepted a string ending in "%" (e.g.
        # "72%") and silently fell back to 0.0 for anything else - including
        # a plain int/float, a string without the "%" suffix, or stray
        # whitespace like "72 %". Since that fallback never raised, a score
        # of 0 looked identical to a legitimately low-scoring candidate,
        # which is a very plausible cause of "every candidate gets
        # rejected." This version accepts int/float directly and is
        # tolerant of the string forms Gemini might return, only truly
        # defaulting to 0.0 (and logging loudly) if the value is genuinely
        # unparseable.
        numeric_score = 0.0
        try:
            if isinstance(raw_score, (int, float)):
                numeric_score = float(raw_score)
            elif isinstance(raw_score, str):
                cleaned = raw_score.strip().rstrip("%").strip()
                numeric_score = float(cleaned)
            else:
                raise TypeError(f"Unsupported compatibility type: {type(raw_score)}")
        except (TypeError, ValueError):
            logger.warning(
                "Could not parse compatibility score for candidate=%s job=%s "
                "raw_score=%r (type=%s) - defaulting to 0.0. This candidate "
                "will look like a legitimate 0 score to HR unless this is fixed.",
                candidate_id, jobid, raw_score, type(raw_score).__name__,
            )
            numeric_score = 0.0
        l0_reasons = (
            screening_json.get("l0_reasons")
            or screening_json.get("l0reasons")
            or []
        )
        l0_passed = numeric_score >= INITIAL_SCREENING_FINAL_PASS_THRESHOLD

        # Persist the L0 result for every candidate, including rejects.
        # Do not zero llm_score / score_breakdown on a fail: those fields
        # belong to final screening, which we only run on an L0 pass.
        # View Score should read compatibility + l0_reasons for rejects.
        score_fields = {
            "candidate_id": candidate_id,
            "job_id": jobid,
            "compatibility": numeric_score,
            "l0_reasons": l0_reasons,
            "l0_passed": l0_passed,
            "status": "completed",
        }
        if l0_passed:
            score_fields.update(
                {
                    "llm_score": 0,
                    "llm_compatibility": 0,
                    "score_breakdown": {},
                    "reasons": [],
                }
            )

        db["Scores"].update_one(
            {"candidate_id": candidate_id, "job_id": jobid},
            {"$set": score_fields},
            upsert=True,
        )
        try:
            from app.credits.hooks import on_application_submitted

            on_application_submitted(candidate_id, jobid, numeric_score)
        except Exception:
            logger.exception("Credit award on application screening failed")

        try:
            if numeric_score < INITIAL_SCREENING_FINAL_PASS_THRESHOLD:
                from email_utils import notify_candidate
                from app.helpers import resolve_candidate_contact

                candidate_info = (
                    db["candidateInformation"].find_one({"candidate_id": candidate_id})
                    or {}
                )
                candidate_name = (
                    f"{candidate_info.get('first_name', '')} "
                    f"{candidate_info.get('last_name', '')}"
                ).strip()
                if not candidate_name:
                    personal = (resume_json.get("personal_information") or {})
                    candidate_name = personal.get("full_name") or "Unknown"
                job_role = get_job_role_helper(jobid, candidate_id)
                contact = resolve_candidate_contact(candidate_id, candidate_info, jobid)
                email = contact.get("email")
                try:
                    from app.services.candidate_comms import notify_candidate_milestone

                    notify_candidate_milestone(
                        event="screening_failed",
                        candidate_id=candidate_id,
                        job_id=jobid,
                        data={
                            "candidate_name": candidate_name,
                            "job_role": job_role,
                            "stage": "L0",
                            "candidate_email": email or "",
                        },
                        force_email_action="candidate_rejected",
                    )
                except Exception:
                    if email:
                        notify_candidate(
                            "candidate_rejected",
                            {
                                "candidate_id": candidate_id,
                                "job_id": jobid,
                                "candidate_name": candidate_name,
                                "job_role": job_role,
                                "stage": "L0",
                            },
                            email,
                        )
        except Exception as email_exc:
            logger.warning("Email notification failed: %s", email_exc)

        # Final screening is a second, heavier LLM call (full resume + JD +
        # section weights). Only spend that on L0 passers. Rejects already
        # have compatibility + l0_reasons from this L0 call.
        if l0_passed:
            try:
                run_final_screening_and_store(jobid, candidate_id)
            except Exception as final_exc:
                logger.warning("Final screening failed: %s", final_exc)
            try:
                from app.services.candidate_comms import notify_candidate_milestone
                from app.helpers import resolve_candidate_contact

                candidate_info = (
                    db["candidateInformation"].find_one({"candidate_id": candidate_id})
                    or {}
                )
                contact = resolve_candidate_contact(candidate_id, candidate_info, jobid)
                notify_candidate_milestone(
                    event="screening_passed",
                    candidate_id=candidate_id,
                    job_id=jobid,
                    data={
                        "candidate_name": (
                            f"{candidate_info.get('first_name', '')} "
                            f"{candidate_info.get('last_name', '')}"
                        ).strip()
                        or "there",
                        "job_role": get_job_role_helper(jobid, candidate_id),
                        "candidate_email": contact.get("email") or "",
                        "status": "Screening passed",
                    },
                )
            except Exception as pass_exc:
                logger.warning("Screening passed notify failed: %s", pass_exc)

    except Exception as exc:
        logger.exception("Initial screening failed for %s: %s", candidate_id, exc)
        db["Scores"].update_one(
            {"candidate_id": candidate_id, "job_id": jobid},
            {
                "$set": {
                    "candidate_id": candidate_id,
                    "job_id": jobid,
                    "compatibility": 0,
                    "l0_passed": False,
                    "status": "failed",
                    "error": str(exc),
                }
            },
            upsert=True,
        )


def run_final_screening_and_store(jobid: str, candidate_id: str) -> None:
    db = get_db()
    try:
        resume_doc = db["ResumeV2"].find_one(
            {"candidate_id": candidate_id, "job_id": jobid}
        )
        jd_doc = db["JobDetailsV2"].find_one({"job_id": jobid})
        job_doc = db["jobview"].find_one({"jobid": jobid})
        
        if not resume_doc or not resume_doc.get("resume_json"):
            logger.error("Resume missing for final screening %s/%s", candidate_id, jobid)
            return
        if not jd_doc or not jd_doc.get("jd_json"):
            logger.error("JD missing for final screening %s", jobid)
            return

        complete_resume = json.dumps(resume_doc["resume_json"])
        complete_jd = json.dumps(jd_doc["jd_json"])
        section_weightages = jd_doc.get("sectionWeightage", {}) or {}
        
        # Check if technical interview is enabled for this job
        technical_enabled = False
        if job_doc:
            technical_enabled = job_doc.get("technicalInterviewEnabled", False)
        
        # If technical interview is disabled, remove technical from weightage
        if not technical_enabled and "technical" in section_weightages:
            section_weightages = {k: v for k, v in section_weightages.items() if k != "technical"}

        result = get_gemini().complete_screening(
            complete_resume, complete_jd, section_weightages
        )
        existing = (
            db["Scores"].find_one({"candidate_id": candidate_id, "job_id": jobid})
            or {}
        )
        db["Scores"].update_one(
            {"candidate_id": candidate_id, "job_id": jobid},
            {
                "$set": {
                    "llm_score": result.get("llm_score", 0),
                    "llm_compatibility": result.get("llm_compatibility", ""),
                    "score_breakdown": result.get("score_breakdown", {}),
                    "reasons": result.get("reasons", []),
                    "compatibility": existing.get("compatibility", 0.0),
                    "l0_reasons": existing.get("l0_reasons", []),
                    "l0_passed": existing.get("l0_passed", True),
                    "status": "final_completed",
                    "technical_interview_enabled": technical_enabled,
                }
            },
            upsert=True,
        )
    except Exception as exc:
        logger.exception("Final screening failed for %s: %s", candidate_id, exc)
        db["Scores"].update_one(
            {"candidate_id": candidate_id, "job_id": jobid},
            {"$set": {"status": "final_failed", "error": str(exc)}},
            upsert=True,
        )


def finalscreening_hr(jobid: str, candidate_id: str) -> None:
    db = get_db()
    try:
        resume_doc = db["ResumeV2"].find_one(
            {"candidate_id": candidate_id, "job_id": jobid}
        )
        jd_doc = db["JobDetailsV2"].find_one({"job_id": jobid})
        job_doc = db["jobview"].find_one({"jobid": jobid})
        
        if not resume_doc or not resume_doc.get("resume_json") or not jd_doc:
            logger.error("Resume or JD not found for HR screening %s/%s", candidate_id, jobid)
            return
        
        section_weightages = jd_doc.get("sectionWeightage", {}) or {}
        
        # Check if technical interview is enabled for this job
        technical_enabled = False
        if job_doc:
            technical_enabled = job_doc.get("technicalInterviewEnabled", False)
        
        # If technical interview is disabled, remove technical from weightage
        if not technical_enabled and "technical" in section_weightages:
            section_weightages = {k: v for k, v in section_weightages.items() if k != "technical"}
        
        result = get_gemini().complete_screening(
            json.dumps(resume_doc["resume_json"]),
            json.dumps(jd_doc.get("jd_json", {})),
            section_weightages,
        )
        db["Scores"].update_one(
            {"candidate_id": candidate_id, "job_id": jobid},
            {
                "$set": {
                    "llm_score": result.get("llm_score", 0),
                    "reasons": result.get("reasons", []),
                    "score_breakdown": result.get("score_breakdown", {}),
                    "status": "hr_final_completed",
                    "technical_interview_enabled": technical_enabled,
                }
            },
            upsert=True,
        )
    except Exception as exc:
        logger.exception("HR final screening failed for %s: %s", candidate_id, exc)


def _l1_public_text(turn: InterviewTurnResponse) -> str:
    """Candidate-facing text for a structured L1 turn.

    `turn.public_response` is schema-constrained to hold only the
    greeting/question/closing message (see `prompts/l1_v3.md`, Instruction 5
    and Note 2), so no XML stripping is normally needed. `sanitize_l1_message`
    is still run defensively in case the model doesn't fully honor the
    schema and leaks an `<interview_state>` block into `public_response`.
    """
    return sanitize_l1_message(turn.public_response or "") or ""


def _fallback_interview_state() -> dict:
    """Default `interview_state` shape to persist when Gemini never returned
    a valid structured turn and `build_fallback_first_question` was used
    instead (see `build_l1_chat_session`)."""
    return {
        "turn": 1,
        "last_question_score": None,
        "question_history": [],
        "skills_assessed": [],
        "next_target_skill": None,
        "strike_count": 0,
    }


def get_visible_question_from_chat(
    interaction_id: str, turn: InterviewTurnResponse
) -> tuple[str, str, InterviewTurnResponse]:
    """Extract the model's opening question from a structured turn.

    Returns `(interaction_id, question, turn)`. Empty `public_response` is
    left for the caller to handle (``build_l1_chat_session`` may retry
    ``live_l1`` with the same primary ``l1_v3`` prompt, then fall back to a
    scripted first question). There is no alternate-prompt retry path.
    """
    return interaction_id, _l1_public_text(turn), turn


def build_history_from_qa(questions: list, answers: list) -> list[dict]:
    """Rebuild L1 chat history from persisted screening Q&A pairs."""
    history: list[dict] = []
    for index, raw_question in enumerate(questions or []):
        question = (raw_question or "").strip()
        if not question:
            continue
        history.append({"role": "model", "text": question})
        if index < len(answers or []):
            answer = (answers[index] or "").strip()
            if answer:
                history.append({"role": "user", "text": answer})
    return history


def transcript_from_screening_doc(doc: Optional[dict]) -> list[dict[str, str]]:
    if not doc:
        return []
    questions = doc.get("questions") or []
    answers = doc.get("answers") or []
    return [
        {"question": q, "answer": a if a is not None else ""}
        for q, a in zip(questions, answers)
        if (q or "").strip()
    ]


def current_l1_question_from_session(session: dict) -> str:
    """Return the unanswered model question, if any."""
    history = list(session.get("history") or [])
    first_question = (session.get("first_question") or "").strip()
    pending = first_question
    for turn in history:
        role = (turn.get("role") or "").strip().lower()
        text = (turn.get("text") or "").strip()
        if not text or text.startswith("<<INTERVIEW_COMPLETE>>"):
            continue
        if role == "model":
            pending = text
        elif role == "user":
            pending = ""
    return pending


def _load_resume_jd_context(candidate_id: str, job_id: str) -> tuple[dict, dict]:
    db = get_db()
    resume_doc = db["ResumeV2"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id}
    )
    if not resume_doc:
        raise HTTPException(status_code=404, detail="Candidate resume not found")
    jd_doc = db["JobDetailsV2"].find_one({"job_id": job_id})
    if not jd_doc:
        raise HTTPException(status_code=404, detail="Job description not found")
    resume_data = extract_resume_fields(resume_doc.get("resume_json", {}) or {})
    jd_data = extract_jd_fields(jd_doc.get("jd_json", {}) or {})
    return resume_data, jd_data


def ensure_current_l1_question(session: dict, key: str) -> tuple[dict, str]:
    """Resolve the question the candidate should see when resuming L1."""
    current = current_l1_question_from_session(session)
    if current:
        return session, current

    history = list(session.get("history") or [])
    if history and history[-1].get("role") == "user":
        session, turn = continue_l1_turn(
            session,
            "Continue the interview and ask your next question only.",
        )
        text = _l1_public_text(turn)
        if text:
            history = list(session.get("history") or [])
            history.append({"role": "model", "text": text})
            session["history"] = history
            chat_sessions.set(key, session)
            return session, text

    return session, (session.get("first_question") or "").strip()


def build_l1_chat_session(candidate_id: str, job_id: str) -> dict:
    db = get_db()
    resume_doc = db["ResumeV2"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id}
    )
    if not resume_doc:
        raise HTTPException(status_code=404, detail="Candidate resume not found")
    jd_doc = db["JobDetailsV2"].find_one({"job_id": job_id})
    if not jd_doc:
        raise HTTPException(status_code=404, detail="Job description not found")

    resume_data = extract_resume_fields(resume_doc.get("resume_json", {}) or {})
    jd_data = extract_jd_fields(jd_doc.get("jd_json", {}) or {})
    difficulty_level = normalize_difficulty_level(
        jd_doc.get("difficulty_level") or resolve_job_difficulty_level(job_id)
    )
    interaction_id = None
    first_question = ""
    turn: Optional[InterviewTurnResponse] = None
    for _ in range(2):
        interaction_id, initial_turn = get_gemini().live_l1(
            resume_data, jd_data, difficulty_level=difficulty_level
        )
        interaction_id, first_question, turn = get_visible_question_from_chat(
            interaction_id, initial_turn
        )
        if first_question:
            break
    interview_state = (
        turn.interview_state.model_dump()
        if turn is not None
        else _fallback_interview_state()
    )
    if not first_question:
        first_question = build_fallback_first_question(jd_data, resume_data)
    return {
        "last_interaction_id": interaction_id,
        "first_question": first_question,
        "resume_data": resume_data,
        "jd_data": jd_data,
        "difficulty_level": difficulty_level,
        "history": [
            {"role": "model", "text": first_question},
        ],
        "interview_state": interview_state,
        "final_report": None,
    }


def rebuild_l1_chat_session(transcript: dict) -> dict:
    """
    Resume an L1 interview on another worker from a Redis transcript.

    The Interactions API keeps full conversation history server-side under
    `last_interaction_id`, so resuming is just reusing that id — no local
    replay needed. Only a legacy transcript with no persisted interaction id
    (e.g. from before this field existed) falls back to starting a fresh
    interaction and replaying prior user turns to reconstruct context.
    """
    resume_data = transcript.get("resume_data") or {}
    jd_data = transcript.get("jd_data") or {}
    history = list(transcript.get("history") or [])
    interaction_id = transcript.get("last_interaction_id")
    difficulty_level = normalize_difficulty_level(
        transcript.get("difficulty_level") or DEFAULT_DIFFICULTY_LEVEL
    )

    if interaction_id:
        return {
            "last_interaction_id": interaction_id,
            "first_question": transcript.get("first_question", ""),
            "resume_data": resume_data,
            "jd_data": jd_data,
            "difficulty_level": difficulty_level,
            "history": history,
            "interview_state": transcript.get("interview_state"),
            "final_report": transcript.get("final_report"),
        }

    interaction_id, initial_turn = get_gemini().live_l1(
        resume_data, jd_data, difficulty_level=difficulty_level
    )
    first_question = _l1_public_text(initial_turn) or transcript.get(
        "first_question", ""
    )
    interview_state = initial_turn.interview_state.model_dump()

    # Skip replaying the first model turn if it matches bootstrap output
    replay = history[:]
    if replay and replay[0].get("role") == "model":
        replay = replay[1:]

    for replay_turn in replay:
        text = replay_turn.get("text") or ""
        if not text:
            continue
        if replay_turn.get("role") == "user":
            interaction_id, continued_turn = get_gemini().continue_l1(interaction_id, text)
            interview_state = continued_turn.interview_state.model_dump()
        # model turns are already produced as responses to user messages

    return {
        "last_interaction_id": interaction_id,
        "first_question": transcript.get("first_question") or first_question,
        "resume_data": resume_data,
        "jd_data": jd_data,
        "difficulty_level": difficulty_level,
        "history": history,
        "interview_state": interview_state,
        "final_report": transcript.get("final_report"),
    }


def resolve_l1_session(candidate_id: str, job_id: str) -> dict:
    """Local cache → Redis rebuild → Mongo Q&A rebuild → fresh session."""
    key = f"{candidate_id}_{job_id}"
    session = chat_sessions.get(key)
    if session and isinstance(session, dict) and session.get("last_interaction_id"):
        return session

    transcript = chat_sessions.load_transcript(key)
    if transcript and transcript.get("resume_data") is not None:
        try:
            session = rebuild_l1_chat_session(transcript)
            chat_sessions.set(key, session)
            return session
        except Exception as exc:
            logger.warning("Failed to rebuild L1 session from Redis: %s", exc)

    db = get_db()
    screening_doc = db["screeningquestions"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id},
        {"questions": 1, "answers": 1},
    )
    saved_questions = (screening_doc or {}).get("questions") or []
    if saved_questions:
        try:
            resume_data, jd_data = _load_resume_jd_context(candidate_id, job_id)
            answers = (screening_doc or {}).get("answers") or []
            rebuilt = {
                "first_question": (saved_questions[0] or "").strip(),
                "resume_data": resume_data,
                "jd_data": jd_data,
                "difficulty_level": resolve_job_difficulty_level(job_id),
                "history": build_history_from_qa(saved_questions, answers),
            }
            session = rebuild_l1_chat_session(rebuilt)
            chat_sessions.set(key, session)
            return session
        except Exception as exc:
            logger.warning(
                "Failed to rebuild L1 session from screening doc for %s: %s",
                key,
                exc,
            )

    session = build_l1_chat_session(candidate_id, job_id)
    chat_sessions.set(key, session)
    return session


def continue_l1_turn(
    session: dict, message: str
) -> tuple[dict, InterviewTurnResponse]:
    """Send one L1 candidate turn, rebuilding the interaction if the id is stale.

    Returns `(updated_session, turn)`, where `turn` is the full structured
    `InterviewTurnResponse` — callers must read `turn.public_response`
    (via `_l1_public_text`) for the candidate-facing text, and
    `turn.interview_state` / `turn.final_report` for internal audit data
    that must never be shown to the candidate. `updated_session` has
    `interview_state` (and `final_report`, once populated) written onto it
    so callers can persist that audit trail alongside the transcript.

    The Interactions API stores history server-side, so a valid
    `last_interaction_id` is enough to continue. If that id is missing or
    rejected, we start a fresh interaction and replay prior user turns
    before sending `message`.
    """
    gemini = get_gemini()
    interaction_id = session.get("last_interaction_id")
    try:
        if not interaction_id:
            raise RuntimeError("missing last_interaction_id")
        new_id, turn = gemini.continue_l1(interaction_id, message)
        session["last_interaction_id"] = new_id
        session["interview_state"] = turn.interview_state.model_dump()
        if turn.final_report is not None:
            session["final_report"] = turn.final_report.model_dump()
        return session, turn
    except Exception as exc:
        logger.warning(
            "continue_l1 failed (%s); rebuilding session from transcript", exc
        )
        history = list(session.get("history") or [])
        if (
            history
            and history[-1].get("role") == "user"
            and history[-1].get("text") == message
        ):
            history = history[:-1]
        transcript = {
            "resume_data": session.get("resume_data") or {},
            "jd_data": session.get("jd_data") or {},
            "first_question": session.get("first_question", ""),
            "difficulty_level": session.get("difficulty_level")
            or DEFAULT_DIFFICULTY_LEVEL,
            "history": history,
        }
        session = rebuild_l1_chat_session(transcript)
        new_id, turn = gemini.continue_l1(session["last_interaction_id"], message)
        session["last_interaction_id"] = new_id
        session["interview_state"] = turn.interview_state.model_dump()
        if turn.final_report is not None:
            session["final_report"] = turn.final_report.model_dump()
        return session, turn


def fetch_candidate_data(candidate_id: str, job_id: str):
    db = get_db()
    resume_doc = db["ResumeV2"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id}
    )
    jd_doc = db["JobDetailsV2"].find_one({"job_id": job_id})
    score_doc = db["Scores"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id}
    )
    l1_doc = (
        db["screeningquestions"].find_one(
            {"candidate_id": candidate_id, "job_id": job_id},
            {"_id": 0},
        )
        or {}
    )
    if not resume_doc or not jd_doc:
        return None, None, None, None, None, score_doc
    return (
        resume_doc.get("resume_json", {}),
        jd_doc.get("jd_json", {}),
        score_doc.get("llm_score", 0) if score_doc else 0,
        score_doc.get("reasons", []) if score_doc else [],
        l1_doc,
        score_doc,
    )


L1_STATUS_ABANDONED = "L1 Attempted - Incomplete"


def qa_from_l1_history(history: list, first_question: str = "") -> tuple[list[str], list[str]]:
    questions: list[str] = []
    answers: list[str] = []
    pending_question = (first_question or "").strip()
    for turn in history or []:
        role = (turn.get("role") or "").strip().lower()
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        if role == "model":
            if text.startswith("<<INTERVIEW_COMPLETE>>"):
                continue
            pending_question = text
        elif role == "user" and pending_question:
            questions.append(pending_question)
            answers.append(text)
            pending_question = ""
    return questions, answers


def _job_id_lookup_variants(job_id: Any) -> list[Any]:
    variants = {job_id, str(job_id)}
    try:
        variants.add(int(job_id))
    except (TypeError, ValueError):
        pass
    return list(variants)


def abandon_l1_interview(candidate_id: str, job_id: str) -> None:
    """Persist partial L1 work and close the session (one-shot attempt policy)."""
    key = f"{candidate_id}_{job_id}"
    session = chat_sessions.get(key)
    if not isinstance(session, dict):
        session = chat_sessions.load_transcript(key) or {}

    history = session.get("history") or []
    first_question = session.get("first_question") or ""
    questions, answers = qa_from_l1_history(history, first_question)

    db = get_db()
    job_ids = _job_id_lookup_variants(job_id)

    if questions:
        payload = {
            "questions": questions,
            "answers": answers,
            "candidate_id": candidate_id,
            "job_id": str(job_id),
        }
        existing = db["screeningquestions"].find_one(
            {"candidate_id": candidate_id, "job_id": {"$in": job_ids}},
            {"_id": 1},
        )
        if existing:
            db["screeningquestions"].update_one(
                {"_id": existing["_id"]}, {"$set": payload}
            )
        else:
            db["screeningquestions"].insert_one(payload)

    db["Scores"].update_one(
        {"candidate_id": candidate_id, "job_id": {"$in": job_ids}},
        {"$set": {"status": L1_STATUS_ABANDONED}},
    )
    chat_sessions.delete(key)