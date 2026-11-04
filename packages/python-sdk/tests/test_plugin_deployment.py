from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from yoao.errors import SDKError
from yoao.plugin_deployment import LocalPluginConfig, LocalPluginDeployment

from .support_plugin_deployment import Core, LocalDocker, config, report_file


def test_cpu_libreyolo_denies_gpu_and_preserves_existing_drain_state(tmp_path):
    body = config().model_dump(mode="json")
    body["gpu_resource_id"] = None
    body["physical_gpu_uuid"] = None
    body["gpu_verify_keyring"] = {}
    body["manifest"]["resource"] = {
        "device": "cpu",
        "memory_mb": 4096,
        "gpu_memory_mb": 0,
    }
    core = Core()
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", LocalDocker(core.events)
    )
    result = deployment.prepare(LocalPluginConfig.model_validate(body))
    receipt = deployment.store.load()
    container = deployment._container(receipt, result)
    env = container.env_file.read_text()
    assert "\nNVIDIA_VISIBLE_DEVICES=void\n" in env
    assert "\nCUDA_VISIBLE_DEVICES=\n" in env

    # A historical CPU launch can still be inspected and drained, but a new
    # launch must not silently reuse the old, unfenced environment.
    old_env = env.replace("NVIDIA_VISIBLE_DEVICES=void\nCUDA_VISIBLE_DEVICES=\n", "")
    container.env_file.write_text(old_env)
    assert (
        deployment._container(receipt, result, provision=False).identity()
        == container.identity()
    )
    assert container.env_file.read_text() == old_env
    with pytest.raises(SDKError, match="Existing deployment file does not match"):
        deployment._container(receipt, result)


def test_install_accepts_qualified_ort_factory_session_counts(tmp_path):
    core = Core()
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", LocalDocker(core.events)
    )
    deployment.prepare(config())
    path = report_file(tmp_path)
    report = json.loads(path.read_text())
    report["deployment"]["runtime_versions"]["onnxruntime"] = "1.23.2"
    report["deployment"]["pool_topology"]["session_counts"] = {"models": 1}
    for cycle in report["cycles"]:
        cycle["session_observations"] = {"models": [["CUDAExecutionProvider"]]}
    path.write_text(json.dumps(report))

    assert deployment.install(path).installation.status == "active"


