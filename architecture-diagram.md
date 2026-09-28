# Screening Assist — Architecture Diagram

This document describes the system architecture and primary data flows for the Screening Assist API as implemented today.

---

## High-level system view

```mermaid
flowchart TB
  subgraph Clients
    FE[Career site / Candidate UI]
    HRUI[HR / Recruiter UI]
  end

  subgraph API["Screening Assist API (FastAPI)"]
    MAIN[main.py]
    FACTORY[app/factory.py]
    AUTH[app/auth.py]
    ROUTERS[app/routers/*]
    SVC[app/services/screening.py]
    LLM[app/llm/gemini_client.py]
    EMAIL[app/email/service.py]
    SESS[app/chat_sessions.py]
    SETTINGS[app/settings.py]
  end

  subgraph External
    MONGO[(MongoDB Atlas)]
    GRID[GridFS]
    GEMINI[Google Gemini API]
    REDIS[(Redis - optional)]
    SMTP[SMTP / SES / SendGrid]
  end

  FE --> ROUTERS
  HRUI --> ROUTERS
  MAIN --> FACTORY --> ROUTERS
  ROUTERS --> AUTH
  ROUTERS --> SVC
  ROUTERS --> LLM
  ROUTERS --> EMAIL
  ROUTERS --> SESS
  FACTORY --> SETTINGS
  SVC --> MONGO
  ROUTERS --> MONGO
  ROUTERS --> GRID
  LLM --> GEMINI
  SESS --> REDIS
  EMAIL --> SMTP
  SETTINGS -. env / .env .-> SETTINGS
```

---

## Application package layout

```text
main.py                      Thin ASGI entry (create_app)
app/
  factory.py                 App factory: CORS, rate limit, routers, startup ping
  settings.py                All config from environment (pydantic-settings)
  auth.py                    API key + JWT + candidate scope checks
  database.py                MongoDB pool + GridFS
  schemas.py                 Pydantic request contracts
  helpers.py                 PDF/DOCX extract, ICS, LibreOffice convert
  chat_sessions.py           L1 session LRU/TTL + Redis transcript
  rate_limit.py              SlowAPI limiter
  routers/                   HTTP endpoints by domain
    system.py                Health, auth token, prompts
    resumes.py               Upload resume / cover letter
    candidates.py            Candidate PII & listings
    screening.py             Scores, L1 chat, L2 questions
    jobs.py                  Job CRUD / public career list
    feedback.py              Notes & L2 feedback
    files.py                 GridFS downloads
    email_api.py             Notifications & calendar mock
  services/
    screening.py             Initial/final screening pipelines, L1 session resolve
  llm/
    base.py                  ScreeningLLM protocol
    gemini_client.py         GeminiAPI (7+ LLM methods)
    schemas.py               Structured output schemas
    retry.py                 Timeout + retries
    prompt_safety.py         Interview-email field sanitization
    errors.py                LLM error types
  email/
    config.py                EmailConfig from settings
    service.py               SMTP / SES / SendGrid + health
    templates.py             HTML-escaped templates
    results.py               DeliveryResult
prompts/*.md                 System prompts (resume, jd, initial, l1_v3, …)
gemini_utils.py              Re-export of GeminiAPI (compat)
email_utils.py               Re-export of email helpers (compat)
sp_manager.py                SystemPromptManager
```

---

## Request path (typical authenticated call)

```mermaid
sequenceDiagram
  participant C as Client
  participant R as Router
  participant A as auth.py
  participant S as Service / Helper
  participant M as MongoDB
  participant G as Gemini

  C->>R: HTTP + X-API-Key or Bearer JWT
  R->>A: get_current_principal / require_hr / assert_candidate_access
  alt Unauthorized
    A-->>C: 401 / 403
  else Authorized
    R->>S: Business logic (often via run_in_thread)
    S->>M: Read / write collections
    opt LLM needed
      S->>G: generate_content / chat (thread pool)
      G-->>S: Structured JSON or text
    end
    S-->>R: Result
    R-->>C: JSON / file stream
  end
```

---

## Candidate apply & initial screening flow

