# Prompt inventory

All LLM instructions live in `prompts/*.md`. Runtime data (resume text, job fields, candidate details) is assembled in Python and sent as the user message. Cache keys are the **filename without `.md`**.

---

## How prompts are loaded

1. `get_gemini()` in `app/services/screening.py` builds a process-wide `GeminiAPI`.
2. `GeminiAPI.__init__` creates `SystemPromptManager` (`sp_manager.py`) with `PROMPT_DIR` (default `prompts`) and `required=REQUIRED_PROMPTS`.
3. `SystemPromptManager._load_all_prompts` reads every `*.md` file in that directory into an in-memory cache.
4. Call sites fetch a prompt with `GeminiAPI._require_prompt(name)` → `prompt_manager.require(name)`.
5. Directory is resolved from the process cwd, then the repo root (next to `sp_manager.py`), so loading still works if the API is not started from the project folder.
6. Missing or empty **required** files fail at Gemini client startup (`LLMConfigError`).
7. Hot reload: `PROMPT_AUTO_RELOAD` (default true). Force reload: `POST /api/prompts/reload` (HR). List: `GET /api/prompts`.

`REQUIRED_PROMPTS` in `app/llm/gemini_client.py`:

- `resume`, `jd`, `initial`, `complete`, `l1_v3`, `L2`
- `job_description_prompt`, `job_keywords`, `experience_years`, `interview_email`

Loaded but **not required**: `question` (legacy, unused).

---

## Used prompts

### `resume.md` → `resume`

- **Gemini function:** `GeminiAPI.resume_parser`
- **Called from:** `profile_upload_resume`, `apply_job`, `upload_resume_hr` in `app/routers/resumes.py`
- **What it does:**
  - Turns raw resume/CV text into structured JSON
  - Extracts personal info, education, skills, experience, projects, certifications, and related sections
  - Output is stored and reused by L0 / L1 / L2 / final scoring

---

### `jd.md` → `jd`

- **Gemini function:** `GeminiAPI.jd_parser`
- **Called from:** `post_job`, `publish_draft`, `update_jd_details` in `app/routers/jobs.py`
- **What it does:**
  - Parses free-form job posting text into structured JD JSON
  - Normalizes title, location, skills, requirements, experience
  - Stored in `JobDetailsV2` and used by screening

---

### `initial.md` → `initial`

- **Gemini function:** `GeminiAPI.initial_screening`
- **Called from:** `run_initial_screening_and_store` in `app/services/screening.py`
- **Triggered by:** `apply_job` after resume parse
- **What it does:**
  - L0 compatibility screen: resume JSON vs JD JSON
  - Returns a compatibility percentage and related fields
  - Used for early fit before L1

---

### `complete.md` → `complete`

- **Gemini function:** `GeminiAPI.complete_screening`
- **Called from:**
  - `run_final_screening_and_store` (`/api/run_final_screening`)
  - `finalscreening_hr` (HR-triggered final score)
  - Both in `app/services/screening.py`
- **What it does:**
  - Full resume-vs-JD evaluation using section weightages
  - Produces `llm_score`, score breakdown, and reasons
  - Written to `Scores`

---

### `l1_v3.md` → `l1_v3`

- **Gemini function:** `GeminiAPI.live_l1`
- **Called from:** `build_l1_chat_session`, `rebuild_l1_chat_session` in `app/services/screening.py`
- **What it does:**
  - System prompt for the live L1 technical interview chat
  - Adaptive questions from resume + JD
  - Produces a machine-readable interview report at the end

---

### `L2.md` → `L2`

- **Gemini function:** `GeminiAPI.generate_l2_questions`
- **Placeholders in the file:** `{resume_json}`, `{jd_json}`, `{resume_score}`, `{resume_reason}`, `{l1_report}`
- **Called from:** `get_l2_questions` in `app/routers/screening.py` (`GET /l2-questions/{candidate_id}/{job_id}`)
- **What it does:**
  - Generates L2 (human interview) questions
  - Avoids repeating topics already covered in L1
  - Cached on the `Scores` document as `l2_questions`

---