def test_restart_quiesces_replaces_and_keeps_history_and_data(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    state = tmp_path / "state"
    deployment = LocalPluginDeployment(core, "https://api.example", state, docker)
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    data = state / "data" / "model-receipt"
    data.write_text("immutable")
    original = deployment.store.load()["launch"]
    result = deployment.restart()
    receipt = deployment.store.load()
    assert result.installation.status == "active"
    assert core.events == [
        "start",
        "activate",
        "disable",
        "quiesce",
        "stop",
        "stop",
        "create",
        "start",
        "activate",
    ]
    assert receipt["launch"]["container_id"] != original["container_id"]
    assert receipt["launch_history"][0]["launch"] == original
    assert receipt["launch_history"][0]["quiescence"]["launch"] == original
    assert core.launch == receipt["launch"]
    assert data.read_text() == "immutable"
    assert deployment.install().installation.status == "active"


@pytest.mark.parametrize("committed", [False, True])
def test_restart_resumes_interrupted_quiescence_with_bound_intent(tmp_path, committed):
    class InterruptedCore(Core):
        def quiesce(self, installation_id, revision):
            if not hasattr(self, "interrupted"):
                self.interrupted = True
                if committed:
                    super().quiesce(installation_id, revision)
                raise SDKError("quiescence response interrupted")
            return super().quiesce(installation_id, revision)

    core = InterruptedCore()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    original = deployment.store.load()["launch"]
    with pytest.raises(SDKError, match="response interrupted"):
        deployment.restart()
    pending = deployment.store.load()
    assert core.result.installation.status == "disabled"
    assert pending["restart_intent"]["launch"] == original
    assert pending["restart_intent"]["started_at"] == "boot-1"
    assert "quiescence" not in pending and "restart_candidate" not in pending
    assert docker.running and "stop" not in core.events
    assert deployment.restart().installation.status == "active"
    saved = deployment.store.load()
    assert "restart_intent" not in saved
    assert saved["launch_history"][0]["quiescence"]["launch"] == original
    assert core.events.count("disable") == 1


@pytest.mark.parametrize("mutation", ["launch", "start", "revision", "registry"])
def test_interrupted_restart_does_not_quiesce_mismatched_intent(tmp_path, mutation):
    class InterruptedCore(Core):
        def quiesce(self, installation_id, revision):
            self.events.append("interrupted")
            raise SDKError("quiescence interrupted")

    core = InterruptedCore()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    with pytest.raises(SDKError, match="quiescence interrupted"):
        deployment.restart()
    receipt = deployment.store.load()
    intent = receipt["restart_intent"]
    if mutation == "launch":
        intent["launch"]["container_id"] = "f" * 64
    elif mutation == "start":
        intent["started_at"] = "other"
    elif mutation == "revision":
        intent["revision"] -= 10
    else:
        intent["registry_id"] = str(uuid4())
    deployment.store.save(receipt)
    with pytest.raises(SDKError, match="intent does not match"):
        deployment.restart()
    assert core.events.count("interrupted") == 1
    assert docker.running and "stop" not in core.events and "create" not in core.events


@pytest.mark.parametrize("proof_change", ["missing", "revision", "launch"])
def test_disabled_restart_requires_matching_local_proof(tmp_path, proof_change):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    deployment.disable()
    receipt = deployment.store.load()
    if proof_change == "missing":
        receipt.pop("quiescence")
    elif proof_change == "revision":
        receipt["quiescence"]["revision"] -= 1
    else:
        receipt["quiescence"]["launch"]["container_id"] = "f" * 64
    deployment.store.save(receipt)
    with pytest.raises(SDKError, match="quiescence proof"):
        deployment.restart()
    assert core.result.installation.status == "disabled"
    assert "create" not in core.events


def test_disabled_restart_resumes_proven_stop(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    deployment.disable()
    assert deployment.restart().installation.status == "active"
    assert core.events.count("quiesce") == 1


@pytest.mark.parametrize("operation", ["disable", "restart", "uninstall"])
def test_proof_replay_never_stops_externally_restarted_process(tmp_path, operation):
    class InterruptedDocker(LocalDocker):
        def stop_and_remove(self, cfg, container_id, started_at):
            if not hasattr(self, "interrupted"):
                self.interrupted = True
                raise SDKError("stop interrupted")
            return super().stop_and_remove(cfg, container_id, started_at)

    core = Core()
    docker = InterruptedDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    with pytest.raises(SDKError, match="stop interrupted"):
        deployment.disable()
    proof = deployment.store.load()["quiescence"]
    assert proof["started_at"] == "boot-1"
    docker.started_at = "boot-external"
    with pytest.raises(SDKError, match="process identity"):
        getattr(deployment, operation)()
    assert docker.running
    assert "stop" not in core.events


def test_legacy_active_receipt_binds_process_before_quiescing(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    receipt = deployment.store.load()
    receipt.pop("started_at")
    deployment.store.save(receipt)
    deployment.disable()
    assert deployment.store.load()["quiescence"]["started_at"] == "boot-1"


def test_restart_replays_candidate_creation_without_losing_proof(tmp_path):
    class InterruptedDocker(LocalDocker):
        def create_candidate(self, cfg):
            result = super().create_candidate(cfg)
            if not hasattr(self, "interrupted"):
                self.interrupted = True
                raise SDKError("create response lost")
            return result

    core = Core()
    docker = InterruptedDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    with pytest.raises(SDKError, match="response lost"):
        deployment.restart()
    saved = deployment.store.load()
    assert (
        saved["launch"]
        == saved["quiescence"]["launch"]
        == saved["restart_candidate"]["proof"]["launch"]
    )
    assert deployment.restart().installation.status == "active"
    assert core.events.count("create") == 1


def test_restart_replays_lost_activation_response_and_rejects_replaced_process(
    tmp_path,
):
    class InterruptedCore(Core):
        def activate(self, installation_id, request):
            result = super().activate(installation_id, request)
            if len([event for event in self.events if event == "activate"]) == 2:
                raise SDKError("activation response lost")
            return result

    core = InterruptedCore()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    with pytest.raises(SDKError, match="response lost"):
        deployment.restart()
    old_start = docker.started_at
    docker.started_at = "external-restart"
    with pytest.raises(SDKError, match="process identity"):
        deployment.restart()
    docker.started_at = old_start
    assert deployment.restart().installation.status == "active"
    assert core.events.count("activate") == 2
    assert len(deployment.store.load()["launch_history"]) == 1


def test_active_install_rejects_external_restart_or_replaced_container(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    docker.started_at = "new-process"
    with pytest.raises(SDKError, match="process identity"):
        deployment.install()
    docker.started_at = "boot-1"
    docker.container_id = "f" * 64
    with pytest.raises(SDKError, match="process identity"):
        deployment.install()
    assert core.events == ["start", "activate"]


def test_uninstall_cancels_never_launched_reservation(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    assert deployment.uninstall().installation.status == "retired"
    assert core.events == ["quiesce", "cancel"]
    assert deployment.uninstall().installation.status == "retired"


def test_unrecorded_local_container_prevents_cancellation(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    docker.running = True
    with pytest.raises(SDKError, match="Unrecorded local launch"):
        deployment.uninstall()
    assert core.result.installation.status == "prepared"
    assert docker.running and "cancel" not in core.events


def test_crashed_launch_attempt_blocks_cancellation_even_if_container_disappeared(
    tmp_path,
):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    report = report_file(tmp_path)

    def failed_start(container):
        raise SDKError("Docker start outcome unknown")

    docker.ensure_started = failed_start
    with pytest.raises(SDKError, match="outcome unknown"):
        deployment.install(report)
    with pytest.raises(SDKError, match="Unrecorded local launch"):
        deployment.uninstall()
    assert core.result.installation.status == "prepared"
    assert "cancel" not in core.events


def test_retired_retry_rejects_same_name_replacement(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    deployment.uninstall()
    docker.container_id = "e" * 64
    docker.running = True
    with pytest.raises(SDKError, match="Container ID"):
        deployment.uninstall()
    assert docker.running


def test_reservation_replays_same_identity_and_install_activates_after_start(tmp_path):
    core = Core()
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", LocalDocker(core.events)
    )
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
    assert (
        "AAP_PLUGIN_QUALIFICATION_SHA256=" + hashlib.sha256(canonical).hexdigest()
        in (tmp_path / "state" / "container.env").read_text()
    )


def test_missing_or_changed_qualification_never_starts(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    with pytest.raises(SDKError, match="qualification report is required"):
        deployment.install()
    assert "start" not in core.events
    deployment.install(report_file(tmp_path))
    changed = tmp_path / "changed.json"
    changed.write_text('{"different":true}')
    with pytest.raises(SDKError, match="differs"):
        deployment.install(changed)


@pytest.mark.parametrize(
    "mutation",
    [
        "incomplete",
        "failed",
        "image",
        "artifact",
        "runtime",
        "registry",
        "resource",
        "device",
        "topology",
    ],
)
def test_invalid_qualification_can_be_corrected_before_persistence(tmp_path, mutation):
    core = Core()
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", LocalDocker(core.events)
    )
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
    with pytest.raises(SDKError):
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
                raise SDKError("inspection failed")
            return super().image_id(config)

    core = Core()
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", FailingInspection(core.events)
    )
    deployment.prepare(config())
    source = report_file(tmp_path)
    with pytest.raises(SDKError, match="inspection failed"):
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
                raise SDKError("response lost")
            return result

    core = InterruptedCore()
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", LocalDocker(core.events)
    )
    with pytest.raises(SDKError, match="response lost"):
        deployment.prepare(config())
    result = deployment.prepare(config())
    assert (
        core.requests[0].installation_id
        == core.requests[1].installation_id
        == result.installation.installation_id
    )
    assert core.requests[0].auth_token == core.requests[1].auth_token


def test_active_install_does_not_restart_missing_container(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    docker.running = False
    with pytest.raises(SDKError, match="not running"):
        deployment.install()
    assert core.events == ["start", "activate"]


def test_disable_does_not_stop_without_core_quiescence_proof(tmp_path):
    core = Core()
    docker = LocalDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    core.safe = False
    with pytest.raises(SDKError, match="quiescence"):
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
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    deployment.install(report_file(tmp_path))
    core.dependent = True
    with pytest.raises(SDKError, match="active dependency"):
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
    with pytest.raises(SDKError, match="quiescence"):
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
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", LocalDocker(core.events)
    )
    deployment.prepare(config())
    changed = config().model_copy(update={"name": "different"})
    with pytest.raises(SDKError, match="different configuration"):
        deployment.prepare(changed)
    assert len(core.requests) == 1


def test_manifest_rejects_docker_option_injection():
    body = config().model_dump(mode="json")
    body["manifest"]["artifact"] = "--privileged@sha256:" + "a" * 64
    with pytest.raises(ValueError):
        LocalPluginConfig.model_validate(body)


@pytest.mark.parametrize("replace_host_directory", [False, True])
def test_asset_approval_measures_owned_bytes_and_refuses_changed_process(
    tmp_path, replace_host_directory
):
    import os

    from yoao._plugin_asset_measure import measured_paths
    from yoao_inference_contract.plugin_assets import (
        PluginAssetDescriptor,
        PluginSelectionKey,
    )

    class AssetCore(Core):
        def set_asset_approval(self, installation_id, request):
            self.last_asset_decision = request
            return request

    class AssetDocker(LocalDocker):
        def measure_assets(self, config, container_id, assets):
            root = os.open(
                self.mounted_data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                return measured_paths(root, assets)
            finally:
                os.close(root)

    core = AssetCore()
    docker = AssetDocker(core.events)
    deployment = LocalPluginDeployment(
        core, "https://api.example", tmp_path / "state", docker
    )
    deployment.prepare(config())
    installation = deployment.install(report_file(tmp_path))
    data = tmp_path / "state" / "data"
    (data / "correct.bin").write_bytes(b"correct")
    (data / "corrupt.bin").write_bytes(b"incorrect")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (data / "link.bin").symlink_to(outside)
    manifest = installation.installation.manifest
    descriptor = PluginAssetDescriptor.model_validate(
        {
            "schema_version": 1,
            "plugin_id": manifest.plugin_id,
            "plugin_version": manifest.version,
            "artifact": manifest.artifact,
            "asset_manifest_sha256": manifest.asset_manifest_sha256,
            "selections": [
                {
                    "model_id": "detect",
                    "variants": {},
                    "asset_free": False,
                    "assets": [
                        {
                            "path": "correct.bin",
                            "sha256": hashlib.sha256(b"correct").hexdigest(),
                            "size_bytes": 7,
                        },
                        {
                            "path": "corrupt.bin",
                            "sha256": hashlib.sha256(b"expected").hexdigest(),
                            "size_bytes": 8,
                        },
                        {
                            "path": "link.bin",
                            "sha256": hashlib.sha256(b"outside").hexdigest(),
                            "size_bytes": 7,
                        },
                    ],
                }
            ],
        }
    )
    choices = [PluginSelectionKey(model_id="detect", variants={})]
    docker.mounted_data = data
    if replace_host_directory:
        (data / "correct.bin").write_bytes(b"wrong!!")
        docker.mounted_data = tmp_path / "still-mounted-data"
        data.rename(docker.mounted_data)
        data.mkdir(mode=0o700)
        (data / "correct.bin").write_bytes(b"correct")
    result = deployment.approve_assets(descriptor, choices, 0)
    assert [asset.path for asset in result.inventory] == (
        [] if replace_host_directory else ["correct.bin"]
    )
    docker.started_at = "replaced-process"
    with pytest.raises(SDKError, match="process identity"):
        deployment.approve_assets(descriptor, choices, 1)
