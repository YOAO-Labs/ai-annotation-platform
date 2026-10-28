from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from ai_annotation.errors import AAPError, APIStatusError
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
    from aap_backend_runtime.lifecycle_evidence import (
        REQUIRED_CONTRACT_CHECKS, build_managed_lifecycle_evidence, memory_cycle_evidence,
    )
    from training_contract.inference_runtime import LIBRE_CUDA_RUNTIME

    receipt = json.loads((tmp_path / "state" / "receipt.json").read_text())
    manifest = receipt["config"]["manifest"]
    registry = receipt["registry_id"]
    resource = receipt["config"]["gpu_resource_id"]
    versions = {key: str(value).lower() if isinstance(value, bool) else str(value)
                for key, value in LIBRE_CUDA_RUNTIME.items()}
    versions.update(plugin_id=manifest["plugin_id"], plugin_version=manifest["version"],
                    artifact=manifest["artifact"], asset_manifest_sha256=manifest["asset_manifest_sha256"],
                    registry_id=registry, gpu_resource_id=resource, driver_version="580.65")
    cycles = [memory_cycle_evidence(cycle=i, generation=str(i * 2),
        context_samples_mb=[100] * 5, loaded_samples_mb=[1100] * 3,
        unloaded_samples_mb=[120] * 5) for i in (1, 2)]
    report = build_managed_lifecycle_evidence(
        backend_name="libreyolo",
        deployment={"git_commit": "a" * 40, "image_id": "sha256:" + "d" * 64,
                    "runtime_versions": versions, "pool_topology": {
                        "registry_id": registry, "gpu_resource_id": resource, "pools": {"models": {"cap": 1}}}},
        artifacts=[{"kind": kind, "name": kind, "size_bytes": 1, "sha256": "a" * 64,
                    "approval_ref": "通过"} for kind in ("weight", "fixture")],
        gpu={"uuid": receipt["config"]["physical_gpu_uuid"], "total_memory_mb": 24576,
             "driver_version": "580.65", "runtime_version": "13.2", "visible_device_count": 1},
        cycles=cycles, contract_checks={name: True for name in REQUIRED_CONTRACT_CHECKS},
        final_residency={"state": "unloaded", "gpu_loaded": False, "evictable": False,
                         "active_requests": 0, "builders": 0, "borrowers": 0,
                         "pools": {"models": {"resident": False}}},
        runtime_ephemera_clean=True, generated_at="2026-07-30T12:00:00Z")
    path = tmp_path / "gpu-evidence.json"
    path.write_text(json.dumps(report, ensure_ascii=False))
    return path


