from __future__ import annotations

import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match
from starlette.types import ASGIApp

from app.observability.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
)

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

HEADER_NAME = "X-Request-ID"

_METRIC_PREFIX = "/api/"


def _metric_route_path(request: Request) -> str:
    """Return a bounded route template, never a UUID-bearing request path."""
    route = request.scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(template, str) and template.startswith(_METRIC_PREFIX):
        return template

    partial_template: str | None = None
    app = request.scope.get("app")
    routes = getattr(getattr(app, "router", None), "routes", ())
    for candidate in routes:
        match, _ = candidate.matches(request.scope)
        candidate_template = getattr(candidate, "path_format", None) or getattr(
            candidate, "path", None
        )
        if not isinstance(candidate_template, str) or not candidate_template.startswith(
            _METRIC_PREFIX
        ):
            continue
        if match is Match.FULL:
            return candidate_template
        if match is Match.PARTIAL and partial_template is None:
            partial_template = candidate_template
    if partial_template is not None:
        return partial_template
    return "/api/unmatched"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    1. 生成/透传 X-Request-ID → ContextVar，审计日志用
    2. 记录 Prometheus HTTP 计数 + 延迟（仅 /api/ 路径）
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get(HEADER_NAME) or uuid.uuid4().hex
        request_id_var.set(rid)

        start = time.monotonic()
        response: Response = await call_next(request)
        elapsed = time.monotonic() - start

        response.headers[HEADER_NAME] = rid

        if request.url.path.startswith(_METRIC_PREFIX):
            path = _metric_route_path(request)
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method,
                path=path,
                status_code=str(response.status_code),
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=request.method,
                path=path,
            ).observe(elapsed)

        return response
