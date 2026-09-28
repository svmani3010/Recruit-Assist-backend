# Issue Resolution Summary

This document explains, in plain English, the serious problems that existed in the Screening Assist codebase and how they were fixed. It is written for project managers, HR stakeholders, and clients. It focuses on business impact and the logic of each solution. Exact file names are listed so technical teams can verify the work.

No programming code is included in this document.

---

## 1. Secret passwords and API keys were stored inside the project files

### What was wrong
Live credentials for the company database, cloud email accounts, artificial intelligence services, and related systems were written directly into project files and saved in version control. Anyone who could read the repository could potentially access candidate personal data, send email as the company, use paid cloud services, or reach production systems.

### Business impact
This created a high risk of data breach, unauthorized email, unexpected cloud bills, and regulatory exposure around candidate personal information.

### Where the issue lived
- main.py (historical)
- config.py (historical; since removed)
- initialscreening.py (historical)

### How it was fixed
All secrets were removed from application source files. The application now reads sensitive values only from a private environment file or from the hosting environment. A sample environment template shows which values are required without containing real secrets. The old dedicated secrets file was deleted. Startup checks fail early if critical secrets are missing or still set to placeholder values.

### Files involved in the fix
- app/settings.py
- .env.example
- .gitignore
- main.py
- initialscreening.py
- config.py (deleted)
- README.md (rotation guidance for operators)

### Important follow-up for the business
Removing secrets from today’s files does not revoke keys that were already exposed in older history. Operations must rotate database, email, cloud, and AI credentials, then treat older copies of the repository as compromised until history cleanup is completed.

---

## 2. Almost every programming interface was open to the public

### What was wrong
API routes that read candidate personal data, run expensive AI screening, change job postings, update candidate status, and trigger company email could be called by anyone who knew the web address. There was no login gate on those routes.

### Business impact
Anonymous callers could view or alter hiring data, spam candidates or HR with email, burn AI budget, and disrupt the hiring pipeline.

### Where the issue lived
Route handlers historically concentrated in main.py; later split across router modules without auth until remediation.

### How it was fixed
Authentication was added across sensitive routes. HR and internal tools use an API key header or a signed HR token. Candidates receive a limited token after uploading a resume; that token only unlocks their own application and interview steps. A small set of intentional public endpoints remains for health checks, the public job board, and starting an application.

### Files involved in the fix
- app/auth.py
- app/routers/system.py
- app/routers/resumes.py
- app/routers/candidates.py
- app/routers/screening.py
- app/routers/jobs.py
- app/routers/feedback.py
- app/routers/files.py
- app/routers/email_api.py
- app/factory.py

---

## 3. Job updates could be abused to rewrite database records arbitrarily

### What was wrong
The job update interface accepted a free-form data dictionary and applied it directly as a database update. A hostile client could attempt to change fields the product never intended to expose, including dangerous database operators.

### Business impact
Job postings and related records could be corrupted or manipulated without HR authorization, undermining trust in the career site and internal tools.

### Where the issue lived
- Job update handling historically in main.py
- Later in app/routers/jobs.py before validation was tightened

### How it was fixed
Job updates now accept only an allow-listed set of fields. Extra or unexpected fields are rejected. The endpoint also requires HR authentication.

### Files involved in the fix
- app/schemas.py (JobUpdateRequest definition)
- app/routers/jobs.py

---

## 4. Artificial intelligence calls blocked the whole server under load

### What was wrong
AI calls to Gemini waited on the main request pathway in a blocking way. When more than a couple of users used screening features at once, requests queued behind each other. The server behaved as if it could only handle one heavy AI task at a time.

### Business impact
Slow or stalled applications during busy hiring periods, poor candidate experience, and unreliable HR tools during peak use.

### Where the issue lived
- gemini_utils.py (historical implementation)
- Call sites in screening and upload flows

### How it was fixed
AI work is moved onto background worker threads so the web server can keep accepting other requests. A concurrency limit prevents the system from opening unlimited simultaneous AI calls. Shared helper functions wrap this pattern for routers and services.

### Files involved in the fix
- app/llm/gemini_client.py
- app/services/screening.py
- app/routers/resumes.py
- app/routers/screening.py
- app/routers/jobs.py
- app/routers/email_api.py
- app/settings.py

---

## 5. The AI model name and thinking settings were unsafe or invalid

### What was wrong
Critical screening paths pointed at a model name that was not a valid production Gemini identifier. Thinking budget was effectively unbounded, which could cause unpredictable cost and latency.

### Business impact
Screenings might fail completely in real runs, or become unpredictably slow and expensive, blocking hiring decisions.

### Where the issue lived
- gemini_utils.py (historical)
- Related screening call paths

### How it was fixed
Defaults were corrected to known valid Gemini model identifiers, with environment overrides for operations teams. Thinking budget is capped to a sensible default. Timeouts and automatic retries were added for transient failures.

### Files involved in the fix
- app/llm/gemini_client.py
- app/llm/retry.py
- app/settings.py
- .env.example
- gemini_utils.py (now only a compatibility pointer to the new client)

---

## 6. Live interview sessions lived only in one server’s memory

### What was wrong
L1 chat interviews kept conversation state in a simple in-memory dictionary on a single process. Restarting or redeploying the server wiped active interviews. Running more than one server copy could not share those sessions reliably.

### Business impact
Candidates could lose mid-interview progress during a routine deploy. Horizontal scaling for growth was effectively blocked for live interviews.

