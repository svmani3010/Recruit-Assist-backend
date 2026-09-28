"""RBAC constants: modules, roles, Yes/No scopes, consumer-domain blocklist."""

from __future__ import annotations

_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
    "protonmail.com",
    "aol.com",
    "live.com",
    "yandex.com",
    "rediffmail.com",
}

MODULE_RECRUITMENT = "recruitment"

# Seven roles from the permission sheet (exact ids)
ROLE_SUPER_ADMIN = "super_admin"
ROLE_CONTENT_ADMIN = "content_admin"
ROLE_MANAGER = "manager"
ROLE_USER = "user"
ROLE_IT_ADMIN_OPS = "it_admin_ops"
ROLE_BGV_VENDOR = "bgv_vendor"
ROLE_VIEWER = "viewer"

# Back-compat alias (old matrix used recruiter → now User)
ROLE_RECRUITER = ROLE_USER

ADMIN_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_CONTENT_ADMIN})

ALL_RECRUITMENT_ROLES = frozenset(
    {
        ROLE_SUPER_ADMIN,
        ROLE_CONTENT_ADMIN,
        ROLE_MANAGER,
        ROLE_USER,
        ROLE_IT_ADMIN_OPS,
        ROLE_BGV_VENDOR,
        ROLE_VIEWER,
    }
)

# Yes / No only (legacy scope names still accepted when reading old docs)
SCOPE_YES = "yes"
SCOPE_NONE = "none"  # No
SCOPE_ORG = SCOPE_YES  # legacy alias
SCOPE_TEAM = SCOPE_YES
SCOPE_OWN = SCOPE_YES
SCOPE_ASSIGNED = SCOPE_YES
SCOPE_CONFIG = SCOPE_YES

ALL_SCOPES = frozenset({SCOPE_YES, SCOPE_NONE})

ORG_STATUS_PROVISIONAL = "provisional"
ORG_STATUS_VERIFIED = "verified"
ORG_STATUS_PENDING_REVIEW = "pending_review"

MEMBERSHIP_ACTIVE = "active"
MEMBERSHIP_PENDING = "pending"

AUDIT_PERMISSION_REFUSED = "permission_refused"
AUDIT_ROLE_ADMIN_CHANGE = "role_admin_change"
AUDIT_MEMBERSHIP_PENDING = "membership_pending"
AUDIT_ORG_CREATED = "org_created"
AUDIT_ORG_CREATE_REQUESTED = "org_create_requested"
AUDIT_SESSION_REVOKED = "session_revoked"
AUDIT_TECHNICAL_INTERVIEW_TOGGLE = "technical_interview_toggle"

# Technical Interview Postponement Audit Events
AUDIT_TECHNICAL_INTERVIEW_POSTPONED = "technical_interview_postponed"
AUDIT_TECHNICAL_INTERVIEW_STARTED_AFTER_POSTPONEMENT = "technical_interview_started_after_postponement"
AUDIT_TECHNICAL_INTERVIEW_DEADLINE_EXTENDED = "technical_interview_deadline_extended"
AUDIT_TECHNICAL_INTERVIEW_REOPENED = "technical_interview_reopened"
AUDIT_TECHNICAL_INTERVIEW_NOT_ATTENDED = "technical_interview_not_attended"
AUDIT_TECHNICAL_INTERVIEW_AUTO_REJECTED = "technical_interview_auto_rejected"
AUDIT_TECHNICAL_INTERVIEW_POSTPONEMENT_CANCELLED = "technical_interview_postponement_cancelled"
AUDIT_TECHNICAL_INTERVIEW_REMINDER_SENT = "technical_interview_reminder_sent"

# Dashboard and Reports
ACTION_DASHBOARD_VIEW = "dashboard.org_view"
ACTION_DASHBOARD_NOTIFICATION_DELIVERY = "dashboard.notification_delivery"
ACTION_REPORTS_FUNNEL_PIPELINE = "reports.funnel_pipeline"
ACTION_REPORTS_TEAM_REPORTS = "reports.team_reports"
ACTION_REPORTS_APPLICANT_LOCATION = "reports.applicant_location"
ACTION_REPORTS_EXPORT = "reports.export"
ACTION_REPORTS_VIEW = "reports.view"
ACTION_ANALYTICS_VIEW = "analytics.view"
ACTION_OFFERS_MANAGE = "offers.manage"
ACTION_BGV_CONTROL = "bgv.control"
ACTION_BGV_ASSIGNED = "bgv.assigned"

# Request Management
ACTION_REQUESTS_VIEW_OWN = "requests.view_own"
ACTION_REQUESTS_RAISE = "requests.raise"
ACTION_REQUESTS_CANCEL = "requests.cancel"
ACTION_REQUESTS_APPROVE_REJECT = "requests.approve_reject"

