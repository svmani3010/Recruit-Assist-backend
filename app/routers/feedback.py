"""Notes and feedback endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import assert_candidate_access, require_hr
from app.database import get_db
from app.schemas import DraftL2FeedbackRequest, NoteRequest, SubmitFeedbackRequest

router = APIRouter(tags=["feedback"])


@router.post("/api/add_note")
async def add_note(payload: NoteRequest, principal=Depends(require_hr)):
    assert_candidate_access(principal, payload.candidate_id, payload.job_id)
    try:
        note_doc = {
            "candidate_id": payload.candidate_id,
            "candidate_name": payload.candidate_name,
            "job_id": payload.job_id,
            "note": payload.note,
            "level": payload.level,
            "author": payload.author,
            "timestamp": datetime.now().strftime("%m/%d/%Y, %I:%M:%S %p"),
        }
        result = get_db()["Notes"].insert_one(note_doc)
        return {"status": "success", "note_id": str(result.inserted_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/get_notes/{candidate_id}")
async def get_notes(
    candidate_id: str,
    job_id: str,
    principal=Depends(require_hr),
):
    assert_candidate_access(principal, candidate_id, job_id)
    try:
        notes = list(
            get_db()["Notes"]
            .find({"candidate_id": candidate_id, "job_id": job_id})
            .sort("_id", -1)
        )
        for note in notes:
            note["_id"] = str(note["_id"])
        return notes
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/save_draft_feedback")
async def save_draft_feedback(
    payload: DraftL2FeedbackRequest, principal=Depends(require_hr)
):
    assert_candidate_access(principal, payload.candidate_id, payload.job_id)
    try:
        get_db()["DraftFeedback"].update_one(
            {"candidate_id": payload.candidate_id, "job_id": payload.job_id},
            {
                "$set": {
                    "feedback": payload.feedback,
                    "updated_at": datetime.now().strftime("%m/%d/%Y, %I:%M:%S %p"),
                }
            },
            upsert=True,
        )
        return {"status": "success", "message": "Draft feedback saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/get_draft_feedback/{candidate_id}/{job_id}")
async def get_draft_feedback(
    candidate_id: str, job_id: str, principal=Depends(require_hr)
):
    assert_candidate_access(principal, candidate_id, job_id)
    try:
        draft = get_db()["DraftFeedback"].find_one(
            {"candidate_id": candidate_id, "job_id": job_id}
        )
        return {"feedback": draft.get("feedback", "") if draft else ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/submit_feedback")
async def submit_feedback(
    payload: SubmitFeedbackRequest, principal=Depends(require_hr)
):
    assert_candidate_access(principal, payload.candidate_id, payload.job_id)
    try:
        db = get_db()
        db["SubmittedFeedback"].insert_one(
            {
                "candidate_id": payload.candidate_id,
                "job_id": payload.job_id,
                "feedback": payload.feedback,
                "author": payload.author,
                "level": payload.level,
                "submitted_at": datetime.now().strftime("%m/%d/%Y, %I:%M:%S %p"),
            }
        )
        db["DraftFeedback"].delete_one(
            {"candidate_id": payload.candidate_id, "job_id": payload.job_id}
        )
        return {"status": "success", "message": "Feedback submitted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/get_submitted_feedbacks/{candidate_id}/{job_id}")
async def get_submitted_feedbacks(
    candidate_id: str, job_id: str, principal=Depends(require_hr)
):
    assert_candidate_access(principal, candidate_id, job_id)
    try:
        feedbacks = list(
            get_db()["SubmittedFeedback"]
            .find({"candidate_id": candidate_id, "job_id": job_id}, {"_id": 0})
            .sort("submitted_at", -1)
        )
        return {"feedbacks": feedbacks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
