"""Centralized configuration loaded exclusively from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import List

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application
    app_name: str = Field(default="Screening Assist", alias="APP_NAME")
    environment: str = Field(default="development", alias="ENVIRONMENT")
    debug: bool = Field(default=False, alias="DEBUG")

    # Auth (required)
    api_key: str = Field(..., alias="API_KEY")
    jwt_secret: str = Field(..., alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_expire_minutes: int = Field(default=480, alias="JWT_EXPIRE_MINUTES")

    # URLs / CORS (required — set per environment in .env)
    cors_origins: str = Field(..., alias="CORS_ORIGINS")
    frontend_url: str = Field(..., alias="FRONTEND_URL")

    # Google OAuth / Identity (required)
    google_client_id: str = Field(..., alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(..., alias="GOOGLE_CLIENT_SECRET")
    google_oauth_redirect_uri: str = Field(..., alias="GOOGLE_OAUTH_REDIRECT_URI")
    # Optional GIS web client used by the frontend Sign-In button. Must match
    # VITE_GOOGLE_CLIENT_ID when it differs from GOOGLE_CLIENT_ID (calendar).
    google_login_client_id: str = Field(
        default="499915320768-sb6j4g267q8apc6q3h4ma64iho2ml1d2.apps.googleusercontent.com",
        alias="GOOGLE_LOGIN_CLIENT_ID",
    )

    # MongoDB (required)
    mongodb_uri: str = Field(..., alias="MONGODB_URI")
    mongodb_db: str = Field(default="screeningassist", alias="MONGODB_DB")
    mongodb_max_pool_size: int = Field(default=50, alias="MONGODB_MAX_POOL_SIZE")
    mongodb_min_pool_size: int = Field(default=5, alias="MONGODB_MIN_POOL_SIZE")

    # Gemini (required)
    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")
    prompt_dir: str = Field(default="prompts", alias="PROMPT_DIR")
    prompt_auto_reload: bool = Field(default=True, alias="PROMPT_AUTO_RELOAD")
    gemini_model_flash: str = Field(default="gemini-3.1-flash-lite", alias="GEMINI_MODEL_FLASH")
    gemini_model_pro: str = Field(default="gemini-3.1-flash-lite", alias="GEMINI_MODEL_PRO")
    gemini_thinking_budget: int = Field(default=2048, alias="GEMINI_THINKING_BUDGET")
    gemini_max_retries: int = Field(default=3, alias="GEMINI_MAX_RETRIES")
    gemini_timeout_seconds: float = Field(default=90.0, alias="GEMINI_TIMEOUT_SECONDS")
    gemini_max_concurrency: int = Field(default=4, alias="GEMINI_MAX_CONCURRENCY")

    # Session / Redis
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    chat_session_ttl_seconds: int = Field(default=3600, alias="CHAT_SESSION_TTL_SECONDS")
    chat_session_max_entries: int = Field(default=500, alias="CHAT_SESSION_MAX_ENTRIES")

    # Multi-tenant org RBAC (dual-path scaffold). When false, HR login and
    # endpoints keep the legacy JWT + hr_user_id bucket behaviour.
    rbac_org_enabled: bool = Field(default=False, alias="RBAC_ORG_ENABLED")
    rbac_session_ttl_seconds: int = Field(
        default=0, alias="RBAC_SESSION_TTL_SECONDS"
    )  # 0 → fall back to jwt_expire_minutes * 60
    rbac_role_cache_ttl_seconds: int = Field(
        default=60, alias="RBAC_ROLE_CACHE_TTL_SECONDS"
    )

    # Rate limiting
    rate_limit_public: str = Field(default="60/minute", alias="RATE_LIMIT_PUBLIC")
    # Global request rate‑limit toggle (affects all @limiter.limit decorators)
    rate_limit_enabled: bool = Field(default=False, alias="RATE_LIMIT_ENABLED")
    # OTP feature toggles
    otp_features_enabled: bool = Field(default=False, alias="OTP_FEATURES_ENABLED")
    otp_rate_limit_enabled: bool = Field(default=False, alias="OTP_RATE_LIMIT_ENABLED")
    otp_resend_cooldown_enabled: bool = Field(default=False, alias="OTP_RESEND_COOLDOWN_ENABLED")
    otp_verify_lockout_enabled: bool = Field(default=False, alias="OTP_VERIFY_LOCKOUT_ENABLED")


    # Email / notifications
    email_enabled: bool = Field(default=True, alias="EMAIL_ENABLED")
    # SMTP settings (commented out — Mailgun is now the active provider)
    # smtp_mode: bool = Field(default=False, alias="SMTP")
    # smtp_server: str = Field(default="smtp.gmail.com", alias="SMTP_SERVER")
    # smtp_port: int = Field(default=587, alias="SMTP_PORT")
    # smtp_username: str = Field(default="", alias="SMTP_USERNAME")
    # smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    hr_emails: str = Field(default="", alias="HR_EMAILS")
    sender_email: str = Field(default="", alias="SENDER_EMAIL")
    sender_name: str = Field(default="Screening Assist System", alias="SENDER_NAME")
    default_meeting_link: str = Field(..., alias="DEFAULT_MEETING_LINK")

    # Mailgun (active email provider)
    mailgun_api_key: str = Field(default="", alias="MAILGUN_API_KEY")
    mailgun_domain: str = Field(default="", alias="MAILGUN_DOMAIN")
    mailgun_base_url: str = Field(default="https://api.mailgun.net", alias="MAILGUN_BASE_URL")
    mailgun_from_email: str = Field(default="", alias="MAILGUN_FROM_EMAIL")

    # Google Calendar (HR OAuth)
    google_calendar_timezone: str = Field(..., alias="GOOGLE_CALENDAR_TIMEZONE")

    # Microsoft identity + Graph Calendar / Teams (optional — required for
    # Microsoft login on recruiter and candidate portals).
    microsoft_client_id: str = Field(default="", alias="MICROSOFT_CLIENT_ID")
    microsoft_client_secret: str = Field(default="", alias="MICROSOFT_CLIENT_SECRET")
    microsoft_tenant_id: str = Field(default="organizations", alias="MICROSOFT_TENANT_ID")
    # Candidate login uses "common" so personal Microsoft accounts work.
    microsoft_candidate_tenant_id: str = Field(
        default="common",
        alias="MICROSOFT_CANDIDATE_TENANT_ID",
    )
    microsoft_login_redirect_uri: str = Field(
        default="",
        alias="MICROSOFT_LOGIN_REDIRECT_URI",
    )
    microsoft_candidate_login_redirect_uri: str = Field(
        default="",
        alias="MICROSOFT_CANDIDATE_LOGIN_REDIRECT_URI",
    )
    microsoft_oauth_redirect_uri: str = Field(
        default="",
        alias="MICROSOFT_OAUTH_REDIRECT_URI",
    )

    # LibreOffice
    libreoffice_path: str = Field(default="soffice", alias="LIBREOFFICE_PATH")

    # WhatsApp OTP (KV03) — replaces SMS for candidate mobile verification
    whatsapp_otp_provider: str = Field(default="console", alias="WHATSAPP_OTP_PROVIDER")
    whatsapp_otp_fallback_provider: str = Field(
        default="console", alias="WHATSAPP_OTP_FALLBACK_PROVIDER"
    )
    whatsapp_access_token: str = Field(default="", alias="WHATSAPP_ACCESS_TOKEN")
    whatsapp_phone_number_id: str = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_api_version: str = Field(default="v21.0", alias="WHATSAPP_API_VERSION")
    whatsapp_sender_id: str = Field(default="", alias="WHATSAPP_SENDER_ID")
    whatsapp_template_id: str = Field(default="", alias="WHATSAPP_TEMPLATE_ID")
    whatsapp_template_language: str = Field(default="en", alias="WHATSAPP_TEMPLATE_LANGUAGE")
    whatsapp_otp_template: str = Field(
        default=(
            "{OTP} is your Kovetted verification code. "
            "Valid for 10 minutes. Do not share this code with anyone."
        ),
        alias="WHATSAPP_OTP_TEMPLATE",
    )
    # WhatsApp Cloud API Webhooks
    whatsapp_verify_token: str = Field(default="", alias="WHATSAPP_VERIFY_TOKEN")
    whatsapp_app_secret: str = Field(default="", alias="WHATSAPP_APP_SECRET")
    
    # Comma-separated dial codes without +. Empty = allow all countries.
    # Re-restrict later (e.g. "91,1,44") once WhatsApp availability is verified per market.
    whatsapp_supported_country_codes: str = Field(
        default="",
        alias="WHATSAPP_SUPPORTED_COUNTRY_CODES",
    )
    # HMAC key for OTP rate-limit / store indexes (never store plaintext phone there)
    otp_phone_hmac_key: str = Field(default="", alias="OTP_PHONE_HMAC_KEY")

    # Candidate notifications (KV42) — SMS / short links
    sms_provider: str = Field(default="console", alias="SMS_PROVIDER")
    sms_api_url: str = Field(default="", alias="SMS_API_URL")
    sms_api_key: str = Field(default="", alias="SMS_API_KEY")
    sms_sender_id: str = Field(default="KOVETD", alias="SMS_SENDER_ID")
    short_link_base_url: str = Field(default="", alias="SHORT_LINK_BASE_URL")
    public_api_url: str = Field(default="", alias="PUBLIC_API_URL")
    # Optional map: event=templateName,event2=templateName2
    whatsapp_notify_templates: str = Field(default="", alias="WHATSAPP_NOTIFY_TEMPLATES")

    # Candidate mobile — MongoDB Queryable Encryption (KV encrypted storage)
    mobile_qe_enabled: bool = Field(default=True, alias="MOBILE_QE_ENABLED")
    mobile_qe_kms_provider: str = Field(default="local", alias="MOBILE_QE_KMS_PROVIDER")
    # Base64 of 96 random bytes — DEV/local provider ONLY. Production: use AWS KMS.
    mobile_qe_local_master_key: str = Field(default="", alias="MOBILE_QE_LOCAL_MASTER_KEY")
    mobile_qe_key_alt_name: str = Field(
        default="candidate-mobile-v1", alias="MOBILE_QE_KEY_ALT_NAME"
    )
    mobile_qe_key_version: int = Field(default=1, alias="MOBILE_QE_KEY_VERSION")
    mobile_qe_collection: str = Field(
        default="candidate_mobiles", alias="MOBILE_QE_COLLECTION"
    )
    mobile_qe_key_vault_namespace: str = Field(
        default="", alias="MOBILE_QE_KEY_VAULT_NAMESPACE"
    )
    aws_kms_key_arn: str = Field(default="", alias="AWS_KMS_KEY_ARN")
    aws_kms_region: str = Field(default="", alias="AWS_KMS_REGION")
    aws_access_key_id: str = Field(default="", alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str = Field(default="", alias="AWS_SECRET_ACCESS_KEY")
    aws_session_token: str = Field(default="", alias="AWS_SESSION_TOKEN")

    @field_validator(
        "email_enabled",
        "debug",
        "prompt_auto_reload",
        "mobile_qe_enabled",
        "rbac_org_enabled",
        mode="before",
    )
    @classmethod
    def parse_bool(cls, value):
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def hr_email_list(self) -> List[str]:
        return [e.strip() for e in self.hr_emails.split(",") if e.strip()]

    @property
    def google_id_token_audiences(self) -> List[str]:
        audiences: List[str] = []
        for value in (self.google_client_id, self.google_login_client_id):
            client_id = str(value or "").strip().strip('"').strip("'")
            if client_id and client_id not in audiences:
                audiences.append(client_id)
        return audiences


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # Export OTP feature toggles for legacy imports
    OTP_FEATURES_ENABLED = settings.otp_features_enabled
    OTP_RATE_LIMIT_ENABLED = settings.otp_rate_limit_enabled
    OTP_RESEND_COOLDOWN_ENABLED = settings.otp_resend_cooldown_enabled
    OTP_VERIFY_LOCKOUT_ENABLED = settings.otp_verify_lockout_enabled
    # Export global rate‑limit flag
    RATE_LIMIT_ENABLED = settings.rate_limit_enabled
    return settings


def require_runtime_secrets() -> None:
    """Fail fast if critical secrets are missing or look like placeholders."""
    settings = get_settings()
    missing = []
    for name, value in (
        ("API_KEY", settings.api_key),
        ("JWT_SECRET", settings.jwt_secret),
        ("MONGODB_URI", settings.mongodb_uri),
        ("GEMINI_API_KEY", settings.gemini_api_key),
        ("GOOGLE_CLIENT_ID", settings.google_client_id),
        ("GOOGLE_CLIENT_SECRET", settings.google_client_secret),
        ("GOOGLE_OAUTH_REDIRECT_URI", settings.google_oauth_redirect_uri),
        ("FRONTEND_URL", settings.frontend_url),
        ("CORS_ORIGINS", settings.cors_origins),
        ("DEFAULT_MEETING_LINK", settings.default_meeting_link),
        ("GOOGLE_CALENDAR_TIMEZONE", settings.google_calendar_timezone),
    ):
        if not value or str(value).startswith("CHANGE_ME"):
            missing.append(name)

    if settings.email_enabled:
        for name, value in (
            ("MAILGUN_API_KEY", settings.mailgun_api_key),
            ("MAILGUN_DOMAIN", settings.mailgun_domain),
            ("SENDER_EMAIL", settings.sender_email),
            ("HR_EMAILS", settings.hr_emails),
        ):
            if not value or str(value).startswith("CHANGE_ME"):
                missing.append(name)

    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and set real values."
        )
