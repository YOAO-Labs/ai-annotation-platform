"""Production security headers middleware.

仅在 environment=="production" 时由 main.py 注册。
注册顺序应在 CORSMiddleware 之前（先注册→后执行→最外层最后写头），
确保 CORS preflight / 业务响应都被覆盖。

CSP script-src nonce 收紧（仅 HTML 路径走 Nginx 注入 nonce, API 响应不含 HTML
所以 script-src 直接收紧到 'self' + Turnstile 即可, 无需 nonce）.
CSP style-src 收紧：前端 TSX inline style 清零后, API 响应路径
style-src 也收紧为 'self'；HTML 出站路径由 Nginx 追加 style/script nonce.
  - HTML 出站 CSP (含 nonce) 由 infra/docker/nginx.conf 的 sub_filter + add_header 处理
  - API 响应 CSP 由本中间件处理: script-src / style-src 均已去 'unsafe-inline'
ADR-0010 已更新.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


_HSTS_MAX_AGE = 60 * 60 * 24 * 365

_CSP_DIRECTIVES = (
    "default-src 'self'; "
    "img-src 'self' data: blob: https:; "
    "style-src 'self'; "
    "script-src 'self' https://challenges.cloudflare.com; "
    "frame-src https://challenges.cloudflare.com; "
    "connect-src 'self' https: wss: ws:; "
    "font-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """写入 production-only 安全响应头。

    /metrics 由独立 ASGI 子应用挂载（main.py），不经过本中间件——这是有意设计：
    Prometheus 内网 scrape 不需要 HSTS / CSP。
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        response: Response = await call_next(request)

        response.headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={_HSTS_MAX_AGE}; includeSubDomains",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault("Content-Security-Policy", _CSP_DIRECTIVES)

        return response