# Job Management
ACTION_JOBS_LIST = "dashboard.org_view"
ACTION_JOBS_READ = "jobs.draft"
ACTION_JOBS_CREATE = "jobs.draft"
ACTION_JOBS_UPDATE = "jobs.draft"
ACTION_JOBS_DELETE = "jobs.delete_permanently"
ACTION_JOBS_PUBLISH = "jobs.publish"
ACTION_JOBS_UNPUBLISH = "jobs.unpublish"
ACTION_JOBS_AI_GENERATE = "jobs.ai_generate"
ACTION_JOBS_CONFIG_SCREENING_QS = "jobs.config_screening_qs"
ACTION_JOBS_CONFIG_WEIGHTAGE = "jobs.config_weightage"
ACTION_JOBS_CONFIG_TECHNICAL_INTERVIEW = "jobs.config_technical_interview"
ACTION_JOBS_SUBMIT_APPROVAL = "jobs.submit_approval"
ACTION_JOBS_SHARE = "jobs.share"
ACTION_JOBS_MANAGE_QUESTION_BANK = "jobs.manage_question_bank"

# Technical Interview Postponement Management
ACTION_TECHNICAL_INTERVIEW_EXTEND = "technical_interview.extend"
ACTION_TECHNICAL_INTERVIEW_REOPEN = "technical_interview.reopen"
ACTION_TECHNICAL_INTERVIEW_REJECT = "technical_interview.reject"

# Candidate Management
ACTION_CANDIDATES_VIEW_LIST = "candidates.view_list"
ACTION_CANDIDATES_VIEW_CONTACT = "candidates.view_contact"
ACTION_CANDIDATES_VIEW_AI_SCORE = "candidates.view_ai_score"
ACTION_CANDIDATES_VIEW_ASSESSMENT = "candidates.view_assessment"
ACTION_CANDIDATES_NOTES_ADD = "candidates.notes_add"
ACTION_CANDIDATES_NOTES_EDIT_DELETE = "candidates.notes_edit_delete"
ACTION_CANDIDATES_APPROVE_REJECT = "candidates.approve_reject"
ACTION_CANDIDATES_VIEW_SHORTLISTED = "candidates.view_shortlisted"
ACTION_CANDIDATES_BULK_ACTIONS = "candidates.bulk_actions"
ACTION_CANDIDATES_EXPORT = "candidates.export"
ACTION_CANDIDATES_REQUEST_INFO = "candidates.request_info"

# Interview Management
ACTION_INTERVIEWS_SCHEDULE = "interviews.schedule"
ACTION_INTERVIEWS_ASSIGN_PANEL = "interviews.assign_panel"
ACTION_INTERVIEWS_SET_AVAILABILITY = "interviews.set_availability"
ACTION_INTERVIEWS_SUBMIT_FEEDBACK = "interviews.submit_feedback"
ACTION_INTERVIEWS_VIEW_RECORDINGS = "interviews.view_recordings"
ACTION_INTERVIEWS_CANCEL = "interviews.cancel"

# Communication
ACTION_COMMUNICATION_WHATSAPP = "communication.whatsapp"
ACTION_COMMUNICATION_VOICE_CALL = "communication.voice_call"
ACTION_COMMUNICATION_VIEW_TIMELINE = "communication.view_timeline"
ACTION_COMMUNICATION_MANAGE_ESCALATION = "communication.manage_escalation"
ACTION_COMMUNICATION_CONFIG_TEMPLATES = "communication.config_templates"
ACTION_COMMUNICATION_RECEIVE_ALERTS = "communication.receive_alerts"

# Offer Management
ACTION_OFFERS_SEND = "offers.send"
ACTION_OFFERS_WITHDRAW = "offers.withdraw"

# Onboarding
ACTION_ONBOARDING_RELEASE_FORM = "onboarding.release_form"
ACTION_ONBOARDING_VIEW_SUBMISSIONS = "onboarding.view_submissions"

# BGV Management
ACTION_BGV_INITIATE = "bgv.initiate"
ACTION_BGV_ASSIGN_VENDOR = "bgv.assign_vendor"
ACTION_BGV_REQUEST_DOCUMENT = "bgv.request_document"
ACTION_BGV_CHECK_DOWNLOAD = "bgv.check_download"
ACTION_BGV_UPDATE_STATUS = "bgv.update_status"
ACTION_BGV_ADD_STATUS_ATTACHMENT = "bgv.add_status_attachment"
ACTION_BGV_VIEW_REPORT = "bgv.view_report"
ACTION_BGV_VIEW_OUTCOME = "bgv.view_outcome"
ACTION_BGV_ACCEPT_CLOSE = "bgv.accept_close"

# IT and Admin
ACTION_IT_CREATE_EMP_ID = "it.create_emp_id"
ACTION_IT_CREATE_EMAIL = "it.create_email"
ACTION_IT_CREATE_SYSTEM_ACCESS = "it.create_system_access"
ACTION_IT_ALLOCATE_PROJECT = "it.allocate_project"
ACTION_IT_MAP_ROLES = "it.map_roles"
ACTION_IT_CLONE_ROLES = "it.clone_roles"
ACTION_ADMIN_MANAGE_TEMPLATES = "admin.manage_templates"