### `job_description_prompt.md` → `job_description_prompt`

- **Gemini function:** `GeminiAPI.job_description_generator`
- **Placeholder in the file:** `{job_name}` (job title)
- **Called from:** `generate_job_description` in `app/routers/jobs.py` (`POST /api/generate-job-description`)
- **What it does:**
  - Drafts a full JD from title only
  - Returns Job Description, Roles, Education, Experience, Skills, Good to Have

---

### `job_keywords.md` → `job_keywords`

- **Gemini function:** `GeminiAPI.job_keywords_generator`
- **Called from:** `generate_job_keywords` in `app/routers/jobs.py` (`POST /api/generate-job-keywords`)
- **User message (not in the file):** job title, description, roles, skills, experience, HR manual keywords
- **What it does:**
  - Produces exactly 5 candidate-search keywords
  - Mixes JD content with any HR-entered keywords
  - Returns `{ "jd_keywords_ai": [...] }`

---

### `experience_years.md` → `experience_years`

- **Gemini function:** `GeminiAPI.experience_years_parser`
- **Called from:** `resolve_experience_years` in `app/experience_years.py`
- **Then attached by:** `attach_experience_years_fields` on job create / draft / publish / update
- **Flow:**
  1. `parse_experience_years_rule` tries regex first (`3-5`, `10+`, `up to 10`, etc.)
  2. If rules cannot parse the text, Gemini uses this prompt
  3. Output is a discrete year list → `experienceYears`, `experienceYearsMin`, `experienceYearsMax`
- **What it does:**
  - Normalizes HR experience phrasing into integer years for job filters
  - `"3 to 5 years"` → `[3, 4, 5]`
  - `"up to 10"` → `[10]` (ceiling only)
  - `"10+"` / `"minimum 10"` → open range up to +15 years, cap 40
  - `"5 years"` → `[5]`

---

### `interview_email.md` → `interview_email`

- **Gemini function:** `GeminiAPI.generate_interview_email`
- **Called from:** `send_email_api` in `app/routers/email_api.py`
- **User message (not in the file):** sanitized candidate/meeting fields inside `<<<CANDIDATE_DATA>>>`
- **What it does:**
  - Writes the interview invitation email body
  - Fields are sanitized first (`sanitize_interview_email_fields`) to reduce prompt injection
  - Returns email body text only (HTML wrappers live in `app/email/templates.py` and are not LLM prompts)

---

## Unused prompt

### `question.md` → `question`

- **Loaded:** yes (every `*.md` is cached)
- **Required:** no
- **Used by Gemini:** no (`_require_prompt("question")` is never called)
- **Original intent:** generate exactly 8 technical screening questions from resume + JD
- **Replaced by:** live L1 (`l1_v3`) and L2 (`L2`)

---

## What is *not* an LLM prompt

These look similar but are not Gemini system prompts:

- Email HTML/text in `app/email/templates.py`
- Interview FYI copy in `app/services/interview_scheduling.py`
- Job compose text in `_compose_job_text` (`app/routers/jobs.py`) — input data for `jd_parser`, not a prompt

---

## Quick map

| File | Cache name | Loader function | Feature |
|---|---|---|---|
| `resume.md` | `resume` | `resume_parser` | Resume JSON extraction |
| `jd.md` | `jd` | `jd_parser` | JD JSON extraction |
| `initial.md` | `initial` | `initial_screening` | L0 compatibility % |
| `complete.md` | `complete` | `complete_screening` | Final weighted score |
| `l1_v3.md` | `l1_v3` | `live_l1` | Live L1 interview |
| `L2.md` | `L2` | `generate_l2_questions` | L2 question set |
| `job_description_prompt.md` | `job_description_prompt` | `job_description_generator` | JD draft from title |
| `job_keywords.md` | `job_keywords` | `job_keywords_generator` | 5 search keywords |
| `experience_years.md` | `experience_years` | `experience_years_parser` | Normalize experience years |
| `interview_email.md` | `interview_email` | `generate_interview_email` | Interview invite body |
| `question.md` | `question` | — | Unused / legacy |
