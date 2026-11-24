from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from yoao.cli.main import app
from yoao.errors import ValidationError
from yoao_inference_contract.plugin import (
    PreparePluginInstallation,
    PluginResourceBinding,
)
from .conftest import API
from .support_plugin_deployment import Core, LocalDocker, config, report_file


def test_prepare_error_never_echoes_token(client, respx_mock):
    cfg = config()
    token = "secret-" + "x" * 40
    request = PreparePluginInstallation(
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

    monkeypatch.setattr("yoao.cli.plugins.get_client", should_not_connect)
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
    from yoao.plugin_deployment import LocalPluginDeployment

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

    monkeypatch.setattr("yoao.cli.plugins.get_client", client)
    monkeypatch.setattr("yoao.cli.plugins._deployment", lambda *_: deployment)
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


def test_inspect_image_cli_outputs_manifest_without_api_client(monkeypatch):
    cfg = config()

    def should_not_connect(*args, **kwargs):
        raise AssertionError("inspect-image must not connect to API")

    monkeypatch.setattr("yoao.cli.plugins.get_client", should_not_connect)
    monkeypatch.setattr(
        "yoao.cli.plugins._inspect_image",
        lambda image: {
            "image_id": "sha256:" + "d" * 64,
            "manifest": cfg.manifest.model_dump(mode="json"),
            "qualification_command": ["yoao-libreyolo", "qualify"],
        },
    )
    result = CliRunner().invoke(
        app,
        ["plugins", "inspect-image", "--image", cfg.manifest.artifact, "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["image_id"] == "sha256:" + "d" * 64
    assert payload["manifest"]["plugin_id"] == "libreyolo"
    assert payload["qualification_command"] == ["yoao-libreyolo", "qualify"]


def test_prepare_cli_supports_image_flags_slim_config_and_legacy_config(
    monkeypatch, tmp_path
):
    from yoao.plugin_deployment import (
        LocalPluginDeployment,
        resolve_local_plugin_config,
    )

    cfg = config()
    docker = LocalDocker([])

    def should_not_connect(*args, **kwargs):
        raise AssertionError("must not connect before confirmation or validation")

    monkeypatch.setattr(
        "yoao.cli.plugins._resolve_local_plugin_config",
        lambda spec: resolve_local_plugin_config(spec, docker=docker),
    )

    keyring_path = tmp_path / "keyring.json"
    keyring_path.write_text(json.dumps(cfg.gpu_verify_keyring), encoding="utf-8")

    monkeypatch.setattr("yoao.cli.plugins.get_client", should_not_connect)
    no_yes = CliRunner().invoke(
        app,
        [
            "plugins",
            "prepare",
            "--image",
            cfg.manifest.artifact,
            "--name",
            cfg.name,
            "--url",
            cfg.url,
            "--network",
            cfg.network,
            "--host-port",
            str(cfg.host_port),
            "--cpus",
            str(cfg.cpus),
            "--image-host",
            cfg.image_hosts[0],
            "--gpu-resource-id",
            cfg.gpu_resource_id,
            "--physical-gpu-uuid",
            cfg.physical_gpu_uuid,
            "--gpu-verify-keyring",
            str(keyring_path),
            "--state-dir",
            str(tmp_path / "state-flags"),
            "--json",
        ],
    )
    assert no_yes.exit_code != 0

    slim_cfg_path = tmp_path / "slim.json"
    slim_body = cfg.model_dump(mode="json")
    slim_body.pop("manifest")
    slim_body["image"] = cfg.manifest.artifact
    slim_cfg_path.write_text(json.dumps(slim_body), encoding="utf-8")
    slim_no_yes = CliRunner().invoke(
        app,
        [
            "plugins",
            "prepare",
            "--config",
            str(slim_cfg_path),
            "--state-dir",
            str(tmp_path / "state-slim"),
            "--json",
        ],
    )
    assert slim_no_yes.exit_code != 0

    mixed = CliRunner().invoke(
        app,
        [
            "plugins",
            "prepare",
            "--config",
            str(slim_cfg_path),
            "--image",
            cfg.manifest.artifact,
            "--state-dir",
            str(tmp_path / "state-mixed"),
            "--yes",
            "--json",
        ],
    )
    assert mixed.exit_code != 0

    @contextmanager
    def client(_json_output):
        yield object()

    monkeypatch.setattr("yoao.cli.plugins.get_client", client)

    def make_deployment(_client, state_dir):
        return LocalPluginDeployment(Core(), "https://api.example", state_dir, docker)

    monkeypatch.setattr("yoao.cli.plugins._deployment", make_deployment)

    with_flags = CliRunner().invoke(
        app,
        [
            "plugins",
            "prepare",
            "--image",
            cfg.manifest.artifact,
            "--name",
            cfg.name,
            "--url",
            cfg.url,
            "--network",
            cfg.network,
            "--host-port",
            str(cfg.host_port),
            "--cpus",
            str(cfg.cpus),
            "--image-host",
            cfg.image_hosts[0],
            "--gpu-resource-id",
            cfg.gpu_resource_id,
            "--physical-gpu-uuid",
            cfg.physical_gpu_uuid,
            "--gpu-verify-keyring",
            str(keyring_path),
            "--state-dir",
            str(tmp_path / "state-flags"),
            "--yes",
            "--json",
        ],
    )
    assert with_flags.exit_code == 0, with_flags.output
    assert json.loads(with_flags.output)["installation"]["status"] == "prepared"

    with_slim = CliRunner().invoke(
        app,
        [
            "plugins",
            "prepare",
            "--config",
            str(slim_cfg_path),
            "--state-dir",
            str(tmp_path / "state-slim"),
            "--yes",
            "--json",
        ],
    )
    assert with_slim.exit_code == 0, with_slim.output
    assert json.loads(with_slim.output)["installation"]["status"] == "prepared"

    legacy_cfg_path = tmp_path / "legacy.json"
    legacy_cfg_path.write_text(
        json.dumps(cfg.model_dump(mode="json")), encoding="utf-8"
    )
    with_legacy = CliRunner().invoke(
        app,
        [
            "plugins",
            "prepare",
            "--config",
            str(legacy_cfg_path),
            "--state-dir",
            str(tmp_path / "state-legacy"),
            "--json",
        ],
    )
    assert with_legacy.exit_code == 0, with_legacy.output
    assert json.loads(with_legacy.output)["installation"]["status"] == "prepared"


def test_qualify_install_and_upgrade_cli_flows(monkeypatch, tmp_path):
    from yoao.plugin_deployment import LocalPluginDeployment

    def should_not_connect(*args, **kwargs):
        raise AssertionError("invalid options must fail before connecting")

    monkeypatch.setattr("yoao.cli.plugins.get_client", should_not_connect)
    conflict = CliRunner().invoke(
        app,
        [
            "plugins",
            "install",
            "--state-dir",
            str(tmp_path / "s"),
            "--qualification",
            str(tmp_path / "r.json"),
            "--qualify",
            "--yes",
            "--json",
        ],
    )
    assert conflict.exit_code != 0

    orphan_opt = CliRunner().invoke(
        app,
        [
            "plugins",
            "install",
            "--state-dir",
            str(tmp_path / "s"),
            "--model-approval-ref",
            "REF",
            "--yes",
            "--json",
        ],
    )
    assert orphan_opt.exit_code != 0

    upgrade_conflict = CliRunner().invoke(
        app,
        [
            "plugins",
            "upgrade",
            "--from-state",
            str(tmp_path / "old"),
            "--state-dir",
            str(tmp_path / "new"),
            "--qualification",
            str(tmp_path / "r.json"),
            "--qualify",
            "--yes",
            "--json",
        ],
    )
    assert upgrade_conflict.exit_code != 0

    old_core = Core()
    old_docker = LocalDocker(old_core.events)
    old_state = tmp_path / "old-state"
    old_dep = LocalPluginDeployment(
        old_core, "https://api.example", old_state, old_docker
    )
    old_dep.prepare(config())

    new_cfg = config().model_copy(
        update={
            "manifest": config().manifest.bind_artifact(
                "example.invalid/plugin@sha256:" + "f" * 64
            )
        }
    )
    new_core = Core()
    new_docker = LocalDocker(new_core.events)
    new_docker.manifest = new_cfg.manifest
    new_state = tmp_path / "new-state"
    new_dep = LocalPluginDeployment(
        new_core, "https://api.example", new_state, new_docker
    )
    new_dep.prepare(new_cfg)

    deployments = {old_state: old_dep, new_state: new_dep}

    @contextmanager
    def client(_json_output):
        yield object()

    monkeypatch.setattr("yoao.cli.plugins.get_client", client)
    monkeypatch.setattr(
        "yoao.cli.plugins._deployment", lambda _c, path: deployments[path]
    )

    out_path = tmp_path / "report-out.json"
    qual_res = CliRunner().invoke(
        app,
        [
            "plugins",
            "qualify",
            "--state-dir",
            str(old_state),
            "--model-approval-ref",
            "M-1",
            "--out",
            str(out_path),
            "--json",
        ],
    )
    assert qual_res.exit_code == 0, qual_res.output
    assert out_path.is_file()
    assert json.loads(qual_res.output)["report"]["passed"] is True
    assert "qualification" not in old_dep.store.load()

    install_res = CliRunner().invoke(
        app,
        [
            "plugins",
            "install",
            "--state-dir",
            str(old_state),
            "--qualify",
            "--model-approval-ref",
            "M-2",
            "--yes",
            "--json",
        ],
    )
    assert install_res.exit_code == 0, install_res.output
    assert json.loads(install_res.output)["installation"]["status"] == "active"
    assert old_core.events == ["qualify", "qualify", "start", "activate"]
    assert old_docker.qualification_runs[0]["context"]["model_approval_ref"] == "M-1"
    assert old_docker.qualification_runs[1]["context"]["model_approval_ref"] == "M-2"

    in_state_core = Core()
    in_state_docker = LocalDocker(in_state_core.events)
    in_state_dir = tmp_path / "in-state"
    in_state_dep = LocalPluginDeployment(
        in_state_core, "https://api.example", in_state_dir, in_state_docker
    )
    in_state_dep.prepare(config())
    deployments[in_state_dir] = in_state_dep
    bind_res = CliRunner().invoke(
        app,
        [
            "plugins",
            "qualify",
            "--state-dir",
            str(in_state_dir),
            "--json",
        ],
    )
    assert bind_res.exit_code == 0, bind_res.output
    assert "qualification" in in_state_dep.store.load()
    assert (in_state_dir / "qualification.json").is_file()

    upgrade_res = CliRunner().invoke(
        app,
        [
            "plugins",
            "upgrade",
            "--from-state",
            str(old_state),
            "--state-dir",
            str(new_state),
            "--qualify",
            "--model-approval-ref",
            "M-3",
            "--yes",
            "--json",
        ],
    )
    assert upgrade_res.exit_code == 0, upgrade_res.output
    assert json.loads(upgrade_res.output)["installation"]["status"] == "active"
    assert new_core.events == ["qualify", "start", "activate"]
    assert old_core.result.installation.status == "active"