# Team Management
ACTION_TEAM_INVITE = "team.invite"
ACTION_TEAM_APPROVE_INVITE = "team.approve_invite"
ACTION_TEAM_CHANGE_ROLE = "team.change_role"
ACTION_TEAM_DEACTIVATE = "team.deactivate"
ACTION_TEAM_BULK_ASSIGN_ROLES = "team.bulk_assign_roles"
ACTION_TEAM_SET_MODULE_ROLES = "team.set_module_roles"

# Organization Settings
ACTION_ORG_SETTINGS = "org.settings"
ACTION_ORG_MANAGE_DOMAINS = "org.manage_domains"
ACTION_ORG_CONFIG_SCORE_MATRIX = "org.config_score_matrix"
ACTION_ORG_CONFIG_CREDITS = "org.config_credits"
ACTION_ORG_MANAGE_INTEGRATIONS = "org.manage_integrations"
ACTION_ORG_MANAGE_BILLING = "org.manage_billing"
ACTION_ORG_KILL_SWITCH = "org.kill_switch"

# Admin and Security
ACTION_ADMIN_TRANSFER_SA = "admin.transfer_sa"
ACTION_ADMIN_GRANT_BGV_ACCESS = "admin.grant_bgv_access"
ACTION_ADMIN_DELEGATE_PERMS = "admin.delegate_perms"

# Audit and Privacy
ACTION_AUDIT_VIEW = "audit.view"
ACTION_AUDIT_EXPORT = "audit.export"
ACTION_PRIVACY_HANDLE_DSR = "privacy.handle_dsr"
ACTION_PRIVACY_PLAY_RECORDINGS = "privacy.play_recordings"
ACTION_PRIVACY_DELETE_RECORD = "privacy.delete_record"
ACTION_SECURITY_ACCESS_REVIEW = "security.access_review"

# Authentication and Identity
ACTION_WHOAMI = "auth.signin_hr"
ACTION_AUTH_SIGNIN_BGV = "auth.signin_bgv"
ACTION_AUTH_SIGNIN_SSO = "auth.signin_sso"
ACTION_AUTH_2FA_REQUIRED = "auth.2fa_required"

# Legacy mappings for backward compatibility
ACTION_MEMBERS_INVITE = "team.invite"
ACTION_MEMBERS_ASSIGN_ROLE = "team.change_role"
ACTION_DATA_EXPORT = "reports.export"
ACTION_OFFERS_MANAGE = "offers.send"
ACTION_BGV_CONTROL = "bgv.initiate"
ACTION_BGV_ASSIGNED = "bgv.assign_vendor"
ACTION_REQUESTS_APPROVE = "requests.approve_reject"
ACTION_REQUESTS_REJECT = "requests.approve_reject"
ACTION_PROVISIONING_RUN = "it.create_system_access"

DENY_CONTACT_MESSAGE = (
    "You cannot move forward until you have been granted access. "
    "Please contact your Super Admin, Manager, or HR team for assistance."
)

CONSUMER_EMAIL_DOMAINS = frozenset(d.lower() for d in _PERSONAL_EMAIL_DOMAINS) | {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.in",
    "yahoo.co.uk",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "protonmail.com",
    "proton.me",
    "aol.com",
    "yandex.com",
    "mail.com",
    "gmx.com",
    "gmx.net",
    "rediffmail.com",
    "zoho.com",
}

COL_ORGANIZATIONS = "organizations"
COL_MEMBERSHIPS = "memberships"
COL_MODULE_ROLE_MAPPINGS = "module_role_mappings"
COL_PERMISSION_MATRIX = "permission_matrix"
COL_TEAMS = "teams"
COL_TEAM_MEMBERSHIPS = "team_memberships"
COL_AUDIT_LOGS = "audit_logs"
COL_ORG_CREATE_REQUESTS = "org_create_requests"
COL_RBAC_SESSIONS = "rbac_sessions"
COL_RBAC_ROLE_CACHE = "rbac_role_cache"
COL_RBAC_NOTIFICATIONS = "rbac_notifications"
COL_ANALYTICS_EVENTS = "analytics_events"
COL_TECHNICAL_INTERVIEW_POSTPONEMENTS = "technical_interview_postponements"
COL_INTERVIEW_REMINDERS = "interview_reminders"

# Technical Interview Postponement Platform Defaults
POSTPONE_TECHNICAL_INTERVIEW_ENABLED = True
DEFAULT_POSTPONEMENT_WINDOW_HOURS = 48
DEFAULT_MAX_POSTPONEMENTS = 1
DEFAULT_EXTENSION_MAX_HOURS = 72
DEFAULT_REMINDER_INTERVAL_HOURS = 12
DEFAULT_FINAL_REMINDER_HOURS = 2
DEFAULT_QUIET_HOURS_START = 22  # 10 PM
DEFAULT_QUIET_HOURS_END = 8    # 8 AM
DEFAULT_AUTO_REJECTION_AFTER_NOT_ATTENDED = False

REDIS_SESSION_PREFIX = "session:"
REDIS_ROLE_PREFIX = "role:"

DEFAULT_ROLE_CACHE_TTL_SECONDS = 60
