# Screening Assist

## What this application does

Screening Assist is a recruiting backend that helps companies screen job applicants faster and more consistently.

In plain terms, it:

- Accepts job postings and candidate resumes.
- Uses Google Gemini (AI) to parse resumes and job descriptions, score how well a candidate fits a role, and run structured interview steps (initial score, live chat-style L1 questions, then deeper L2 questions for HR).
- Stores candidate information, scores, notes, and uploaded files securely in MongoDB.
- Sends email notifications to HR and candidates (SMTP, Amazon SES, or SendGrid) when configured.
- Exposes a REST API that a careers website and an HR dashboard can call.

Candidates apply by uploading a resume. The system returns a short-lived access token so they can complete onboarding and the L1 interview without exposing everyone else’s data. HR staff authenticate with an API key or a signed token to manage jobs, review scores, download files, and advance candidates.

---

## How to clone this repository

Replace the repository URL with your team’s actual Git remote if it differs.

### 1. Install Git (if needed)

Confirm Git is available:

```bash
git --version
```

### 2. Clone the repo

```bash
git clone <YOUR_REPOSITORY_URL> screening-assist
cd screening-assist
```

Example if your remote is on GitHub:

```bash
git clone https://github.com/<org>/<repo>.git screening-assist
cd screening-assist
```

If you already have the folder locally and only need to refresh:

```bash
cd screening-assist
git pull
```

### 3. Create a Python environment (recommended)

```bash
python -m venv venv
```

**Windows (PowerShell):**

```powershell
.\venv\Scripts\Activate.ps1
```

**macOS / Linux:**

```bash
source venv/bin/activate
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Configure environment variables

```bash
copy .env.example .env
```

On macOS/Linux use `cp .env.example .env`.

Open `.env` and set at least:

- `API_KEY`
- `JWT_SECRET`
- `MONGODB_URI`
- `GEMINI_API_KEY`

Never commit the `.env` file. It is listed in `.gitignore`.

### 6. Run the API

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

- Health check: `http://localhost:8000/health`
- Interactive API docs (when debug / non-production): `http://localhost:8000/docs`

---

## Authentication (short)

| Who | How |
|-----|-----|
| HR / service | Header `X-API-Key: <API_KEY>` or `Authorization: Bearer <HR JWT>` |
| HR JWT | `POST /api/auth/token` with your API key in the JSON body |
| Candidate | Token returned by `POST /api/upload_resume` as `access_token` |

Public (no login): health check, public job list, job onboarding questions, resume upload (rate-limited), and token exchange.

---

## Documentation index

| Document | Contents |
|----------|----------|
| [api-documentation.md](api-documentation.md) | Full list of endpoints, auth, and payloads |
| [architecture-diagram.md](architecture-diagram.md) | System architecture and data-flow diagrams |
| [tech-stack.md](tech-stack.md) | Languages, libraries, and external services |
| [issue-resolution-summary.md](issue-resolution-summary.md) | Plain-English summary of problems fixed |

---

## Architecture (brief)

```
main.py                 # starts the app
app/
  factory.py            # wires middleware and routers
  settings.py           # reads configuration from the environment
  auth.py               # API key and JWT checks
  database.py           # MongoDB and file storage
  routers/              # HTTP endpoints by area
  services/screening.py # screening workflows
  llm/                  # Gemini AI client
  email/                # outbound email
prompts/                # AI instruction files
```

Compatibility shims (`gemini_utils.py`, `email_utils.py`, and similar) re-export the newer modules so older import paths keep working.

---

## AI models

- Defaults: `gemini-2.5-flash` and `gemini-2.5-pro` (overridable in `.env`).
- AI work runs off the main request thread so the API can serve multiple users.
- Optional Redis (`REDIS_URL`) keeps live L1 interview progress available across multiple servers.

---

## Email

Configure SMTP, AWS SES, and/or SendGrid through environment variables. Delivery health is available to HR at `GET /api/email/health`.

---

## Security reminders before production

1. Use strong unique values for `API_KEY` and `JWT_SECRET`.
2. Rotate any credentials that were ever committed to git history (database, Gemini, AWS, email providers).
3. Keep `.env` out of version control.
4. Prefer Redis when running more than one API instance that handles live L1 interviews.

See [issue-resolution-summary.md](issue-resolution-summary.md) for a stakeholder-friendly explanation of earlier risks and how they were addressed.
