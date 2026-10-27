from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from aap_assets_mask_content.contract import MaskContentFailure
from aap_workspace.configuration_policy import ConfigurationPolicyError
from app.config import settings
from app.api.v1.router import api_router
from app.api.v1.ws import router as ws_router, close_redis_pool as _close_ws_redis_pool
from app.api.v1.bug_reports import close_bug_reopen_redis_pool
from app.api.health import router as health_router
from app.core.logging import setup_logging
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.ratelimit import limiter
from app.middleware.audit import AuditMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.middleware.upload_body_limits import UploadBodyLimitMiddleware
from app.services.storage import storage_service
from app.services.ml_routing.contracts import RoutingError
from app.services.labeling_prediction_contract import ApplyPredictionResultError
from app.services.identity_contracts import IdentityBriefsUnavailable
from app.deps import get_db
from sqlalchemy.ext.asyncio import AsyncSession

import logging

logger = logging.getLogger(__name__)

setup_logging(level="DEBUG" if settings.debug else "INFO")


# v0.6.6 · Sentry 初始化（DSN 留空则完全不启用）
if settings.sentry_dsn:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

    def _sentry_before_send(event, hint):
        # 移除请求头中的 Authorization，避免 token 上报
        req = (event or {}).get("request") or {}
        headers = req.get("headers") or {}
        if isinstance(headers, dict):
            for k in list(headers.keys()):
                if k.lower() == "authorization":
                    headers[k] = "[REDACTED]"
        return event

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        before_send=_sentry_before_send,
        send_default_pii=False,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if (
        settings.environment == "production"
        and settings.secret_key == "dev-secret-change-in-production"
    ):
        raise RuntimeError(
            "PRODUCTION ENVIRONMENT DETECTED WITH DEFAULT SECRET KEY. "
            "Set SECRET_KEY to a strong random value in your .env file."
        )
    # v0.8.8 · production 环境未配置 SENTRY_DSN 时启动告警（不阻断启动，
    # 避免运维忘记填导致线上错误失踪）。
    if settings.environment == "production" and not settings.sentry_dsn:
        logger.warning(
            "Production environment has no SENTRY_DSN configured; "
            "error tracking is disabled. Set SENTRY_DSN in .env to enable."
        )
    from app.services.managed_inference_auth import initialize_default_credentials

    credential_states = initialize_default_credentials()
    if "invalid" in credential_states.values():
        logger.warning("Some managed inference credential stores are unavailable")
    storage_service.ensure_all_buckets()
    # v0.19.0 ADR-0044 · env 配置的 ML backend 启动时 upsert 成全局注册项 (source=env)。
    # 失败不阻断启动 (探测超时/DB 抖动仅告警)。
    try:
        from app.services.ml_backend_env_sync import sync_env_backends

        await sync_env_backends()
    except Exception:
        logger.warning("env ML backend sync failed at startup", exc_info=True)
    from app.services.original_playback_registry import PlaybackRegistry

    playback_registry = PlaybackRegistry()
    app.state.original_playback_registry = playback_registry
    playback_registry.start_reaper()
    try:
        yield
    finally:
        try:
            await playback_registry.shutdown()
        finally:
            try:
                await _close_ws_redis_pool()
                await close_bug_reopen_redis_pool()
            except Exception:
                pass


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def _routing_error_handler(_request: Request, exc: RoutingError):
    """Expose router rejections with their stable HTTP/error-code contract."""

    headers = (
        {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    )
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": exc.as_detail()},
        headers=headers,
    )


app.add_exception_handler(RoutingError, _routing_error_handler)


async def _mask_content_error_handler(_request: Request, _exc: MaskContentFailure):
    return JSONResponse(
        status_code=503,
        content={"detail": {"reason": "mask_storage_unavailable", "retryable": True}},
    )


app.add_exception_handler(MaskContentFailure, _mask_content_error_handler)


async def _identity_briefs_error_handler(
    _request: Request, _exc: IdentityBriefsUnavailable
):
    return JSONResponse(status_code=503, content={"detail": "身份资料服务暂不可用"})


app.add_exception_handler(IdentityBriefsUnavailable, _identity_briefs_error_handler)


