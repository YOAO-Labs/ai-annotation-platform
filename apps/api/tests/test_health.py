"""health 端点 + CORS 配置守卫。"""

from __future__ import annotations


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


def test_cors_settings_dev_default():
    """dev 环境保留三 origin + localhost regex（行为零变化）。"""
    from app.config import Settings

    s = Settings(environment="development")
    assert "http://localhost:5173" in s.cors_allow_origins
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


def test_cors_origins_parses_comma_string():
    """env 用逗号分隔字符串也能解析为 list。"""
    from app.config import Settings

    s = Settings(cors_allow_origins="https://a.example,https://b.example")
    assert s.cors_allow_origins == ["https://a.example", "https://b.example"]


def test_cors_origins_parses_json_string():
    """env 用 JSON list 字符串也能解析为 list。"""
    from app.config import Settings

    s = Settings(cors_allow_origins='["https://a.example","https://b.example"]')
    assert s.cors_allow_origins == ["https://a.example", "https://b.example"]
