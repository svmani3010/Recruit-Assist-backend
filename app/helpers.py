"""Shared helpers: extractors, PDF conversion, ICS, job role lookup."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.database import get_db
from app.settings import get_settings

logger = logging.getLogger(__name__)

ALLOWED_UPLOAD_SUFFIXES = {".pdf", ".doc", ".docx"}
WORD_UPLOAD_SUFFIXES = {".doc", ".docx"}

# Matches FRONT-END/src/utils/jobPostingPayload.js — HR stores the optional
# application expiry inside the allow-listed joiningRequirement field.
APPLICATION_DEADLINE_PREFIX = "__APP_DEADLINE__:"


def decode_application_deadline(raw: Any) -> Optional[str]:
    """Return the local datetime string encoded in joiningRequirement, if any."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith(APPLICATION_DEADLINE_PREFIX):
        value = text[len(APPLICATION_DEADLINE_PREFIX) :].strip()
        return value or None
    # Accept already-decoded ISO-ish values for resilience.
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text):
        return text
    return None


def parse_application_deadline(raw: Any) -> Optional[datetime]:
    """Parse joiningRequirement / deadline encoding into a naive-local or aware UTC datetime.

    The FE encodes a *local* wall-clock datetime (no timezone). For comparison
    we treat an explicit offset as UTC if present; otherwise compare as local
    wall time against ``datetime.now()`` (same approach as the FE's
    ``new Date(y, m-1, d, h, mi)`` construction).
    """
    value = decode_application_deadline(raw)
    if not value:
        return None
    match = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?",
        value,
    )
    if match:
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5)),
                int(match.group(6) or 0),
            )
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed
    except ValueError:
        return None


def is_application_deadline_passed(raw: Any, now: Optional[datetime] = None) -> bool:
    deadline = parse_application_deadline(raw)
    if deadline is None:
        return False
    current = now or datetime.now()
    # Compare naive-to-naive / aware-to-aware. If only one side is aware,
    # drop tzinfo on the aware side so we still get a deterministic result.
    if deadline.tzinfo is not None and current.tzinfo is None:
        deadline = deadline.replace(tzinfo=None)
    elif deadline.tzinfo is None and current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    return current >= deadline


def is_job_expired_for_candidates(job_doc: Optional[dict]) -> bool:
    """True when a listing should no longer accept or continue applications.

    Covers: application deadline passed, listing paused (show=off), or closed.
    """
    if not job_doc:
        return True
    show = str(job_doc.get("show") or "").strip().lower()
    status = str(job_doc.get("status") or "").strip().lower()
    if status in {"draft", "closed"} or show in {"off", "closed"} or (
        show and show != "on"
    ):
        return True
    return is_application_deadline_passed(job_doc.get("joiningRequirement"))


def get_job_role_helper(job_id: str, candidate_id: Optional[str] = None) -> str:
    db = get_db()
    jd_doc = db["JobDetailsV2"].find_one({"job_id": job_id})
    if jd_doc:
        jd_json = jd_doc.get("jd_json", {}) or {}
        job_title = (
            jd_json.get("Job_information", {}).get("job_title")
            or jd_json.get("job_information", {}).get("job_title")
            or jd_json.get("job_title")
        )
        if job_title:
            return job_title

    if candidate_id:
        resume_doc = db["ResumeV2"].find_one(
            {"candidate_id": candidate_id, "job_id": job_id}
        )
        if resume_doc and resume_doc.get("job_role"):
            return resume_doc["job_role"]
    return "N/A"


def extract_personal_info(resume_json: dict) -> dict:
    personal_info = resume_json.get("personal_information", {}) or {}
    full_name = personal_info.get("full_name", "")
    first_name, last_name = "", ""
    if full_name:
        parts = full_name.split()
        first_name = parts[0]
        last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": personal_info.get("email", ""),
        # Resume phone is not authoritative — never treat as verified mobile
        "phone": "",
    }


def _usable_contact(value: Optional[str]) -> str:
    text = str(value or "").strip()
    if not text or text.upper() == "N/A":
        return ""
    return text


