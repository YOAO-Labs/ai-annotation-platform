"""v0.11.19/21 · /api/v1/internal/metrics-targets — http_sd 服务发现端点。

覆盖三块:
  - _url_to_hostport 对 backend.url 各种形态的解析。
  - _check_token 在 token 未配置 / 已配置(命中/错配/缺失) 四种情况下的鉴权行为。
  - HTTP 端点端到端: disconnected 排除 + host:port 去重 + 响应 schema。
"""

from __future__ import annotations

import pytest


def test_url_to_hostport_variants() -> None:
    from app.api.v1.internal import _url_to_hostport

    assert _url_to_hostport("http://host:8080") == "host:8080"
    assert _url_to_hostport("https://host:8443/x") == "host:8443"
    assert _url_to_hostport("host:9000") == "host:9000"
    assert _url_to_hostport("host") == "host"
    assert _url_to_hostport("") is None


def test_check_token_no_token_configured(monkeypatch) -> None:
    """token 为空时直接放行, 不读 Authorization。"""
    from app.api.v1 import internal

    monkeypatch.setattr(internal.settings, "metrics_sd_token", "")
    internal._check_token(None)
    internal._check_token("Bearer anything")


def test_check_token_valid_and_invalid(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.api.v1 import internal

    monkeypatch.setattr(internal.settings, "metrics_sd_token", "secret-abc")
    internal._check_token("Bearer secret-abc")

    for bad in (None, "", "Bearer wrong", "secret-abc", "Token secret-abc"):
        with pytest.raises(HTTPException) as exc:
            internal._check_token(bad)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_metrics_targets_excludes_disconnected_and_dedupes(
    httpx_client, db_session, monkeypatch
) -> None:
    from app.api.v1 import internal
    from app.db.models.ml_backend_registry import MLBackendRegistry

    monkeypatch.setattr(internal.settings, "metrics_sd_token", "")

    # v0.19.0 ADR-0044 · backend 是全局注册项 (url unique), 不再按项目拆分。
    # 两个 distinct url 解析到同一 host:port → 仍按 host:port 兜底去重为 1。
    db_session.add_all(
        [
            MLBackendRegistry(
                name="sam3",
                url="http://sam3-backend:8080",
                state="connected",
            ),
            MLBackendRegistry(
                name="sam3",
                url="http://sam3-backend:8080/v2",
                state="connected",
            ),
            MLBackendRegistry(
                name="grounded-sam2",
                url="http://gs2:8001",
                state="error",
            ),
            MLBackendRegistry(
                name="ignored",
                url="http://offline:9000",
                state="disconnected",
            ),
        ]
    )
    await db_session.flush()

    res = await httpx_client.get("/api/v1/internal/metrics-targets")
    assert res.status_code == 200
    body = res.json()

    targets = {entry["targets"][0]: entry["labels"] for entry in body}
    assert "offline:9000" not in targets
    assert targets["sam3-backend:8080"]["service"] == "sam3"
    assert targets["gs2:8001"]["service"] == "grounded-sam2"
    assert len(body) == 2  # 同 host:port 去重


@pytest.mark.asyncio
async def test_metrics_targets_token_gate(
    httpx_client, db_session, super_admin, monkeypatch
) -> None:
    from app.api.v1 import internal

    monkeypatch.setattr(internal.settings, "metrics_sd_token", "tk-1")

    no_auth = await httpx_client.get("/api/v1/internal/metrics-targets")
    assert no_auth.status_code == 401

    wrong = await httpx_client.get(
        "/api/v1/internal/metrics-targets",
        headers={"Authorization": "Bearer nope"},
    )
    assert wrong.status_code == 401

    ok = await httpx_client.get(
        "/api/v1/internal/metrics-targets",
        headers={"Authorization": "Bearer tk-1"},
    )
    assert ok.status_code == 200
