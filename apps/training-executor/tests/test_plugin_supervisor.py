"""Behavioral verification for ML-free dual-mode yoao-trainer/v1 supervision."""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import tomllib
from typing import Any
import uuid
from zipfile import ZipFile

import httpx
import psutil
import pytest
from training_contract.budget import POLICY_DIGEST as BUDGET_POLICY_DIGEST
from training_contract.input_membership import (
    POLICY_DIGEST as INPUT_POLICY_DIGEST,
    contract_for,
    member_records,
    observed_members,
    receipt_for,
)
from training_contract.observability import InputMismatchError
from training_contract.preview import POLICY_DIGEST as PREVIEW_POLICY_DIGEST
from training_contract.trainer_plugin import (
    TRAINER_PROTOCOL_VERSION,
    PreviewReportV1,
    PreviewSpecV1,
    TrainArtifactV1,
    TrainEventV1,
    TrainReceiptV1,
    atomic_write_json,
    canonical_json_digest,
    write_event_jsonl,
)
from yoao.container_runtime import (
    ContainerImageInfo,
    ContainerInspection,
    ContainerRuntimePort,
    ContainerStateInfo,
    DetachedContainerSpec,
    OneShotContainerSpec,
)

from training_executor.files import digest_file
from training_executor.plugin_runner import (
    ContainerJobHandle,
    LeaseRetainedError,
    build_train_container_spec,
    live_group,
    reconcile_orphan_containers,
    validate_plugin_entrypoint,
)
from training_executor.preview_worker import PreviewSupervisor
from training_executor.worker import Supervisor, runtime_identity

CORE_SUPERVISOR_MODULES = (
    "worker.py",
    "preview_worker.py",
    "plugin_runner.py",
    "process_budget.py",
    "log_capture.py",
    "observability.py",
    "files.py",
    "input_membership.py",
)
FORBIDDEN_ML_MODULES = frozenset({"torch", "ultralytics", "libreyolo", "pycocotools"})
PINNED_IMAGE = "registry.test/yoao-trainer-dummy@sha256:" + "a" * 64
GPU_UUID = "GPU-11111111-2222-3333-4444-555555555555"
EXECUTOR_TOKEN = "executor-secret-token-" + "9" * 32
LEASE_TOKEN = "lease-secret-token-" + "8" * 32


class _PipeLogProcess:
    def __init__(self, payload: bytes) -> None:
        r_fd, w_fd = os.pipe()
        os.write(w_fd, payload)
        os.close(w_fd)
        self.stdout = os.fdopen(r_fd, "rb")
        self.returncode: int | None = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class _ContainerRecord:
    def __init__(
        self,
        *,
        container_id: str,
        name: str,
        image_ref: str,
        labels: dict[str, str],
        running: bool,
        exit_code: int = 0,
        refuse_kill: bool = False,
        log_bytes: bytes = b"",
    ) -> None:
        self.container_id = container_id
        self.name = name
        self.image_ref = image_ref
        self.labels = dict(labels)
        self.running = running
        self.exit_code = exit_code
        self.refuse_kill = refuse_kill
        self.log_bytes = log_bytes

    def to_inspection(self) -> ContainerInspection:
        return ContainerInspection(
            container_id=self.container_id,
            name=self.name,
            image_id="sha256:" + "b" * 64,
            image_ref=self.image_ref,
            labels=dict(self.labels),
            host_config={},
            mounts=(),
            user="1000:1000",
            entrypoint=None,
            cmd=None,
            state=ContainerStateInfo(
                running=self.running,
                restarting=False,
                paused=False,
                dead=False,
                started_at="2026-11-26T00:00:00Z",
                finished_at="" if self.running else "2026-11-26T00:00:01Z",
                exit_code=self.exit_code,
            ),
        )


