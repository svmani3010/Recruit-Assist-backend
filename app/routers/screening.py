"""Screening scores, L1 chat, L2 questions, status updates."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.auth import assert_candidate_access, get_current_principal, require_hr
from app.chat_sessions import chat_sessions
from app.database import get_db
from app.helpers import get_job_role_helper, resolve_candidate_contact, sanitize_l1_message
from app.rate_limit import limiter
from app.schemas import (
    SaveAnswersRequest,
    SaveL2QuestionsRequest,
    SendMessageRequest,
    UpdateL1StatusRequest,
)
from app.services.application_policy import _job_id_variants
from app.services.screening import (
    assert_l0_passed_for_interview,
    continue_l1_turn,
    ensure_current_l1_question,
    fetch_candidate_data,
    get_gemini,
    resolve_l1_session,
    run_final_screening_and_store,
    run_in_thread,
    serialize_l0_result,
    transcript_from_screening_doc,
)
from gemini_schemas import InterviewTurnResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["screening"])

# Keep for display/docs only — L1 must NEVER auto pass/fail from this threshold.
# Round 2 advancement requires HR Approve on View Score.
L1_PASS_THRESHOLD = 5


def _qa_from_l1_history(history: list, first_question: str = "") -> tuple[list[str], list[str]]:
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


def _find_screening_doc(candidate_id: str, job_id: str) -> Optional[dict]:
    job_ids = _job_id_variants(job_id)
    return get_db()["screeningquestions"].find_one(
        {"candidate_id": candidate_id, "job_id": {"$in": job_ids}},
        {"_id": 0},
    )


def _upsert_screening_doc(candidate_id: str, job_id: str, fields: dict[str, Any]) -> None:
    db = get_db()
    job_ids = _job_id_variants(job_id)
    payload = {
        **fields,
        "candidate_id": candidate_id,
        "job_id": str(job_id),
    }
    existing = db["screeningquestions"].find_one(
        {"candidate_id": candidate_id, "job_id": {"$in": job_ids}},
        {"_id": 1},
    )
    if existing:
        db["screeningquestions"].update_one({"_id": existing["_id"]}, {"$set": payload})
    else:
        db["screeningquestions"].insert_one(payload)


def _persist_l1_transcript(
    candidate_id: str, job_id: str, extra: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Write live Q&A (and optional report) so HR can read it before/after L1 ends."""
    key = f"{candidate_id}_{job_id}"
    session = chat_sessions.get(key) if key in chat_sessions else None
    if not isinstance(session, dict):
        session = chat_sessions.load_transcript(key) or {}
    history = session.get("history") or []
    first_question = session.get("first_question") or ""
    questions, answers = _qa_from_l1_history(history, first_question)
    fields: dict[str, Any] = {}
    if questions:
        fields["questions"] = questions
        fields["answers"] = answers
    if extra:
        fields.update(extra)
    if fields:
        _upsert_screening_doc(candidate_id, job_id, fields)
    return fields


def _finalize_l1_status(candidate_id: str, job_id: str, overall_l1_score) -> str:
    """Record that L1 finished and is awaiting HR review.

    Auto pass/fail belongs to L0 only. After L1, every candidate must be
    manually approved or rejected by HR before Round 2. The AI score (if any)
    is stored for the View Score page but does not change pipeline status.
    """
    status = "L1 Completed - Pending Review"
    update_fields: dict[str, Any] = {"status": status}
    if isinstance(overall_l1_score, (int, float)):
        update_fields["overall_l1_score"] = float(overall_l1_score)

    get_db()["Scores"].update_one(
        {"candidate_id": candidate_id, "job_id": job_id},
        {"$set": update_fields},
    )
    try:
        from app.services.application_policy import (
            finalize_application_to_applied,
            get_latest_active_application,
        )

        app_doc = get_latest_active_application(get_db(), candidate_id, job_id)
        if app_doc:
            finalize_application_to_applied(
                get_db(),
                app_doc,
                reason="interview_completed",
                notify_hr=True,
            )
    except Exception as exc:
        logger.warning("Failed to finalize application after L1: %s", exc)
    _notify_l1_pipeline_outcome(candidate_id, job_id, status, overall_l1_score)
    return status


