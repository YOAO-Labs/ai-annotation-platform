from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation.cli.main import app
from ai_annotation.errors import ValidationError
from ai_annotation.plugin_models import PluginActivateRequest, PluginLaunchReceipt, PluginPrepareRequest, PluginQualification, PluginResourceBinding
from .conftest import API
from .test_plugin_deployment import config


def test_prepare_and_revision_actions_use_public_contract(client, respx_mock):
    cfg = config()
    installation_id, registry_id = uuid4(), uuid4()
    request = PluginPrepareRequest(installation_id=installation_id, name=cfg.name, url=cfg.url,
        auth_token="secret-" + "x" * 40, manifest=cfg.manifest,
        resource=PluginResourceBinding(gpu_resource_id=cfg.gpu_resource_id,
            physical_gpu_uuid=cfg.physical_gpu_uuid, logical_device="cuda:0"))
    body = {"registry_id": str(registry_id), "installation": {"installation_id": str(installation_id),
        "manifest": cfg.manifest.model_dump(mode="json"), "status": "prepared", "revision": 1,
        "resource": request.resource.model_dump(mode="json")}}
    root = f"{API}/admin/ml-integrations/plugins/installations"
    prepare = respx_mock.post(root).mock(return_value=httpx.Response(200, json=body))
    activate = respx_mock.post(f"{root}/{installation_id}/activate").mock(return_value=httpx.Response(200, json=body))
    quiesce = respx_mock.post(f"{root}/{installation_id}/quiesce").mock(
        return_value=httpx.Response(200, json={**body, "safe_to_stop": True}))
    result = client.plugins.prepare(request)
    evidence = PluginLaunchReceipt(installation_id=installation_id, registry_id=registry_id,
        artifact=cfg.manifest.artifact, image_id="sha256:" + "d" * 64,
        container_id="c" * 64, resource=request.resource)
    digest = hashlib.sha256(b'{"evidence":"passed"}').hexdigest()
    qualification = PluginQualification(report={"evidence": "passed"}, sha256=digest)
    client.plugins.activate(installation_id, PluginActivateRequest(expected_revision=1, receipt=evidence, qualification=qualification))
    proof = client.plugins.quiesce(installation_id, 1)
    assert result.registry_id == registry_id
    assert proof.safe_to_stop is True
    assert json.loads(prepare.calls.last.request.content)["auth_token"] == request.auth_token
    assert json.loads(quiesce.calls.last.request.content) == {"expected_revision": 1}
    activation_body = json.loads(activate.calls.last.request.content)
    assert activation_body["receipt"]["container_id"] == "c" * 64
    assert activation_body["receipt"]["image_id"] == "sha256:" + "d" * 64
    assert activation_body["expected_revision"] == 1
    assert activation_body["qualification"] == {"report": {"evidence": "passed"}, "sha256": digest}
    assert request.auth_token not in repr(request)


def test_prepare_error_never_echoes_token(client, respx_mock):
    cfg = config()
    token = "secret-" + "x" * 40
    request = PluginPrepareRequest(installation_id=uuid4(), name=cfg.name, url=cfg.url,
        auth_token=token, manifest=cfg.manifest, resource=PluginResourceBinding(
        gpu_resource_id=cfg.gpu_resource_id, physical_gpu_uuid=cfg.physical_gpu_uuid, logical_device="cuda:0"))
    respx_mock.post(f"{API}/admin/ml-integrations/plugins/installations").mock(
        return_value=httpx.Response(422, json={"detail": {"input": token}}))
    with pytest.raises(ValidationError) as exc:
        client.plugins.prepare(request)
    assert token not in str(exc.value)
    assert token not in str(exc.value.detail)


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_json_destructive_command_requires_confirmation_before_mutation(monkeypatch, tmp_path, command):
    def should_not_connect(*args, **kwargs):
        raise AssertionError("must not connect before confirmation")
    monkeypatch.setattr("ai_annotation.cli.plugins.get_client", should_not_connect)
    result = CliRunner().invoke(app, ["--error-format", "json", "plugins", command, "--state-dir", str(tmp_path), "--json"])
    assert result.exit_code != 0
    assert "secret" not in result.output
