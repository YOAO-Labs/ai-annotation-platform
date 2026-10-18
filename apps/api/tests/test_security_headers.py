"""SecurityHeadersMiddleware CSP 收紧验证.

API 响应路径 script-src 已去 'unsafe-inline' (HTML 路径走 Nginx sub_filter 注入 nonce).
v0.10.12 起 style-src 也去 'unsafe-inline'.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.security_headers import SecurityHeadersMiddleware
from app.config import Settings


def _build_app() -> Starlette:
    async def hello(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/hello", hello)])
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def test_csp_script_src_drops_unsafe_inline():
    """API 响应 CSP script-src 不再含 'unsafe-inline'."""
    client = TestClient(_build_app())
    resp = client.get("/hello")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    # script-src 区块定位: 找 "script-src ..." 子串到下一 ";"
    script_src_idx = csp.find("script-src ")
    assert script_src_idx >= 0, csp
    script_src_end = csp.find(";", script_src_idx)
    script_src = csp[script_src_idx:script_src_end]
    assert "'unsafe-inline'" not in script_src, (
        f"script-src 仍含 unsafe-inline: {script_src}"
    )
    # Turnstile 仍允许
    assert "https://challenges.cloudflare.com" in script_src


def test_csp_style_src_drops_unsafe_inline():
    """API 响应 CSP style-src 不再含 'unsafe-inline'."""
    client = TestClient(_build_app())
    resp = client.get("/hello")
    csp = resp.headers["content-security-policy"]
    style_idx = csp.find("style-src ")
    assert style_idx >= 0
    style_end = csp.find(";", style_idx)
    style_src = csp[style_idx:style_end]
    assert "'unsafe-inline'" not in style_src
    assert style_src == "style-src 'self'"


def test_csp_contains_no_unsafe_inline():
    """API 响应 CSP 所有 directive 都不再放开 inline 执行."""
    client = TestClient(_build_app())
    resp = client.get("/hello")
    csp = resp.headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp


def test_other_security_headers_intact():
    """其他安全 header 保持不变."""
    client = TestClient(_build_app())
    resp = client.get("/hello")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "max-age=" in resp.headers.get("strict-transport-security", "")


def test_production_header_defaults_allow_idempotent_unlink_preflight():
    settings = Settings(_env_file=None, environment="development")
    app = _build_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://frontend.example.test"],
        allow_methods=["DELETE"],
        allow_headers=settings.cors_allow_headers,
    )
    response = TestClient(app).options(
        "/hello",
        headers={
            "Origin": "https://frontend.example.test",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "Authorization,Idempotency-Key",
        },
    )
    assert response.status_code == 200
    assert "idempotency-key" in response.headers["access-control-allow-headers"].lower()