def resolve_candidate_contact(
    candidate_id: str,
    info_doc: Optional[dict] = None,
    job_id: Optional[str] = None,
    *,
    audience: str = "hr",
) -> dict:
    """Resolve email + mobile for API responses.

    Mobile always comes from QE / verified candidate record — never from
    resume or application form plaintext copies.

    audience \"hr\"|\"self\": international display with country code.
    audience \"masked\": last-4 mask only.
    """
    from app.mobile_phone import format_mobile_display, split_e164
    from app.services.candidate_mobile_identity import (
        get_masked_mobile,
        get_plaintext_mobile,
    )

    db = get_db()
    email = _usable_contact((info_doc or {}).get("email"))

    user = None
    try:
        user = db["candidates"].find_one({"_id": ObjectId(candidate_id)})
    except Exception:
        user = None

    if not email and user:
        email = _usable_contact(user.get("email"))

    if not email:
        resume_query: Dict[str, Any] = {"candidate_id": candidate_id}
        if job_id:
            resume_query["job_id"] = job_id
        resume = db["ResumeV2"].find_one(resume_query) or db["ResumeV2"].find_one(
            {"candidate_id": candidate_id}
        )
        personal = ((resume or {}).get("resume_json") or {}).get(
            "personal_information"
        ) or {}
        email = email or _usable_contact(personal.get("email"))

    e164 = ""
    try:
        e164 = get_plaintext_mobile(candidate_id) or ""
    except Exception:
        e164 = ""
    if not e164 and user:
        e164 = _usable_contact(user.get("phone"))

    region, dial, national = split_e164(e164) if e164 else ("", "", "")
    if user:
        region = str(user.get("phoneCountry") or region or "")
        dial = str(user.get("phoneDialCode") or dial or "")

    display = format_mobile_display(e164) if e164 else ""
    if audience == "masked":
        if e164:
            from app.mobile_phone import format_otp_masked_display

            phone_out = format_otp_masked_display(e164)
        else:
            phone_out = get_masked_mobile(candidate_id, user) if candidate_id else ""
    else:
        phone_out = display or e164

    return {
        "email": email,
        "phone": phone_out,
        "phoneE164": e164 if audience != "masked" else "",
        "phoneDisplay": display,
        "phoneCountry": region,
        "phoneDialCode": dial,
        "phoneNational": national,
    }


def extract_resume_fields(resume_json: dict) -> dict:
    extracted: Dict[str, Any] = {
        "work_experience": resume_json.get("work_experience", []),
        "skills": resume_json.get("skills", []),
        "certifications": [
            c.get("name")
            for c in resume_json.get("certifications", [])
            if isinstance(c, dict) and "name" in c
        ],
        "education": resume_json.get("education", []),
        "projects": [],
    }
    for item in resume_json.get("projects", []):
        if not isinstance(item, dict):
            continue
        extracted["projects"].append(
            {
                "description": item.get("description", ""),
                "technologies_used": item.get("technologies_used", []),
                "role": item.get("role", ""),
            }
        )
    if "work_experience" in resume_json:
        extracted["job_title"] = [
            we.get("job_title", "")
            for we in resume_json["work_experience"]
            if isinstance(we, dict)
        ]
        extracted["responsibilities"] = [
            we.get("responsibilities", [])
            for we in resume_json["work_experience"]
            if isinstance(we, dict)
        ]
        extracted["achievements"] = [
            we.get("achievements", [])
            for we in resume_json["work_experience"]
            if isinstance(we, dict)
        ]
    return extracted


def extract_jd_fields(jd_json: dict) -> dict:
    job_info = jd_json.get("Job_information") or jd_json.get("job_information") or {}
    return {
        "job_title": job_info.get("job_title", ""),
        "responsibilities": job_info.get("responsibilities", [])
        or jd_json.get("responsibilities", []),
        "requirements": jd_json.get("requirements", []),
        "preferred_qualifications": jd_json.get("preferred_qualifications", []),
    }


def prune_resume_fields_all_except_exclusions(resume_json: dict) -> dict:
    exclude = {"awards_and_honors", "publications", "languages", "referrals"}
    return {k: v for k, v in resume_json.items() if k not in exclude}