def _notify_l1_pipeline_outcome(
    candidate_id: str,
    job_id: str,
    status: str,
    overall_l1_score: Any = None,
) -> None:
    """Notify HR when L1 finishes (pending review) or when HR rejects."""
    try:
        from email_utils import notify_candidate, notify_hr

        db = get_db()
        info = db["candidateInformation"].find_one({"candidate_id": candidate_id}) or {}
        contact = resolve_candidate_contact(candidate_id, info, job_id)
        candidate_name = (
            f"{info.get('first_name', '')} {info.get('last_name', '')}"
        ).strip() or "Candidate"
        job_role = get_job_role_helper(job_id, candidate_id)
        payload = {
            "candidate_id": candidate_id,
            "job_id": job_id,
            "candidate_name": candidate_name,
            "job_role": job_role,
            "stage": "L1",
            "overall_l1_score": overall_l1_score,
        }
        status_key = str(status or "").strip().lower()
        if status_key in {
            "l1 completed - pending review",
            "pending review",
            "l1 completed",
        }:
            # HR must manually approve — do not treat score as a pass.
            notify_hr("l1_interview_complete", payload)
        elif status_key in {"l1 rejected", "rejected"}:
            email = contact.get("email")
            if email:
                notify_candidate("candidate_rejected", payload, email)
        elif status_key in {"approved", "l1 passed"}:
            # Manual HR approval path (e.g. after scheduling Round 2).
            notify_hr("l1_passed", payload)
    except Exception as exc:
        logger.warning("L1 outcome email failed: %s", exc)


@router.get("/api/get_initial_screening_result/{candidate_id}")
async def get_initial_screening_result(
    candidate_id: str,
    job_id: Optional[str] = Query(None),
    principal=Depends(get_current_principal),
):
    assert_candidate_access(principal, candidate_id, job_id)
    db = get_db()
    score_query: dict[str, Any] = {"candidate_id": candidate_id}
    if job_id:
        score_query["job_id"] = {"$in": _job_id_variants(job_id)}
    score_doc = db["Scores"].find_one(score_query)
    if score_doc:
        return serialize_l0_result(score_doc)
    resume_query: dict[str, Any] = {"candidate_id": candidate_id}
    if job_id:
        resume_query["job_id"] = {"$in": _job_id_variants(job_id)}
    resume_doc = db["ResumeV2"].find_one(resume_query)
    if resume_doc:
        return {
            "status": "pending",
            "score": None,
            "passed": None,
            "l0_passed": None,
            "ready": False,
            "job_id": resume_doc.get("job_id"),
            "message": "Initial screening is not available yet.",
        }
    raise HTTPException(status_code=404, detail="Candidate resume not found")


@router.post("/api/run_final_screening")
async def run_final_screening_endpoint(
    candidate_id: str,
    job_id: str,
    background_tasks: BackgroundTasks,
    principal=Depends(require_hr),
):
    assert_candidate_access(principal, candidate_id, job_id)
    background_tasks.add_task(run_final_screening_and_store, job_id, candidate_id)
    return {"status": "final screening started"}


def _numeric_score(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%").strip())
        except ValueError:
            return 0.0
    return 0.0


@router.get("/api/get_final_score")
async def get_final_score(
    candidate_id: str, job_id: str, principal=Depends(require_hr)
):
    assert_candidate_access(principal, candidate_id, job_id)
    doc = get_db()["Scores"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id}
    )
    if not doc:
        return {}
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    # Rejected L0 candidates never get final screening, so llm_score stays 0
    # even though compatibility already holds the L0 score. View Score reads
    # llm_score for the Resume Score card — surface the L0 value there.
    if _numeric_score(doc.get("llm_score")) <= 0:
        l0_score = _numeric_score(doc.get("compatibility"))
        if l0_score > 0:
            doc["llm_score"] = l0_score
    return doc


@router.get("/api/candidate_scores/{job_id}")
async def get_candidates_scores_for_job(
    job_id: str, principal=Depends(require_hr)
):
    assert_candidate_access(principal, "", job_id)
    scores = list(get_db()["Scores"].find({"job_id": job_id}))
    return [
        {
            "candidate_id": score.get("candidate_id", ""),
            "llm_score": score.get("llm_score", 0),
            "compatibility": score.get("compatibility", 0),
            "status": score.get("status", "Pending"),
        }
        for score in scores
    ]


@router.get("/api/candidate_scores_with_info/{job_id}")
async def get_candidates_scores_with_info(
    job_id: str, principal=Depends(require_hr)
):
    assert_candidate_access(principal, "", job_id)
    db = get_db()
    scores = list(db["Scores"].find({"job_id": job_id}))
    candidates = []
    for score in scores:
        candidate_id = score.get("candidate_id")
        info = db["candidateInformation"].find_one({"candidate_id": candidate_id}) or {}
        full_name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
        candidates.append(
            {
                "candidate_id": candidate_id,
                "llm_score": score.get("llm_score", 0),
                "compatibility": score.get("compatibility", 0),
                "name": full_name if full_name else "N/A",
                "email": info.get("email", "N/A"),
                "l0_reasons": score.get("l0_reasons", []),
                "status": score.get("status", "Pending"),
            }
        )
    return candidates