class InMemoryContainerRuntime:
    def __init__(
        self,
        *,
        on_run_detached: Callable[[DetachedContainerSpec, _ContainerRecord], None]
        | None = None,
    ) -> None:
        self.containers: dict[str, _ContainerRecord] = {}
        self.specs: list[DetachedContainerSpec] = []
        self.calls: list[tuple[str, Any]] = []
        self.on_run_detached = on_run_detached
        self._counter = 0

    def add_existing(
        self,
        *,
        name: str,
        labels: Mapping[str, str],
        running: bool = True,
        exit_code: int = 0,
        refuse_kill: bool = False,
    ) -> str:
        self._counter += 1
        cid = hashlib.sha256(f"existing:{self._counter}:{name}".encode()).hexdigest()
        self.containers[cid] = _ContainerRecord(
            container_id=cid,
            name=name,
            image_ref=PINNED_IMAGE,
            labels=dict(labels),
            running=running,
            exit_code=exit_code,
            refuse_kill=refuse_kill,
        )
        return cid

    def inspect_image(self, image: str) -> ContainerImageInfo:
        return ContainerImageInfo(
            image_id="sha256:" + "b" * 64,
            repo_digests=(image,),
            labels={},
        )

    def run_oneshot(self, spec: OneShotContainerSpec) -> str:
        spec.validate()
        self.calls.append(("run_oneshot", spec))
        return ""

    def run_detached(self, spec: DetachedContainerSpec, *, start: bool = True) -> str:
        spec.validate()
        self._counter += 1
        cid = hashlib.sha256(f"run:{self._counter}:{spec.name}".encode()).hexdigest()
        record = _ContainerRecord(
            container_id=cid,
            name=spec.name,
            image_ref=spec.image,
            labels=dict(spec.labels),
            running=start,
            exit_code=0,
        )
        self.specs.append(spec)
        self.containers[cid] = record
        self.calls.append(("run_detached", spec.name))
        if self.on_run_detached is not None:
            self.on_run_detached(spec, record)
        return cid

    def start_container(self, container_id: str) -> None:
        self.containers[container_id].running = True
        self.calls.append(("start_container", container_id))

    def inspect_container(
        self,
        *,
        name: str | None = None,
        container_id: str | None = None,
    ) -> ContainerInspection | None:
        if container_id is not None:
            record = self.containers.get(container_id)
            return record.to_inspection() if record is not None else None
        for record in self.containers.values():
            if record.name == name:
                return record.to_inspection()
        return None

    def list_containers(
        self,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> tuple[ContainerInspection, ...]:
        self.calls.append(("list_containers", dict(labels or {})))
        matched = []
        for record in self.containers.values():
            if labels and any(record.labels.get(k) != v for k, v in labels.items()):
                continue
            matched.append(record.to_inspection())
        return tuple(matched)

    def exec_in_container(
        self,
        container_id: str,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> str:
        self.calls.append(("exec_in_container", container_id))
        return ""

    def kill_container(self, container_id: str, *, signal: str = "KILL") -> None:
        self.calls.append(("kill_container", (container_id, signal)))
        record = self.containers.get(container_id)
        if record is None or not record.running:
            return
        if record.refuse_kill:
            return
        record.running = False
        record.exit_code = -9 if signal == "KILL" else -15

    def wait_container(self, container_id: str, *, timeout: float | None = None) -> int:
        self.calls.append(("wait_container", container_id))
        record = self.containers[container_id]
        record.running = False
        return record.exit_code

    def remove_container(self, container_id: str, *, force: bool = False) -> None:
        self.calls.append(("remove_container", (container_id, force)))
        self.containers.pop(container_id, None)

    def open_log_stream(self, container_id: str) -> Any:
        self.calls.append(("open_log_stream", container_id))
        record = self.containers[container_id]
        return _PipeLogProcess(record.log_bytes)

    def stop_and_remove(
        self, container_id: str, *, expected_started_at: str | None = None
    ) -> None:
        self.kill_container(container_id, signal="TERM")
        self.remove_container(container_id, force=True)


def _write_dummy_trainer_script(path: Path) -> Path:
    source = """
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from training_contract.trainer_plugin import (
    TRAINER_PROTOCOL_VERSION,
    PreviewOutputV1,
    PreviewReportV1,
    PreviewSpecV1,
    ServingCompatibilityV1,
    TrainArtifactV1,
    TrainEventV1,
    TrainReceiptV1,
    TrainSpecV1,
    atomic_write_json,
    write_event_jsonl,
)


def sha256_file(file_path: Path) -> str:
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def run_train(spec_path: Path, output_dir: Path) -> int:
    spec = TrainSpecV1.model_validate(json.loads(spec_path.read_text()))
    output_dir.mkdir(parents=True, exist_ok=True)
    env_dump = {key: os.environ[key] for key in sorted(os.environ)}
    (output_dir / "env-check.json").write_text(json.dumps(env_dump))
    mode = spec.parameters.get("dummy_mode", "succeed")
    if spec.parameters.get("echo_secrets"):
        print(
            "secret-leak " + " ".join(spec.parameters["echo_secrets"]),
            flush=True,
        )
    if mode == "sleep_with_grandchild":
        grandchild = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        (output_dir / "grandchild.pid").write_text(str(grandchild.pid))
        print("ready", flush=True)
        time.sleep(60)
        return 0
    if mode == "sleep_budget":
        print("budget-running", flush=True)
        time.sleep(60)
        return 0
    if mode == "fail_receipt":
        receipt = TrainReceiptV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            status="failed",
            failure_code=spec.parameters.get("failure_code", "cuda_oom"),
            failure_message=spec.parameters.get(
                "failure_message", "synthetic out of memory"
            ),
        )
        atomic_write_json(output_dir / "result.json", receipt)
        return 1
    write_event_jsonl(
        output_dir / "events.jsonl",
        TrainEventV1(
            seq=1,
            kind="phase",
            phase="training",
            metrics={"loss": 1.5, "label": "ignored", "flag": True},
        ),
    )
    write_event_jsonl(
        output_dir / "events.jsonl",
        TrainEventV1(
            seq=2,
            kind="epoch",
            phase="training",
            epoch=1,
            total_epochs=2,
            progress=0.5,
            metrics={"train/loss": 0.42, "val/mAP50": 0.85, "note": "skip"},
        ),
    )
    write_event_jsonl(
        output_dir / "events.jsonl",
        TrainEventV1(
            seq=3,
            kind="phase",
            phase="evaluating",
            metrics={"eval/mAP50": 0.88},
        ),
    )
    weights = output_dir / "best.bin"
    weights.write_bytes(b"trained-weights-payload")
    checkpoint = output_dir / "checkpoint-0001.bin"
    checkpoint.write_bytes(b"checkpoint-payload")
    metrics = output_dir / "metrics.json"
    metrics.write_text(json.dumps({"summary": {"mAP50": 0.88}}))
    native_cfg = output_dir / "native-config.json"
    native_cfg.write_text(json.dumps({"config_digest": spec.config_digest}))
    extra_meta = output_dir / "extra-meta.json"
    extra_meta.write_text(json.dumps({"dataset_dir_exists": Path(spec.dataset_dir).is_dir()}))
    weights_sha = "0" * 64 if mode == "bad_artifact_sha" else sha256_file(weights)
    receipt = TrainReceiptV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        status="succeeded",
        artifacts=[
            TrainArtifactV1(
                role="weights",
                filename=weights.name,
                sha256=weights_sha,
                size_bytes=weights.stat().st_size,
            ),
            TrainArtifactV1(
                role="checkpoint",
                filename=checkpoint.name,
                sha256=sha256_file(checkpoint),
                size_bytes=checkpoint.stat().st_size,
            ),
            TrainArtifactV1(
                role="metrics",
                filename=metrics.name,
                sha256=sha256_file(metrics),
                size_bytes=metrics.stat().st_size,
            ),
            TrainArtifactV1(
                role="config",
                filename=native_cfg.name,
                sha256=sha256_file(native_cfg),
                size_bytes=native_cfg.stat().st_size,
            ),
            TrainArtifactV1(
                role="extra",
                filename=extra_meta.name,
                sha256=sha256_file(extra_meta),
                size_bytes=extra_meta.stat().st_size,
            ),
        ],
        serving_compatibility=ServingCompatibilityV1(
            framework=spec.framework_id,
            format=None,
            training_runtime={"python": "3.12"},
            compatible_serving_runtimes=[{"framework": spec.framework_id}],
        ),
    )
    atomic_write_json(output_dir / "result.json", receipt)
    return 0


def run_preview(spec_path: Path, output_dir: Path) -> int:
    spec = PreviewSpecV1.model_validate(json.loads(spec_path.read_text()))
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = spec.parameters.get("preview_mode", "succeed")
    if mode == "sleep_timeout":
        time.sleep(60)
        return 0
    assert os.environ.get("CUDA_VISIBLE_DEVICES") == ""
    assert os.environ.get("NVIDIA_VISIBLE_DEVICES") == "void"
    assert os.environ.get("OMP_NUM_THREADS") == "1"
    for forbidden in ("TRAINING_EXECUTOR_TOKEN", "DATABASE_URL", "AWS_SECRET_ACCESS_KEY", "SECRET_KEY"):
        assert forbidden not in os.environ
    png = output_dir / "preview-0.png"
    png_bytes = b"\\x89PNG\\r\\n\\x1a\\npreview-dummy-image-bytes"
    png.write_bytes(png_bytes)
    report = {
        "protocol_version": TRAINER_PROTOCOL_VERSION,
        "status": "succeeded",
        "input_digest": (
            "f" * 64 if mode == "bad_input_digest" else spec.input_digest
        ),
        "policy_digest": (
            "e" * 64 if mode == "bad_policy_digest" else spec.policy_digest
        ),
        "cuda_initialized": mode == "cuda_initialized",
        "outputs": [
            {
                "filename": png.name,
                "sha256": (
                    "0" * 64 if mode == "bad_output_sha" else sha256_file(png)
                ),
                "size_bytes": len(png_bytes),
                "width": 64,
                "height": 64,
                "box_count": 1,
                "metadata": {},
            }
        ],
        "warnings": [],
        "runtime": {"framework_id": spec.framework_id},
        "details": {},
        "failure_code": None,
        "failure_message": None,
    }
    if mode == "cuda_initialized":
        (output_dir / "report.json").write_text(json.dumps(report))
    else:
        atomic_write_json(
            output_dir / "report.json", PreviewReportV1.model_validate(report)
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("train", "preview"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--spec", type=Path, required=True)
        cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.cmd == "train":
        return run_train(args.spec, args.output)
    return run_preview(args.spec, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
"""
    path.write_text(source)
    return path


def _make_dataset_zip(path: Path) -> tuple[Path, str]:
    with ZipFile(path, "w") as archive:
        archive.writestr("images/train/sample.png", b"dummy-image-bytes")
        archive.writestr("annotations.json", json.dumps({"samples": ["sample.png"]}))
    return path, digest_file(path)


def _make_plugin_supervisor(
    tmp_path: Path,
    *,
    device_kind: str = "cpu",
    gpu_uuid: str | None = None,
    container_runtime: ContainerRuntimePort | None = None,
    local_plugins: dict[str, dict[str, Any]] | None = None,
) -> Supervisor:
    supervisor = object.__new__(Supervisor)
    supervisor.state = tmp_path / "state"
    supervisor.state.mkdir(parents=True, exist_ok=True)
    supervisor.models = tmp_path / "models"
    supervisor.models.mkdir(parents=True, exist_ok=True)
    supervisor.boot = str(uuid.uuid4())
    supervisor.executor_id = "executor-test"
    supervisor.container_runtime = container_runtime
    supervisor.local_plugins = dict(local_plugins or {})
    supervisor.runtime = {
        "executor_protocol": 3,
        "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
        "execution_modes": [
            mode
            for mode, enabled in (
                ("container", container_runtime is not None),
                ("local_process", bool(supervisor.local_plugins)),
            )
            if enabled
        ],
        "allowed_local_plugins": sorted(supervisor.local_plugins),
        "device_kind": device_kind,
        "gpu_uuid": gpu_uuid,
        "gpu_name": "NVIDIA Test GPU" if gpu_uuid else None,
        "driver_version": "560.35.03" if gpu_uuid else None,
        "image_digest": None,
        "frameworks": {},
    }
    supervisor.resource = f"node-a/{gpu_uuid}" if gpu_uuid else "node-a/cpu-0"
    supervisor.redaction_secrets = (EXECUTOR_TOKEN,)
    supervisor.client = httpx.Client(
        base_url="http://platform.test/internal/training/",
        headers={"X-Training-Executor-Token": EXECUTOR_TOKEN},
        trust_env=False,
    )
    supervisor.journal_path = supervisor.state / "assignment.json"
    supervisor.shutdown = threading.Event()
    supervisor.stop_training = threading.Event()
    supervisor.heartbeat_done = threading.Event()
    supervisor.heartbeat_thread = None
    supervisor.stop_reason = None
    supervisor.lock = (supervisor.state / "slot.lock").open("a+")
    fcntl.flock(supervisor.lock, fcntl.LOCK_EX)
    supervisor.active_process = None
    supervisor.active_budget = None
    supervisor.active_capture = None
    supervisor.retiring_observation = None
    supervisor.heartbeat = lambda *args: None
    return supervisor


def _make_plugin_journal(
    supervisor: Supervisor,
    *,
    dataset_sha256: str,
    execution_mode: str,
    plugin_id: str = "yoao.trainer.dummy",
    entrypoint: list[str] | None = None,
    image: str | None = PINNED_IMAGE,
    requires_gpu: bool = False,
    parameters: dict[str, Any] | None = None,
    resource: dict[str, Any] | None = None,
    native_budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    trainer_plugin: dict[str, Any] = {
        "plugin_id": plugin_id,
        "execution_mode": execution_mode,
        "requires_gpu": requires_gpu,
    }
    if entrypoint is not None:
        trainer_plugin["entrypoint"] = entrypoint
    if image is not None and execution_mode == "container":
        trainer_plugin["image"] = image
    if resource is not None:
        trainer_plugin["resource"] = resource
    config: dict[str, Any] = {
        "trainer_protocol": TRAINER_PROTOCOL_VERSION,
        "adapter_revision": 3,
        "framework": "dummy",
        "task": "detect",
        "architecture_id": "dummy-net",
        "dataset_id": str(uuid.uuid4()),
        "dataset_contract": "custom-json-v1",
        "dataset_sha256": dataset_sha256,
        "dataset_digest": "1" * 64,
        "classes": ["widget", "gadget"],
        "parameters": dict(parameters or {"epochs": 2}),
        "trainer_plugin": trainer_plugin,
    }
    config_digest = canonical_json_digest(config)
    assignment: dict[str, Any] = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "lease_token": LEASE_TOKEN,
        "config": config,
        "config_digest": config_digest,
        "dataset_url": "https://storage.test/dataset.zip",
        "resume": None,
    }
    if native_budget is not None:
        assignment["native_budget"] = {
            **native_budget,
            "attempt_id": attempt_id,
            "config_digest": config_digest,
        }
    return {
        "phase": "preparing",
        "claim": {
            "executor_id": supervisor.executor_id,
            "boot_id": supervisor.boot,
            "runtime": supervisor.runtime,
        },
        "assignment": assignment,
    }


def test_core_supervisor_modules_and_packaging_are_ml_free():
    executor_root = Path(__file__).resolve().parents[1]
    package_dir = executor_root / "training_executor"
    for filename in CORE_SUPERVISOR_MODULES:
        source = (package_dir / filename).read_text()
        tree = ast.parse(source, filename=filename)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_name = alias.name.split(".", 1)[0]
                    assert root_name not in FORBIDDEN_ML_MODULES, (
                        f"{filename} imports forbidden ML package {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                root_name = node.module.split(".", 1)[0]
                assert root_name not in FORBIDDEN_ML_MODULES, (
                    f"{filename} imports from forbidden ML package {node.module}"
                )
    pyproject = tomllib.loads((executor_root / "pyproject.toml").read_text())
    core_deps = pyproject["project"]["dependencies"]
    assert core_deps == ["yoao-sdk", "httpx==0.28.1", "psutil==7.0.0"]
    legacy_ml = pyproject["project"]["optional-dependencies"]["legacy-ml"]
    assert any(dep.startswith("torch==") for dep in legacy_ml)
    assert any(dep.startswith("ultralytics==") for dep in legacy_ml)
    assert any(dep.startswith("libreyolo==") for dep in legacy_ml)
    assert pyproject["dependency-groups"]["handoff"] == [
        "httpx",
        "psutil",
        "yoao-sdk",
    ]
    dockerfile = (executor_root / "Dockerfile.supervisor").read_text()
    assert "FROM python:3.12-slim" in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "training_executor.worker"]' in dockerfile
    for forbidden in ("pytorch", "torch", "cuda", "ultralytics", "libreyolo"):
        assert forbidden not in dockerfile.lower()


def test_runtime_identity_ml_free_gpu_and_cpu_slots(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAINING_MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("TRAINING_SUPERVISOR_MODE", "plugin")
    monkeypatch.setenv("TRAINING_GPU_UUID", GPU_UUID)
    monkeypatch.setenv("TRAINING_CONTAINER_RUNTIME", "none")
    monkeypatch.setenv(
        "TRAINING_LOCAL_PLUGINS_JSON",
        json.dumps({"yoao.trainer.dummy": {"entrypoint": ["dummy-trainer"]}}),
    )
    monkeypatch.delenv("TRAINING_IMAGE_REFERENCE", raising=False)
    monkeypatch.delenv("TRAINING_DEVICE_KIND", raising=False)
    calls: list[list[str]] = []

    def fake_check_output(cmd, **kwargs):
        calls.append(list(cmd))
        if "--query-gpu=uuid" in cmd:
            return f"{GPU_UUID}\n"
        if "--query-gpu=name,driver_version" in cmd:
            return "NVIDIA RTX 4090, 560.35.03\n"
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(
        "training_executor.worker.subprocess.check_output", fake_check_output
    )
    gpu_identity = runtime_identity()
    assert gpu_identity["executor_protocol"] == 3
    assert gpu_identity["trainer_protocols"] == [TRAINER_PROTOCOL_VERSION]
    assert gpu_identity["device_kind"] == "gpu"
    assert gpu_identity["gpu_uuid"] == GPU_UUID
    assert gpu_identity["gpu_name"] == "NVIDIA RTX 4090"
    assert gpu_identity["driver_version"] == "560.35.03"
    assert gpu_identity["image_digest"] is None
    assert gpu_identity["execution_modes"] == ["local_process"]
    assert gpu_identity["allowed_local_plugins"] == ["yoao.trainer.dummy"]
    assert gpu_identity["frameworks"] == {}
    assert gpu_identity["native_budget_policy_digest"] == BUDGET_POLICY_DIGEST
    assert gpu_identity["input_receipt_policy_digest"] == INPUT_POLICY_DIGEST
    assert gpu_identity["preview_policy_digest"] == PREVIEW_POLICY_DIGEST
    assert len(calls) == 2

    monkeypatch.setenv("TRAINING_IMAGE_REFERENCE", "mutable:latest")
    with pytest.raises(ValueError, match="immutable"):
        runtime_identity()

    monkeypatch.setenv("TRAINING_IMAGE_REFERENCE", PINNED_IMAGE)
    assert runtime_identity()["image_digest"] == "sha256:" + "a" * 64

    calls.clear()
    monkeypatch.delenv("TRAINING_IMAGE_REFERENCE", raising=False)
    monkeypatch.delenv("TRAINING_GPU_UUID", raising=False)
    monkeypatch.setenv("TRAINING_DEVICE_KIND", "cpu")
    monkeypatch.setenv("TRAINING_CPU_SLOT", "2")
    monkeypatch.setenv("TRAINING_NODE_ID", "node-cpu")
    monkeypatch.setenv("TRAINING_STATE_DIR", str(tmp_path / "cpu-state"))
    monkeypatch.setenv("TRAINING_API_URL", "http://platform.test/internal/training")
    monkeypatch.setenv("TRAINING_EXECUTOR_TOKEN", EXECUTOR_TOKEN)
    cpu_identity = runtime_identity()
    assert calls == []
    assert cpu_identity["executor_protocol"] == 3
    assert cpu_identity["device_kind"] == "cpu"
    assert cpu_identity["gpu_uuid"] is None
    assert cpu_identity["gpu_name"] is None
    assert cpu_identity["driver_version"] is None
    assert cpu_identity["frameworks"] == {}
    supervisor = Supervisor(container_runtime=None)
    try:
        assert supervisor.resource == "node-cpu/cpu-2"
        assert supervisor.runtime["execution_modes"] == ["local_process"]
    finally:
        supervisor.client.close()


@pytest.mark.parametrize(
    "bad_entrypoint",
    [
        ["sh", "-c", "echo bad"],
        ["/bin/bash", "run.sh"],
        ["python", "-c", "print(1)"],
        ["/usr/bin/python3", "-uc", "print(1)"],
        ["../relative/trainer"],
    ],
)
def test_entrypoint_validation_rejects_shells_and_python_c(bad_entrypoint):
    with pytest.raises(InputMismatchError):
        validate_plugin_entrypoint(bad_entrypoint, allow_absolute_head=True)


def test_container_sandbox_write_before_spawn_and_log_redaction(tmp_path, monkeypatch):
    dataset_zip, dataset_sha = _make_dataset_zip(tmp_path / "dataset.zip")
    observed_journal_before_spawn: list[dict[str, Any]] = []

    def on_run_detached(spec: DetachedContainerSpec, record: _ContainerRecord) -> None:
        persisted = json.loads(supervisor.journal_path.read_text())
        observed_journal_before_spawn.append(persisted)
        output_mount = next(m for m in spec.mounts if m.target == "/work/output")
        output_dir = output_mount.source
        write_event_jsonl(
            output_dir / "events.jsonl",
            TrainEventV1(
                seq=1,
                kind="epoch",
                epoch=1,
                total_epochs=1,
                metrics={"loss": 0.25},
            ),
        )
        weight_path = output_dir / "best.bin"
        weight_path.write_bytes(b"container-weights")
        receipt = TrainReceiptV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            status="succeeded",
            artifacts=[
                TrainArtifactV1(
                    role="weights",
                    filename="best.bin",
                    sha256=digest_file(weight_path),
                    size_bytes=weight_path.stat().st_size,
                )
            ],
        )
        atomic_write_json(output_dir / "result.json", receipt)
        record.log_bytes = (
            f"container stdout token={EXECUTOR_TOKEN} lease={LEASE_TOKEN}\n".encode()
        )
        record.running = False
        record.exit_code = 0

    runtime = InMemoryContainerRuntime(on_run_detached=on_run_detached)
    assert isinstance(runtime, ContainerRuntimePort)
    supervisor = _make_plugin_supervisor(
        tmp_path, device_kind="gpu", gpu_uuid=GPU_UUID, container_runtime=runtime
    )
    supervisor.download = lambda url, target, *args, **kwargs: target.write_bytes(
        dataset_zip.read_bytes()
    )
    published: list[tuple[dict[str, Any], Path]] = []

    def fake_publish(cur_journal: dict[str, Any], directory: Path) -> bool:
        published.append((copy.deepcopy(cur_journal), directory))
        return False

    supervisor.upload_and_finish = fake_publish
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession",
        lambda *args: (_ for _ in ()).throw(RuntimeError("obs disabled")),
    )
    try:
        gpu_journal = _make_plugin_journal(
            supervisor,
            dataset_sha256=dataset_sha,
            execution_mode="container",
            entrypoint=["yoao-trainer-dummy"],
            image=PINNED_IMAGE,
            requires_gpu=True,
            resource={
                "memory_mb": 8192,
                "cpus": 4.0,
                "pids_limit": 192,
                "shm_size_mb": 1024,
            },
        )
        supervisor.execute(gpu_journal)
        assert len(observed_journal_before_spawn) == 1
        persisted = observed_journal_before_spawn[0]
        assert persisted["spawn_intent"] is True
        assert persisted["execution_mode"] == "container"
        assert persisted["container_name"] == runtime.specs[0].name
        assert persisted["container_labels"] == dict(runtime.specs[0].labels)

        spec = runtime.specs[0]
        assert spec.network == "none"
        assert spec.gpu_uuid == GPU_UUID
        assert spec.memory_mb == 8192
        assert spec.cpus == 4.0
        assert spec.pids_limit == 192
        assert spec.shm_size_mb == 1024
        assert spec.user == f"{os.getuid()}:{os.getgid()}"
        assert spec.env == ()
        mounts = {mount.target: mount for mount in spec.mounts}
        assert mounts["/work/input"].readonly is True
        assert mounts["/work/output"].readonly is False
        assert mounts["/models"].readonly is True
        assert spec.command == (
            "yoao-trainer-dummy",
            "train",
            "--spec",
            "/work/input/spec.json",
            "--output",
            "/work/output",
        )
        assert runtime.containers == {}
        assert published[0][0]["outcome"] == "succeeded"
        log_text = (published[0][1] / "train.log").read_text()
        assert EXECUTOR_TOKEN not in log_text
        assert LEASE_TOKEN not in log_text
        assert "container stdout" in log_text

        cpu_spec = build_train_container_spec(
            resource_id=supervisor.resource,
            attempt_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()),
            plugin_id="yoao.trainer.dummy",
            image=PINNED_IMAGE,
            entrypoint=["yoao-trainer-dummy"],
            input_dir=tmp_path / "in",
            output_dir=tmp_path / "out",
            models_dir=supervisor.models,
            requires_gpu=False,
            slot_gpu_uuid=GPU_UUID,
        )
        assert cpu_spec.gpu_uuid is None
        assert ("NVIDIA_VISIBLE_DEVICES", "void") in cpu_spec.env
        assert ("CUDA_VISIBLE_DEVICES", "") in cpu_spec.env
        assert cpu_spec.pids_limit == 256
        assert cpu_spec.shm_size_mb == 512
    finally:
        supervisor.lock.close()
        supervisor.client.close()