### Where the issue lived
- In-process session dictionary historically in main.py / related modules

### How it was fixed
Session handling was redesigned. Live chat objects still sit in local memory for speed, but a serializable transcript can be stored in Redis. Another server instance can rebuild the interview from that transcript if the original process is gone. Time-to-live and size limits prevent unbounded growth.

### Files involved in the fix
- app/chat_sessions.py
- app/services/screening.py
- app/routers/screening.py
- app/settings.py
- .env.example

---

## 7. The application was one oversized file that mixed many concerns

### What was wrong
A large single entry file mixed routing, database access, AI calls, email, and configuration. That made reviews, testing, and safe changes difficult and increased the chance of repeating security mistakes.

### Business impact
Slower feature delivery, higher defect risk, and harder onboarding for new engineers supporting the hiring product.

### Where the issue lived
- main.py (historical monolith)

### How it was fixed
The application was reorganized into clear packages: settings, authentication, database, routers by business area, screening services, AI client, and email. The entry file only starts the application. Older import names were kept as thin compatibility layers so existing integrations do not break abruptly.

### Files involved in the fix
- main.py
- app/factory.py
- app/settings.py
- app/auth.py
- app/database.py
- app/schemas.py
- app/helpers.py
- app/routers/ (system, resumes, candidates, screening, jobs, feedback, files, email_api)
- app/services/screening.py
- app/llm/ package
- app/email/ package
- gemini_utils.py
- email_utils.py
- email_templates.py
- initialscreening.py

---

## 8. Candidate personal data routes were reachable without a candidate-specific gate

### What was wrong
Even after general restructuring, some candidate personal information and interview routes remained callable without proving the caller was that candidate or an authorized HR user.

### Business impact
Risk of personal data exposure and interview tampering between applicants.

### Where the issue lived
- app/routers/candidates.py
- app/routers/screening.py
- app/routers/resumes.py (cover letter upload)
- app/routers/email_api.py (apply notification)

### How it was fixed
Those routes now require a valid identity. Candidate tokens are checked against the candidate and job they belong to. HR credentials retain broad access for legitimate recruiting work. Resume upload issues a scoped candidate token in the response for the apply flow.

### Files involved in the fix
- app/auth.py
- app/routers/candidates.py
- app/routers/screening.py
- app/routers/resumes.py
- app/routers/email_api.py

---

## 9. Email templates and credentials were intertwined with insecure patterns

### What was wrong
Email sending mixed transport credentials with template rendering. Untrusted text could be inserted into HTML notifications without careful escaping. Failures were harder to observe as a product health signal.

### Business impact
Phishing-style content injection into emails, unreliable delivery during hiring campaigns, and harder support diagnosis when candidates do not receive messages.

### Where the issue lived
- email_utils.py (historical)
- email_templates.py (historical)

### How it was fixed
Email configuration comes only from settings. Templates escape user-provided values for HTML safety. Delivery results are tracked, with health reporting available to HR. Compatibility files re-export the new email package.

### Files involved in the fix
- app/email/config.py
- app/email/service.py
- app/email/templates.py
- app/email/results.py
- email_utils.py
- email_templates.py
- app/routers/system.py
- app/routers/email_api.py

---

## 10. Prompt files could fail silently or reload inconsistently

### What was wrong
System prompts that steer AI behavior could be missing, empty, or reloaded in inconsistent ways, leading to confusing AI failures during live screening.

### Business impact
Unreliable screening quality and opaque failures that look like “AI is broken” to recruiters and candidates.

### Where the issue lived
- sp_manager.py

### How it was fixed
Prompt loading validates that files exist and are not empty, supports controlled reload, and is safe for concurrent use. HR can list and reload prompts through authenticated admin endpoints.

### Files involved in the fix
- sp_manager.py
- app/routers/system.py
- app/llm/gemini_client.py
- prompts/ directory (content files consumed by the manager)

---

## 11. Initial screening helper still carried legacy risk patterns

### What was wrong
The older initial screening module had been a place where secrets and debug dumps could appear, and it was easy for callers to bypass the safer service path.

### Business impact
Confusion about which path was “official,” plus residual risk of leaking sensitive material through obsolete helpers.

### Where the issue lived
- initialscreening.py

### How it was fixed
The file is now a deprecated thin wrapper over the shared screening service. It no longer holds secrets or writes debug payload dumps. Callers are directed to the service layer.

### Files involved in the fix
- initialscreening.py
- app/services/screening.py

---

## Summary for stakeholders

| Theme | Outcome |
|-------|---------|
| Secrets in the repository | Removed from source; environment-only configuration |
| Open APIs | Auth and role checks on sensitive routes |
| Dangerous job edits | Allow-listed fields only, plus HR auth |
| Slow AI under load | Background execution with concurrency limits |
| Broken or unbounded AI settings | Valid models and capped thinking budget |
| Lost live interviews on restart | Redis-backed transcript rebuild option |
| Hard-to-maintain monolith | Modular application structure |
| Candidate data exposure | Scoped candidate tokens and access checks |
| Email reliability and safety | Settings-based transports, escaped templates, health visibility |

The product is substantially safer and more operable than the original codebase. Full production readiness still requires operational steps outside the code itself: rotating any previously exposed credentials, confirming email and database settings in each environment, and validating a complete hiring journey end to end with real candidate and HR users.