@router.get("/api/generate_questions")
@limiter.limit("20/minute")
async def generate_questions(
    request: Request,
    candidate_id: str = Query(...),
    job_id: str = Query(...),
    principal=Depends(get_current_principal),
):
    assert_candidate_access(principal, candidate_id, job_id)
    assert_l0_passed_for_interview(candidate_id, job_id)
    
    # Check if technical interview is enabled for this job
    db = get_db()
    job = db["jobview"].find_one({"jobid": str(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    technical_enabled = job.get("technicalInterviewEnabled", False)
    if not technical_enabled:
        raise HTTPException(
            status_code=403,
            detail="Technical interview is not enabled for this job"
        )
    
    try:
        from app.services.application_policy import mark_interview_started

        try:
            mark_interview_started(get_db(), candidate_id, job_id)
        except Exception:
            logger.warning(
                "mark_interview_started failed for candidate=%s job=%s",
                candidate_id,
                job_id,
                exc_info=True,
            )

        key = f"{candidate_id}_{job_id}"
        session = await run_in_thread(resolve_l1_session, candidate_id, job_id)
        session, current_question = await run_in_thread(
            ensure_current_l1_question, session, key
        )
        question_text = current_question or session.get("first_question") or ""
        # `interview_state` is internal audit data persisted on the session by
        # the service layer (see `build_l1_chat_session` / `continue_l1_turn`
        # in app/services/screening.py) — never leaked beyond its `turn`
        # counter here.
        turn_number = (session.get("interview_state") or {}).get("turn")
        screening_doc = _find_screening_doc(candidate_id, job_id)
        saved_transcript = transcript_from_screening_doc(screening_doc)
        return {
            "question": question_text,
            "is_complete": False,
            "turn": turn_number,
            # Legacy fields kept for the current FE client (FRONT-END/src/pages/JobApply.jsx).
            "questions": [question_text],
            "transcript": saved_transcript,
            "resumed": bool(saved_transcript),
        }
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(
            status_code=500, detail=f"Generating questions failed: {str(ex)}"
        )


@router.post("/api/send_message")
@limiter.limit("20/minute")
async def send_message(
    request: Request,
    payload: SendMessageRequest,
    principal=Depends(get_current_principal),
):
    assert_candidate_access(principal, payload.candidate_id, payload.job_id)
    assert_l0_passed_for_interview(payload.candidate_id, payload.job_id)
    
    # Check if technical interview is enabled for this job
    db = get_db()
    job = db["jobview"].find_one({"jobid": str(payload.job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    technical_enabled = job.get("technicalInterviewEnabled", False)
    if not technical_enabled:
        raise HTTPException(
            status_code=403,
            detail="Technical interview is not enabled for this job"
        )
    
    key = f"{payload.candidate_id}_{payload.job_id}"
    session = await run_in_thread(resolve_l1_session, payload.candidate_id, payload.job_id)

    def _public_text(turn: InterviewTurnResponse) -> str:
        # Defensive re-sanitize: `public_response` is schema-constrained to
        # hold only candidate-facing text (see prompts/l1_v3.md, Note 2),
        # but this strips any stray <interview_state> block if the model
        # doesn't fully honor that. Internal scoring/rubrics must never
        # reach the response below.
        return sanitize_l1_message(turn.public_response or "") or ""

    def _exchange(message: str) -> InterviewTurnResponse:
        nonlocal session
        prior_history = list(session.get("history") or [])
        session, turn = continue_l1_turn(session, message)
        session["history"] = prior_history
        chat_sessions.set(key, session)
        return turn

    try:
        chat_sessions.append_history(key, "user", payload.message)
        try:
            turn = await run_in_thread(_exchange, payload.message)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Sending message failed: {e}")

        question = _public_text(turn)
        if not question and not turn.is_interview_complete:
            turn = await run_in_thread(
                _exchange,
                "Continue the interview: respond with a non-empty public_response "
                "containing only the next interview question.",
            )
            question = _public_text(turn)
        if not question:
            raise HTTPException(
                status_code=500, detail="Could not generate a visible next question"
            )

        # Only the sanitized candidate-facing text ever enters chat history —
        # `interview_state` / `final_report` are audit-only and persisted
        # separately below, never into the transcript the candidate sees.
        chat_sessions.append_history(key, "model", question)
        _persist_l1_transcript(
            payload.candidate_id,
            payload.job_id,
            extra={"interview_state": turn.interview_state.model_dump()},
        )

        turn_number = turn.interview_state.turn

        if turn.is_interview_complete:
            report = turn.final_report.model_dump() if turn.final_report else {}

            _persist_l1_transcript(
                payload.candidate_id,
                payload.job_id,
                extra={
                    "interview_report": report,
                    "interview_state": turn.interview_state.model_dump(),
                },
            )
            chat_sessions.delete(key)

            _finalize_l1_status(
                payload.candidate_id,
                payload.job_id,
                report.get("overall_l1_score"),
            )

            return {
                "question": question,
                "is_complete": True,
                "turn": turn_number,
                # Legacy fields kept for the current FE client (FRONT-END/src/pages/JobApply.jsx).
                "ai_message": question,
                "completed": True,
                "report": report,
            }

        return {
            "question": question,
            "is_complete": False,
            "turn": turn_number,
            # Legacy fields kept for the current FE client (FRONT-END/src/pages/JobApply.jsx).
            "ai_message": question,
            "completed": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sending message failed: {e}")


@router.post("/api/save_screening_answers")
async def save_screening_answers(
    payload: SaveAnswersRequest,
    background_tasks: BackgroundTasks = None,
    principal=Depends(get_current_principal),
):
    assert_candidate_access(principal, payload.candidate_id, payload.job_id)
    
    # Check if technical interview is enabled for this job
    db = get_db()
    job = db["jobview"].find_one({"jobid": str(payload.job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    technical_enabled = job.get("technicalInterviewEnabled", False)
    if not technical_enabled:
        raise HTTPException(
            status_code=403,
            detail="Technical interview is not enabled for this job"
        )
    
    if len(payload.questions) != len(payload.answers):
        raise HTTPException(
            status_code=400, detail="Questions and answers length mismatch"
        )
    _upsert_screening_doc(
        payload.candidate_id,
        payload.job_id,
        {
            "questions": payload.questions,
            "answers": payload.answers,
        },
    )

    # Mid-interview saves only persist Q&A. Finalizing here used to mark the
    # application Applied after the first answer and break L1.
    if payload.finalize:
        existing_report = get_db()["screeningquestions"].find_one(
            {
                "candidate_id": payload.candidate_id,
                "job_id": {"$in": _job_id_variants(payload.job_id)},
            },
            {"interview_report.overall_l1_score": 1},
        ) or {}
        existing_score = (existing_report.get("interview_report") or {}).get(
            "overall_l1_score"
        )
        if existing_score is None:
            _finalize_l1_status(payload.candidate_id, payload.job_id, None)

    return {"status": "success", "message": "Answers saved"}


@router.get("/api/screening_questions")
async def get_screening_questions(
    candidate_id: str, job_id: str, principal=Depends(require_hr)
):
    assert_candidate_access(principal, candidate_id, job_id)
    doc = _find_screening_doc(candidate_id, job_id)
    if not doc or not (
        doc.get("interview_report")
        or (isinstance(doc.get("questions"), list) and doc.get("questions"))
    ):
        live = _persist_l1_transcript(candidate_id, job_id)
        if live:
            doc = _find_screening_doc(candidate_id, job_id)
    if not doc:
        return {"candidate_id": candidate_id, "job_id": job_id}
    return doc


@router.post("/api/update_l1_status")
async def update_l1_status(
    payload: UpdateL1StatusRequest, principal=Depends(require_hr)
):
    assert_candidate_access(principal, payload.candidate_id, payload.job_id)

    db = get_db()
    candidate_id = payload.candidate_id
    job_id = payload.job_id
    status = payload.status

    main_reviews_list = []
    main_reviews_emails = []
    m_reviews = payload.main_reviews
    if m_reviews:
        if isinstance(m_reviews, str):
            main_reviews_emails.append(m_reviews.strip())
            main_reviews_list.append({"name": "Reviewer", "email": m_reviews.strip()})
        elif isinstance(m_reviews, list):
            for item in m_reviews:
                if isinstance(item, str):
                    main_reviews_emails.append(item.strip())
                    main_reviews_list.append(
                        {"name": "Reviewer", "email": item.strip()}
                    )
                elif hasattr(item, "email") or (
                    isinstance(item, dict) and "email" in item
                ):
                    email = (
                        item.email if hasattr(item, "email") else item.get("email")
                    )
                    name = (
                        item.name
                        if hasattr(item, "name")
                        else item.get("name", "Reviewer")
                    )
                    if email:
                        main_reviews_emails.append(email.strip())
                        main_reviews_list.append(
                            {"name": name, "email": email.strip()}
                        )

    update_fields: dict = {"status": status}
    if status.lower() == "approved":
        update_fields["l1_approved_at"] = datetime.now(timezone.utc)

    db["candidateInformation"].update_one(
        {"candidate_id": candidate_id},
        {"$set": {"main_reviews": main_reviews_list}},
    )
    db["Scores"].update_one(
        {"candidate_id": candidate_id, "job_id": job_id},
        {"$set": update_fields},
    )

    if status.lower() == "rejected":
        _notify_l1_pipeline_outcome(candidate_id, job_id, "rejected")
    return {"status": "ok"}


def _normalize_l2_questions(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    questions: list[str] = []
    for item in raw:
        text = str(item).strip() if item is not None else ""
        if text:
            questions.append(text)
    return questions


def _saved_l2_questions(candidate_id: str, job_id: str) -> list[str]:
    score_doc = get_db()["Scores"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id},
        {"l2_questions": 1},
    )
    return _normalize_l2_questions((score_doc or {}).get("l2_questions"))


def _persist_l2_questions(candidate_id: str, job_id: str, questions: list[str]) -> None:
    get_db()["Scores"].update_one(
        {"candidate_id": candidate_id, "job_id": job_id},
        {
            "$set": {
                "l2_questions": questions,
                "l2_questions_generated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        upsert=True,
    )


def _l2_questions_payload(questions: list[str], generated: bool) -> dict:
    return {"question": questions, "generated": generated}


async def _generate_and_save_l2_questions(candidate_id: str, job_id: str) -> list[str]:
    (
        resume_json,
        jd_json,
        resume_score,
        resume_reason,
        l1_report,
        _score_doc,
    ) = fetch_candidate_data(candidate_id, job_id)
    if not resume_json or not jd_json:
        raise HTTPException(status_code=404, detail="Required resume or job data not found")
    try:
        result = await run_in_thread(
            get_gemini().generate_l2_questions,
            resume_json,
            jd_json,
            resume_score,
            resume_reason,
            l1_report or {},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    questions = _normalize_l2_questions(result.get("question", []))
    if not questions:
        raise HTTPException(status_code=500, detail="L2 questions list empty or missing")
    _persist_l2_questions(candidate_id, job_id, questions)
    return questions


@router.get("/api/l2-questions/{candidate_id}/{job_id}")
async def get_saved_l2_questions(
    candidate_id: str, job_id: str, principal=Depends(require_hr)
):
    """Return saved L2 questions without generating new ones."""
    assert_candidate_access(principal, candidate_id, job_id)
    questions = _saved_l2_questions(candidate_id, job_id)
    return _l2_questions_payload(questions, generated=bool(questions))


@router.post("/api/l2-questions/{candidate_id}/{job_id}")
@limiter.limit("10/minute")
async def generate_l2_questions_endpoint(
    request: Request,
    candidate_id: str,
    job_id: str,
    principal=Depends(require_hr),
):
    """Generate L2 questions on first click and reuse the saved set afterwards."""
    assert_candidate_access(principal, candidate_id, job_id)
    existing = _saved_l2_questions(candidate_id, job_id)
    if existing:
        return _l2_questions_payload(existing, generated=True)
    questions = await _generate_and_save_l2_questions(candidate_id, job_id)
    return _l2_questions_payload(questions, generated=True)


@router.put("/api/l2-questions")
async def save_l2_questions(
    payload: SaveL2QuestionsRequest, principal=Depends(require_hr)
):
    """Persist an L2 question list for later retrieval."""
    assert_candidate_access(principal, payload.candidate_id, payload.job_id)
    questions = _normalize_l2_questions(payload.questions)
    if not questions:
        raise HTTPException(status_code=400, detail="At least one L2 question is required")
    _persist_l2_questions(payload.candidate_id, payload.job_id, questions)
    return _l2_questions_payload(questions, generated=True)


@router.get("/l2-questions/{candidate_id}/{job_id}")
async def get_l2_questions(
    candidate_id: str, job_id: str, principal=Depends(require_hr)
):
    """Legacy generate-or-cache endpoint used by older clients."""
    assert_candidate_access(principal, candidate_id, job_id)
    existing = _saved_l2_questions(candidate_id, job_id)
    if existing:
        return JSONResponse(_l2_questions_payload(existing, generated=True), status_code=200)
    try:
        questions = await _generate_and_save_l2_questions(candidate_id, job_id)
        return JSONResponse(_l2_questions_payload(questions, generated=True), status_code=200)
    except HTTPException as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)