def test_startup_reconciles_orphan_containers_before_claim(tmp_path):
    runtime = InMemoryContainerRuntime()
    supervisor = _make_plugin_supervisor(
        tmp_path, device_kind="gpu", gpu_uuid=GPU_UUID, container_runtime=runtime
    )
    orphan_running = runtime.add_existing(
        name="yoao-trainer-deadbeef00000001",
        labels={
            "yoao.trainer.managed": "true",
            "yoao.trainer.gpu_resource_id": supervisor.resource,
            "yoao.trainer.attempt_id": "orphan-1",
        },
        running=True,
    )
    orphan_exited = runtime.add_existing(
        name="yoao-trainer-deadbeef00000002",
        labels={
            "yoao.trainer.managed": "true",
            "yoao.trainer.gpu_resource_id": supervisor.resource,
            "yoao.trainer.attempt_id": "orphan-2",
        },
        running=False,
    )
    other_gpu = runtime.add_existing(
        name="yoao-trainer-deadbeef00000003",
        labels={
            "yoao.trainer.managed": "true",
            "yoao.trainer.gpu_resource_id": "node-a/GPU-other",
            "yoao.trainer.attempt_id": "other-1",
        },
        running=True,
    )
    claim_events: list[str] = []

    def fake_claim(journal: dict[str, Any]) -> None:
        assert orphan_running not in runtime.containers
        assert orphan_exited not in runtime.containers
        assert other_gpu in runtime.containers
        claim_events.append("claimed")
        supervisor.shutdown.set()
        return None

    supervisor.claim = fake_claim
    supervisor.lock.close()
    try:
        supervisor.run_training()
        assert claim_events == ["claimed"]
        assert orphan_running not in runtime.containers
        assert orphan_exited not in runtime.containers
        assert other_gpu in runtime.containers
    finally:
        if supervisor.lock is not None:
            supervisor.lock.close()
        supervisor.client.close()


