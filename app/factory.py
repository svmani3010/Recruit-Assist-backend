"""Application factory for Screening Assist."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.rate_limit import limiter
from app.routers import (
    analytics,
    auth_candidate,
    auth_hr,
    calendar_oauth,
    candidate_comms,
    candidate_mobile,
    candidate_profile_setup,
    candidate_region,
    candidates,
    credits,
    email_api,
    feedback,
    files,
    geo,
    interviews,
    jobs,
    notifications,
    profile_score,
    resumes,
    screening,
    system,
    settings_hr,
    whatsapp_webhook,
    technical_interview_postponement,
)
from app.rbac import router as rbac_router
from app.services import short_links
from app.settings import get_settings, require_runtime_secrets

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    require_runtime_secrets()
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        docs_url="/docs" if settings.debug or settings.environment != "production" else None,
        redoc_url=None,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if getattr(settings, "rbac_org_enabled", False):
        from app.rbac.http_middleware import RbacMatrixMiddleware

        app.add_middleware(RbacMatrixMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        """Return a string `detail` so clients can render it without crashing.

        FastAPI's default 422 body is a list of objects. React treats that as
        an invalid child (minified error #31) if the UI prints `data.detail`.
        """
        messages = []
        for err in exc.errors():
            msg = str(err.get("msg") or "Invalid input")
            if msg.lower().startswith("value error, "):
                msg = msg[13:]
            parts = [
                part
                for part in err.get("loc", ())
                if part not in ("body", "query", "path")
            ]
            loc = ".".join(str(part) for part in parts)
            if not loc and "field required" in msg.lower():
                msg = (
                    "Request body is missing or was not valid JSON. "
                    "Refresh the page and try saving again."
                )
            messages.append(f"{loc}: {msg}" if loc else msg)
        return JSONResponse(
            status_code=422,
            content={"detail": "; ".join(messages) or "Invalid request"},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        if isinstance(exc, HTTPException):
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=getattr(exc, "headers", None),
            )
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "detail": str(exc)},
        )

    for module in (
        system,
        resumes,
        candidates,
        screening,
        jobs,
        analytics,
        feedback,
        files,
        email_api,
        calendar_oauth,
        interviews,
        auth_candidate,
        auth_hr,
        settings_hr,
        candidate_profile_setup,
        candidate_mobile,
        candidate_comms,
        candidate_region,
        notifications,
        short_links,
        geo,
        whatsapp_webhook,
        rbac_router,
        profile_score,
        credits,
        technical_interview_postponement,
    ):
        app.include_router(module.router)
    
    # Mount priority router with /api prefix
    from app.routers import routes_priority
    app.include_router(routes_priority.router, prefix="/api")

    @app.on_event("startup")
    async def startup():
        from app.database import get_db
        from app.mobile_crypto import (
            ensure_encrypted_mobiles_collection,
            is_mobile_qe_ready,
        )

        db = get_db()
        db.command("ping")
        
        # OTP store — TTL on expiresAt; unique active subject+purpose
        db["otp_store"].create_index("expiresAt", expireAfterSeconds=0)
        db["otp_store"].create_index([("subject", 1), ("purpose", 1)], unique=True)
        # KV03 rate-limit event log (phone / IP / account)
        db["otp_send_events"].create_index("createdAt", expireAfterSeconds=60 * 60 * 48)
        db["otp_send_events"].create_index(
            [("phone_hash", 1), ("purpose", 1), ("createdAt", -1)]
        )
        db["otp_send_events"].create_index([("ip", 1), ("purpose", 1), ("createdAt", -1)])
        db["otp_send_events"].create_index(
            [("account_id", 1), ("purpose", 1), ("createdAt", -1)]
        )
        db["otp_flow_state"].create_index(
            [("subject", 1), ("purpose", 1)], unique=True
        )
        db["otp_verify_audit"].create_index("createdAt", expireAfterSeconds=60 * 60 * 24 * 90)
        db["otp_verify_audit"].create_index(
            [("candidate_id", 1), ("createdAt", -1)]
        )
        db["interviewInvites"].create_index("token", unique=True)
        db["interviewInvites"].create_index(
            [("status", 1), ("scheduled_date", 1)]
        )
        # KV140 E19 — Job Performance Analytics event stores
        try:
            from app.services.analytics import ensure_analytics_indexes

            ensure_analytics_indexes(db)
        except Exception:
            logger.exception("Failed creating analytics indexes")
        db["microsoft_oauth_states"].create_index("expires_at", expireAfterSeconds=0)
        db["microsoft_oauth_states"].create_index("state", unique=True)
        db["hr_microsoft_login_codes"].create_index("expires_at", expireAfterSeconds=0)
        db["hr_microsoft_login_codes"].create_index("code", unique=True)
        db["mobile_reveal_audit"].create_index([("created_at", -1)])
        db["mobile_reveal_audit"].create_index([("candidate_id", 1), ("created_at", -1)])
        # KV42 / KV58 — multi-channel communication log + short links
        db["communication_logs"].create_index([("candidate_id", 1), ("createdAt", -1)])
        db["communication_logs"].create_index([("event", 1), ("createdAt", -1)])
        db["communication_logs"].create_index(
            [("status", 1), ("fallback_after", 1)]
        )
        db["communication_logs"].create_index(
            [("status", 1), ("deliver_after", 1)]
        )
        db["communication_logs"].create_index([("dedupe_key", 1), ("channel", 1)])
        db["short_links"].create_index("code", unique=True)
        db["short_link_clicks"].create_index([("code", 1), ("createdAt", -1)])
        db["geo_cities_cache"].create_index("expiresAt", expireAfterSeconds=0)
        db["whatsapp_inbound"].create_index([("timestamp", -1)])
        # Phase 3 — anonymous candidate region sessions (cookie/session override)
        try:
            from app.services.candidate_region import ensure_region_session_indexes

            ensure_region_session_indexes()
        except Exception:
            logger.exception("Failed creating geo_region_sessions indexes")
            db["geo_region_sessions"].create_index("expiresAt", expireAfterSeconds=0)
            db["geo_region_sessions"].create_index("session_id", unique=True)

        if is_mobile_qe_ready():
            try:
                ensure_encrypted_mobiles_collection()
                from app.services.candidate_mobile_identity import (
                    ensure_mobile_uniqueness_indexes,
                )

                ensure_mobile_uniqueness_indexes()
                logger.info("Mobile Queryable Encryption collection ready")
            except Exception:
                logger.exception(
                    "Failed to initialize mobile QE collection — "
                    "duplicate checks will fall back where possible"
                )
        else:
            try:
                from app.services.candidate_mobile_identity import (
                    ensure_mobile_uniqueness_indexes,
                )

                ensure_mobile_uniqueness_indexes()
            except Exception:
                logger.exception("Failed creating legacy mobile uniqueness indexes")
            logger.warning(
                "Mobile QE not configured (MOBILE_QE_LOCAL_MASTER_KEY / KMS). "
                "Candidate phones remain plaintext until configured."
            )

        db["mobile_duplicate_audit"].create_index(
            "createdAt", expireAfterSeconds=60 * 60 * 24 * 90
        )
        db["mobile_duplicate_audit"].create_index(
            [("attempting_account_id", 1), ("createdAt", -1)]
        )
        db["mobile_duplicate_audit"].create_index(
            [("phone_hash", 1), ("createdAt", -1)]
        )

        try:
            from app.rbac import startup_rbac

            startup_rbac()
        except Exception:
            logger.exception("Failed RBAC startup (indexes/matrix seed)")
        
        # Technical Interview Postponement collections and indexes
        try:
            # technical_interview_postponements collection
            db["technical_interview_postponements"].create_index(
                [("applicationId", 1)], unique=True
            )
            db["technical_interview_postponements"].create_index(
                [("candidateId", 1), ("status", 1)]
            )
            db["technical_interview_postponements"].create_index(
                [("jobId", 1), ("status", 1)]
            )
            db["technical_interview_postponements"].create_index(
                [("deadlineAt", 1), ("status", 1)]
            )
            
            # interview_reminders collection
            db["interview_reminders"].create_index(
                [("applicationId", 1), ("status", 1)]
            )
            db["interview_reminders"].create_index(
                [("scheduledAt", 1), ("status", 1)]
            )
            db["interview_reminders"].create_index("scheduledAt", expireAfterSeconds=0)
            
            logger.info("Technical interview postponement collections initialized")
        except Exception:
            logger.exception("Failed to initialize technical interview postponement collections")
        
        # Priority listing system collections and indexes
        try:
            # subscriptions collection with unique employer_id index
            db["subscriptions"].create_index("employer_id", unique=True)
            db["subscriptions"].create_index("updated_at")
            
            # entitlement_cache collection with unique employer_id index
            db["entitlement_cache"].create_index("employer_id", unique=True)
            db["entitlement_cache"].create_index("cached_at")
            
            # sponsored_jobs collection for tracking boosted jobs
            db["sponsored_jobs"].create_index([("employer_id", 1), ("job_id", 1)], unique=True)
            db["sponsored_jobs"].create_index("job_id", unique=True)
            db["sponsored_jobs"].create_index("employer_id")
            
            # round_counters collection with unique query_footprint index
            db["round_counters"].create_index("query_footprint", unique=True)
            db["round_counters"].create_index("updated_at")
            
            # impressions collection for logging
            db["impressions"].create_index([("employer_id", 1), ("ts", -1)])
            db["impressions"].create_index([("query_footprint", 1), ("ts", -1)])
            db["impressions"].create_index("ts")
            
            # admin_sla_flags collection for SLA monitoring
            db["admin_sla_flags"].create_index([("employer_id", 1), ("date", -1)])
            db["admin_sla_flags"].create_index("date")

            from app.services.priority_list import ensure_priority_list

            ensure_priority_list(db, force=True)
            
            logger.info("Priority listing system collections initialized")
        except Exception:
            logger.exception("Failed to initialize priority listing collections")
        
        logger.info("%s started (env=%s)", settings.app_name, settings.environment)

    return app
