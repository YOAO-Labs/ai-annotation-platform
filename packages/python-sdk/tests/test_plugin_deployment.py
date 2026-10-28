from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from ai_annotation.errors import AAPError
from ai_annotation.plugin_deployment import LocalPluginConfig, LocalPluginDeployment
from ai_annotation.plugin_models import PluginInstallationResult, PluginQuiesceResult


def config():
    return LocalPluginConfig.model_validate({
        "name": "Libre detector", "url": "http://plugin.example:9080", "gpu_resource_id": "node/gpu",
        "physical_gpu_uuid": "GPU-12345678-1234-1234-1234-123456789abc", "network": "approved", "host_port": 9080,
        "cpus": 2, "gpu_verify_keyring": {"key1": "A" * 43}, "image_hosts": ["assets.example"],
        "manifest": {"plugin_id": "libreyolo", "version": "1", "publisher": "example",
            "license": "Apache-2.0", "artifact": "example.invalid/plugin@sha256:" + "b" * 64,
            "sdk_compatibility": ">=1", "entrypoint": ["aap-libreyolo", "serve", "--host", "0.0.0.0", "--port", "8000"],
            "capabilities": ["inference", "detection"],
            "resource": {"device": "cuda", "memory_mb": 4096, "gpu_memory_mb": 2048},
            "asset_manifest_sha256": "a" * 64, "allowed_asset_sources": ["https://assets.example"]},
    })


def report_file(tmp_path):
    path = tmp_path / "gpu-evidence.json"
    path.write_text(json.dumps({"note": "通过", "deployment": {"pools": ["models"]}}, ensure_ascii=False))
    return path


