"""conftest 测试库解析/守卫的纯规则测试（不触数据库）。

保护点（计划 §5.3-8 / P0 C8 缺口）：
- 显式 TEST_DATABASE_URL 优先，且同样必须过目标校验（不能借显式覆盖绕过守卫）；
- app 配置不可用时明确报错，绝不回退到硬编码历史默认连接串；
- 解析目标必须是 postgresql + `_test` 后缀的一次性测试库；
- 用户可见错误不回显原始连接串/底层异常文本，异常链被抑制，
  避免畸形 URL 中的凭据泄漏。
"""

from __future__ import annotations

import pytest

from tests.conftest import _resolve_test_db_url, _validate_test_db_target


class _ExplodingSettings:
    @property
    def effective_migration_database_url(self) -> str:
        raise RuntimeError("settings boom")


class _MalformedSettings:
    effective_migration_database_url = "not-a-url%%%"


def _use_broken_settings(monkeypatch: pytest.MonkeyPatch, stub: object) -> None:
    import app.config

    monkeypatch.setattr(app.config, "settings", stub)


def _force_derived_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉显式覆盖，强制走 settings 派生路径（test 模式启动器会注入该变量）。"""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)


def test_explicit_env_override_wins(monkeypatch: pytest.MonkeyPatch):
    url = "postgresql+asyncpg://owner:pw@localhost:5433/aap_wt_abcdef_test"
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    _use_broken_settings(monkeypatch, _ExplodingSettings())  # settings must not be read
    assert _resolve_test_db_url() == url


def test_explicit_override_cannot_bypass_target_guard(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://user:pw@localhost:5432/aap_wt_x_dev"
    )
    with pytest.raises(RuntimeError, match="_test"):
        _resolve_test_db_url()


def test_derived_url_fixes_annotation_test_and_keeps_password(
    monkeypatch: pytest.MonkeyPatch,
):
    import app.config
    from types import SimpleNamespace

    _force_derived_resolution(monkeypatch)
    monkeypatch.setattr(
        app.config,
        "settings",
        SimpleNamespace(
            effective_migration_database_url=(
                "postgresql+asyncpg://migrator:s3cret@db.internal:5433/aap_wt_abc_dev"
            )
        ),
    )
    resolved = _resolve_test_db_url()
    assert resolved.endswith("annotation_test")
    # render_as_string(hide_password=False)：派生串必须保留真实密码，否则连不上。
    assert "s3cret" in resolved
    assert "db.internal:5433" in resolved


def test_settings_failure_raises_without_fallback(monkeypatch: pytest.MonkeyPatch):
    _force_derived_resolution(monkeypatch)
    _use_broken_settings(monkeypatch, _ExplodingSettings())
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL"):
        _resolve_test_db_url()


def test_malformed_settings_url_raises_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    _force_derived_resolution(monkeypatch)
    _use_broken_settings(monkeypatch, _MalformedSettings())
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL"):
        _resolve_test_db_url()


@pytest.mark.parametrize(
    "bad_url",
    [
        "postgresql+asyncpg://user:pw@localhost:5432/annotation_dev",  # 非 _test 后缀
        "postgresql+asyncpg://user:pw@localhost:5432/annotation",  # 无后缀
        "postgresql+asyncpg://user:pw@localhost:5432/",  # 空库名
        "sqlite+aiosqlite:///./t_test.db",  # 非 postgres 驱动
        "postgresql+asyncpg://user:sup3rsecret@host:notaport/db_test",  # 畸形 URL
    ],
)
def test_invalid_targets_rejected(bad_url: str):
    with pytest.raises(RuntimeError):
        _validate_test_db_target(bad_url)


def test_malformed_url_error_does_not_leak_credentials():
    bad = "postgresql+asyncpg://user:sup3rsecret@host:notaport/db_test"
    with pytest.raises(RuntimeError) as excinfo:
        _validate_test_db_target(bad)
    message = str(excinfo.value)
    assert "sup3rsecret" not in message
    assert bad not in message
    # 异常链被抑制：traceback 不会展示含凭据的底层解析错误。
    assert excinfo.value.__suppress_context__ is True
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize(
    "ok_url",
    [
        "postgresql+asyncpg://user:pw@localhost:5432/annotation_test",
        "postgresql+asyncpg://owner:pw@localhost:5433/aap_wt_abcdef_test",
    ],
)
def test_documented_disposable_targets_accepted(ok_url: str):
    assert _validate_test_db_target(ok_url) == ok_url
