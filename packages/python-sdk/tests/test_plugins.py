from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation.cli.main import app
from ai_annotation.errors import ValidationError
from ai_annotation.plugin_models import PluginPrepareRequest, PluginResourceBinding
from .conftest import API
from .test_plugin_deployment import Core, LocalDocker, config, report_file


def test_prepare_error_never_echoes_token(client, respx_mock):
    cfg = config()
    token = "secret-" + "x" * 40
    request = PluginPrepareRequest(
        installation_id=uuid4(),
        name=cfg.name,
        url=cfg.url,
        auth_token=token,
        manifest=cfg.manifest,
        resource=PluginResourceBinding(
            gpu_resource_id=cfg.gpu_resource_id,
            physical_gpu_uuid=cfg.physical_gpu_uuid,
            logical_device="cuda:0",
        ),
    )
    assert token not in repr(request)
    respx_mock.post(f"{API}/admin/ml-integrations/plugins/installations").mock(
        return_value=httpx.Response(422, json={"detail": {"input": token}})
    )
    with pytest.raises(ValidationError) as exc:
        client.plugins.prepare(request)
    assert token not in str(exc.value)
    assert token not in str(exc.value.detail)


@pytest.mark.parametrize("command", ["install", "restart", "uninstall"])
def test_json_destructive_command_requires_confirmation_before_mutation(
    monkeypatch, tmp_path, command
):
    def should_not_connect(*args, **kwargs):
        raise AssertionError("must not connect before confirmation")

    monkeypatch.setattr("ai_annotation.cli.plugins.get_client", should_not_connect)
    result = CliRunner().invoke(
        app,
        [
            "--error-format",
            "json",
            "plugins",
            command,
            "--state-dir",
            str(tmp_path),
            "--json",
        ],
    )
    assert result.exit_code != 0
    assert "secret" not in result.output


def test_restart_cli_returns_active_json_after_real_transition(monkeypatch, tmp_path):
    from ai_annotation.plugin_deployment import LocalPluginDeployment

    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))

    @contextmanager
    def client(_json_output):
        yield object()

    monkeypatch.setattr("ai_annotation.cli.plugins.get_client", client)
    monkeypatch.setattr("ai_annotation.cli.plugins._deployment", lambda *_: deployment)
    result = CliRunner().invoke(
        app,
        [
            "plugins",
            "restart",
            "--state-dir",
            str(tmp_path / "state"),
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["installation"]["status"] == "active"
    assert core.events[-4:] == ["stop", "create", "start", "activate"]