class Core:
    def __init__(self):
        self.result = None
        self.requests = []
        self.events = []
        self.safe = True
        self.dependent = False

    def prepare(self, request):
        self.requests.append(request)
        if self.result is None:
            self.result = PluginInstallationResult.model_validate({"registry_id": str(uuid4()), "installation": {
                "installation_id": str(request.installation_id), "manifest": request.manifest.model_dump(mode="json"),
                "status": "prepared", "revision": 1, "resource": request.resource.model_dump(mode="json")}})
        return self.result

    def get(self, installation_id):
        assert str(self.result.installation.installation_id) == installation_id
        return self.result

    def _transition(self, status):
        payload = self.result.model_dump(mode="json")
        payload["installation"]["status"] = status
        payload["installation"]["revision"] += 1
        self.result = PluginInstallationResult.model_validate(payload)
        return self.result

    def activate(self, installation_id, request):
        assert request.expected_revision == self.result.installation.revision
        assert request.receipt.registry_id == self.result.registry_id
        assert request.receipt.installation_id == self.result.installation.installation_id
        assert request.receipt.resource == self.result.installation.resource
        assert request.receipt.artifact == self.result.installation.manifest.artifact
        assert request.qualification is not None
        canonical = json.dumps(request.qualification.report, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        assert request.qualification.sha256 == hashlib.sha256(canonical.encode()).hexdigest()
        self.events.append("activate")
        return self._transition("active")

    def disable(self, installation_id, revision):
        self.events.append("disable")
        return self._transition("disabled")

    def quiesce(self, installation_id, revision):
        self.events.append("quiesce")
        return PluginQuiesceResult.model_validate({**self.result.model_dump(mode="json"), "safe_to_stop": self.safe})

    def retire(self, installation_id, revision):
        self.events.append("retire")
        if self.dependent:
            raise AAPError("active dependency")
        return self._transition("retired")


class LocalDocker:
    def __init__(self, events):
        self.events = events
        self.running = False
    def image_id(self, config):
        return "sha256:" + "d" * 64
    def ensure_started(self, config):
        self.events.append("start")
        self.running = True
        return {"id": "c" * 64, "image_id": "sha256:" + "d" * 64}
    def verify_running(self, config):
        if not self.running:
            raise AAPError("owned container is not running")
    def exists(self, config):
        return self.running
    def stop_and_remove(self, config):
        self.events.append("stop")
        self.running = False
    def prepare_assets(self, config):
        self.events.append("assets")


def test_reservation_replays_same_identity_and_install_activates_after_start(tmp_path):
    core = Core()
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", LocalDocker(core.events))
    first = deployment.prepare(config())
    second = deployment.prepare(config())
    assert first.registry_id == second.registry_id
    assert core.requests[0].installation_id == core.requests[1].installation_id
    assert core.requests[0].auth_token == core.requests[1].auth_token
    assert len(core.requests[0].auth_token) >= 32
    deployment.prepare_assets()
    active = deployment.install(report_file(tmp_path))
    assert active.installation.status == "active"
    assert core.events == ["assets", "start", "activate"]
    assert "token" not in active.model_dump_json()
    canonical = (tmp_path / "state" / "qualification.json").read_bytes()
    assert b"\\u901a\\u8fc7" in canonical
    assert "AAP_PLUGIN_QUALIFICATION_SHA256=" + hashlib.sha256(canonical).hexdigest() in (tmp_path / "state" / "container.env").read_text()


def test_missing_or_changed_qualification_never_starts(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", docker)
    deployment.prepare(config())
    with pytest.raises(AAPError, match="qualification report is required"):
        deployment.install()
    assert "start" not in core.events
    deployment.install(report_file(tmp_path))
    changed = tmp_path / "changed.json"
    changed.write_text('{"different":true}')
    with pytest.raises(AAPError, match="differs"):
        deployment.install(changed)


def test_lost_prepare_response_reuses_persisted_identity(tmp_path):
    class InterruptedCore(Core):
        def prepare(self, request):
            result = super().prepare(request)
            if len(self.requests) == 1:
                raise AAPError("response lost")
            return result

    core = InterruptedCore()
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", LocalDocker(core.events))
    with pytest.raises(AAPError, match="response lost"):
        deployment.prepare(config())
    result = deployment.prepare(config())
    assert core.requests[0].installation_id == core.requests[1].installation_id == result.installation.installation_id
    assert core.requests[0].auth_token == core.requests[1].auth_token


def test_active_install_does_not_restart_missing_container(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", docker)
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    docker.running = False
    with pytest.raises(AAPError, match="not running"):
        deployment.install()
    assert core.events == ["start", "activate"]


def test_disable_does_not_stop_without_core_quiescence_proof(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", docker)
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    core.safe = False
    with pytest.raises(AAPError, match="quiescence"):
        deployment.disable()
    assert docker.running
    assert core.events[-2:] == ["disable", "quiesce"]
    core.safe = True
    deployment.disable()
    assert core.events[-2:] == ["quiesce", "stop"]
    assert not docker.running


def test_uninstall_preserves_container_when_core_reports_active_dependency(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", docker)
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    core.dependent = True
    with pytest.raises(AAPError, match="active dependency"):
        deployment.uninstall()
    assert docker.running
    assert core.result.installation.status == "disabled"
    assert "stop" not in core.events


def test_retire_removes_local_token_and_remains_queryable(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    state = tmp_path / "state"
    deployment = LocalPluginDeployment(core, "https://api.example", state, docker)
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    result = deployment.uninstall()
    assert result.installation.status == "retired"
    assert not (state / "data" / "token").exists()
    assert '"token"' not in (state / "receipt.json").read_text()
    assert deployment.status().installation.status == "retired"
    assert deployment.uninstall().installation.status == "retired"


def test_config_refuses_arbitrary_entrypoint_and_private_gpu_key():
    body = config().model_dump(mode="json")
    body["manifest"]["entrypoint"] = ["/bin/sh", "-c", "anything"]
    with pytest.raises(ValueError):
        LocalPluginConfig.model_validate(body)
    body = config().model_dump(mode="json")
    body["gpu_verify_keyring"] = {"private": "-----BEGIN PRIVATE KEY-----"}
    with pytest.raises(ValueError):
        LocalPluginConfig.model_validate(body)


def test_retry_rejects_changed_configuration_without_mutation(tmp_path):
    core = Core()
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", LocalDocker(core.events))
    deployment.prepare(config())
    changed = config().model_copy(update={"name": "different"})
    with pytest.raises(AAPError, match="different configuration"):
        deployment.prepare(changed)
    assert len(core.requests) == 1


def test_manifest_rejects_docker_option_injection():
    body = config().model_dump(mode="json")
    body["manifest"]["artifact"] = "--privileged@sha256:" + "a" * 64
    with pytest.raises(ValueError):
        LocalPluginConfig.model_validate(body)