async def _configuration_policy_error_handler(
    _request: Request, exc: ConfigurationPolicyError
):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


app.add_exception_handler(ConfigurationPolicyError, _configuration_policy_error_handler)


async def _apply_prediction_result_error_handler(
    _request: Request, exc: ApplyPredictionResultError
):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


app.add_exception_handler(
    ApplyPredictionResultError, _apply_prediction_result_error_handler
)


async def _request_validation_error_handler(
    request: Request, exc: RequestValidationError
):
    """Keep specialized 422 envelopes stable and omit credential input/context."""

    path = request.url.path
    if path in {
        "/api/v1/auth/me/password",
        "/api/v1/auth/reset-password",
        "/api/v1/auth/verify-email",
        "/api/v1/auth/register-open",
    }:
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"type": error["type"], "loc": error["loc"], "msg": error["msg"]}
                    for error in exc.errors()
                ]
            },
        )
    is_registry_write = request.method in {"POST", "PUT"} and path.startswith(
        "/api/v1/admin/ml-integrations/registry"
    )
    errors = exc.errors()
    gpu_errors = []
    if is_registry_write:
        for error in errors:
            location = tuple(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "GPU 资源配置无效"))
            gpu_fields = (
                "gpu_resource_id",
                "vram_budget_mb",
                "eviction_priority",
            )
            if any(field in location for field in gpu_fields) or any(
                field in message for field in gpu_fields
            ):
                gpu_errors.append((location, message))
    if not gpu_errors:
        return await request_validation_exception_handler(request, exc)

    diagnostics = []
    for location, message in gpu_errors:
        field = next(
            (
                candidate
                for candidate in (
                    "gpu_resource_id",
                    "vram_budget_mb",
                    "eviction_priority",
                )
                if candidate in location or candidate in message
            ),
            "gpu_resource_id",
        )
        diagnostics.append(
            {
                "code": "gpu_config_invalid",
                "level": "blocker",
                "message": message.removeprefix("Value error, "),
                "field": field,
                "resource_id": None,
                "backend_id": None,
            }
        )
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "error_code": "gpu_config_invalid",
                "message": diagnostics[0]["message"],
                "diagnostics": diagnostics,
            }
        },
    )


app.add_exception_handler(RequestValidationError, _request_validation_error_handler)

# 中间件注册顺序：先注册 → 后执行（dispatch 包装）。
# AuditMiddleware 在 CORS 之后注册，保证 CORS preflight 不被审计。
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(AuditMiddleware)
app.add_middleware(RequestIDMiddleware)
if settings.environment == "production" and not settings.cors_allow_origins:
    raise RuntimeError(
        "production 环境必须显式设置 CORS_ALLOW_ORIGINS（JSON 列表或逗号分隔）"
    )

# v0.8.8 · SecurityHeadersMiddleware production-only。
# 注册顺序在 CORSMiddleware 之前 → dispatch 后执行 → 写 HSTS/CSP 时
# CORS 头已就位，可以一并出站。
if settings.environment == "production":
    app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_origin_regex=settings.effective_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"]
    if settings.environment != "production"
    else settings.cors_allow_methods,
    allow_headers=["*"]
    if settings.environment != "production"
    else settings.cors_allow_headers,
    expose_headers=["Idempotency-Key"],
)

# Pure ASGI pre-parser guard: registered outermost so multipart/JSON parsing can
# never spool an unbounded selected upload before endpoint validation runs.
app.add_middleware(UploadBodyLimitMiddleware)

app.include_router(api_router, prefix="/api/v1")
app.include_router(ws_router)
app.include_router(health_router, prefix="/health", tags=["health"])

# Prometheus metrics — 仅供内部 scrape，不经过 AuditMiddleware
from prometheus_client import (  # noqa: E402
    make_asgi_app as _prom_app,
    CONTENT_TYPE_LATEST,
)
from starlette.responses import Response as StarletteResponse  # noqa: E402

_prom_asgi = _prom_app()


@app.get("/metrics", include_in_schema=False)
async def metrics(db: AsyncSession = Depends(get_db)):
    from prometheus_client import generate_latest
    from app.observability.mask_ai import refresh_mask_ai_inventory_safely

    await refresh_mask_ai_inventory_safely(db)
    return StarletteResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