def test_failed_container_kill_retains_lease_and_never_publishes_exit(
    tmp_path, monkeypatch
):
    runtime = InMemoryContainerRuntime()
    supervisor = _make_plugin_supervisor(
        tmp_path, device_kind="gpu", gpu_uuid=GPU_UUID, container_runtime=runtime
    )
    stuck_cid = runtime.add_existing(
        name="yoao-trainer-stuck00000000001",
        labels={
            "yoao.trainer.managed": "true",
            "yoao.trainer.gpu_resource_id": supervisor.resource,
        },
        running=True,
        refuse_kill=True,
    )
    with pytest.raises(LeaseRetainedError):
        reconcile_orphan_containers(
            runtime,
            supervisor.resource,
            term_grace_seconds=0.02,
            kill_grace_seconds=0.02,
            poll_interval_seconds=0.01,
        )
    assert stuck_cid in runtime.containers

    monkeypatch.setattr(
        "training_executor.worker.reconcile_container_by_id_or_name",
        lambda rt, **kwargs: (_ for _ in ()).throw(
            LeaseRetainedError("Container still running")
        ),
    )
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession",
        lambda *args: (_ for _ in ()).throw(RuntimeError("obs disabled")),
    )
    published: list[bool] = []
    supervisor.upload_and_finish = lambda *args: published.append(True) or True
    dataset_zip, dataset_sha = _make_dataset_zip(tmp_path / "dataset.zip")
    journal = _make_plugin_journal(
        supervisor,
        dataset_sha256=dataset_sha,
        execution_mode="container",
        entrypoint=["yoao-trainer-dummy"],
    )
    journal["container_id"] = stuck_cid
    journal["container_name"] = "yoao-trainer-stuck00000000001"
    try:
        with pytest.raises(LeaseRetainedError):
            supervisor.execute(journal, recovery=True)
        assert published == []
        assert "outcome" not in journal
    finally:
        supervisor.lock.close()
        supervisor.client.close()

    handle = ContainerJobHandle(
        runtime,
        stuck_cid,
        name="yoao-trainer-stuck00000000001",
        term_grace_seconds=0.02,
        kill_grace_seconds=0.02,
        poll_interval_seconds=0.01,
    )
    with pytest.raises(LeaseRetainedError):
        handle.stop()
    assert handle.is_alive() is True


