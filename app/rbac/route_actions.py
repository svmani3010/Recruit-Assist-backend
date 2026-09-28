"""Map HTTP method + path → Recruitment matrix action (Yes/No check)."""

from __future__ import annotations

import re
from typing import Optional

from app.rbac import constants as C

# First match wins. Paths are regexes against request.url.path.
# Keep public careers / candidate booking routes UNMAPPED so they stay open.
# Only map specific endpoints that need permission checks - be conservative to avoid blocking legitimate APIs
_ROUTE_ACTIONS: list[tuple[str, str, str]] = [
    # Data / audit export
    ("GET", r"^/api/rbac/export(/|$)", C.ACTION_AUDIT_EXPORT),
    ("POST", r"^/api/rbac/export(/|$)", C.ACTION_AUDIT_EXPORT),
    ("GET", r"^/api/audit/export(/|$)", C.ACTION_AUDIT_EXPORT),
    ("POST", r"^/api/audit/export(/|$)", C.ACTION_AUDIT_EXPORT),
    ("GET", r"^/api/audit(/|$)", C.ACTION_AUDIT_VIEW),
    
    # Team and Role Management
    ("POST", r"^/api/rbac/members/assign-role$", C.ACTION_TEAM_CHANGE_ROLE),
    ("POST", r"^/api/rbac/members/invite$", C.ACTION_TEAM_INVITE),
    ("GET", r"^/api/rbac/members$", C.ACTION_TEAM_CHANGE_ROLE),
    ("POST", r"^/api/rbac/members/revoke$", C.ACTION_TEAM_DEACTIVATE),
    ("GET", r"^/api/rbac/matrix$", C.ACTION_TEAM_CHANGE_ROLE),
    ("GET", r"^/api/rbac/notifications$", C.ACTION_WHOAMI),
    
    # Organization Settings
    ("GET", r"^/api/recruit/account$", C.ACTION_WHOAMI),
    ("PATCH", r"^/api/recruit/account$", C.ACTION_WHOAMI),
    ("GET", r"^/api/recruit/company$", C.ACTION_DASHBOARD_VIEW),
    ("PATCH", r"^/api/recruit/company$", C.ACTION_ORG_SETTINGS),
    ("GET", r"^/api/recruit/application-policy$", C.ACTION_JOBS_READ),
    ("PATCH", r"^/api/recruit/application-policy$", C.ACTION_ORG_SETTINGS),
    ("PUT", r"^/api/recruit/application-policy$", C.ACTION_ORG_SETTINGS),
    ("POST", r"^/api/recruit/application-policy$", C.ACTION_ORG_SETTINGS),
    ("GET", r"^/api/recruit/kyc", C.ACTION_ORG_SETTINGS),
    ("POST", r"^/api/recruit/kyc", C.ACTION_ORG_SETTINGS),
    ("POST", r"^/api/hr/branding/upload$", C.ACTION_ORG_SETTINGS),
    ("GET", r"^/api/hr/onboarding-questions$", C.ACTION_ADMIN_MANAGE_TEMPLATES),
    ("POST", r"^/api/hr/onboarding-questions$", C.ACTION_ADMIN_MANAGE_TEMPLATES),
    
    # Dashboard and Reports
    ("GET", r"^/api/recruit/dashboard/stats$", C.ACTION_DASHBOARD_VIEW),
    ("GET", r"^/api/reports/funnel", C.ACTION_REPORTS_FUNNEL_PIPELINE),
    ("GET", r"^/api/reports/team", C.ACTION_REPORTS_TEAM_REPORTS),
    ("GET", r"^/api/reports/location", C.ACTION_REPORTS_APPLICANT_LOCATION),
    ("GET", r"^/api/reports/notification-delivery", C.ACTION_DASHBOARD_NOTIFICATION_DELIVERY),
    
    # Jobs — create / publish / delete / update (more specific patterns)
    ("POST", r"^/api/jobs$", C.ACTION_JOBS_CREATE),
    ("POST", r"^/api/recruit/jobs/draft$", C.ACTION_JOBS_CREATE),
    ("POST", r"^/api/recruit/jobs/[^/]+/publish$", C.ACTION_JOBS_PUBLISH),
    ("POST", r"^/api/recruit/jobs/[^/]+/unpublish$", C.ACTION_JOBS_UNPUBLISH),
    ("DELETE", r"^/api/recruit/jobs/draft/[^/]+$", C.ACTION_JOBS_DELETE),
    ("DELETE", r"^/api/jobs/", C.ACTION_JOBS_DELETE),
    ("PATCH", r"^/api/jobview/[^/]+/show$", C.ACTION_JOBS_UPDATE),
    ("PATCH", r"^/api/jobview/[^/]+$", C.ACTION_JOBS_UPDATE),
    ("POST", r"^/api/jobview/[^/]+/mark-viewed$", C.ACTION_JOBS_READ),
    ("PATCH", r"^/api/recruit/jobs/", C.ACTION_JOBS_UPDATE),
    ("PUT", r"^/api/recruit/jobs/", C.ACTION_JOBS_UPDATE),
    ("POST", r"^/api/upd/jobs$", C.ACTION_JOBS_UPDATE),
    ("POST", r"^/api/generate-job-description$", C.ACTION_JOBS_AI_GENERATE),
    ("POST", r"^/api/generate-job-keywords$", C.ACTION_JOBS_AI_GENERATE),
    ("POST", r"^/api/jobs/[^/]+/screening-questions$", C.ACTION_JOBS_CONFIG_SCREENING_QS),
    ("POST", r"^/api/jobs/[^/]+/weightage$", C.ACTION_JOBS_CONFIG_WEIGHTAGE),
    ("POST", r"^/api/jobs/[^/]+/submit-approval$", C.ACTION_JOBS_SUBMIT_APPROVAL),
    ("POST", r"^/api/jobs/[^/]+/share$", C.ACTION_JOBS_SHARE),
    ("GET", r"^/api/question-bank", C.ACTION_JOBS_MANAGE_QUESTION_BANK),
    ("POST", r"^/api/question-bank", C.ACTION_JOBS_MANAGE_QUESTION_BANK),
    
    # Jobs — list / dashboard (exact prefixes; do NOT match /api/jobviewmain)
    ("GET", r"^/api/recruit/jobs/active$", C.ACTION_JOBS_LIST),
    ("GET", r"^/api/recruit/jobs/drafts$", C.ACTION_JOBS_LIST),
    ("GET", r"^/api/recruit/jobs(/|$)", C.ACTION_JOBS_LIST),
    ("GET", r"^/api/recruit/dashboard/stats$", C.ACTION_DASHBOARD_VIEW),
    # KV140–KV149 — Job Performance Analytics (analytics.view)
    ("GET", r"^/api/recruit/analytics/definitions$", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/dashboard$", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/jobs/", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/sources$", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/funnel", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/time$", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/insights$", C.ACTION_ANALYTICS_VIEW),
    ("POST", r"^/api/recruit/analytics/insights/dismiss$", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/saved-views", C.ACTION_ANALYTICS_VIEW),
    ("POST", r"^/api/recruit/analytics/saved-views$", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/export$", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/refresh$", C.ACTION_ANALYTICS_VIEW),
    ("POST", r"^/api/recruit/analytics/refresh$", C.ACTION_ANALYTICS_VIEW),
    ("GET", r"^/api/recruit/analytics/reconcile$", C.ACTION_ANALYTICS_VIEW),
    ("POST", r"^/api/recruit/analytics/offers$", C.ACTION_ANALYTICS_VIEW),
    ("POST", r"^/api/recruit/analytics/hires$", C.ACTION_ANALYTICS_VIEW),
    ("POST", r"^/api/recruit/analytics/stage-exits$", C.ACTION_ANALYTICS_VIEW),
    ("POST", r"^/api/recruit/analytics/job-events$", C.ACTION_ANALYTICS_VIEW),
    # Public analytics event ingestion intentionally unmapped (open):
    # POST /api/analytics/job-views, POST /api/analytics/featured-events
    ("GET", r"^/api/jobview$", C.ACTION_JOBS_LIST),
    ("GET", r"^/api/jobview/$", C.ACTION_JOBS_LIST),
    
    # Candidate Management (re-enabled with permission checks)
    ("GET", r"^/api/candidates/[^/]+$", C.ACTION_CANDIDATES_VIEW_LIST),
    ("GET", r"^/api/candidates$", C.ACTION_CANDIDATES_VIEW_LIST),
    ("POST", r"^/api/candidates/[^/]+/mobile/reveal$", C.ACTION_CANDIDATES_VIEW_CONTACT),
    ("POST", r"^/api/candidates/[^/]+/mobile/release$", C.ACTION_CANDIDATES_VIEW_CONTACT),
    ("GET", r"^/api/candidate_scores_with_info/", C.ACTION_CANDIDATES_VIEW_AI_SCORE),
    ("GET", r"^/api/candidate_scores/", C.ACTION_CANDIDATES_VIEW_AI_SCORE),
    ("GET", r"^/api/manual_candidates/", C.ACTION_CANDIDATES_VIEW_LIST),
    ("GET", r"^/api/candidate/[^/]+$", C.ACTION_CANDIDATES_VIEW_LIST),
    ("GET", r"^/api/candidate_by_id/", C.ACTION_CANDIDATES_VIEW_LIST),
    ("GET", r"^/api/candidates/[^/]+/assessment", C.ACTION_CANDIDATES_VIEW_ASSESSMENT),
    ("POST", r"^/api/candidates/[^/]+/notes$", C.ACTION_CANDIDATES_NOTES_ADD),
    ("PATCH", r"^/api/candidates/[^/]+/notes/[^/]+$", C.ACTION_CANDIDATES_NOTES_EDIT_DELETE),
    ("DELETE", r"^/api/candidates/[^/]+/notes/[^/]+$", C.ACTION_CANDIDATES_NOTES_EDIT_DELETE),
    ("POST", r"^/api/candidates/[^/]+/approve$", C.ACTION_CANDIDATES_APPROVE_REJECT),
    ("POST", r"^/api/candidates/[^/]+/reject$", C.ACTION_CANDIDATES_APPROVE_REJECT),
    ("GET", r"^/api/candidates/shortlisted", C.ACTION_CANDIDATES_VIEW_SHORTLISTED),
    ("POST", r"^/api/candidates/bulk-action$", C.ACTION_CANDIDATES_BULK_ACTIONS),
    ("POST", r"^/api/candidates/export$", C.ACTION_CANDIDATES_EXPORT),
    ("POST", r"^/api/candidates/[^/]+/request-info$", C.ACTION_CANDIDATES_REQUEST_INFO),
    
    # Interview Management
    ("GET", r"^/api/interviews/job/", C.ACTION_INTERVIEWS_SCHEDULE),
    ("POST", r"^/api/interviews/invite$", C.ACTION_INTERVIEWS_SCHEDULE),
    ("POST", r"^/api/interviews/reschedule$", C.ACTION_INTERVIEWS_SCHEDULE),
    ("POST", r"^/api/interviews/[^/]+/outcome$", C.ACTION_INTERVIEWS_SUBMIT_FEEDBACK),
    ("POST", r"^/api/interviews/[^/]+/assign-panel$", C.ACTION_INTERVIEWS_ASSIGN_PANEL),
    ("POST", r"^/api/interviews/availability$", C.ACTION_INTERVIEWS_SET_AVAILABILITY),
    ("GET", r"^/api/interviews/[^/]+/recordings", C.ACTION_INTERVIEWS_VIEW_RECORDINGS),
    ("POST", r"^/api/interviews/[^/]+/cancel$", C.ACTION_INTERVIEWS_CANCEL),
    
    # Communication
    ("POST", r"^/api/communication/whatsapp$", C.ACTION_COMMUNICATION_WHATSAPP),
    ("POST", r"^/api/communication/voice-call$", C.ACTION_COMMUNICATION_VOICE_CALL),
    ("GET", r"^/api/communication/timeline/[^/]+$", C.ACTION_COMMUNICATION_VIEW_TIMELINE),
    ("GET", r"^/api/communication/escalation", C.ACTION_COMMUNICATION_MANAGE_ESCALATION),
    ("POST", r"^/api/communication/templates$", C.ACTION_COMMUNICATION_CONFIG_TEMPLATES),
    ("PATCH", r"^/api/communication/templates/[^/]+$", C.ACTION_COMMUNICATION_CONFIG_TEMPLATES),
    
    # Offer Management
    ("POST", r"^/api/offers/send$", C.ACTION_OFFERS_SEND),
    ("POST", r"^/api/offers/[^/]+/withdraw$", C.ACTION_OFFERS_WITHDRAW),
    
    # Onboarding
    ("POST", r"^/api/onboarding/release-form$", C.ACTION_ONBOARDING_RELEASE_FORM),
    ("GET", r"^/api/onboarding/submissions", C.ACTION_ONBOARDING_VIEW_SUBMISSIONS),
    
    # BGV Management
    ("POST", r"^/api/bgv/initiate$", C.ACTION_BGV_INITIATE),
    ("POST", r"^/api/bgv/[^/]+/assign-vendor$", C.ACTION_BGV_ASSIGN_VENDOR),
    ("POST", r"^/api/bgv/[^/]+/request-document$", C.ACTION_BGV_REQUEST_DOCUMENT),
    ("GET", r"^/api/bgv/[^/]+/documents$", C.ACTION_BGV_CHECK_DOWNLOAD),
    ("POST", r"^/api/bgv/[^/]+/status$", C.ACTION_BGV_UPDATE_STATUS),
    ("POST", r"^/api/bgv/[^/]+/attachment$", C.ACTION_BGV_ADD_STATUS_ATTACHMENT),
    ("GET", r"^/api/bgv/[^/]+/report$", C.ACTION_BGV_VIEW_REPORT),
    ("GET", r"^/api/bgv/[^/]+/outcome$", C.ACTION_BGV_VIEW_OUTCOME),
    ("POST", r"^/api/bgv/[^/]+/accept$", C.ACTION_BGV_ACCEPT_CLOSE),
    ("POST", r"^/api/bgv/[^/]+/close$", C.ACTION_BGV_ACCEPT_CLOSE),
    
    # IT and Admin Operations
    ("POST", r"^/api/it/create-emp-id$", C.ACTION_IT_CREATE_EMP_ID),
    ("POST", r"^/api/it/create-email$", C.ACTION_IT_CREATE_EMAIL),
    ("POST", r"^/api/it/create-system-access$", C.ACTION_IT_CREATE_SYSTEM_ACCESS),
    ("POST", r"^/api/it/allocate-project$", C.ACTION_IT_ALLOCATE_PROJECT),
    ("POST", r"^/api/it/map-roles$", C.ACTION_IT_MAP_ROLES),
    ("POST", r"^/api/it/clone-roles$", C.ACTION_IT_CLONE_ROLES),
    
    # Recruiter notification feed only (candidate uses /api/notifications/candidate)
    ("GET", r"^/api/notifications/hr$", C.ACTION_DASHBOARD_VIEW),
    ("POST", r"^/api/notifications/hr/read-all$", C.ACTION_DASHBOARD_VIEW),
    ("PATCH", r"^/api/notifications/[^/]+/read$", C.ACTION_DASHBOARD_VIEW),
    
    # Identity / whoami — always allowed for authenticated org members
    ("GET", r"^/api/auth/whoami$", C.ACTION_WHOAMI),
    ("GET", r"^/api/rbac/me$", C.ACTION_WHOAMI),
    ("GET", r"^/api/rbac/capabilities$", C.ACTION_WHOAMI),
    ("GET", r"^/api/rbac/can$", C.ACTION_WHOAMI),
    
    # /auth/hr/status is intentionally unmapped — it bootstraps org/session
    # and must run before matrix context exists.
]


def resolve_action(method: str, path: str) -> Optional[str]:
    method = (method or "GET").upper()
    path = path or ""
    for m, pattern, action in _ROUTE_ACTIONS:
        if m != method:
            continue
        if re.search(pattern, path):
            return action
    return None