```mermaid
flowchart LR
  A[POST /api/upload_resume] --> B[Validate file + extract text]
  B --> C[Gemini resume_parser]
  C --> D[Store ResumeV2 + GridFS PDF]
  D --> E[Scores status = processing]
  D --> F[Return candidate_id + access_token]
  E --> G[Background: initial_screening]
  G --> H[Update Scores compatibility]
  F --> I[Candidate UI uses Bearer token]
  I --> J[store_candidate_info / personal_info / L1 chat]
```

---

## L1 live interview session flow

```mermaid
flowchart TB
  Q[GET /api/generate_questions] --> R[resolve_l1_session]
  R --> L{Local chat cache hit?}
  L -->|yes| RET[Return first_question]
  L -->|no| RD{Redis transcript?}
  RD -->|yes| RB[rebuild_l1_chat_session]
  RD -->|no| NEW[build_l1_chat_session via live_l1]
  RB --> STORE[chat_sessions.set + Redis persist]
  NEW --> STORE
  STORE --> RET
  MSG[POST /api/send_message] --> R2[resolve_l1_session]
  R2 --> EX[send_message on Gemini chat in thread]
  EX --> HIST[append_history → Redis]
  EX --> DONE{Interview complete marker?}
  DONE -->|yes| REP[Save interview_report + delete session]
  DONE -->|no| AI[Return ai_message]
```

---

## Final / complete screening flow

```mermaid
flowchart LR
  HR[HR: resume-screening or run_final_screening] --> BG[Background complete_screening]
  BG --> JD[Load JobDetailsV2]
  BG --> RV[Load ResumeV2]
  BG --> LLM[Gemini complete_screening]
  LLM --> SC[Write Scores llm_score / breakdown]
```

---

## Email notification path

```mermaid
flowchart TB
  TRIG[Router / background task] --> EU[email_utils / EmailService]
  EU --> CFG[EmailConfig from settings]
  EU --> TPL[HTML-escaped templates]
  CFG --> MODE{Transport}
  MODE -->|SMTP=true| SMTP[Gmail/SMTP]
  MODE -->|AWS keys| SES[Amazon SES]
  MODE -->|fallback| SG[SendGrid]
  EU --> HL[Delivery health counters]
```

---

## Data stores

| Store | Role |
|-------|------|
| **MongoDB** | Jobs, resumes (JSON), candidates, scores, screening Q&A, feedback, notes |
| **GridFS** | Binary resumes and cover letters |
| **Redis (optional)** | Serialized L1 interview transcripts for multi-instance rebuild |
| **In-process memory** | Live Gemini chat objects (not serializable) with TTL/LRU |

Without Redis, L1 interviews require sticky sessions to one API instance. With `REDIS_URL` set, another instance can rebuild the chat from the transcript.

---

## Security boundaries

```text
Public surface
  ├── GET /health
  ├── POST /api/auth/token          (needs valid API_KEY in body)
  ├── POST /api/upload_resume       (rate-limited; issues candidate JWT)
  ├── GET /api/jobviewmain
  └── GET /api/jobview/{id}/onboarding

Candidate surface (Bearer candidate JWT or HR credentials)
  ├── PII read/write
  ├── Initial screening result
  └── L1 generate_questions / send_message / save answers

HR / service surface (X-API-Key or HR JWT)
  ├── Job admin & JD generation
  ├── Scores, L2, files, feedback
  ├── Email send / email health
  └── Prompt list / reload
```

Secrets never live in source code. Configuration is loaded only from environment variables via `app/settings.py` (see `.env.example`).

---

## LLM capability map

| Method (`GeminiAPI`) | Typical trigger |
|----------------------|-----------------|
| `resume_parser` | Resume upload |
| `jd_parser` | Job create / JD update |
| `initial_screening` | Post-upload background L0 |
| `live_l1` | L1 chat bootstrap / rebuild |
| `complete_screening` | Final scoring |
| `job_description_generator` | HR JD assist |
| `generate_l2_questions` | L2 question pack |
| `generate_interview_email` | Interview invite drafting |

Blocking Gemini SDK calls are offloaded with `asyncio.to_thread` / `run_in_thread`, capped by `GEMINI_MAX_CONCURRENCY`.