class Core:
    def __init__(self):
        self.result = None
        self.requests = []
        self.events = []
        self.safe = True
        self.dependent = False
        self.proven_revision = None
        self.launch = None

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
        self.proven_revision = None
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
        self.launch = request.receipt.model_dump(mode="json")
        assert request.qualification is not None
        canonical = json.dumps(request.qualification.report, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        assert request.qualification.sha256 == hashlib.sha256(canonical.encode()).hexdigest()
        self.events.append("activate")
        return self._transition("active")

    def disable(self, installation_id, revision):
        self.events.append("disable")
        return self._transition("disabled")

    def quiesce(self, installation_id, revision):
        assert revision == self.result.installation.revision
        assert self.result.installation.status in {"prepared", "disabled"}
        self.events.append("quiesce")
        if self.launch is None:
            raise APIStatusError(409, {"error_code": "plugin_launch_unrecorded"})
        if self.safe and self.proven_revision != revision:
            self._transition("disabled")
            self.proven_revision = self.result.installation.revision
        return PluginQuiesceResult.model_validate({**self.result.model_dump(mode="json"), "safe_to_stop": self.safe, "launch": self.launch})

    def retire(self, installation_id, revision):
        self.events.append("retire")
        assert revision == self.result.installation.revision
        if self.dependent:
            raise AAPError("active dependency")
        if self.proven_revision != revision:
            raise AAPError("quiescence proof required")
        return self._transition("retired")

    def cancel_prepared(self, installation_id, revision):
        assert self.result.installation.status == "prepared" and self.launch is None
        assert revision == self.result.installation.revision
        self.events.append("cancel")
        return self._transition("retired")


class LocalDocker:
    def __init__(self, events):
        self.events = events
        self.running = False
        self.container_id = "c" * 64
    def image_id(self, config):
        return "sha256:" + "d" * 64
    def ensure_started(self, config):
        self.events.append("start")
        self.running = True
        return {"id": self.container_id, "image_id": "sha256:" + "d" * 64}
    def verify_running(self, config):
        if not self.running:
            raise AAPError("owned container is not running")
    def exists(self, config):
        return self.running
    def stop_and_remove(self, config, proven_id):
        if self.running and proven_id != self.container_id:
            raise AAPError("Container ID differs from the proven container")
        self.events.append("stop")
        self.running = False
    def prepare_assets(self, config):
        self.events.append("assets")


def test_uninstall_cancels_never_launched_reservation(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", docker)
    deployment.prepare(config())
    assert deployment.uninstall().installation.status == "retired"
    assert core.events == ["quiesce", "cancel"]
    assert deployment.uninstall().installation.status == "retired"


def test_unrecorded_local_container_prevents_cancellation(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", docker)
    deployment.prepare(config())
    docker.running = True
    with pytest.raises(AAPError, match="Unrecorded local launch"):
        deployment.uninstall()
    assert core.result.installation.status == "prepared"
    assert docker.running and "cancel" not in core.events


def test_crashed_launch_attempt_blocks_cancellation_even_if_container_disappeared(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", docker)
    deployment.prepare(config())
    report = report_file(tmp_path)

    def failed_start(container):
        raise AAPError("Docker start outcome unknown")

    docker.ensure_started = failed_start
    with pytest.raises(AAPError, match="outcome unknown"):
        deployment.install(report)
    with pytest.raises(AAPError, match="Unrecorded local launch"):
        deployment.uninstall()
    assert core.result.installation.status == "prepared"
    assert "cancel" not in core.events


def test_retired_retry_rejects_same_name_replacement(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", docker)
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    deployment.uninstall()
    docker.container_id = "e" * 64
    docker.running = True
    with pytest.raises(AAPError, match="Container ID"):
        deployment.uninstall()
    assert docker.running


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


@pytest.mark.parametrize("mutation", [
    "incomplete", "failed", "image", "artifact", "runtime", "registry",
    "resource", "device", "topology",
])
def test_invalid_qualification_can_be_corrected_before_persistence(tmp_path, mutation):
    core = Core()
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", LocalDocker(core.events))
    deployment.prepare(config())
    source = report_file(tmp_path)
    report = json.loads(source.read_text())
    if mutation == "incomplete":
        report.pop("cycles")
    elif mutation == "failed":
        report["passed"] = False
        report["blockers"] = [{"code": "failed", "message": "failed"}]
    elif mutation == "image":
        report["deployment"]["image_id"] = "sha256:" + "e" * 64
    elif mutation == "artifact":
        report["deployment"]["runtime_versions"]["artifact"] = "wrong"
    elif mutation == "runtime":
        report["deployment"]["runtime_versions"]["driver_version"] = "wrong"
    elif mutation == "registry":
        report["deployment"]["runtime_versions"]["registry_id"] = "wrong"
    elif mutation == "resource":
        report["deployment"]["runtime_versions"]["gpu_resource_id"] = "wrong"
    elif mutation == "device":
        report["gpu"]["uuid"] = "wrong"
    else:
        report["deployment"]["pool_topology"]["pools"]["models"]["cap"] = 2
    source.write_text(json.dumps(report))
    with pytest.raises(AAPError):
        deployment.install(source)
    assert not (tmp_path / "state" / "qualification.json").exists()
    assert "qualification" not in deployment.store.load()
    assert "start" not in core.events
    deployment.install(report_file(tmp_path))
    assert core.events == ["start", "activate"]


def test_image_inspection_failure_can_be_retried(tmp_path):
    class FailingInspection(LocalDocker):
        def image_id(self, config):
            if not hasattr(self, "failed"):
                self.failed = True
                raise AAPError("inspection failed")
            return super().image_id(config)

    core = Core()
    deployment = LocalPluginDeployment(core, "https://api.example", tmp_path / "state", FailingInspection(core.events))
    deployment.prepare(config())
    source = report_file(tmp_path)
    with pytest.raises(AAPError, match="inspection failed"):
        deployment.install(source)
    assert not (tmp_path / "state" / "qualification.json").exists()
    assert "qualification" not in deployment.store.load()
    deployment.install(source)
    assert core.events == ["start", "activate"]


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


def test_uninstall_preserves_container_and_credentials_without_quiescence(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    state = tmp_path / "state"
    deployment = LocalPluginDeployment(core, "https://api.example", state, docker)
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    core.safe = False
    with pytest.raises(AAPError, match="quiescence"):
        deployment.uninstall()
    assert docker.running
    assert (state / "data" / "token").exists()
    assert core.result.installation.status == "disabled"
    assert "retire" not in core.events


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
