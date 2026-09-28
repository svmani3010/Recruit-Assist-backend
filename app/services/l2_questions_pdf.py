"""Build a confidential L2 question PDF for HR and interviewers only."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

CONFIDENTIAL_BANNER = (
    "CONFIDENTIAL — HR and interviewer reference only. "
    "Do not share this file or these questions with the candidate."
)


def normalize_l2_questions(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    questions: List[str] = []
    for item in raw:
        text = str(item).strip() if item is not None else ""
        if text:
            questions.append(text)
    return questions


def get_saved_l2_questions(candidate_id: str, job_id: str) -> List[str]:
    if not candidate_id or not job_id:
        return []
    from app.database import get_db

    score_doc = get_db()["Scores"].find_one(
        {"candidate_id": candidate_id, "job_id": job_id},
        {"l2_questions": 1},
    )
    return normalize_l2_questions((score_doc or {}).get("l2_questions"))


def _pdf_safe(text: str) -> str:
    return (text or "").encode("latin-1", "replace").decode("latin-1")


def _content_width(pdf) -> float:
    return max(1.0, pdf.w - pdf.l_margin - pdf.r_margin)


def _write_block(pdf, h: float, text: str) -> None:
    """Write a wrapping block and return the cursor to the left margin.

    fpdf2's multi_cell(0, ...) leaves x at the right edge (new_x=RIGHT).
    The next multi_cell(0, ...) then has no remaining width and raises
    "Not enough horizontal space to render a single character".
    """
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(_content_width(pdf), h, _pdf_safe(text or ""))
    pdf.set_x(pdf.l_margin)


def build_l2_questions_pdf(
    *,
    candidate_name: str,
    job_role: str,
    questions: List[str],
) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    _write_block(pdf, 8, "L2 Interview Question Reference")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(180, 40, 40)
    _write_block(pdf, 6, CONFIDENTIAL_BANNER)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 11)
    _write_block(pdf, 6, f"Candidate: {candidate_name or 'Candidate'}")
    _write_block(pdf, 6, f"Role: {job_role or 'this role'}")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 11)
    for index, question in enumerate(questions, start=1):
        pdf.set_font("Helvetica", "B", 11)
        _write_block(pdf, 6, f"Q{index}")
        pdf.set_font("Helvetica", "", 11)
        _write_block(pdf, 6, question)
        pdf.ln(2)
    return bytes(pdf.output())


def l2_questions_pdf_attachment(
    candidate_id: str,
    job_id: str,
    *,
    candidate_name: str = "Candidate",
    job_role: str = "this role",
) -> Optional[Dict[str, Any]]:
    questions = get_saved_l2_questions(candidate_id, job_id)
    if not questions:
        return None
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate_name or "candidate").strip("_")
    return {
        "filename": f"L2_Questions_{safe_name or 'candidate'}_CONFIDENTIAL.pdf",
        "content": build_l2_questions_pdf(
            candidate_name=candidate_name,
            job_role=job_role,
            questions=questions,
        ),
        "mime_type": "application/pdf",
    }


def l2_pdf_recipients(
    *,
    candidate_email: Optional[str],
    hr_calendar_email: Optional[str] = None,
    interviewers: Optional[List[dict]] = None,
) -> List[str]:
    """HR always; interviewers when added. Never the candidate."""
    from app.settings import get_settings

    candidate = (candidate_email or "").strip().lower()
    emails: List[str] = []
    for raw in get_settings().hr_email_list or []:
        email = (raw or "").strip().lower()
        if email:
            emails.append(email)
    hr_cal = (hr_calendar_email or "").strip().lower()
    if hr_cal:
        emails.append(hr_cal)
    for interviewer in interviewers or []:
        if isinstance(interviewer, str):
            email = interviewer.strip().lower()
        else:
            email = str((interviewer or {}).get("email") or "").strip().lower()
        if email:
            emails.append(email)
    unique: List[str] = []
    seen = set()
    for email in emails:
        if not email or email == candidate or email in seen:
            continue
        seen.add(email)
        unique.append(email)
    return unique