def prune_jd_fields(jd_json: dict) -> dict:
    requirements = jd_json.get("requirements", {}) or {}
    preferred = jd_json.get("preferred_qualifications", {}) or {}
    return {
        "responsibilities": jd_json.get("responsibilities", []),
        "requirements": {
            "education": requirements.get("education", []),
            "technical_skills": requirements.get("technical_skills", []),
            "soft_skills": requirements.get("soft_skills", []),
            "tools_and_technologies": requirements.get("tools_and_technologies", []),
            "certifications_required": requirements.get("certifications_required", []),
        },
        "preferred_qualifications": {
            "education": preferred.get("education", []),
            "technical_skills": preferred.get("technical_skills", []),
            "soft_skills": preferred.get("soft_skills", []),
            "tools_and_technologies": preferred.get("tools_and_technologies", []),
            "certifications_preferred": preferred.get("certifications_preferred", []),
        },
        "experience_required": jd_json.get("experience_required", {}),
    }


def fetch_jd_json(job_id: str) -> dict:
    db = get_db()
    jd_doc = db["JobDetailsV2"].find_one({"job_id": job_id})
    if not jd_doc:
        raise ValueError(f"Job description not found for job_id {job_id}")
    return jd_doc["jd_json"]


def resolve_job_difficulty_level(job_id: str) -> str:
    """Load the HR-configured L1 difficulty for a job (default Intermediate).

    Prefers ``JobDetailsV2.difficulty_level`` (authoritative for the interview
    pipeline), then falls back to ``jobview.difficulty_level`` for drafts /
    older rows that only stored the field on the listing document.
    """
    from app.schemas import normalize_difficulty_level

    db = get_db()
    jd_doc = db["JobDetailsV2"].find_one(
        {"job_id": job_id}, {"difficulty_level": 1}
    ) or {}
    if jd_doc.get("difficulty_level"):
        return normalize_difficulty_level(jd_doc.get("difficulty_level"))
    job_doc = db["jobview"].find_one(
        {"jobid": job_id}, {"difficulty_level": 1}
    ) or {}
    return normalize_difficulty_level(job_doc.get("difficulty_level"))


def validate_upload_suffix(filename: Optional[str]) -> str:
    suffix = os.path.splitext(filename or "")[-1].lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: {sorted(ALLOWED_UPLOAD_SUFFIXES)}",
        )
    return suffix


def requires_word_pdf_conversion(suffix: str) -> bool:
    return suffix in WORD_UPLOAD_SUFFIXES


def derived_pdf_path(source_path: str) -> str:
    base, _ = os.path.splitext(os.path.abspath(source_path))
    return base + ".pdf"


_LIBREOFFICE_MISSING_MSG = (
    "Could not process this Word document because LibreOffice is not available "
    "on the server. Please upload a PDF or DOCX, or set LIBREOFFICE_PATH."
)


def resolve_libreoffice_binary() -> Optional[str]:
    """Resolve LibreOffice executable from settings, PATH, or common install paths."""
    settings = get_settings()
    configured = (settings.libreoffice_path or "").strip()
    candidates: List[str] = []
    if configured:
        candidates.append(configured)
    if configured and configured.lower() not in {"soffice", "soffice.exe"}:
        # Keep configured path first; still try PATH names if it fails later.
        pass
    candidates.extend(["soffice", "soffice.exe"])

    for name in candidates:
        if os.path.isabs(name) and os.path.isfile(name):
            return name
        found = shutil.which(name)
        if found:
            return found

    # Windows / macOS common install locations when not on PATH.
    extra_paths = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
        "/usr/local/bin/soffice",
        "/snap/bin/libreoffice",
    ]
    for path in extra_paths:
        if os.path.isfile(path):
            return path
    return None


