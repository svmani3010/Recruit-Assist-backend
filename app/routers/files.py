"""File streaming and cover letter endpoints."""

from __future__ import annotations

import logging
import os
from io import BytesIO

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.auth import assert_candidate_access, require_hr
from app.database import get_db, get_gridfs
from app.helpers import grid_file_to_preview_pdf

logger = logging.getLogger(__name__)
router = APIRouter(tags=["files"])


@router.get("/api/cover_letter_file/{file_id}")
async def cover_letter_file(file_id: str, principal=Depends(require_hr)):
    try:
        file_obj = get_gridfs().get(ObjectId(file_id))
        metadata = getattr(file_obj, "metadata", None) or {}
        job_id = metadata.get("job_id")
        if job_id:
            assert_candidate_access(principal, metadata.get("candidate_id", ""), str(job_id))
        filename = file_obj.filename or "cover_letter.pdf"
        headers = {"Content-Disposition": f'inline; filename="{filename}"'}
        return StreamingResponse(
            file_obj, media_type=file_obj.content_type, headers=headers
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")


@router.get("/api/resume_pdf/{file_id}")
async def get_resume_pdf(file_id: str, principal=Depends(require_hr)):
    try:
        oid = ObjectId(file_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid file ID format")
    file_obj = get_gridfs().find_one({"_id": oid})
    if not file_obj:
        raise HTTPException(status_code=404, detail="File not found")
    metadata = file_obj.metadata or {}
    job_id = metadata.get("job_id")
    if job_id:
        assert_candidate_access(
            principal, metadata.get("candidate_id", ""), str(job_id)
        )
    try:
        pdf_bytes = grid_file_to_preview_pdf(file_obj)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("resume_pdf conversion failed for %s", file_id)
        raise HTTPException(
            status_code=500,
            detail="Could not convert resume to PDF for preview.",
        ) from exc

    base_name = os.path.splitext(file_obj.filename or "resume")[0] or "resume"
    headers = {"Content-Disposition": f'inline; filename="{base_name}.pdf"'}
    return StreamingResponse(
        BytesIO(pdf_bytes), media_type="application/pdf", headers=headers
    )


@router.get("/api/resume_upload_date/{candidate_id}/{job_id}")
async def get_resume_upload_date(
    candidate_id: str, job_id: str, principal=Depends(require_hr)
):
    assert_candidate_access(principal, candidate_id, job_id)
    file_doc = get_db().fs.files.find_one(
        {"metadata.candidate_id": candidate_id, "metadata.job_id": job_id}
    )
    if not file_doc or "uploadDate" not in file_doc:
        raise HTTPException(status_code=404, detail="Resume file not found in GridFS")
    return {"uploadDate": file_doc["uploadDate"]}