def test_active_container_cancel_failure_retains_lease_and_never_finishes(
    tmp_path, monkeypatch
):
    dataset_zip, dataset_sha = _make_dataset_zip(tmp_path / "dataset.zip")

    def on_run_detached(spec: DetachedContainerSpec, record: _ContainerRecord) -> None:
        record.running = True
        record.refuse_kill = True
        supervisor.stop_training.set()

    runtime = InMemoryContainerRuntime(on_run_detached=on_run_detached)
    supervisor = _make_plugin_supervisor(
        tmp_path, device_kind="gpu", gpu_uuid=GPU_UUID, container_runtime=runtime
    )
    supervisor.download = lambda url, target, *args, **kwargs: target.write_bytes(
        dataset_zip.read_bytes()
    )
    published: list[bool] = []
    supervisor.upload_and_finish = lambda *args: published.append(True) or True
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession",
        lambda *args: (_ for _ in ()).throw(RuntimeError("obs disabled")),
    )
    from training_executor import plugin_runner

    original_launch = plugin_runner.launch_container_job
    monkeypatch.setattr(
        "training_executor.worker.launch_container_job",
        lambda rt, spec, **kwargs: original_launch(
            rt,
            spec,
            term_grace_seconds=0.02,
            kill_grace_seconds=0.02,
            poll_interval_seconds=0.01,
            **kwargs,
        ),
    )
    journal = _make_plugin_journal(
        supervisor,
        dataset_sha256=dataset_sha,
        execution_mode="container",
        entrypoint=["yoao-trainer-dummy"],
    )
    try:
        with pytest.raises(LeaseRetainedError):
            supervisor.execute(journal)
        assert published == []
        assert supervisor.active_process is not None
        assert supervisor.active_process.is_alive() is True
    finally:
        supervisor.lock.close()
        supervisor.client.close()


def test_container_plugin_embeds_verified_input_membership_in_manifest(
    tmp_path, monkeypatch
):
    dataset_zip, dataset_sha = _make_dataset_zip(tmp_path / "dataset.zip")
    sample_sha = hashlib.sha256(b"img").hexdigest()
    label_sha = hashlib.sha256(b"lbl").hexdigest()
    dataset_manifest = {
        "classes": ["widget"],
        "samples": [
            {
                "task_id": str(uuid.UUID(int=1)),
                "split": "train",
                "image_path": "images/train/0.png",
                "label_path": "labels/train/0.txt",
                "sha256": sample_sha,
                "label_sha256": label_sha,
                "width": 32,
                "height": 32,
                "size_bytes": 3,
                "instances": [],
            },
            {
                "task_id": str(uuid.UUID(int=2)),
                "split": "val",
                "image_path": "images/val/1.png",
                "label_path": "labels/val/1.txt",
                "sha256": sample_sha,
                "label_sha256": label_sha,
                "width": 32,
                "height": 32,
                "size_bytes": 3,
                "instances": [],
            },
        ],
    }
    input_contract = contract_for(dataset_manifest)
    records = member_records(dataset_manifest)
    observed = {
        "train": observed_members(records, "train", ["images/train/0.png"]),
        "val": observed_members(records, "val", ["images/val/1.png"]),
    }

    def on_run_detached(spec: DetachedContainerSpec, record: _ContainerRecord) -> None:
        output_dir = next(m for m in spec.mounts if m.target == "/work/output").source
        proof = receipt_for(
            journal["assignment"]["config"],
            journal["assignment"]["run_id"],
            journal["assignment"]["attempt_id"],
            observed,
        )
        membership_path = output_dir / "input-membership.json"
        membership_path.write_text(json.dumps(proof))
        weights_path = output_dir / "best.bin"
        weights_path.write_bytes(b"weights")
        receipt = TrainReceiptV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            status="succeeded",
            artifacts=[
                TrainArtifactV1(
                    role="weights",
                    filename="best.bin",
                    sha256=digest_file(weights_path),
                    size_bytes=weights_path.stat().st_size,
                ),
                TrainArtifactV1(
                    role="input_membership",
                    filename="input-membership.json",
                    sha256=digest_file(membership_path),
                    size_bytes=membership_path.stat().st_size,
                ),
            ],
        )
        atomic_write_json(output_dir / "result.json", receipt)
        record.running = False
        record.exit_code = 0

    runtime = InMemoryContainerRuntime(on_run_detached=on_run_detached)
    supervisor = _make_plugin_supervisor(
        tmp_path, device_kind="cpu", container_runtime=runtime
    )
    supervisor.download = lambda url, target, *args, **kwargs: target.write_bytes(
        dataset_zip.read_bytes()
    )
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession",
        lambda *args: (_ for _ in ()).throw(RuntimeError("obs disabled")),
    )
    journal = _make_plugin_journal(
        supervisor,
        dataset_sha256=dataset_sha,
        execution_mode="container",
        entrypoint=["yoao-trainer-dummy"],
    )
    journal["assignment"]["config"]["input_contract"] = input_contract
    journal["assignment"]["config_digest"] = canonical_json_digest(
        journal["assignment"]["config"]
    )
    published_dirs: list[Path] = []
    supervisor.upload_and_finish = lambda cur, directory: (
        published_dirs.append(directory) or False
    )
    try:
        supervisor.execute(journal)
        assert journal["outcome"] == "succeeded"
        inventory = supervisor.inventory(journal, published_dirs[0])
        names = [item["name"] for item in inventory]
        assert "input-membership.json" not in names
        assert "manifest.json" in names
        manifest = json.loads((published_dirs[0] / "manifest.json").read_text())
        assert manifest["input_membership"]["counts"] == {"train": 1, "val": 1}
        assert (
            manifest["input_membership"]["members_digest"]
            == input_contract["members_digest"]
        )
    finally:
        supervisor.lock.close()
        supervisor.client.close()


