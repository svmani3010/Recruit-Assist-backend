# Screening Assist — API Documentation

Complete reference for the FastAPI backend as implemented in the current codebase.

**Base URL (local):** `http://localhost:8000`  
**Interactive docs:** `GET /docs` (available when `DEBUG=true` or `ENVIRONMENT` is not `production`)  
**Entrypoint:** `main.py` → `app.factory.create_app()`

---

## Authentication

| Mechanism | How to send | Role granted |
|-----------|-------------|--------------|
| API key | Header `X-API-Key: <API_KEY>` | `service` (treated as HR for protected routes) |
| HR JWT | Header `Authorization: Bearer <token>` | `hr` or `service` |
| Candidate JWT | Header `Authorization: Bearer <token>` | `candidate` (scoped to `candidate_id` + `job_id`) |

### Obtain an HR token

`POST /api/auth/token`

```json
{
  "api_key": "<API_KEY from .env>",
  "role": "hr",
  "subject": "hr-user"
}
```

**Response:**

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "role": "hr"
}
```

### Obtain a candidate token

Issued automatically by `POST /api/upload_resume` in the response fields `access_token` and `token_type`. Use that bearer token for candidate-scoped routes.

### Auth labels used below

- **Public** — no credentials required  
- **HR** — `require_hr` (API key or HR/service JWT)  
- **Authenticated** — any valid API key or JWT (`get_current_principal`)  
- **Candidate-scoped** — authenticated, plus candidate tokens may only access their own `candidate_id` / `job_id`

---

## Rate limits

SlowAPI limits (by client IP) on:

| Endpoint | Limit |
|----------|-------|
| `POST /api/upload_resume` | 20/minute |
| `GET /api/generate_questions` | 20/minute |
| `POST /api/send_message` | 20/minute |

Settings also define `RATE_LIMIT_PUBLIC` and `RATE_LIMIT_LLM` (defaults `60/minute` and `20/minute`); those env values are reserved for configuration and are not applied as global defaults today.

---

## System

### `GET /health`

| | |
|--|--|
| **Auth** | Public |
| **Description** | Liveness / health check |

**Response:**

```json
{ "status": "ok", "service": "Screening Assist" }
```

---

### `GET /api/email/health`

| | |
|--|--|
| **Auth** | HR |
| **Description** | Email delivery subsystem health |

**Response fields:** `email_enabled`, `smtp_mode`, `hr_recipient_count`, `sender_configured`, `consecutive_failures`, `failure_alert_threshold`, `unhealthy`, `last_success_at`, `last_failure_at`, `last_error`, `recent`

---

### `GET /api/prompts`

| | |
|--|--|
| **Auth** | HR |
| **Description** | List loaded system prompt names |

**Response:** `{ "prompts": ["resume", "jd", ...] }`

---

### `POST /api/prompts/reload`

| | |
|--|--|
| **Auth** | HR |
| **Description** | Force-reload markdown prompts from disk |

**Response:** `{ "reloaded": true, "prompts": [...], "count": N }`

---

### `POST /api/auth/token`

| | |
|--|--|
| **Auth** | Public (body must contain a valid `api_key`) |
| **Body** | `TokenRequest` |

| Field | Type | Default | Required |
|-------|------|---------|----------|
| `api_key` | string | — | yes |
| `role` | string | `"hr"` | no |
| `subject` | string | `"hr-user"` | no |
| `candidate_id` | string \| null | `null` | no |

---

### `GET /api/auth/whoami`

| | |
|--|--|
| **Auth** | HR |
| **Description** | Return current principal |

**Response:** `{ "subject", "role", "candidate_id", "job_id" }`

---

## Resumes

### `POST /api/upload_resume`  
### `POST /api/upload_resume/` (alias)

| | |
|--|--|
| **Auth** | Public |
| **Rate limit** | 20/minute |
| **Content-Type** | `multipart/form-data` |
| **Description** | Candidate apply: parse resume, store PDF in GridFS, start initial screening in background, return scoped candidate JWT |

| Form field | Type | Required |
|------------|------|----------|
| `resume` | file (PDF/DOCX) | yes |
| `jobid` | string | yes |
| `jobrole` | string | yes |

**Response:**

```json
{
  "candidate_id": "<uuid>",
  "status": "resume stored, initial screening started",
  "personal_info": { },
  "resume_file_id": "<gridfs-id>",
  "access_token": "<candidate-jwt>",
  "token_type": "bearer"
}
```

---

### `POST /api/resume-screening`

| | |
|--|--|
| **Auth** | HR |
| **Content-Type** | `multipart/form-data` |
| **Description** | HR-added resume; starts final (complete) screening in background |

| Form field | Type | Required |
|------------|------|----------|
| `resume` | file | yes |
| `jobid` | string | yes |
| `jobrole` | string | yes |

**Response:** `{ "candidate_id", "status", "resume_file_id" }`

---

### `POST /api/upload_cover_letter`

| | |
|--|--|
| **Auth** | Authenticated |
| **Content-Type** | `multipart/form-data` |

| Form field | Type | Required |
|------------|------|----------|
| `filename` | string | yes |
| `cover_letter` | file | yes |

**Response:** `{ "file_id", "filename" }`

---

## Candidates

### `POST /api/store_candidate_info`

| | |
|--|--|
| **Auth** | Candidate-scoped |
| **Body** | Raw JSON (no Pydantic model) |
| **Description** | Upsert onboarding / personal information |

**Recognized fields** (camelCase or snake_case accepted where noted):

| Field | Notes |
|-------|-------|
| `candidate_id` | Required |
| `job_id` | Used for access check |
| `first_name`, `last_name`, `email`, `phone`, `linkedin` | |
| `coverLetter` / `cover_letter`, `coverLetterType` | Cover letter payload |
| `hasCoverLetter` / `has_cover_letter` | |
| `workMode` / `work_mode` | |
| `currentLocation` / `current_location` | |
| `preferredCity` / `preferred_city` | |
| `noticePeriod` / `notice_period` | |
| `currentSalary` / `current_salary` | |
| `expectedSalary` / `expected_salary` | |
| `jobChangeReason` / `job_change_reason` | |
| `careerGap` / `career_gap_reason` | |
| `interview_date` | |
| `interview_time` / `interview_time_slots` | |
| `authorized` / `legally_authorized` | |
| `openToRelocate` / `open_to_relocate` | |
| `foundUs` / `found_us` | |
| *(any other keys)* | Stored under `extra_onboarding_fields` |

**Response:** `{ "status": "success" }`

---

### `GET /api/personal_info_status/{candidate_id}`

| | |
|--|--|
| **Auth** | Candidate-scoped |
| **Path** | `candidate_id` |
| **Query** | `job_id` (optional) |

**Response:**

```json
{
  "ready": true,
  "personal_info": {
    "first_name": "",
    "last_name": "",
    "email": "",
    "phone": ""
  }
}
```

---

### `GET /api/candidates/{job_id}`

| | |
|--|--|
| **Auth** | HR |
| **Description** | List candidates for a job |

**Response:** array of `{ candidate_id, name, email, age, reason, score_breakdown, status, score, applied }`

---

### `GET /api/candidate/{candidate_id}`

| | |
|--|--|
| **Auth** | HR |
| **Query** | `job_id` (required) |

**Response:** Full `candidateInformation` document plus `resume_file_id` and computed `name`.

---

### `GET /api/candidate_by_id/{candidate_id}`

| | |
|--|--|
| **Auth** | HR |

**Response:** `{ "candidate_id", "name", "email", "phone" }`

---

### `GET /api/manual_candidates/{jobid}`

| | |
|--|--|
| **Auth** | HR |
| **Description** | Candidates added by HR (`added_by: "HR"`) |

**Response:** array of `{ candidate_id, full_name, email, phone, llm_score, score_breakdown, reasons }`

---

### `GET /api/resume_json_personal/{candidate_id}`

| | |
|--|--|
| **Auth** | Candidate-scoped |
| **Query** | `job_id` (optional) |

**Response:** `resume_json.personal_information` object from `ResumeV2`.

---

## Screening

### `GET /api/get_initial_screening_result/{candidate_id}`

| | |
|--|--|
| **Auth** | Candidate-scoped |
| **Query** | `job_id` (optional) |

**Response (ready):** `{ "status", "score", "job_id" }`  
**Response (pending):** `{ "status": "pending", "score": 0, "job_id", "message" }`

---

### `POST /api/run_final_screening`

| | |
|--|--|
| **Auth** | HR |
| **Query** | `candidate_id`, `job_id` (required) |

**Response:** `{ "status": "final screening started" }`

---

### `GET /api/get_final_score`

| | |
|--|--|
| **Auth** | HR |
| **Query** | `candidate_id`, `job_id` |

**Response:** Scores document, or `{}` if missing.

---

### `GET /api/candidate_scores/{job_id}`

| | |
|--|--|
| **Auth** | HR |

**Response:** `[{ candidate_id, llm_score, compatibility, status }]`

---

### `GET /api/candidate_scores_with_info/{job_id}`

| | |
|--|--|
| **Auth** | HR |

**Response:** `[{ candidate_id, llm_score, compatibility, name, email, l0_reasons, status }]`

---

### `GET /api/generate_questions`

| | |
|--|--|
| **Auth** | Candidate-scoped |
| **Rate limit** | 20/minute |
| **Query** | `candidate_id`, `job_id` (required) |
| **Description** | Start or resume L1 live interview; return first question |

**Response:** `{ "questions": ["<first_question>"] }`

---

### `POST /api/send_message`

| | |
|--|--|
| **Auth** | Candidate-scoped |
| **Rate limit** | 20/minute |
| **Body** | `SendMessageRequest` |

| Field | Type | Required |
|-------|------|----------|
| `candidate_id` | string | yes |
| `job_id` | string | yes |
| `message` | string | yes |

**Response (ongoing):**

```json
{ "ai_message": "...", "completed": false }
```

**Response (complete):**

```json
{ "ai_message": "...", "completed": true, "report": { } }
```

---

### `POST /api/save_screening_answers`

| | |
|--|--|
| **Auth** | Candidate-scoped |
| **Body** | `SaveAnswersRequest` |

| Field | Type | Required |
|-------|------|----------|
| `candidate_id` | string | yes |
| `job_id` | string | yes |
| `questions` | list | yes |
| `answers` | list | yes |

`questions` and `answers` must be the same length.

**Response:** `{ "status": "success", "message": "Answers saved" }`

---

### `GET /api/screening_questions`

| | |
|--|--|
| **Auth** | HR |
| **Query** | `candidate_id`, `job_id` |

**Response:** `screeningquestions` document (without `_id`), or `404` `{ "error": "Not found" }`.

---

### `POST /api/update_l1_status`

| | |
|--|--|
| **Auth** | HR |
| **Body** | `UpdateL1StatusRequest` |
| **Description** | Update L1 decision status; when `status` is `"approved"`, may trigger interview invitation emails |

| Field | Type | Required |
|-------|------|----------|
| `candidate_id` | string | yes |
| `job_id` | string | yes |
| `status` | string | yes |
| `candidate_name` | string \| null | no |
| `candidate_email` | string \| null | no |
| `position` | string \| null | no |
| `interview_level` | string \| null | no |
| `date` | string \| null | no |
| `time` | string \| null | no |
| `duration_minutes` | int \| null | no |
| `timezone` | string \| null | no |
| `emails` | string[] \| null | no |
| `main_reviews` | any \| null | no |
| `interviewers` | `{ name, email }[]` \| null | no |

**Response:** `{ "status": "ok" }`

---

### `GET /l2-questions/{candidate_id}/{job_id}`

| | |
|--|--|
| **Auth** | HR |
| **Description** | Return cached or newly generated L2 interview questions |

**Response:** `{ "question": [ ... ] }` or `{ "error": "..." }`

---

## Jobs

### `POST /api/jobs`

| | |
|--|--|
| **Auth** | HR |
| **Body** | Raw JSON |
| **Description** | Create job posting, parse JD with Gemini, store in `jobview` + `JobDetailsV2` |

Common body fields: `jobrole`, `jobDescription`, `educationalQualifications`, `experienceRequired`, `desiredSkills`, `requiredCertifications`, `goodToHave`, `employmentType`, `workMode`, `location`, `joiningRequirement`, `salaryPackage`, `sectionWeightage`, plus any additional fields stored on the job document.

**Response:** `{ "message": "Job added successfully", "jobid": "<12-hex-id>" }`

---

### `GET /api/jobview`

| | |
|--|--|
| **Auth** | HR |
| **Description** | All jobs (admin) |

---

### `GET /api/jobviewmain`

| | |
|--|--|
| **Auth** | Public |
| **Description** | Career-site listing (`show: "on"` only) |

---

### `PATCH /api/jobview/{jobid}`

| | |
|--|--|
| **Auth** | HR |
| **Body** | `JobUpdateRequest` (`extra = forbid` — unknown fields rejected) |

| Field | Type | Notes |
|-------|------|-------|
| `jobrole` | string \| null | |
| `jobDescription` | string \| null | |
| `educationalQualifications` | string \| null | |
| `experienceRequired` | string \| null | |
| `desiredSkills` | string \| null | |
| `requiredCertifications` | string \| null | |
| `goodToHave` | string \| null | |
| `employmentType` | string \| null | |
| `workMode` | string \| null | |
| `location` | string \| null | |
| `joiningRequirement` | string \| null | |
| `salaryPackage` | string \| null | |
| `sectionWeightage` | object \| null | |
| `onboarding` | any \| null | |
| `show` | `"on"` \| `"off"` \| null | pattern enforced |

**Response:** `{ "success": true }`

---

### `POST /api/upd/jobs`

| | |
|--|--|
| **Auth** | HR |
| **Body** | Raw JSON; **must include** `jobid` |
| **Description** | Re-parse and replace JD details for an existing job |

**Response:** `{ "message": "JD chunk updated", "jobid": "..." }`

---

### `PATCH /api/jobview/{jobid}/show`

| | |
|--|--|
| **Auth** | HR |
| **Body** | `JobShowToggleRequest`: `{ "show": "on" | "off" }` |

**Response:** `{ "success": true, "show": "on"|"off" }`

---

### `GET /api/jobview/{job_id}/onboarding`

| | |
|--|--|
| **Auth** | Public |
| **Description** | Return onboarding question config for a job |

---

### `POST /api/generate-job-description`

| | |
|--|--|
| **Auth** | HR |
| **Body** | `GenerateJobDescriptionRequest`: `{ "job_title": "..." }` |

**Response:** Gemini-generated JD object (structured JSON).

---

## Feedback

### `POST /api/add_note`

| | |
|--|--|
| **Auth** | HR |
| **Body** | `NoteRequest` |

| Field | Type | Default |
|-------|------|---------|
| `candidate_id` | string | required |
| `candidate_name` | string | required |
| `job_id` | string | required |
| `note` | string | required |
| `level` | string | required |
| `author` | string | `"HR Manager"` |

**Response:** `{ "status": "success", "note_id": "..." }`

---

### `GET /api/get_notes/{candidate_id}`

| | |
|--|--|
| **Auth** | HR |

**Response:** array of note documents (`_id` as string).

---

### `POST /api/save_draft_feedback`

| | |
|--|--|
| **Auth** | HR |
| **Body** | `DraftL2FeedbackRequest`: `candidate_id`, `job_id`, `feedback` |

**Response:** `{ "status": "success", "message": "Draft feedback saved" }`

---

### `GET /api/get_draft_feedback/{candidate_id}/{job_id}`

| | |
|--|--|
| **Auth** | HR |

**Response:** `{ "feedback": "..." }` (empty string if none).

---

### `POST /api/submit_feedback`

| | |
|--|--|
| **Auth** | HR |
| **Body** | `SubmitFeedbackRequest` |

| Field | Type | Default |
|-------|------|---------|
| `candidate_id` | string | required |
| `job_id` | string | required |
| `feedback` | string | required |
| `author` | string | `"HR Manager"` |
| `level` | string | `"L2"` |

**Response:** `{ "status": "success", "message": "Feedback submitted successfully" }`

---

### `GET /api/get_submitted_feedbacks/{candidate_id}/{job_id}`

| | |
|--|--|
| **Auth** | HR |

**Response:** `{ "feedbacks": [ ... ] }`

---

## Files

### `GET /api/cover_letter_file/{file_id}`

| | |
|--|--|
| **Auth** | HR |
| **Description** | Stream cover letter bytes from GridFS |

---

### `GET /api/resume_pdf/{file_id}`

| | |
|--|--|
| **Auth** | HR |
| **Description** | Stream resume PDF/file bytes from GridFS |

---

### `GET /api/resume_upload_date/{candidate_id}/{job_id}`

| | |
|--|--|
| **Auth** | HR |

**Response:** `{ "uploadDate": "<datetime>" }`

---

## Email & calendar

### `POST /api/candidate_apply_notification`

| | |
|--|--|
| **Auth** | Authenticated |
| **Body** | `CandidateApplyNotificationRequest`: `{ "job_role": "N/A" }` (default) |

**Response:** `{ "status": "success", "message": "...", "job_role": "..." }`

---

### `POST /api/send_email`

| | |
|--|--|
| **Auth** | HR |
| **Body** | `SendEmailRequest` |
| **Description** | Generate interview invitation email body via Gemini (does not always send by itself — see response) |

| Field | Type | Default |
|-------|------|---------|
| `Company` | string | `"Fanisko"` |
| `candidate_id` | string \| null | null |
| `job_id` | string \| null | null |
| `candidate_name` | string \| null | null |
| `candidate_email` | string \| null | null |
| `position` | string \| null | null |
| `interview_level` | string | `"Technical L3"` |
| `date` | string \| null | null |
| `time` | string \| null | null |
| `duration_minutes` | int | `60` |
| `timezone` | string | `"Asia/Kolkata"` |
| `meeting_link` | string \| null | null |
| `interviewers` | `{ name, email }[]` \| null | null |

**Response shape:**

```json
{
  "role": "assistant",
  "parts": [{ "text": "<email body>" }],
  "resolved_details": {
    "candidate_name": "",
    "candidate_email": "",
    "position": "",
    "candidate_id": "",
    "job_id": ""
  }
}
```

---

### `POST /api/list-events`

| | |
|--|--|
| **Auth** | HR |
| **Body** | `ListEventsRequest` |

| Field | Type | Constraints |
|-------|------|-------------|
| `email_id` | string | required |
| `days` | int | `0`–`90` |

**Response (mock mode):** `{ "source": "simulated", "calendar_mode": "mock", "warning": "...", "total_events": N, "data": { "events": [...] } }`  
**Disabled / unsupported:** HTTP `501`

---

## Endpoint summary by auth

| Auth class | Approx. count | Examples |
|------------|---------------|----------|
| Public | 5 | `/health`, `/api/auth/token`, `/api/upload_resume`, `/api/jobviewmain`, `/api/jobview/{job_id}/onboarding` |
| HR | ~32 | Jobs admin, scores, files, feedback, L2, email send |
| Authenticated | 2 | Cover letter upload, apply notification |
| Candidate-scoped | 8 | PII, L1 chat, save answers, initial screening result |

---

## Related MongoDB collections (for context)

| Collection | Typical use |
|------------|-------------|
| `jobview` | Job postings / visibility |
| `JobDetailsV2` | Parsed JD JSON |
| `ResumeV2` | Parsed resume JSON + file refs |
| `candidateInformation` | Onboarding / PII |
| `Scores` | Compatibility / LLM scores / status |
| `screeningquestions` | L1 answers / interview reports |
| GridFS | Resume PDFs, cover letters |

Schema contracts for request bodies live in `app/schemas.py`. Router modules live under `app/routers/`.
