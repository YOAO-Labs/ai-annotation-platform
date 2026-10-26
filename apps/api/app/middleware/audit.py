from __future__ import annotations

import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.config import settings
from app.db.base import async_session
from app.middleware.request_id import request_id_var
from app.services.audit import append_http_metadata, extract_client_ip

logger = logging.getLogger(__name__)

_API_PREFIX = "/api/v1"
_WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
# 跳过的路径：登录 body 含明文密码，注册体含密码且接口本身会显式打点
_SKIP_PATHS = {
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/me/password",
}


class AuditMiddleware(BaseHTTPMiddleware):
    """
    全自动写请求审计：
      - 仅匹配 /api/v1 + 写方法（POST/PATCH/PUT/DELETE）
      - 在 call_next 之后写入，绝不阻塞主响应
      - v0.7.6：默认走所选异步任务队列，投递失败时回退同步
      - 任何异常被吞掉，仅 logger.warning，永不影响响应
      - actor 仅取认证依赖写入的 Identity principal，不自行解析凭据
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        path = request.url.path
        method = request.method

        if (
            method not in _WRITE_METHODS
            or not path.startswith(_API_PREFIX)
            or path in _SKIP_PATHS
        ):
            return response

        try:
            payload = _build_payload(request, response.status_code)
            if settings.audit_async:
                # selected queue delivery; broker failure falls back to sync
                if not _enqueue_audit(payload):
                    await _persist_audit_sync(payload)
            else:
                await _persist_audit_sync(payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("AuditMiddleware persist failed: %s", exc)

        return response


def _build_payload(request: Request, status_code: int) -> dict:
    actor_id = None
    actor_role = None
    principal = getattr(request.state, "identity_principal", None)
    if principal is not None:
        actor_id = principal.actor_id
        actor_role = principal.role

    rid = request_id_var.get()
    return {
        "actor_id": str(actor_id) if actor_id else None,
        "actor_role": actor_role,
        "action": f"http.{request.method.lower()}",
        "method": request.method,
        "path": path_short(request.url.path),
        "status_code": status_code,
        "ip": extract_client_ip(request),
        "request_id": rid if rid and len(rid) <= 36 else None,
    }


def _enqueue_audit(payload: dict) -> bool:
    """Send HTTP metadata to the selected queue; return False for sync fallback."""
    try:
        if settings.jobs_queue_url:
            from app.services.jobs_queue import dispatch_job_sync

            dispatch_job_sync(
                task_name="app.workers.audit.persist_audit_entry",
                execution_id=str(uuid.uuid4()),
                args=[payload],
                options={"queue": "audit"},
            )
        else:
            from app.workers.audit import persist_audit_entry

            persist_audit_entry.delay(payload)
        return True
    except Exception as exc:
        logger.warning("audit task enqueue failed, falling back to sync: %s", exc)
        return False


async def _persist_audit_sync(payload: dict) -> None:
    async with async_session() as session:
        try:
            await append_http_metadata(session, payload)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def path_short(path: str) -> str:
    if len(path) <= 256:
        return path
    return path[:255] + "…"