def test_local_process_plugin_success_events_redaction_and_artifacts(
    tmp_path, monkeypatch
):
    trainer_script = _write_dummy_trainer_script(tmp_path / "dummy_trainer.py")
    dataset_zip, dataset_sha = _make_dataset_zip(tmp_path / "dataset.zip")
    monkeypatch.setenv("TRAINING_EXECUTOR_TOKEN", EXECUTOR_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgres://secret-db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "minio-secret")
    monkeypatch.setenv("SECRET_KEY", "app-secret")
    monkeypatch.setenv("JWT_SECRET", "jwt-secret")
    supervisor = _make_plugin_supervisor(
        tmp_path,
        device_kind="cpu",
        local_plugins={
            "yoao.trainer.dummy": {"entrypoint": [sys.executable, str(trainer_script)]}
        },
    )
    supervisor.download = lambda url, target, *args, **kwargs: target.write_bytes(
        dataset_zip.read_bytes()
    )
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession",
        lambda *args: (_ for _ in ()).throw(RuntimeError("obs disabled")),
    )
    api_calls: list[tuple[str, dict[str, Any]]] = []
    uploaded_files: dict[str, bytes] = {}

    def fake_api(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        suffix = endpoint.rsplit("/", 1)[-1]
        api_calls.append((suffix, copy.deepcopy(payload)))
        if suffix == "uploads":
            return {
                item["name"]: {
                    "url": "https://storage.test/" + item["name"],
                    "headers": {},
                }
                for item in payload["artifacts"]
            }
        return {"stop": False, "reason": None}

    def fake_put(url: str, *, content: Any, **kwargs: Any) -> httpx.Response:
        uploaded_files[url.rsplit("/", 1)[-1]] = content.read()
        return httpx.Response(200, request=httpx.Request("PUT", url))

    supervisor.api = fake_api
    monkeypatch.setattr(httpx, "put", fake_put)
    journal = _make_plugin_journal(
        supervisor,
        dataset_sha256=dataset_sha,
        execution_mode="local_process",
        entrypoint=[sys.executable, str(trainer_script)],
        requires_gpu=False,
        parameters={
            "epochs": 2,
            "dummy_mode": "succeed",
            "echo_secrets": [EXECUTOR_TOKEN, LEASE_TOKEN],
        },
    )
    attempt_dir = supervisor.state / "attempts" / journal["assignment"]["attempt_id"]
    try:
        supervisor.execute(journal)
        assert journal["outcome"] == "succeeded"
        assert not attempt_dir.exists()
        heartbeats = [body for suffix, body in api_calls if suffix == "heartbeat"]
        assert len(heartbeats) == 1
        events = heartbeats[0]["events"]
        assert [event["sequence"] for event in events] == [1, 2, 3]
        assert events[0]["kind"] == "training"
        assert events[0]["metrics"] == {"loss": 1.5}
        assert events[1] == {
            "sequence": 2,
            "kind": "epoch",
            "epoch": 1,
            "metrics": {"train/loss": 0.42, "val/mAP50": 0.85},
        }
        assert events[2]["kind"] == "evaluating"
        uploads_payload = next(
            body for suffix, body in api_calls if suffix == "uploads"
        )
        artifacts_by_name = {
            item["name"]: item for item in uploads_payload["artifacts"]
        }
        assert len(artifacts_by_name) <= 12
        assert artifacts_by_name["best.bin"]["role"] == "weights"
        assert artifacts_by_name["checkpoint-0001.bin"]["role"] == "checkpoint"
        assert artifacts_by_name["checkpoint-0001.bin"]["epoch"] == 1
        assert artifacts_by_name["metrics.json"]["role"] == "metrics"
        assert artifacts_by_name["extra-meta.json"]["role"] == "config"
        assert artifacts_by_name["result.json"]["role"] == "config"
        assert artifacts_by_name["config.json"]["role"] == "config"
        assert artifacts_by_name["train.log"]["role"] == "log"
        assert artifacts_by_name["manifest.json"]["role"] == "manifest"
        log_text = uploaded_files["train.log"].decode("utf-8")
        assert EXECUTOR_TOKEN not in log_text
        assert LEASE_TOKEN not in log_text
        assert "[credential redacted]" in log_text
        manifest = json.loads(uploaded_files["manifest.json"])
        assert manifest["serving_compatibility"]["framework"] == "dummy"
        assert manifest["serving_compatibility"]["format"] is None
        extra_meta = json.loads(uploaded_files["extra-meta.json"])
        assert extra_meta["dataset_dir_exists"] is True
        finish_payload = next(body for suffix, body in api_calls if suffix == "finish")
        assert finish_payload["status"] == "succeeded"
        assert finish_payload["process_exited"] is True
    finally:
        supervisor.lock.close()
        supervisor.client.close()


def test_local_process_plugin_env_scrubbing_tamper_and_failed_receipt(
    tmp_path, monkeypatch
):
    trainer_script = _write_dummy_trainer_script(tmp_path / "dummy_trainer.py")
    dataset_zip, dataset_sha = _make_dataset_zip(tmp_path / "dataset.zip")
    monkeypatch.setenv("TRAINING_EXECUTOR_TOKEN", EXECUTOR_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgres://secret-db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "minio-secret")
    monkeypatch.setenv("SECRET_KEY", "app-secret")
    monkeypatch.setenv("JWT_SECRET", "jwt-secret")
    supervisor = _make_plugin_supervisor(
        tmp_path,
        device_kind="cpu",
        local_plugins={
            "yoao.trainer.dummy": {"entrypoint": [sys.executable, str(trainer_script)]}
        },
    )
    supervisor.download = lambda url, target, *args, **kwargs: target.write_bytes(
        dataset_zip.read_bytes()
    )
    supervisor.upload_and_finish = lambda *args: False
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession",
        lambda *args: (_ for _ in ()).throw(RuntimeError("obs disabled")),
    )
    try:
        tampered = _make_plugin_journal(
            supervisor,
            dataset_sha256=dataset_sha,
            execution_mode="local_process",
            entrypoint=[sys.executable, str(trainer_script)],
            parameters={"epochs": 2, "dummy_mode": "bad_artifact_sha"},
        )
        supervisor.execute(tampered)
        assert tampered["outcome"] == "failed"
        attempt_dir = (
            supervisor.state / "attempts" / tampered["assignment"]["attempt_id"]
        )
        env_seen = json.loads((attempt_dir / "output" / "env-check.json").read_text())
        for forbidden in (
            "TRAINING_EXECUTOR_TOKEN",
            "DATABASE_URL",
            "AWS_SECRET_ACCESS_KEY",
            "MINIO_ROOT_PASSWORD",
            "SECRET_KEY",
            "JWT_SECRET",
        ):
            assert forbidden not in env_seen
        assert env_seen["CUDA_VISIBLE_DEVICES"] == ""
        assert env_seen["NVIDIA_VISIBLE_DEVICES"] == "void"

        failed_receipt = _make_plugin_journal(
            supervisor,
            dataset_sha256=dataset_sha,
            execution_mode="local_process",
            entrypoint=[sys.executable, str(trainer_script)],
            parameters={
                "epochs": 2,
                "dummy_mode": "fail_receipt",
                "failure_code": "cuda_oom",
                "failure_message": f"boom token={EXECUTOR_TOKEN}",
            },
        )
        supervisor.execute(failed_receipt)
        assert failed_receipt["outcome"] == "failed"
        assert failed_receipt["failure"]["code"] == "cuda_oom"
        assert EXECUTOR_TOKEN not in failed_receipt["failure"]["diagnostic"]

        unlisted = _make_plugin_journal(
            supervisor,
            dataset_sha256=dataset_sha,
            execution_mode="local_process",
            plugin_id="unlisted.plugin",
            entrypoint=[sys.executable, str(trainer_script)],
        )
        supervisor.execute(unlisted)
        assert unlisted["outcome"] == "failed"
        assert unlisted["failure"]["code"] == "input_mismatch"
    finally:
        supervisor.lock.close()
        supervisor.client.close()


def test_local_process_cancellation_kills_child_and_grandchild_before_finish(
    tmp_path, monkeypatch
):
    trainer_script = _write_dummy_trainer_script(tmp_path / "dummy_trainer.py")
    dataset_zip, dataset_sha = _make_dataset_zip(tmp_path / "dataset.zip")
    supervisor = _make_plugin_supervisor(
        tmp_path,
        device_kind="cpu",
        local_plugins={
            "yoao.trainer.dummy": {"entrypoint": [sys.executable, str(trainer_script)]}
        },
    )
    supervisor.download = lambda url, target, *args, **kwargs: target.write_bytes(
        dataset_zip.read_bytes()
    )
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession",
        lambda *args: (_ for _ in ()).throw(RuntimeError("obs disabled")),
    )
    journal = _make_plugin_journal(
        supervisor,
        dataset_sha256=dataset_sha,
        execution_mode="local_process",
        entrypoint=[sys.executable, str(trainer_script)],
        parameters={"epochs": 2, "dummy_mode": "sleep_with_grandchild"},
    )
    attempt_dir = supervisor.state / "attempts" / journal["assignment"]["attempt_id"]
    grandchild_pid_file = attempt_dir / "output" / "grandchild.pid"
    grandchild_holder: list[int] = []

    def cancel_when_ready() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if grandchild_pid_file.is_file():
                text = grandchild_pid_file.read_text().strip()
                if text.isdigit():
                    grandchild_holder.append(int(text))
                    supervisor.stop_training.set()
                    return
            time.sleep(0.05)

    def assert_dead_on_publish(cur_journal: dict[str, Any], directory: Path) -> bool:
        assert cur_journal["outcome"] == "cancelled"
        assert not live_group(cur_journal["process"]["pid"])
        assert grandchild_holder
        grandchild_pid = grandchild_holder[0]
        assert not psutil.pid_exists(grandchild_pid) or (
            psutil.Process(grandchild_pid).status() == psutil.STATUS_ZOMBIE
        )
        return False

    supervisor.upload_and_finish = assert_dead_on_publish
    canceller = threading.Thread(target=cancel_when_ready, daemon=True)
    canceller.start()
    try:
        supervisor.execute(journal)
        canceller.join(timeout=5)
        assert journal["outcome"] == "cancelled"
    finally:
        supervisor.lock.close()
        supervisor.client.close()


