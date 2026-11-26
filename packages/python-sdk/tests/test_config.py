import json
import os

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import pytest
from typer.testing import CliRunner

from yoao.cli.main import app
from yoao.config import config_path, load_config, save_config
from yoao.errors import SDKError


def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("YOAO_BASE_URL", raising=False)
    monkeypatch.delenv("YOAO_API_KEY", raising=False)


def test_load_config_priority(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    cfg_dir = tmp_path / ".config" / "yoao"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(
        'base_url = "http://file"\napi_key = "ak_file"\n'
    )

    assert load_config() == ("http://file", "ak_file")

    monkeypatch.setenv("YOAO_BASE_URL", "http://env")
    monkeypatch.setenv("YOAO_API_KEY", "ak_env")
    assert load_config() == ("http://env", "ak_env")

    assert load_config(base_url="http://arg", api_key="ak_arg") == (
        "http://arg",
        "ak_arg",
    )

    assert load_config(base_url="http://arg") == ("http://arg", "ak_env")
    monkeypatch.delenv("YOAO_API_KEY")
    assert load_config(base_url="http://arg") == ("http://arg", "ak_file")
    monkeypatch.setenv("AAP_BASE_URL", "http://old")
    monkeypatch.setenv("AAP_API_KEY", "old-secret")
    monkeypatch.delenv("YOAO_BASE_URL")
    assert load_config() == ("http://file", "ak_file")


def test_load_config_missing_file(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    assert load_config() == (None, None)


def test_load_config_broken_toml(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    cfg = config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("not [valid toml")
    assert load_config() == (None, None)


def test_save_config_round_trips_toml_strings_in_private_file(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    url = 'https://example.test/"路径"/\U00020000?q=\\a'
    key = 'ak_"\\\t\b\f\r\n\x00\x1f\x7f'
    p = save_config(url, key)
    assert p == config_path()
    assert tomllib.loads(p.read_text(encoding="utf-8")) == {
        "base_url": url,
        "api_key": key,
    }
    assert (p.stat().st_mode & 0o777) == 0o600
    assert load_config() == (url, key)


def test_save_config_does_not_expose_new_key_to_existing_reader(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    p = config_path()
    p.parent.mkdir(parents=True)
    p.write_text("old data")
    p.chmod(0o644)
    with p.open() as public_reader:
        save_config("http://h", "new-credential")
        assert public_reader.read() == "old data"
    assert (p.stat().st_mode & 0o777) == 0o600
    assert load_config() == ("http://h", "new-credential")


def test_save_config_failure_keeps_previous_file_private(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    p = save_config("http://h", "old-key")

    def denied(*_):
        raise PermissionError("cannot replace file")

    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(SDKError) as failure:
        save_config("http://new", "never-log-this-key")
    assert "never-log-this-key" not in str(failure.value)
    assert load_config() == ("http://h", "old-key")
    assert list(p.parent.iterdir()) == [p]


def test_explicit_migration_preserves_source_and_refuses_conflicts(
    monkeypatch, tmp_path
):
    _isolate_home(monkeypatch, tmp_path)
    legacy = tmp_path / ".config" / "ai-annotation" / "config.toml"
    legacy.parent.mkdir(parents=True)
    content = 'base_url = "http://legacy"\napi_key = "secret-token"\n'
    legacy.write_text(content)
    runner = CliRunner()

    assert load_config() == (None, None)
    result = runner.invoke(app, ["migrate-config", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"config_path": str(config_path())}
    assert "secret-token" not in result.output
    assert config_path().read_text() == legacy.read_text() == content
    assert config_path().stat().st_mode & 0o777 == 0o600
    assert load_config() == ("http://legacy", "secret-token")

    config_path().write_text('base_url = "http://existing"\n')
    result = runner.invoke(app, ["migrate-config", "--json"])
    assert result.exit_code == 1
    assert "secret-token" not in result.output
    assert config_path().read_text() == 'base_url = "http://existing"\n'
    assert legacy.read_text() == content


def test_migration_missing_source_creates_no_target(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["migrate-config", "--json"])
    assert result.exit_code == 1
    assert not config_path().exists()
