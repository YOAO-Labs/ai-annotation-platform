from typer.testing import CliRunner

from importlib.metadata import version

from yoao import __platform_target_version__, __version__
from yoao.cli.main import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert (
        result.output.strip()
        == f"yoao {__version__}@platform{__platform_target_version__}"
    )


def test_runtime_version_matches_distribution_metadata():
    assert version("yoao-sdk") == __version__


def test_tui_unconfigured_exits_with_login_hint(monkeypatch, tmp_path):
    monkeypatch.delenv("YOAO_BASE_URL", raising=False)
    monkeypatch.delenv("YOAO_API_KEY", raising=False)
    monkeypatch.setattr("yoao.config.config_path", lambda: tmp_path / "config.toml")
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 1
    assert "yoao login" in result.output + str(result.exception or "")