def test_local_process_budget_deadline_kills_trainer_and_emits_exact_accounting(
    tmp_path, monkeypatch
):
    trainer_script = _write_dummy_trainer_script(tmp_path / "dummy_trainer.py")
    dataset_zip, dataset_sha = _make_dataset_zip(tmp_path / "dataset.zip")
    supervisor = _make_plugin_supervisor(
        tmp_path,
        device_kind="cpu",
        local_plugins={
            "yoao.trainer.dummy": {"entrypoint": [sys.executable, str(trainer_script)]}
        },
    )
    supervisor.download = lambda url, target, *args, **kwargs: target.write_bytes(
        dataset_zip.read_bytes()
    )
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession",
        lambda *args: (_ for _ in ()).throw(RuntimeError("obs disabled")),
    )
    finished_bodies: list[dict[str, Any]] = []

    def fake_publish(cur_journal: dict[str, Any], directory: Path) -> bool:
        assert not live_group(cur_journal["process"]["pid"])
        finished_bodies.append(supervisor.finish_body(cur_journal))
        return False

    supervisor.upload_and_finish = fake_publish
    journal = _make_plugin_journal(
        supervisor,
        dataset_sha256=dataset_sha,
        execution_mode="local_process",
        entrypoint=[sys.executable, str(trainer_script)],
        parameters={"epochs": 2, "dummy_mode": "sleep_budget"},
        native_budget={
            "policy_digest": BUDGET_POLICY_DIGEST,
            "reservation_id": str(uuid.uuid4()),
            "allowance_seconds": 0.15,
        },
    )
    try:
        supervisor.execute(journal)
        assert journal["outcome"] == "failed"
        assert journal["failure"]["code"] == "budget_exhausted"
        accounting = journal["native_accounting"]
        assert accounting["state"] == "exact"
        assert accounting["deadline_triggered"] is True
        assert accounting["native_seconds"] >= 0.15
        assert accounting["exit_confirmed"] is True
        assert finished_bodies[0]["process_exited"] is True
        assert finished_bodies[0]["native_accounting"] == accounting
    finally:
        supervisor.lock.close()
        supervisor.client.close()


def _make_preview_journal(
    source_bytes: bytes,
    *,
    execution_mode: str,
    plugin_id: str = "yoao.trainer.dummy",
    entrypoint: list[str] | None = None,
    image: str | None = PINNED_IMAGE,
    preview_mode: str = "succeed",
) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    label_text = "0 0.5 0.5 0.4 0.4\n"
    label_sha = hashlib.sha256(label_text.encode()).hexdigest()
    trainer_plugin: dict[str, Any] = {
        "plugin_id": plugin_id,
        "execution_mode": execution_mode,
    }
    if entrypoint is not None:
        trainer_plugin["entrypoint"] = entrypoint
    if image is not None and execution_mode == "container":
        trainer_plugin["image"] = image
    return {
        "phase": "preparing",
        "claim": {"executor_id": "owned", "boot_id": str(uuid.uuid4())},
        "assignment": {
            "preview_id": str(uuid.uuid4()),
            "lease_token": LEASE_TOKEN,
            "input_digest": "3" * 64,
            "stop": False,
            "source_urls": {task_id: "https://storage.test/source.png"},
            "spec": {
                "trainer_plugin": trainer_plugin,
                "framework_id": "dummy",
                "task": "detect",
                "architecture_id": "dummy-net",
                "config": {"framework": "dummy", "parameters": {"epochs": 2}},
                "config_digest": "4" * 64,
                "dataset_snapshot_digest": "5" * 64,
                "policy_digest": PREVIEW_POLICY_DIGEST,
                "seed": 42,
                "epoch": 1,
                "count": 1,
                "parameters": {"preview_mode": preview_mode},
                "native_values": {},
                "pool_summary": {"selected": 1},
                "sources": [
                    {
                        "task_id": task_id,
                        "image_path": "images/train/sample.png",
                        "label_path": "labels/train/sample.txt",
                        "sha256": source_sha,
                        "label_sha256": label_sha,
                        "size_bytes": len(source_bytes),
                        "width": 64,
                        "height": 64,
                        "labels": label_text,
                    }
                ],
            },
        },
    }


