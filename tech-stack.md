# Screening Assist — Tech Stack

Comprehensive inventory of technologies, libraries, and platforms used by this repository (current state).

---

## Runtime & language

| Technology | Role |
|------------|------|
| **Python 3** | Application language |
| **FastAPI** | Async HTTP API framework |
| **Uvicorn** | ASGI server (`uvicorn[standard]`) |
| **Pydantic v2** | Request/response validation |
| **pydantic-settings** | Environment-backed configuration (`app/settings.py`) |

---

## Core application libraries (`requirements.txt`)

| Package | Purpose |
|---------|---------|
| `fastapi` | Web API, dependency injection, OpenAPI |
| `uvicorn[standard]` | Production/dev ASGI server |
| `pymongo` | MongoDB driver + GridFS |
| `python-multipart` | Multipart form uploads (resumes, cover letters) |
| `python-dotenv` | Load `.env` into process environment |
| `pydantic` / `pydantic-settings` | Schemas and settings |
| `python-jose[cryptography]` | JWT encode/decode for HR and candidate tokens |
| `slowapi` | Per-route rate limiting |
| `redis` | Optional L1 interview transcript store |
| `google-genai` | Google Gemini LLM client |
| `pdfplumber` | PDF text extraction |
| `PyPDF2` | PDF utilities |
| `pillow` | Image support used in document pipelines |
| `python-docx` | DOCX handling |
| `boto3` | Amazon SES email transport |
| `sendgrid` | SendGrid email transport |

---

## AI / LLM

| Component | Detail |
|-----------|--------|
| **Provider** | Google Gemini via `google-genai` |
| **API surface** | Interactions API (`client.interactions.create`) |
| **Default flash model** | `gemini-3.1-flash-lite` (`GEMINI_MODEL_FLASH`) |
| **Default pro model** | `gemini-3.1-flash-lite` (`GEMINI_MODEL_PRO`) |
| **Client module** | `app/llm/gemini_client.py` (`GeminiAPI`) |
| **Protocol** | `app/llm/base.py` (`ScreeningLLM`) |
| **Prompts** | Markdown files under `prompts/` managed by `sp_manager.py` |
| **Structured outputs** | Pydantic schemas in `gemini_schemas/` |
| **Resilience** | Timeouts, retries (`app/llm/retry.py`), concurrency semaphore |
| **Thinking level** | Bounded via `GEMINI_THINKING_BUDGET` (mapped to `minimal`/`low`/`medium`/`high`) |

---

## Data & session storage

| Technology | Usage |
|------------|-------|
| **MongoDB** | Primary persistence (jobs, resumes JSON, candidates, scores, feedback) |
| **MongoDB GridFS** | Binary resume PDFs and cover letters |
| **Redis** (optional) | Cross-instance L1 interview transcript rebuild (`REDIS_URL`) |
| **In-process OrderedDict** | Live chat object cache with TTL/LRU (`app/chat_sessions.py`) |

---

## Authentication & security

| Technology | Usage |
|------------|-------|
| **API keys** | `X-API-Key` header matched to `API_KEY` |
| **JWT (HS256 default)** | HR and candidate bearer tokens (`python-jose`) |
| **CORS** | Configurable origin list (`CORS_ORIGINS`) |
| **Rate limiting** | SlowAPI on high-cost candidate routes |
| **Input allow-lists** | Pydantic models with `extra="forbid"` on sensitive writes (e.g. job PATCH) |
| **Secrets management** | Environment / `.env` only — no secrets in source |

---

## Email & calendar

| Technology | Usage |
|------------|-------|
| **SMTP** (e.g. Gmail) | Optional via `SMTP=true` and `SMTP_*` vars |
| **Amazon SES** | Via `boto3` when AWS credentials present |
| **SendGrid** | Fallback / alternate transport |
| **ICS attachments** | Calendar invites generated in helpers / email service |
| **Calendar listing** | Mock or disabled (`CALENDAR_MODE`) — not a live Google Calendar integration by default |

---

## Document processing

| Technology | Usage |
|------------|-------|
| **pdfplumber / PyPDF2** | Extract text from uploaded resumes |
| **python-docx** | DOCX support |
| **LibreOffice (`soffice`)** | DOCX → PDF conversion (`LIBREOFFICE_PATH`) |
| **Pillow** | Image-related document support |

---

## Observability & ops (lightweight)

| Mechanism | Usage |
|-----------|-------|
| Python `logging` | Application and delivery logs |
| `GET /health` | Process liveness |
| `GET /api/email/health` | Email delivery health (HR-protected) |
| FastAPI `/docs` | OpenAPI UI in non-production / debug |

---

## Project structure technologies (logical modules)

| Area | Key files / packages |
|------|----------------------|
| App factory | `app/factory.py` |
| Config | `app/settings.py`, `.env.example` |
| Auth | `app/auth.py` |
| HTTP routes | `app/routers/*.py` |
| Screening pipelines | `app/services/screening.py` |
| LLM | `app/llm/*` |
| Email | `app/email/*` |
| Compatibility shims | `gemini_utils.py`, `email_utils.py`, `email_templates.py`, `initialscreening.py` |

---

## External services (runtime dependencies)

| Service | Required? | Used for |
|---------|-----------|----------|
| MongoDB Atlas (or compatible) | **Yes** | All persistent data |
| Google Gemini API | **Yes** | Screening, parsing, L1/L2, JD/email generation |
| Redis | Recommended for multi-instance | L1 session continuity |
| SMTP / SES / SendGrid | Optional for email | HR/candidate notifications |
| LibreOffice | Optional for DOCX uploads | DOCX → PDF |

---

## Development & configuration files

| File | Purpose |
|------|---------|
| `requirements.txt` | Python dependencies |
| `.env.example` | Documented environment template |
| `.env` | Local secrets (gitignored) |
| `.gitignore` | Excludes `.env`, venvs, caches, certs |
| `README.md` | Getting started |
| `api-documentation.md` | Full API reference |
| `architecture-diagram.md` | Architecture & flows |
| `tech-stack.md` | This document |
| `issue-resolution-summary.md` | Non-technical issue/fix narrative |

---

## Default notable configuration values

| Setting | Default (if unset) |
|---------|-------------------|
| `GEMINI_MODEL_FLASH` | `gemini-2.5-flash` |
| `GEMINI_MODEL_PRO` | `gemini-2.5-pro` |
| `GEMINI_THINKING_BUDGET` | `2048` |
| `GEMINI_MAX_CONCURRENCY` | `4` |
| `GEMINI_TIMEOUT_SECONDS` | `90` |
| `GEMINI_MAX_RETRIES` | `3` |
| `JWT_EXPIRE_MINUTES` | `480` |
| `MONGODB_DB` | `screeningassist` |
| `CHAT_SESSION_TTL_SECONDS` | `3600` |
| `CALENDAR_MODE` | `mock` |

---

## What this stack is not

- Not a frontend framework repo (API-only; UIs are separate clients).
- Not using Django / Flask / Express as the primary framework.
- Not embedding secrets or cloud credentials in Python modules.
- Not requiring Redis for single-instance development (optional for scale).
