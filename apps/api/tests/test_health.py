"""health 端点 + CORS 配置守卫。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


DEV_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:5173",
]


def test_health_subroutes_registered(app_module):
    """/health/{db,redis,minio,celery} + 聚合 /health 都已注册。"""
    paths = [r.path for r in app_module.routes]
    assert "/health/db" in paths
    assert "/health/redis" in paths
    assert "/health/minio" in paths
    assert "/health/celery" in paths
    assert "/health" in paths


def test_check_celery_no_workers(monkeypatch):
    """celery_app.control.inspect().stats() 返回 None 时 status='error'。"""
    from app.api import health

    class _FakeInspect:
        def stats(self, *a, **kw):
            return None

    class _FakeControl:
        def inspect(self, *a, **kw):
            return _FakeInspect()

    monkeypatch.setattr(health.celery_app, "control", _FakeControl())
    result = health._check_celery()
    assert result["status"] == "error"
    assert result["active_count"] == 0
    assert result["workers"] == []


def test_check_celery_with_workers(monkeypatch):
    """celery_app.control.inspect().stats() 返回 worker dict 时 status='ok'。

    workers 包含 name、last_heartbeat_seconds_ago 和 pool_max；
    未发现 broker 队列时，queues 为空。"""
    from app.api import health

    class _FakeInspect:
        def stats(self, *a, **kw):
            return {
                "celery@host1": {"pool": {"max-concurrency": 4}},
                "celery@host2": {"pool": {"max-concurrency": 2}},
            }

    class _FakeControl:
        def inspect(self, *a, **kw):
            return _FakeInspect()

    monkeypatch.setattr(health.celery_app, "control", _FakeControl())
    monkeypatch.setattr(health, "_read_celery_queue_lengths", lambda: {})
    result = health._check_celery()
    assert result["status"] == "ok"
    assert result["active_count"] == 2
    assert {w["name"] for w in result["workers"]} == {"celery@host1", "celery@host2"}
    assert "queues" in result


@pytest.mark.parametrize("environment", ["development", "staging"])
def test_cors_settings_local_defaults(monkeypatch, environment):
    from app.config import Settings

    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ALLOW_ORIGIN_REGEX", raising=False)
    s = Settings(_env_file=None, environment=environment)
    assert s.cors_allow_origins == DEV_ORIGINS
    assert s.effective_cors_origin_regex == r"http://localhost:\d+"


def test_cors_settings_production_disables_regex():
    """production 强制 regex=None（即使 env 显式给）。"""
    from app.config import Settings

    s = Settings(
        environment="production",
        cors_allow_origins=["https://app.example.com"],
        cors_allow_origin_regex=r"http://localhost:\d+",
    )
    assert s.effective_cors_origin_regex is None
    assert s.cors_allow_origins == ["https://app.example.com"]


@pytest.mark.parametrize("source", ["env", "dotenv"])
@pytest.mark.parametrize(
    "value, expected",
    [
        (
            " https://a.example, , https://b.example ",
            ["https://a.example", "https://b.example"],
        ),
        (
            '["https://a.example","https://b.example"]',
            ["https://a.example", "https://b.example"],
        ),
        ("[]", []),
        ("", []),
    ],
)
def test_cors_origins_parses_settings_sources(
    monkeypatch, tmp_path, source, value, expected
):
    from app.config import Settings

    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    env_file = None
    if source == "env":
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", value)
    else:
        env_file = tmp_path / ".env"
        env_file.write_text(f"CORS_ALLOW_ORIGINS='{value}'\n", encoding="utf-8")

    s = Settings(_env_file=env_file, environment="development")
    assert s.cors_allow_origins == expected
    assert "cors_allow_origins" in s.model_fields_set


@pytest.mark.parametrize(
    "value, accepted",
    [
        pytest.param(None, False, id="missing"),
        pytest.param("", False, id="empty"),
        pytest.param(" , ", False, id="empty-csv"),
        pytest.param("[]", False, id="empty-json"),
        pytest.param('[" "]', False, id="blank-json-item"),
        pytest.param(json.dumps(DEV_ORIGINS), True, id="explicit-dev-defaults-json"),
        pytest.param(",".join(DEV_ORIGINS), True, id="explicit-dev-defaults-csv"),
    ],
)
def test_production_api_startup_requires_explicit_cors_origins(value, accepted):
    env = {
        "ENVIRONMENT": "production",
        "SECRET_KEY": "isolated-test-secret-never-used-to-sign",
        "CORS_ALLOW_ORIGIN_REGEX": r"http://localhost:\d+",
    }
    if value is not None:
        env["CORS_ALLOW_ORIGINS"] = value
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from fastapi.testclient import TestClient

from app import config

config.settings = config.Settings(_env_file=None)
from app.main import app

client = TestClient(app)
for origin, status in (("http://localhost:5173", 200), ("http://localhost:9999", 400)):
    response = client.options("/health", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "GET",
    })
    assert response.status_code == status
    assert response.headers.get("access-control-allow-origin") == (
        origin if status == 200 else None
    )
""",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if accepted:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "production 环境必须显式设置 CORS_ALLOW_ORIGINS" in result.stderr, (
            result.stderr
        )
        assert env["SECRET_KEY"] not in result.stderr


def test_smtp_password_file_supplies_effective_settings(monkeypatch, tmp_path):
    from app.config import Settings

    secret = tmp_path / "smtp-password"
    secret.write_text(" fixture-password \r\n", encoding="utf-8")
    monkeypatch.setenv("SMTP_PASSWORD_FILE", str(secret))
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    s = Settings(
        _env_file=None,
        smtp_host="smtp.example.test",
        smtp_port=587,
        smtp_from="noreply@example.test",
    )
    assert s.smtp_configured
    assert s.smtp_password == " fixture-password "
    assert "fixture-password" not in repr(s)


@pytest.mark.parametrize(
    "content, message",
    [(None, "无法读取 SMTP_PASSWORD_FILE"), ("\n", "SMTP_PASSWORD_FILE 不能为空")],
)
def test_smtp_password_file_fails_closed(monkeypatch, tmp_path, content, message):
    from app.config import Settings

    secret = tmp_path / "smtp-password"
    if content is not None:
        secret.write_text(content, encoding="utf-8")
    monkeypatch.setenv("SMTP_PASSWORD_FILE", str(secret))
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match=message) as exc:
        Settings(_env_file=None)
    assert str(secret) not in str(exc.value)


def test_smtp_password_rejects_ambiguous_sources(monkeypatch, tmp_path):
    from app.config import Settings

    monkeypatch.setenv("SMTP_PASSWORD", "fixture-password")
    monkeypatch.setenv("SMTP_PASSWORD_FILE", str(tmp_path / "smtp-password"))
    with pytest.raises(
        RuntimeError, match="SMTP_PASSWORD 和 SMTP_PASSWORD_FILE 只能设置一项"
    ) as exc:
        Settings(_env_file=None)
    assert "fixture-password" not in str(exc.value)