def test_preview_supervisor_container_cpu_only_enforcement_and_rejections(
    tmp_path, monkeypatch
):
    source_bytes = b"source-preview-image"
    png_bytes = b"\x89PNG\r\n\x1a\ncontainer-preview-output"
    mode_box = {"mode": "succeed"}

    def on_run_detached(spec: DetachedContainerSpec, record: _ContainerRecord) -> None:
        output_mount = next(m for m in spec.mounts if m.target == "/work/output")
        input_mount = next(m for m in spec.mounts if m.target == "/work/input")
        preview_spec = PreviewSpecV1.model_validate(
            json.loads((input_mount.source / "preview-spec.json").read_text())
        )
        png_path = output_mount.source / "preview-0.png"
        png_path.write_bytes(png_bytes)
        mode = mode_box["mode"]
        raw_report = {
            "protocol_version": TRAINER_PROTOCOL_VERSION,
            "status": "succeeded",
            "input_digest": (
                "f" * 64 if mode == "bad_input_digest" else preview_spec.input_digest
            ),
            "policy_digest": (
                "e" * 64 if mode == "bad_policy_digest" else preview_spec.policy_digest
            ),
            "cuda_initialized": mode == "cuda_initialized",
            "outputs": [
                {
                    "filename": "preview-0.png",
                    "sha256": (
                        "0" * 64 if mode == "bad_sha" else digest_file(png_path)
                    ),
                    "size_bytes": len(png_bytes),
                    "width": 64,
                    "height": 64,
                    "box_count": 1,
                    "metadata": {},
                }
            ],
            "warnings": [],
            "runtime": {"framework_id": "dummy"},
            "details": {},
            "failure_code": None,
            "failure_message": None,
        }
        (output_mount.source / "report.json").write_text(json.dumps(raw_report))
        record.running = False
        record.exit_code = 0

    runtime = InMemoryContainerRuntime(on_run_detached=on_run_detached)
    training = _make_plugin_supervisor(
        tmp_path, device_kind="gpu", gpu_uuid=GPU_UUID, container_runtime=runtime
    )
    preview = PreviewSupervisor(training)
    preview.heartbeat = lambda journal: None
    preview.lock = (tmp_path / "preview.lock").open("a+")

    def fake_download(self_obj, url, target, expected_sha, maximum, *, exact_size=None):
        target.write_bytes(source_bytes)

    monkeypatch.setattr(Supervisor, "download", fake_download)
    published: list[tuple[dict[str, Any], Path]] = []
    preview.publish = lambda cur_journal, directory: (
        published.append((copy.deepcopy(cur_journal), directory)) or False
    )
    try:
        journal = _make_preview_journal(
            source_bytes,
            execution_mode="container",
            entrypoint=["yoao-trainer-dummy"],
        )
        preview.execute(journal)
        assert journal["outcome"] == "succeeded"
        spec = runtime.specs[0]
        assert spec.gpu_uuid is None
        assert spec.network == "none"
        assert spec.cpus == 1.0
        assert spec.memory_mb == 16384
        assert spec.pids_limit == 128
        assert spec.shm_size_mb == 256
        assert ("NVIDIA_VISIBLE_DEVICES", "void") in spec.env
        assert ("CUDA_VISIBLE_DEVICES", "") in spec.env
        mounts = {mount.target: mount for mount in spec.mounts}
        assert mounts["/work/input"].readonly is True
        assert mounts["/work/output"].readonly is False
        assert spec.command == (
            "yoao-trainer-dummy",
            "preview",
            "--spec",
            "/work/input/preview-spec.json",
            "--output",
            "/work/output",
        )
        out_dir = published[0][1]
        assert (out_dir / "preview-0.png").read_bytes() == png_bytes
        uploaded_report = PreviewReportV1.model_validate(
            json.loads((out_dir / "report.json").read_text())
        )
        assert uploaded_report.cuda_initialized is False

        for bad_mode in (
            "cuda_initialized",
            "bad_input_digest",
            "bad_policy_digest",
            "bad_sha",
        ):
            mode_box["mode"] = bad_mode
            bad_journal = _make_preview_journal(
                source_bytes,
                execution_mode="container",
                entrypoint=["yoao-trainer-dummy"],
            )
            preview.execute(bad_journal)
            assert bad_journal["outcome"] == "failed", (
                f"Expected failure for {bad_mode}"
            )
    finally:
        preview.lock.close()
        preview.client.close()
        training.lock.close()
        training.client.close()


def test_preview_supervisor_reconciles_orphan_preview_containers_and_local_mode(
    tmp_path, monkeypatch
):
    trainer_script = _write_dummy_trainer_script(tmp_path / "dummy_trainer.py")
    source_bytes = b"source-preview-local"
    runtime = InMemoryContainerRuntime()
    training = _make_plugin_supervisor(
        tmp_path,
        device_kind="cpu",
        container_runtime=runtime,
        local_plugins={
            "yoao.trainer.dummy": {"entrypoint": [sys.executable, str(trainer_script)]}
        },
    )
    preview = PreviewSupervisor(training)
    orphan_preview = runtime.add_existing(
        name="yoao-trainer-preview-orphan00000001",
        labels={
            "yoao.trainer.managed": "true",
            "yoao.trainer.preview": "true",
            "yoao.trainer.gpu_resource_id": preview.resource,
        },
        running=True,
    )
    training_container = runtime.add_existing(
        name="yoao-trainer-active000000000001",
        labels={
            "yoao.trainer.managed": "true",
            "yoao.trainer.gpu_resource_id": training.resource,
        },
        running=True,
    )
    claims: list[str] = []

    def stop_on_claim(journal: dict[str, Any]) -> None:
        assert orphan_preview not in runtime.containers
        assert training_container in runtime.containers
        claims.append("claimed")
        preview.shutdown.set()
        return None

    preview.claim = stop_on_claim
    preview.run()
    assert claims == ["claimed"]

    preview = PreviewSupervisor(training)
    preview.shutdown.clear()
    preview.heartbeat = lambda journal: None
    preview.lock = (tmp_path / "preview-local.lock").open("a+")
    monkeypatch.setenv("TRAINING_EXECUTOR_TOKEN", EXECUTOR_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("SECRET_KEY", "secret")
    monkeypatch.setattr(
        Supervisor,
        "download",
        lambda self_obj, url, target, *args, **kwargs: target.write_bytes(source_bytes),
    )
    published: list[tuple[dict[str, Any], Path]] = []
    preview.publish = lambda cur_journal, directory: (
        published.append((copy.deepcopy(cur_journal), directory)) or False
    )
    try:
        local_journal = _make_preview_journal(
            source_bytes,
            execution_mode="local_process",
            entrypoint=[sys.executable, str(trainer_script)],
        )
        preview.execute(local_journal)
        assert local_journal["outcome"] == "succeeded"
        assert (published[0][1] / "preview-0.png").is_file()
        assert (published[0][1] / "report.json").is_file()

        preview.preview_deadline_seconds = 0.15
        timeout_journal = _make_preview_journal(
            source_bytes,
            execution_mode="local_process",
            entrypoint=[sys.executable, str(trainer_script)],
            preview_mode="sleep_timeout",
        )
        preview.execute(timeout_journal)
        assert timeout_journal["outcome"] == "cancelled"
        assert not live_group(timeout_journal["process"]["pid"])
    finally:
        preview.lock.close()
        preview.client.close()
        training.lock.close()
        training.client.close()