def convert_word_to_pdf_with_libreoffice(word_path: str, output_pdf_path: str) -> str:
    """Convert .doc or .docx to PDF using LibreOffice (argv list, no shell)."""
    suffix = os.path.splitext(word_path)[1].lower()
    if suffix not in WORD_UPLOAD_SUFFIXES:
        raise ValueError("Input must be a Word document (.doc or .docx)")
    outdir = os.path.dirname(os.path.abspath(output_pdf_path))
    abs_word = os.path.abspath(word_path)
    if not abs_word.startswith(os.path.abspath(outdir)) and not abs_word.startswith(
        os.path.abspath(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")))
    ):
        temp_roots = [
            os.path.abspath(os.environ.get("TEMP", "")),
            os.path.abspath(os.environ.get("TMP", "")),
            os.path.abspath("/tmp"),
        ]
        if not any(r and abs_word.startswith(r) for r in temp_roots):
            raise ValueError("Refusing to convert file outside temp directories")

    binary = resolve_libreoffice_binary()
    if not binary:
        raise FileNotFoundError(_LIBREOFFICE_MISSING_MSG)

    try:
        subprocess.run(
            [
                binary,
                "--headless",
                "--convert-to",
                "pdf",
                abs_word,
                "--outdir",
                outdir,
            ],
            check=True,
            shell=False,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(_LIBREOFFICE_MISSING_MSG) from exc
    return output_pdf_path


def convert_docx_to_pdf_with_libreoffice(docx_path: str, output_pdf_path: str) -> str:
    """Backward-compatible alias for Word → PDF conversion."""
    return convert_word_to_pdf_with_libreoffice(docx_path, output_pdf_path)


def extract_text_exact(pdf_path: str) -> str:
    import pdfplumber

    extracted_text: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(layout=True)
            if text:
                extracted_text.append(text)
            extracted_text.append(
                "\n" + ("-" * 40) + f" [Page {page_num}] " + ("-" * 40) + "\n"
            )
    return "\n".join(extracted_text)


def extract_text_from_docx(docx_path: str) -> str:
    """Extract plain text from a .docx without LibreOffice (python-docx)."""
    from docx import Document

    document = Document(docx_path)
    parts: List[str] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if text:
            parts.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [
                (cell.text or "").strip()
                for cell in row.cells
                if (cell.text or "").strip()
            ]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError("Could not extract text from the Word document.")
    return text


def resume_content_type_for_suffix(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get((suffix or "").lower(), "application/octet-stream")


def is_pdf_bytes(data: bytes) -> bool:
    return bool(data) and len(data) >= 4 and data[:4] == b"%PDF"


def resolve_resume_suffix(filename: str, metadata: Optional[dict] = None) -> str:
    suffix = os.path.splitext(filename or "")[-1].lower()
    if suffix in ALLOWED_UPLOAD_SUFFIXES:
        return suffix
    meta_suffix = (metadata or {}).get("suffix")
    if meta_suffix:
        return str(meta_suffix).lower()
    return suffix


def file_bytes_to_preview_pdf(
    raw: bytes, *, filename: str = "resume", suffix: str = ""
) -> bytes:
    """Return PDF bytes for in-browser preview (PDF passthrough; Word via LibreOffice)."""
    if is_pdf_bytes(raw):
        return raw

    resolved_suffix = (suffix or os.path.splitext(filename or "")[-1]).lower()
    if resolved_suffix not in WORD_UPLOAD_SUFFIXES:
        raise ValueError(
            f"Unsupported preview type '{resolved_suffix or 'unknown'}'. "
            f"Allowed: {sorted(ALLOWED_UPLOAD_SUFFIXES)}"
        )

    with tempfile.NamedTemporaryFile(delete=False, suffix=resolved_suffix) as tmp:
        tmp.write(raw)
        word_path = tmp.name

    pdf_path = derived_pdf_path(word_path)
    try:
        try:
            convert_word_to_pdf_with_libreoffice(word_path, pdf_path)
        except FileNotFoundError as exc:
            raise ValueError(str(exc) or _LIBREOFFICE_MISSING_MSG) from exc
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                "Could not convert this Word document to PDF for preview. "
                "Please upload a PDF or DOCX."
            ) from exc
        if not os.path.exists(pdf_path):
            raise ValueError("LibreOffice did not produce a PDF output")
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for path in (word_path, pdf_path):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def grid_file_to_preview_pdf(grid_file) -> bytes:
    """Read a GridFS resume object and return PDF bytes suitable for iframe preview."""
    raw = grid_file.read()
    metadata = dict(getattr(grid_file, "metadata", None) or {})
    filename = (
        getattr(grid_file, "filename", None)
        or metadata.get("original_filename")
        or "resume"
    )
    suffix = resolve_resume_suffix(filename, metadata)
    return file_bytes_to_preview_pdf(raw, filename=filename, suffix=suffix)


def extract_resume_text_for_upload(tmp_path: str, suffix: str) -> str:
    """Extract resume text for parsing. PDF direct; Word via LibreOffice or docx fallback."""
    suffix = (suffix or "").lower()
    if suffix == ".pdf":
        return extract_text_exact(tmp_path)

    if suffix not in WORD_UPLOAD_SUFFIXES:
        raise ValueError(f"Unsupported resume type: {suffix}")

    pdf_path = derived_pdf_path(tmp_path)
    try:
        convert_word_to_pdf_with_libreoffice(tmp_path, pdf_path)
        if not os.path.exists(pdf_path):
            raise FileNotFoundError("LibreOffice did not produce a PDF output")
        return extract_text_exact(pdf_path)
    except Exception as conv_err:
        if suffix == ".docx":
            # Job apply / profile uploads should still succeed when LibreOffice is missing.
            logger.info(
                "LibreOffice Word→PDF unavailable; extracting .docx text directly: %s",
                conv_err,
            )
            return extract_text_from_docx(tmp_path)
        raise ValueError(
            "Could not process this .doc file. Please upload a PDF or DOCX, "
            "or ensure LibreOffice is installed on the server."
        ) from conv_err
    finally:
        if pdf_path != tmp_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except OSError:
                pass


def insert_resume_mongodb_single_chunk(
    resume_data,
    candidate_id,
    job_id,
    job_role,
    file_id,
    filename,
    added_by=None,
):
    collection = get_db()["ResumeV2"]
    doc = {
        "chunk_name": f"Resume_{candidate_id}_full",
        "candidate_id": candidate_id,
        "job_id": job_id,
        "job_role": job_role,
        "resume_json": resume_data,
        "resume_file_id": str(file_id),
        "original_filename": filename,
    }
    if added_by:
        doc["added_by"] = added_by
    collection.insert_one(doc)


def generate_unique_jobid(max_attempts: int = 25) -> str:
    """Generate a collision-resistant job id (12 hex chars) with DB uniqueness check."""
    db = get_db()
    for _ in range(max_attempts):
        job_id = uuid.uuid4().hex[:12]
        if not db["jobview"].find_one({"jobid": job_id}):
            if not db["JobDetailsV2"].find_one({"job_id": job_id}):
                return job_id
    raise RuntimeError("Unable to allocate unique job id")


def _ics_escape(value: str) -> str:
    """RFC 5545 text escaping for SUMMARY/DESCRIPTION/LOCATION."""
    text = (value or "").replace("\\", "\\\\")
    text = text.replace(";", "\\;").replace(",", "\\,")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return text


def generate_ics_content(data: dict) -> Optional[str]:
    try:
        from app.timezone_utils import localize, normalize_tz_name

        start_dt_str = f"{data['date']} {data['time']}"
        start_dt = datetime.strptime(start_dt_str, "%Y-%m-%d %I:%M %p")
        tz_name = normalize_tz_name(data.get("timezone"))
        start_dt = localize(start_dt, tz_name)
        end_dt = start_dt + timedelta(minutes=data.get("duration_minutes", 60))
        ics_format = "%Y%m%dT%H%M%S"
        start_str = start_dt.strftime(ics_format)
        end_str = end_dt.strftime(ics_format)
        now_str = datetime.now().strftime(ics_format)
        uid = str(uuid.uuid4())

        summary = _ics_escape(
            f"Interview: {data.get('interview_level')} - "
            f"{data.get('candidate_name')} ({data.get('position')})"
        )
        description = _ics_escape(
            f"Professional Interview for {data.get('position')}\n"
            f"Meeting Link: {data.get('meeting_link')}\n"
            f"Level: {data.get('interview_level')}"
        )
        from app.services.calendar_identity import video_call_label

        location = _ics_escape(
            data.get("location")
            or data.get("meeting_link")
            or f"Virtual/{video_call_label(data)}"
        )
        organizer_email = get_settings().sender_email or "noreply@localhost"

        attendees = [
            f"ATTENDEE;RSVP=TRUE;CN={_ics_escape(data.get('candidate_name', ''))}:"
            f"mailto:{data.get('candidate_email')}"
        ]
        for interviewer in data.get("interviewers", []) or []:
            attendees.append(
                f"ATTENDEE;RSVP=TRUE;CN={_ics_escape(interviewer.get('name', ''))}:"
                f"mailto:{interviewer.get('email')}"
            )
        attendee_str = "\r\n".join(attendees)

        return (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Screening Assist//NONSGML v1.0//EN\r\n"
            "METHOD:REQUEST\r\n"
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\n"
            f"DTSTAMP:{now_str}\r\n"
            f"DTSTART;TZID={tz_name}:{start_str}\r\n"
            f"DTEND;TZID={tz_name}:{end_str}\r\n"
            f"SUMMARY:{summary}\r\n"
            f"DESCRIPTION:{description}\r\n"
            f"LOCATION:{location}\r\n"
            f"ORGANIZER;CN=Hiring Team:mailto:{organizer_email}\r\n"
            f"{attendee_str}\r\n"
            "STATUS:CONFIRMED\r\n"
            "SEQUENCE:0\r\n"
            "BEGIN:VALARM\r\n"
            "TRIGGER:-PT15M\r\n"
            "ACTION:DISPLAY\r\n"
            "DESCRIPTION:Reminder\r\n"
            "END:VALARM\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR"
        )
    except Exception as exc:
        logger.error("Error generating ICS: %s", exc)
        return None


def sanitize_l1_message(message: str) -> str:
    if not message:
        return message
    cleaned = re.sub(
        r"<interview_state>.*?</interview_state>\s*",
        "",
        message,
        flags=re.DOTALL,
    ).strip()
    if cleaned.startswith("<interview_state>"):
        cleaned = ""
    return cleaned


def build_fallback_first_question(jd_data: dict, resume_data: dict) -> str:
    technical_skills = (
        jd_data.get("requirements", {}).get("technical_skills", [])
        if isinstance(jd_data.get("requirements"), dict)
        else []
    )
    target_skill = technical_skills[0] if technical_skills else "core technical troubleshooting"
    experience_items = resume_data.get("work_experience", []) if isinstance(resume_data, dict) else []
    exp_hint = "your recent role"
    if experience_items and isinstance(experience_items[0], dict):
        exp_hint = experience_items[0].get("job_title") or exp_hint
    return (
        f"Let's start with {target_skill}. "
        f"Based on {exp_hint}, describe a real production issue you investigated, "
        f"the steps you took to diagnose it, and how you resolved it."
    )


PROFILE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PROFILE_IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


def _is_google_picture_url(url: str) -> bool:
    lowered = url.lower()
    return "googleusercontent.com" in lowered or "ggpht.com" in lowered


def public_asset_url(path: Optional[str], base_url: Optional[str] = None) -> Optional[str]:
    """Turn relative /api/files/... paths into absolute URLs for frontend img src."""
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://") or path.startswith("data:"):
        return path
    if path.startswith("/") and base_url:
        return f"{base_url.rstrip('/')}{path}"
    return path


def store_profile_picture_bytes(
    candidate_id: str,
    data: bytes,
    content_type: str,
    fs=None,
) -> Optional[str]:
    """Save raw image bytes to GridFS and return the file id string."""
    if not data or len(data) > 5 * 1024 * 1024:
        return None
    normalized = (content_type or "").split(";")[0].strip().lower()
    if normalized == "image/jpg":
        normalized = "image/jpeg"
    if normalized not in PROFILE_IMAGE_CONTENT_TYPES:
        return None
    ext = normalized.split("/")[-1]
    if ext == "jpeg":
        ext = "jpg"
    if fs is None:
        from app.database import get_gridfs

        fs = get_gridfs()
    stored_id = fs.put(
        data,
        filename=f"profile_{candidate_id}.{ext}",
        content_type=normalized,
        metadata={
            "kind": "candidate_profile_picture",
            "candidate_id": candidate_id,
        },
    )
    return str(stored_id)


def persist_profile_picture(candidate_id: str, raw: Optional[str], fs) -> Optional[str]:
    """Persist a profile picture from GridFS path, data URL, or external URL."""
    import base64
    import re

    value = (raw or "").strip()
    if not value:
        return None

    if value.startswith("/api/files/"):
        return value

    if value.startswith("data:image"):
        match = re.match(r"data:(image/[^;]+);base64,(.+)", value, re.DOTALL)
        if not match:
            return None
        content_type = match.group(1).lower()
        data = base64.b64decode(match.group(2))
        stored_id = store_profile_picture_bytes(candidate_id, data, content_type, fs)
        return f"/api/files/{stored_id}" if stored_id else None

    if value.startswith("http://") or value.startswith("https://"):
        if _is_google_picture_url(value):
            return None
        return value

    return None


def extract_profile_picture_from_payload(data: dict) -> Optional[str]:
    """Read profile picture value from common frontend field names."""
    for key in (
        "profilePicture",
        "profile_picture",
        "picture",
        "photo",
        "avatar",
        "profilePhoto",
        "profile_photo",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def apply_candidate_profile_picture(candidate_id: str, raw_picture: Optional[str]) -> Optional[str]:
    """Parse, store, and persist profile picture on the candidate account."""
    from bson import ObjectId
    from bson.errors import InvalidId

    from app.database import get_gridfs

    try:
        ObjectId(candidate_id)
    except InvalidId:
        return None

    fs = get_gridfs()
    stored = persist_profile_picture(candidate_id, raw_picture, fs)
    if not stored:
        return None

    if not _set_candidate_profile_picture(candidate_id, stored):
        return None
    return stored


def apply_candidate_profile_picture_bytes(
    candidate_id: str, data: bytes, content_type: str
) -> Optional[str]:
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        ObjectId(candidate_id)
    except InvalidId:
        return None

    stored_id = store_profile_picture_bytes(candidate_id, data, content_type)
    if not stored_id:
        return None
    stored = f"/api/files/{stored_id}"
    if not _set_candidate_profile_picture(candidate_id, stored):
        return None
    return stored


def _set_candidate_profile_picture(candidate_id: str, stored: str) -> bool:
    from bson import ObjectId

    result = get_db()["candidates"].update_one(
        {"_id": ObjectId(candidate_id)},
        {"$set": {"profile_picture": stored}},
    )
    return result.matched_count > 0


def resolve_profile_picture(candidate_doc: Optional[dict]) -> Optional[str]:
    """Return uploaded profile picture URL only (never Google OAuth picture)."""
    if not candidate_doc:
        return None
    return candidate_doc.get("profile_picture") or None


def get_candidate_profile_picture(candidate_id: str) -> Optional[str]:
    if not candidate_id:
        return None
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(candidate_id)
    except InvalidId:
        return None

    doc = get_db()["candidates"].find_one(
        {"_id": oid},
        {"profile_picture": 1},
    )
    return resolve_profile_picture(doc)


def get_candidate_profile_pictures(candidate_ids: list[str]) -> dict[str, Optional[str]]:
    if not candidate_ids:
        return {}

    from bson import ObjectId
    from bson.errors import InvalidId

    unique_ids = {cid for cid in candidate_ids if cid}
    valid_oids = []
    for cid in unique_ids:
        try:
            valid_oids.append(ObjectId(cid))
        except InvalidId:
            continue

    pictures: dict[str, Optional[str]] = {cid: None for cid in unique_ids}
    if not valid_oids:
        return pictures

    for doc in get_db()["candidates"].find(
        {"_id": {"$in": valid_oids}},
        {"profile_picture": 1},
    ):
        pictures[str(doc["_id"])] = resolve_profile_picture(doc)
    return pictures
