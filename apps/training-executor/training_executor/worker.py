"""Single-resource supervisor with durable claims, heartbeats and upload recovery."""

from __future__ import annotations

from collections.abc import Mapping
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

import httpx
import psutil
from training_contract.assets import (
    available_asset_digests,
    resolve_asset,
)
from training_contract.observability import (
    FAILURE_CODES,
    PHASES,
    InputMismatchError,
    failure_receipt,
)
from training_contract.trainer_plugin import (
    TRAINER_PROTOCOL_VERSION,
    TrainEventV1,
    TrainReceiptV1,
    TrainSpecV1,
    atomic_write_json,
    canonical_json_digest,
)
from yoao.container_runtime import ContainerRuntimePort

from .files import (
    atomic_json,
    digest_file,
    extract_dataset,
    extract_dataset_archive,
    immutable_copy,
)
from .plugin_runner import (
    JobHandle,
    LeaseRetainedError,
    build_train_container_spec,
    launch_container_job,
    launch_local_process,
    live_group,
    load_local_plugins_allowlist,
    reconcile_container_by_id_or_name,
    reconcile_orphan_containers,
    resolve_container_runtime,
    resolve_local_plugin_entrypoint,
    stop_process_group,
    trainer_container_labels,
    trainer_container_name,
)

_UNSET = object()
_IMAGE_REFERENCE_RE = re.compile(r"[^@\s]+@(sha256:[0-9a-f]{64})")
_CPU_SLOT_RE = re.compile(r"^[0-9]{1,6}$")
_CHECKPOINT_EPOCH_RE = re.compile(r"^checkpoint-([0-9]{1,4})\.[^.]+$")
_VALID_EVENT_PHASES = frozenset({"preparing", "training", "evaluating", "uploading"})
_SUPERVISOR_OWNED_ARTIFACTS = frozenset(
    {"manifest.json", "train.log", "observability.json", "config.json", "result.json"}
)
_MAX_UPLOAD_ARTIFACTS = 12


class RetiredAttempt(Exception):
    pass


class StopTraining(Exception):
    pass


def retryable_publication_response(response, *, storage=False):
    """Classify transient HTTP failures without relaxing immutable API conflicts."""
    status = response.status_code
    if status >= 500 or status in {408, 425, 429}:
        return True
    if storage:
        return status in {403, 404, 409}
    if status != 409:
        return False
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, dict) and detail.get("reason") in {
        "account_busy",
        "membership_busy",
        "project_busy",
    }


def require_upload_response(response, target):
    """A prior conditional upload must still pass the API's finish verification."""
    if response.status_code != 412 or target["headers"].get("If-None-Match") != "*":
        response.raise_for_status()


def _parse_image_reference(raw: str, *, required: bool) -> str | None:
    if not raw:
        if required:
            raise ValueError("An immutable executor image reference is required")
        return None
    selected_image = _IMAGE_REFERENCE_RE.fullmatch(raw)
    if selected_image is None:
        raise ValueError("An immutable executor image reference is required")
    return selected_image[1]


def _probe_nvidia_gpu(configured_gpu_uuid: str) -> tuple[str | None, str | None]:
    try:
        raw_uuids = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"], text=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "Executor must expose exactly the configured physical GPU"
        ) from exc
    devices = [line.strip() for line in raw_uuids.strip().splitlines() if line.strip()]
    if devices != [configured_gpu_uuid]:
        raise RuntimeError("Executor must expose exactly the configured physical GPU")
    gpu_name: str | None = None
    driver_version: str | None = None
    try:
        raw_meta = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            text=True,
        ).strip()
        lines = [line.strip() for line in raw_meta.splitlines() if line.strip()]
        if len(lines) == 1 and "," in lines[0]:
            parts = [item.strip() for item in lines[0].split(",", 1)]
            if len(parts) == 2:
                gpu_name = parts[0] or None
                driver_version = parts[1] or None
    except (OSError, subprocess.SubprocessError):
        pass
    return gpu_name, driver_version


def _cpu_slot() -> str:
    raw = os.environ.get("TRAINING_CPU_SLOT", "0").strip()
    if _CPU_SLOT_RE.fullmatch(raw) is None:
        raise ValueError("TRAINING_CPU_SLOT must be a non-negative integer")
    return str(int(raw))


def _optional_distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def runtime_identity(
    *,
    container_runtime_available: bool | None = None,
    local_plugins: Mapping[str, Any] | None = None,
):
    from training_contract.budget import POLICY_DIGEST as BUDGET_POLICY_DIGEST
    from training_contract.input_membership import POLICY_DIGEST as INPUT_POLICY_DIGEST
    from training_contract.preview import POLICY_DIGEST

    supervisor_mode = os.environ.get("TRAINING_SUPERVISOR_MODE", "").strip().lower()
    if supervisor_mode not in {"", "plugin", "legacy"}:
        raise ValueError(f"Unsupported TRAINING_SUPERVISOR_MODE: {supervisor_mode}")
    device_env = os.environ.get("TRAINING_DEVICE_KIND", "").strip().lower()
    if device_env not in {"", "gpu", "cpu"}:
        raise ValueError(f"Unsupported TRAINING_DEVICE_KIND: {device_env}")
    ultralytics_version = _optional_distribution_version("ultralytics")
    plugin_mode = (
        supervisor_mode == "plugin"
        or device_env == "cpu"
        or ultralytics_version is None
    )
    models_dir = os.environ.get("TRAINING_MODELS_DIR", "/models")
    if plugin_mode:
        device_kind = "cpu" if device_env == "cpu" else "gpu"
        if device_kind == "gpu":
            gpu_uuid: str | None = os.environ["TRAINING_GPU_UUID"]
            gpu_name, driver_version = _probe_nvidia_gpu(gpu_uuid)
        else:
            _cpu_slot()
            gpu_uuid = None
            gpu_name = None
            driver_version = None
        image_digest = _parse_image_reference(
            os.environ.get("TRAINING_IMAGE_REFERENCE", ""), required=False
        )
        allowlist = (
            load_local_plugins_allowlist()
            if local_plugins is None
            else dict(local_plugins)
        )
        allowed_local_plugins = sorted(allowlist.keys())
        has_container = (
            resolve_container_runtime() is not None
            if container_runtime_available is None
            else bool(container_runtime_available)
        )
        execution_modes: list[str] = []
        if has_container:
            execution_modes.append("container")
        if allowed_local_plugins:
            execution_modes.append("local_process")
        return {
            "executor_protocol": 3,
            "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
            "execution_modes": execution_modes,
            "allowed_local_plugins": allowed_local_plugins,
            "device_kind": device_kind,
            "gpu_uuid": gpu_uuid,
            "gpu_name": gpu_name,
            "driver_version": driver_version,
            "image_digest": image_digest,
            "training_saved_prefix_schema": 2,
            "preview_protocol": 1,
            "observability_protocol": 1,
            "native_budget_policy_digest": BUDGET_POLICY_DIGEST,
            "input_receipt_policy_digest": INPUT_POLICY_DIGEST,
            "preview_policy_digest": POLICY_DIGEST,
            "base_asset_sha256": available_asset_digests(models_dir),
            "python": sys.version.split()[0],
            "frameworks": {},
        }
    from training_contract.catalog import instance_rle_capability, supported_identities
    from training_contract.initialization import (
        POLICY_DIGEST as INITIALIZATION_POLICY_DIGEST,
        supported_policy_digests,
    )

    gpu = os.environ["TRAINING_GPU_UUID"]
    gpu_name, driver_version = _probe_nvidia_gpu(gpu)
    image = _parse_image_reference(
        os.environ.get("TRAINING_IMAGE_REFERENCE", ""), required=True
    )
    frameworks = {"ultralytics": ultralytics_version}
    libreyolo_version = _optional_distribution_version("libreyolo")
    if libreyolo_version is not None:
        frameworks["libreyolo"] = libreyolo_version
    identity = {
        "executor_protocol": 2,
        "training_saved_prefix_schema": 2,
        "preview_protocol": 1,
        "observability_protocol": 1,
        "initialization_policy_digest": INITIALIZATION_POLICY_DIGEST,
        "initialization_policy_digests": supported_policy_digests(
            frameworks["ultralytics"]
        ),
        "native_budget_policy_digest": BUDGET_POLICY_DIGEST,
        "input_receipt_policy_digest": INPUT_POLICY_DIGEST,
        "preview_policy_digest": POLICY_DIGEST,
        "supported_configs": supported_identities(frameworks),
        "instance_rle_consumer": instance_rle_capability(frameworks["ultralytics"]),
        "base_asset_sha256": available_asset_digests(models_dir),
        "image_digest": image,
        "gpu_uuid": gpu,
        "frameworks": frameworks,
        "python": sys.version.split()[0],
        "gpu_name": gpu_name,
        "driver_version": driver_version,
    }
    torch_version = _optional_distribution_version("torch")
    if torch_version is not None:
        identity["torch"] = torch_version
    torchvision_version = _optional_distribution_version("torchvision")
    if torchvision_version is not None:
        identity["torchvision"] = torchvision_version
    return identity


def stop_process(process: subprocess.Popen):
    stop_process_group(process)


def _is_plugin_run(assignment: Mapping[str, Any]) -> bool:
    config = assignment.get("config") or {}
    return bool(
        config.get("trainer_protocol") == TRAINER_PROTOCOL_VERSION
        or config.get("adapter_revision") == 3
        or isinstance(config.get("trainer_plugin"), Mapping)
        or isinstance(assignment.get("trainer_plugin"), Mapping)
    )


def _extract_image_reference(
    plugin: Mapping[str, Any], config: Mapping[str, Any]
) -> str | None:
    for candidate in (
        plugin.get("image"),
        plugin.get("artifact"),
        config.get("image_reference"),
        config.get("image"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, Mapping):
            for key in ("image", "reference", "oci_reference"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _resolve_plugin_descriptor(
    assignment: Mapping[str, Any], runtime: Mapping[str, Any]
) -> dict[str, Any]:
    config = assignment.get("config") or {}
    merged: dict[str, Any] = {}
    if isinstance(config.get("trainer_plugin"), Mapping):
        merged.update(config["trainer_plugin"])
    if isinstance(assignment.get("trainer_plugin"), Mapping):
        merged.update(assignment["trainer_plugin"])
    plugin_id = (
        merged.get("plugin_id") or config.get("plugin_id") or config.get("framework")
    )
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise InputMismatchError("Plugin run is missing plugin_id")
    execution_mode = merged.get("execution_mode") or config.get("execution_mode")
    if execution_mode not in {"container", "local_process"}:
        raise InputMismatchError(
            "Plugin execution_mode must be container or local_process"
        )
    raw_requires_gpu = merged.get("requires_gpu", config.get("requires_gpu"))
    if isinstance(raw_requires_gpu, bool):
        requires_gpu = raw_requires_gpu
    elif raw_requires_gpu is None:
        requires_gpu = runtime.get("device_kind", "gpu") == "gpu"
    else:
        raise InputMismatchError("Plugin requires_gpu must be a boolean")
    resource = merged.get("resource") or config.get("resource") or {}
    if not isinstance(resource, Mapping):
        raise InputMismatchError("Plugin resource specification must be a mapping")
    return {
        "plugin_id": plugin_id.strip(),
        "execution_mode": execution_mode,
        "entrypoint": merged.get("entrypoint") or config.get("entrypoint"),
        "image": _extract_image_reference(merged, config),
        "resource": dict(resource),
        "requires_gpu": requires_gpu,
    }


def _finite_float_metrics(raw: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, value in raw.items():
        if len(result) >= 100:
            break
        if (
            isinstance(key, str)
            and 1 <= len(key) <= 100
            and not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
        ):
            as_float = float(value)
            result[key] = 0.0 if as_float == 0.0 else as_float
    return result


def _translate_plugin_events(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    last_seq = 0
    with path.open() as source:
        for line in source:
            if not line.endswith("\n"):
                break
            event = TrainEventV1.model_validate(json.loads(line))
            if event.seq <= last_seq:
                raise ValueError("Plugin events.jsonl sequence must strictly increase")
            last_seq = event.seq
            metrics = _finite_float_metrics(event.metrics)
            sequence = len(result) + 1
            if (
                event.kind == "epoch"
                and event.epoch is not None
                and 1 <= event.epoch <= 1000
            ):
                translated: dict[str, Any] = {
                    "sequence": sequence,
                    "kind": "epoch",
                    "epoch": event.epoch,
                    "metrics": metrics,
                }
            else:
                kind = event.phase if event.phase in _VALID_EVENT_PHASES else "training"
                translated = {
                    "sequence": sequence,
                    "kind": kind,
                    "metrics": metrics,
                }
                if event.epoch is not None and 1 <= event.epoch <= 1000:
                    translated["epoch"] = event.epoch
            result.append(translated)
    return result


def _checkpoint_epoch_for(filename: str, output_dir: Path) -> int | None:
    pointer_path = output_dir / "checkpoint.json"
    if pointer_path.is_file() and pointer_path.stat().st_size <= 64 * 1024:
        try:
            pointer = json.loads(pointer_path.read_text())
            if (
                isinstance(pointer, dict)
                and pointer.get("name") == filename
                and type(pointer.get("epoch")) is int
                and 1 <= pointer["epoch"] <= 1000
            ):
                return pointer["epoch"]
        except ValueError:
            pass
    match = _CHECKPOINT_EPOCH_RE.fullmatch(filename)
    if match:
        epoch = int(match[1])
        if 1 <= epoch <= 1000:
            return epoch
    return None


def _project_plugin_failure(
    receipt: TrainReceiptV1, config_digest: str, secrets: tuple[str, ...]
) -> dict[str, Any]:
    raw_code = receipt.failure_code or "native_exception"
    code = raw_code if raw_code in FAILURE_CODES else "native_exception"
    message = receipt.failure_message or raw_code
    if raw_code != code and not message.startswith(raw_code):
        message = f"{raw_code}: {message}"
    return failure_receipt(
        code,
        "execution",
        config_digest,
        message,
        secrets=secrets,
        source="native",
    )


class Supervisor:
    def __init__(
        self, *, container_runtime: ContainerRuntimePort | None | object = _UNSET
    ):
        self.state = Path(os.environ.get("TRAINING_STATE_DIR", "/state"))
        self.state.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)
        self.models = Path(os.environ.get("TRAINING_MODELS_DIR", "/models"))
        self.boot = str(uuid.uuid4())
        self.container_runtime: ContainerRuntimePort | None = (
            resolve_container_runtime()
            if container_runtime is _UNSET
            else container_runtime
        )
        self.local_plugins = load_local_plugins_allowlist()
        self.runtime = runtime_identity(
            container_runtime_available=self.container_runtime is not None,
            local_plugins=self.local_plugins,
        )
        identity_path = self.state / "executor.json"
        if not identity_path.exists():
            atomic_json(identity_path, {"id": "executor-" + uuid.uuid4().hex})
        self.executor_id = json.loads(identity_path.read_text())["id"]
        node_id = os.environ.get("TRAINING_NODE_ID", "local")
        if self.runtime.get("device_kind") == "cpu":
            self.resource = f"{node_id}/cpu-{_cpu_slot()}"
        else:
            self.resource = f"{node_id}/{self.runtime['gpu_uuid']}"
        token_file = os.environ.get("TRAINING_EXECUTOR_TOKEN_FILE")
        token = (
            Path(token_file).read_text().strip()
            if token_file
            else os.environ["TRAINING_EXECUTOR_TOKEN"]
        )
        if len(token) < 32:
            raise ValueError("Executor authentication token is too short")
        self.redaction_secrets = (token,)
        self.client = httpx.Client(
            base_url=os.environ["TRAINING_API_URL"].rstrip("/") + "/",
            headers={"X-Training-Executor-Token": token},
            timeout=15,
            trust_env=False,
        )
        self.journal_path = self.state / "assignment.json"
        self.shutdown = threading.Event()
        self.stop_training = threading.Event()
        self.heartbeat_done = threading.Event()
        self.heartbeat_thread = None
        self.stop_reason = None
        self.lock = None
        self.active_process = None
        self.active_budget = None
        self.active_capture = None
        self.retiring_observation = None

    def api(self, endpoint, payload):
        response = self.client.post(endpoint, json=payload)
        if response.status_code in {404, 409}:
            detail = response.json().get("detail")
            if response.status_code == 404 or (
                isinstance(detail, dict) and detail.get("code") == "stale_attempt"
            ):
                raise RetiredAttempt("Attempt was retired")
        response.raise_for_status()
        return response.json()

    def save(self, journal):
        atomic_json(self.journal_path, journal)
        self.journal_path.chmod(0o600)

    def fence(self, journal):
        return {
            "executor_id": journal["claim"]["executor_id"],
            "boot_id": journal["claim"]["boot_id"],
            "lease_token": journal["assignment"]["lease_token"],
        }

    def endpoint(self, journal, suffix):
        return "attempts/" + journal["assignment"]["attempt_id"] + "/" + suffix

    def claim(self, journal):
        result = self.api("claim", journal["claim"])["assignment"]
        if result:
            journal["assignment"] = result
            journal["phase"] = "preparing"
            self.save(journal)
        return result

    def events(self, directory):
        plugin_path = directory / "output" / "events.jsonl"
        if plugin_path.exists():
            return _translate_plugin_events(plugin_path)
        path = directory / "events.jsonl"
        if not path.exists():
            return []
        result = []
        with path.open() as source:
            for line in source:
                if not line.endswith("\n"):
                    break
                event = json.loads(line)
                result.append({"sequence": len(result) + 1, **event})
        return result

    def heartbeat(self, journal, directory):
        acknowledged = 0
        last_success = time.monotonic()
        with httpx.Client(
            base_url=self.client.base_url,
            headers=self.client.headers,
            timeout=10,
            trust_env=False,
        ) as transport:
            while not self.heartbeat_done.is_set():
                events = []
                try:
                    events = self.events(directory)[acknowledged : acknowledged + 100]
                    response = transport.post(
                        self.endpoint(journal, "heartbeat"),
                        json={**self.fence(journal), "events": events},
                    )
                    if response.status_code in {403, 404}:
                        self.stop_reason = "executor_authority_lost"
                        self.stop_training.set()
                        return
                    response.raise_for_status()
                    if response.json()["stop"]:
                        self.stop_reason = (
                            response.json().get("reason") or "cancel_requested"
                        )
                        self.stop_training.set()
                    acknowledged += len(events)
                    last_success = time.monotonic()
                except Exception:
                    if time.monotonic() - last_success > 30:
                        self.stop_reason = "heartbeat_unavailable"
                        self.stop_training.set()
                self.heartbeat_done.wait(0.2 if len(events) == 100 else 5)

    def download(self, url, path, expected_sha, maximum, *, exact_size=None):
        temporary = path.with_suffix(".download")
        digest = hashlib.sha256()
        size = 0
        with (
            httpx.stream("GET", url, timeout=60, trust_env=False) as response,
            temporary.open("wb") as target,
        ):
            response.raise_for_status()
            for chunk in response.iter_bytes(1024 * 1024):
                if self.shutdown.is_set() or self.stop_training.is_set():
                    raise StopTraining()
                size += len(chunk)
                if size > maximum:
                    raise InputMismatchError("Downloaded input exceeds its limit")
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if digest.hexdigest() != expected_sha or (
            exact_size is not None and size != exact_size
        ):
            raise InputMismatchError(
                "Downloaded input digest or size differs from the frozen configuration"
            )
        os.replace(temporary, path)

    def _stage_plugin_receipt(
        self, journal, directory: Path, output_dir: Path, *, config_digest: str, secrets
    ) -> None:
        receipt_path = output_dir / "result.json"
        if journal["outcome"] == "succeeded":
            if (
                not receipt_path.is_file()
                or receipt_path.is_symlink()
                or receipt_path.stat().st_size > 1024 * 1024
            ):
                journal["outcome"] = "failed"
                journal["error"] = "训练插件未生成合法的 result.json 终态收据"
                journal["failure"] = failure_receipt(
                    "native_exception",
                    "execution",
                    config_digest,
                    journal["error"],
                    secrets=secrets,
                )
                return
            try:
                receipt = TrainReceiptV1.model_validate(
                    json.loads(receipt_path.read_text())
                )
            except (ValueError, OSError) as exc:
                journal["outcome"] = "failed"
                journal["error"] = "训练插件 result.json 终态收据校验失败"
                journal["failure"] = failure_receipt(
                    "native_exception",
                    "execution",
                    config_digest,
                    str(exc),
                    exception_type=type(exc).__name__,
                    secrets=secrets,
                )
                return
            if receipt.status == "failed":
                journal["outcome"] = "failed"
                journal["error"] = (
                    receipt.failure_message
                    or receipt.failure_code
                    or "训练插件报告失败"
                )
                journal["failure"] = _project_plugin_failure(
                    receipt, config_digest, secrets
                )
                immutable_copy(receipt_path, directory / "result.json")
                return
            if receipt.status == "cancelled":
                journal["outcome"] = "cancelled"
                immutable_copy(receipt_path, directory / "result.json")
                return
            resolved_output = output_dir.resolve()
            staged: list[dict[str, Any]] = []
            try:
                for item in receipt.artifacts:
                    src = output_dir / item.filename
                    if (
                        src.is_symlink()
                        or not src.is_file()
                        or not src.resolve().is_relative_to(resolved_output)
                    ):
                        raise ValueError(
                            f"Declared plugin artifact is missing or unsafe: {item.filename}"
                        )
                    if (
                        src.stat().st_size != item.size_bytes
                        or digest_file(src) != item.sha256
                    ):
                        raise ValueError(
                            f"Declared plugin artifact digest or size mismatch: {item.filename}"
                        )
                    if (
                        item.role == "input_membership"
                        or item.filename == "input-membership.json"
                    ):
                        immutable_copy(src, directory / "input-membership.json")
                        continue
                    if item.filename in _SUPERVISOR_OWNED_ARTIFACTS:
                        raise ValueError(
                            f"Plugin artifact collides with supervisor-owned file: {item.filename}"
                        )
                    if item.size_bytes <= 0:
                        raise ValueError(
                            f"Declared plugin artifact must be non-empty: {item.filename}"
                        )
                    copied = immutable_copy(src, directory / item.filename)
                    mapped_role = "config" if item.role == "extra" else item.role
                    entry: dict[str, Any] = {
                        "name": copied["name"],
                        "role": mapped_role,
                        "sha256": copied["sha256"],
                        "size_bytes": copied["size_bytes"],
                    }
                    if mapped_role == "checkpoint":
                        epoch = _checkpoint_epoch_for(item.filename, output_dir)
                        if epoch is not None:
                            entry["epoch"] = epoch
                    staged.append(entry)
                immutable_copy(receipt_path, directory / "result.json")
            except (ValueError, OSError) as exc:
                journal["outcome"] = "failed"
                journal["error"] = f"训练插件产物校验失败：{exc}"
                journal["failure"] = failure_receipt(
                    "native_exception",
                    "execution",
                    config_digest,
                    str(exc),
                    exception_type=type(exc).__name__,
                    secrets=secrets,
                )
                return
            journal["plugin_artifacts"] = staged
            if receipt.serving_compatibility is not None:
                journal["serving_compatibility"] = (
                    receipt.serving_compatibility.model_dump(mode="json")
                )
            return
        if (
            journal["outcome"] == "failed"
            and receipt_path.is_file()
            and not receipt_path.is_symlink()
            and receipt_path.stat().st_size <= 64 * 1024
        ):
            try:
                receipt = TrainReceiptV1.model_validate(
                    json.loads(receipt_path.read_text())
                )
                immutable_copy(receipt_path, directory / "result.json")
                if receipt.status == "failed" and "failure" not in journal:
                    journal["failure"] = _project_plugin_failure(
                        receipt, config_digest, secrets
                    )
            except (ValueError, OSError):
                pass

    def _build_train_spec(
        self,
        assignment: Mapping[str, Any],
        config: Mapping[str, Any],
        *,
        execution_mode: str,
        input_dir: Path,
        base_path: Path | None,
        resume_path: Path | None,
        initialization_path: Path | None,
    ) -> TrainSpecV1:
        def container_or_host(path: Path | None, container_path: str) -> str | None:
            if path is None:
                return None
            return (
                container_path if execution_mode == "container" else str(path.resolve())
            )

        base_weights_path: str | None = None
        if base_path is not None:
            if execution_mode == "container":
                rel = base_path.resolve().relative_to(self.models.resolve()).as_posix()
                base_weights_path = f"/models/{rel}"
            else:
                base_weights_path = str(base_path.resolve())
        base_model = config.get("base_model")
        architecture_id = (
            config.get("architecture_id")
            or (
                base_model.get("id")
                if isinstance(base_model, Mapping)
                else base_model
                if isinstance(base_model, str)
                else None
            )
            or "default"
        )
        dataset_contract = (
            config.get("dataset_contract")
            or (
                config["data_contract"].get("bridge")
                if isinstance(config.get("data_contract"), Mapping)
                else None
            )
            or config.get("format")
        )
        if not isinstance(dataset_contract, str) or not dataset_contract.strip():
            raise InputMismatchError(
                "Plugin run configuration is missing dataset_contract"
            )
        dataset_snapshot_digest = config.get("dataset_snapshot_digest") or config.get(
            "dataset_digest"
        )
        if not isinstance(dataset_snapshot_digest, str):
            raise InputMismatchError(
                "Plugin run configuration is missing dataset_digest"
            )
        classes_digest = config.get("classes_digest")
        if classes_digest is None:
            if "classes" not in config:
                raise InputMismatchError("Plugin run configuration is missing classes")
            classes_digest = canonical_json_digest(config["classes"])
        configuration = config.get("configuration")
        schema_id = config.get("schema_id")
        schema_digest = config.get("schema_digest")
        if schema_id is None and isinstance(configuration, Mapping):
            schema_id = configuration.get("schema_id")
            schema_digest = configuration.get("schema_digest")
        task = config.get("task")
        if task is None and isinstance(configuration, Mapping):
            identity = configuration.get("identity")
            if isinstance(identity, Mapping):
                task = identity.get("task")
        return TrainSpecV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            run_id=str(assignment["run_id"]),
            attempt_id=str(assignment["attempt_id"]),
            framework_id=str(config.get("framework_id") or config["framework"]),
            task=str(task or "detect"),
            architecture_id=str(architecture_id),
            dataset_contract=dataset_contract.strip(),
            dataset_dir=container_or_host(input_dir / "dataset", "/work/input/dataset"),
            dataset_snapshot_digest=dataset_snapshot_digest,
            config_digest=str(assignment["config_digest"]),
            classes_digest=str(classes_digest),
            schema_id=schema_id,
            schema_digest=schema_digest,
            parameters=dict(config.get("parameters") or {}),
            base_weights_path=base_weights_path,
            resume_checkpoint_path=container_or_host(
                resume_path, "/work/input/resume.pt"
            ),
            init_weights_path=container_or_host(
                initialization_path, "/work/input/initialization.pt"
            ),
            native_budget=assignment.get("native_budget"),
        )

    def inventory(self, journal, directory):
        artifacts = []
        seen_names = set()
        if "plugin_artifacts" in journal or _is_plugin_run(journal["assignment"]):
            for item in journal.get("plugin_artifacts", []):
                path = directory / item["name"]
                if not path.is_file() or not path.stat().st_size:
                    raise ValueError(
                        f"Plugin artifact is missing or empty: {item['name']}"
                    )
                if (
                    path.stat().st_size != item["size_bytes"]
                    or digest_file(path) != item["sha256"]
                ):
                    raise ValueError(f"Plugin artifact changed locally: {item['name']}")
                artifacts.append(dict(item))
                seen_names.add(item["name"])
            checkpoint = directory / "checkpoint.json"
            if checkpoint.is_file():
                cp = json.loads(checkpoint.read_text())
                if cp["name"] not in seen_names:
                    artifacts.append(cp)
                    seen_names.add(cp["name"])
            for name, role in (
                ("result.json", "config"),
                ("config.json", "config"),
                ("train.log", "log"),
                ("observability.json", "telemetry"),
            ):
                if name in seen_names:
                    continue
                path = directory / name
                if path.is_file() and path.stat().st_size:
                    artifacts.append(
                        {
                            "name": name,
                            "role": role,
                            "sha256": digest_file(path),
                            "size_bytes": path.stat().st_size,
                        }
                    )
                    seen_names.add(name)
        else:
            checkpoint = directory / "checkpoint.json"
            if checkpoint.is_file():
                artifacts.append(json.loads(checkpoint.read_text()))
            for name, role in (
                ("best.pt", "weights"),
                ("metrics.json", "metrics"),
                ("config.json", "config"),
                ("native-config.json", "config"),
                ("rle-consumer.json", "config"),
                ("rle-train-preflight.json", "config"),
                ("rle-val-preflight.json", "config"),
                ("train.log", "log"),
                ("observability.json", "telemetry"),
            ):
                path = directory / name
                if path.is_file() and path.stat().st_size:
                    artifacts.append(
                        {
                            "name": name,
                            "role": role,
                            "sha256": digest_file(path),
                            "size_bytes": path.stat().st_size,
                        }
                    )
        manifest = {
            "run_id": journal["assignment"]["run_id"],
            "attempt_id": journal["assignment"]["attempt_id"],
            "config_digest": journal["assignment"]["config_digest"],
            "config": journal["assignment"]["config"],
            "runtime": journal["claim"]["runtime"],
            "publication_runtime": self.runtime,
            "artifacts": artifacts,
        }
        if "serving_compatibility" in journal:
            manifest["serving_compatibility"] = journal["serving_compatibility"]
        from training_contract.input_membership import declared_contract
        from .input_membership import verified_local_receipt

        if (
            journal["outcome"] == "succeeded"
            and declared_contract(manifest["config"]) is not None
        ):
            manifest["input_membership"] = verified_local_receipt(
                manifest["config"],
                manifest["run_id"],
                manifest["attempt_id"],
                directory / "input-membership.json",
            )
        atomic_json(directory / "manifest.json", manifest)
        artifacts.append(
            {
                "name": "manifest.json",
                "role": "manifest",
                "sha256": digest_file(directory / "manifest.json"),
                "size_bytes": (directory / "manifest.json").stat().st_size,
            }
        )
        if len(artifacts) > _MAX_UPLOAD_ARTIFACTS:
            raise ValueError("Artifact inventory exceeds maximum upload limit")
        return artifacts

    def finish_body(self, journal):
        body = {
            **self.fence(journal),
            "status": journal["outcome"],
            "process_exited": True,
            "error_message": journal.get("error"),
        }
        for key in ("failure", "native_accounting"):
            if journal.get(key) is not None:
                body[key] = journal[key]
        return body

    def reject_publication(self, journal, stage, status=None):
        previous = journal.get("finish_body", {})
        journal["outcome"] = "failed"
        journal["error"] = f"训练产物发布失败：{stage}" + (
            f" 返回 HTTP {status}" if status is not None else " 产物校验未通过"
        )
        journal["failure"] = failure_receipt(
            "upload_failed",
            "uploading",
            journal["assignment"]["config_digest"],
            journal["error"],
        )
        journal["publication_rejection"] = {"stage": stage, "status_code": status}
        journal["finish_body"] = self.finish_body(journal)
        if "native_accounting" in previous:
            journal["finish_body"]["native_accounting"] = previous["native_accounting"]
        journal["phase"] = "finishing"
        self.save(journal)

    def upload_and_finish(self, journal, directory):
        while not self.shutdown.is_set():
            stage = "finish" if "finish_body" in journal else "inventory"
            try:
                if "finish_body" in journal:
                    self.save(journal)
                    self.api(self.endpoint(journal, "finish"), journal["finish_body"])
                    return True
                if "artifacts" not in journal:
                    journal["artifacts"] = self.inventory(journal, directory)
                    journal["phase"] = "uploading"
                    self.save(journal)
                stage = "events"
                events = self.events(directory)
                for offset in range(0, len(events), 100):
                    stage = "heartbeat"
                    self.api(
                        self.endpoint(journal, "heartbeat"),
                        {
                            **self.fence(journal),
                            "events": events[offset : offset + 100],
                        },
                    )
                stage = "uploads"
                urls = self.api(
                    self.endpoint(journal, "uploads"),
                    {**self.fence(journal), "artifacts": journal["artifacts"]},
                )
                for spec in journal["artifacts"]:
                    if self.shutdown.is_set():
                        return False
                    stage = "artifact"
                    path = directory / spec["name"]
                    if digest_file(path) != spec["sha256"]:
                        raise ValueError("Local immutable artifact changed")
                    target = urls[spec["name"]]
                    with path.open("rb") as source:
                        stage = "storage"
                        response = httpx.put(
                            target["url"],
                            content=source,
                            headers={
                                **target["headers"],
                                "Content-Length": str(spec["size_bytes"]),
                            },
                            timeout=120,
                            trust_env=False,
                        )
                    require_upload_response(response, target)
                journal["finish_body"] = self.finish_body(journal)
                self.save(journal)
                stage = "finish"
                self.api(self.endpoint(journal, "finish"), journal["finish_body"])
                return True
            except RetiredAttempt:
                return True
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                transient = retryable_publication_response(
                    exc.response, storage=stage == "storage"
                )
                if 400 <= status < 500 and not transient:
                    if stage == "finish" and "publication_rejection" in journal:
                        print(
                            f"Failed terminal publication rejected (HTTP {status}); "
                            "stopping executor "
                            "and retaining the journal and lease for operator repair.",
                            flush=True,
                        )
                        self.shutdown.set()
                        return False
                    self.reject_publication(journal, stage, status)
                    continue
            except (ValueError, FileNotFoundError):
                if stage in {"inventory", "events", "artifact"}:
                    self.reject_publication(journal, stage)
                    continue
            except Exception:
                pass
            print(
                "Artifact publication pending; retaining local files for upload retry.",
                flush=True,
            )
            self.shutdown.wait(5)
        return False

    def retire_observation(self, observation):
        observation.done.set()
        try:
            observation.stop()
        except Exception:
            self.retiring_observation = observation
        else:
            if getattr(self, "retiring_observation", None) is observation:
                self.retiring_observation = None

    def reap_observation(self):
        observation = getattr(self, "retiring_observation", None)
        if observation and not any(thread.is_alive() for thread in observation.threads):
            self.retire_observation(observation)

    def terminate_process(self, process, reason="failure"):
        budget = getattr(self, "active_budget", None)
        if budget is not None and budget.process is process:
            budget.stop(reason)
        elif isinstance(process, JobHandle):
            process.stop()
        else:
            stop_process(process)

    def budget_deadline(self):
        self.stop_reason = "native_budget_exhausted"
        self.stop_training.set()

    def freeze_accounting(self, journal):
        assignment = journal["assignment"]
        if not assignment.get("native_budget") or "native_accounting" in journal:
            return
        from training_contract.budget import receipt

        owner = getattr(self, "active_budget", None)
        if owner is not None:
            journal["native_accounting"] = owner.accounting()
        elif journal.get("spawn_intent"):
            journal["native_accounting"] = receipt(
                assignment["native_budget"], "unknown", reason="recovery"
            )
        else:
            journal["native_accounting"] = receipt(
                assignment["native_budget"], "not_started", seconds=0.0
            )

    def _run_plugin_attempt(
        self,
        journal,
        directory: Path,
        capture,
        observation,
        *,
        config_digest: str,
        secrets,
    ):
        assignment = journal["assignment"]
        config = assignment["config"]
        plugin = _resolve_plugin_descriptor(assignment, getattr(self, "runtime", {}))
        execution_mode = plugin["execution_mode"]
        container_runtime = getattr(self, "container_runtime", None)
        if execution_mode == "container" and container_runtime is None:
            raise InputMismatchError(
                "Container runtime is unavailable on this supervisor"
            )
        local_allowlist = getattr(self, "local_plugins", None)
        if local_allowlist is None:
            local_allowlist = load_local_plugins_allowlist()
        if execution_mode == "local_process":
            resolve_local_plugin_entrypoint(
                plugin["plugin_id"],
                local_allowlist,
                requested_entrypoint=plugin["entrypoint"],
            )
        input_dir = directory / "input"
        output_dir = directory / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "config.json", config)
        base_path: Path | None = None
        base_model = config.get("base_model")
        if (
            isinstance(base_model, Mapping)
            and base_model.get("sha256")
            and base_model.get("filename")
        ):
            try:
                base_path = resolve_asset(self.models, base_model)
            except ValueError as exc:
                raise InputMismatchError(
                    "Controlled base weights are missing or changed"
                ) from exc
        self.download(
            assignment["dataset_url"],
            directory / "dataset.zip",
            config["dataset_sha256"],
            24 * 1024**3,
        )
        try:
            extract_dataset_archive(
                directory / "dataset.zip",
                input_dir / "dataset",
                require_data_yaml=False,
            )
        except ValueError as exc:
            raise InputMismatchError(str(exc)) from exc
        resume_path: Path | None = None
        initialization_path: Path | None = None
        if assignment.get("resume"):
            resume = assignment["resume"]
            resume_path = input_dir / "resume.pt"
            self.download(
                resume["url"],
                resume_path,
                resume["sha256"],
                2 * 1024**3,
                exact_size=resume["size_bytes"],
            )
            epoch = resume.get("epoch")
            total_epochs = (config.get("parameters") or {}).get("epochs")
            if (
                type(epoch) is not int
                or epoch <= 0
                or (type(total_epochs) is int and epoch >= total_epochs)
            ):
                raise InputMismatchError(
                    "Resume checkpoint has no valid completed epoch"
                )
            checkpoint = immutable_copy(
                resume_path, directory / f"checkpoint-{epoch:04d}.pt"
            )
            checkpoint.update(role="checkpoint", epoch=epoch)
            atomic_json(directory / "checkpoint.json", checkpoint)
        elif config.get("initialization"):
            from training_contract.initialization import MAX_WEIGHTS_BYTES, validate_pin

            pin = validate_pin(config["initialization"])
            source = assignment.get("initialization")
            if (
                not source
                or any(
                    source.get(key) != value for key, value in pin["artifact"].items()
                )
                or source.get("source_digest") != pin["source_digest"]
            ):
                raise InputMismatchError(
                    "Initialization assignment differs from its frozen source"
                )
            initialization_path = input_dir / "initialization.pt"
            self.download(
                source["url"],
                initialization_path,
                pin["artifact"]["sha256"],
                MAX_WEIGHTS_BYTES,
                exact_size=pin["artifact"]["size_bytes"],
            )
        train_spec = self._build_train_spec(
            assignment,
            config,
            execution_mode=execution_mode,
            input_dir=input_dir,
            base_path=base_path,
            resume_path=resume_path,
            initialization_path=initialization_path,
        )
        atomic_write_json(input_dir / "spec.json", train_spec)
        slot_gpu_uuid = getattr(self, "runtime", {}).get("gpu_uuid")
        container_spec = None
        if execution_mode == "container":
            if not plugin["image"]:
                raise InputMismatchError(
                    "Container plugin run is missing pinned image reference"
                )
            if not plugin["entrypoint"]:
                raise InputMismatchError("Container plugin run is missing entrypoint")
            container_spec = build_train_container_spec(
                resource_id=self.resource,
                attempt_id=str(assignment["attempt_id"]),
                run_id=str(assignment["run_id"]),
                plugin_id=plugin["plugin_id"],
                image=plugin["image"],
                entrypoint=plugin["entrypoint"],
                input_dir=input_dir,
                output_dir=output_dir,
                models_dir=self.models,
                resource=plugin["resource"],
                requires_gpu=plugin["requires_gpu"],
                slot_gpu_uuid=slot_gpu_uuid,
            )
        if self.stop_training.is_set() or self.shutdown.is_set():
            raise StopTraining()
        budget = assignment.get("native_budget")
        if budget:
            from training_contract.budget import validate_budget
            from .process_budget import ProcessBudget

            validate_budget(budget, assignment["attempt_id"], config_digest)
            self.active_budget = ProcessBudget(
                budget,
                stop=lambda handle: handle.stop(),
                live=None,
                on_deadline=self.budget_deadline,
            )
        journal["phase"] = "training"
        journal["execution_mode"] = execution_mode
        journal["spawn_intent"] = True
        if execution_mode == "container":
            journal["container_name"] = trainer_container_name(
                self.resource, str(assignment["attempt_id"])
            )
            journal["container_labels"] = trainer_container_labels(
                resource_id=self.resource,
                attempt_id=str(assignment["attempt_id"]),
                run_id=str(assignment["run_id"]),
                plugin_id=plugin["plugin_id"],
            )
        self.save(journal)
        if self.stop_training.is_set() or self.shutdown.is_set():
            raise StopTraining()

        def spawn():
            if execution_mode == "container":
                return launch_container_job(container_runtime, container_spec)
            lock_fd = self.lock.fileno() if self.lock is not None else None
            return launch_local_process(
                plugin_id=plugin["plugin_id"],
                allowlist=local_allowlist,
                requested_entrypoint=plugin["entrypoint"],
                args=[
                    "train",
                    "--spec",
                    str((input_dir / "spec.json").resolve()),
                    "--output",
                    str(output_dir.resolve()),
                ],
                requires_gpu=plugin["requires_gpu"],
                gpu_uuid=slot_gpu_uuid,
                lock_fd=lock_fd,
            )

        handle = self.active_budget.launch(spawn) if budget else spawn()
        self.active_process = handle
        if handle.stdout is not None:
            capture.start(handle.stdout)
        if execution_mode == "container":
            journal["container_id"] = handle.container_id
            journal["process"] = {
                "container_id": handle.container_id,
                "container_name": handle.name,
            }
        else:
            journal["process"] = {
                "pid": handle.pid,
                "started_at": psutil.Process(handle.pid).create_time(),
            }
            if observation:
                observation.sampler.attach(handle.pid, journal["process"]["started_at"])
        self.save(journal)
        while handle.poll() is None:
            if self.stop_training.is_set() or self.shutdown.is_set():
                self.terminate_process(
                    handle,
                    "shutdown" if self.shutdown.is_set() else "cancel",
                )
                break
            time.sleep(0.05)
        if handle.is_alive():
            self.terminate_process(handle)
        handle.wait()
        self.freeze_accounting(journal)
        self.active_process = None
        interrupted = self.stop_reason in {
            "heartbeat_expired",
            "heartbeat_unavailable",
            "executor_authority_lost",
        }
        journal["outcome"] = (
            "failed"
            if interrupted or self.stop_reason == "native_budget_exhausted"
            else "cancelled"
            if self.stop_training.is_set()
            else "succeeded"
            if handle.returncode == 0
            else "failed"
        )
        if journal["outcome"] == "failed":
            journal["error"] = (
                f"训练进程退出码 {handle.returncode}；请下载脱敏日志查看原因"
            )
            if interrupted or self.stop_reason == "native_budget_exhausted":
                code = (
                    "budget_exhausted"
                    if self.stop_reason == "native_budget_exhausted"
                    else "authority_lost"
                    if self.stop_reason == "executor_authority_lost"
                    else "execution_lost"
                )
                journal["failure"] = failure_receipt(
                    code, "execution", config_digest, journal["error"], secrets=secrets
                )
        self._stage_plugin_receipt(
            journal, directory, output_dir, config_digest=config_digest, secrets=secrets
        )
        if journal["outcome"] == "failed" and "failure" not in journal:
            code = (
                "native_signal" if (handle.returncode or 0) < 0 else "native_exception"
            )
            journal["failure"] = failure_receipt(
                code, "execution", config_digest, journal["error"], secrets=secrets
            )

    def execute(self, journal, *, recovery=False):
        from .log_capture import LogCapture
        from .observability import ObservationSession

        assignment = journal["assignment"]
        directory = self.state / "attempts" / assignment["attempt_id"]
        directory.mkdir(parents=True, exist_ok=True)
        config_digest = assignment.get("config_digest", "")
        raw_secrets = list(getattr(self, "redaction_secrets", ()))
        if assignment.get("lease_token"):
            raw_secrets.append(assignment["lease_token"])
        secrets = tuple(raw_secrets)
        observation = None
        self.reap_observation()
        capture = getattr(self, "active_capture", None)
        published = False
        process = getattr(self, "active_process", None)
        budget_owner = getattr(self, "active_budget", None)
        if budget_owner is not None and budget_owner.process is not None:
            process = budget_owner.process
        if process is not None:
            self.terminate_process(process, "recovery")
            self.freeze_accounting(journal)
            self.active_process = None
        self.stop_training.clear()
        self.stop_reason = None
        self.heartbeat_done.clear()
        self.heartbeat_thread = threading.Thread(
            target=self.heartbeat, args=(journal, directory), daemon=True
        )
        self.heartbeat_thread.start()
        try:
            try:
                try:
                    if getattr(self, "retiring_observation", None) is None:
                        observation = ObservationSession(self, journal, directory)
                        observation.start()
                except Exception:
                    if observation:
                        self.retire_observation(observation)
                    observation = None
                if recovery:
                    container_id = journal.get("container_id")
                    container_name = journal.get("container_name")
                    if container_id or container_name:
                        container_runtime = getattr(self, "container_runtime", None)
                        if container_runtime is None:
                            raise LeaseRetainedError(
                                "Container runtime unavailable during recovery; retaining lease"
                            )
                        reconcile_container_by_id_or_name(
                            container_runtime,
                            container_id=container_id,
                            name=container_name,
                        )
                    else:
                        previous = journal.get("process")
                        if (
                            previous
                            and "pid" in previous
                            and journal["claim"]["boot_id"] == self.boot
                            and live_group(previous["pid"])
                        ):
                            raise LeaseRetainedError(
                                "Previous training group is still alive; retaining its lease"
                            )
                    if "outcome" not in journal:
                        journal["outcome"] = "failed"
                        journal["error"] = (
                            "执行器重启；已通过独占资源锁确认旧训练进程退出"
                        )
                        journal["failure"] = failure_receipt(
                            "execution_lost",
                            "execution",
                            config_digest,
                            journal["error"],
                        )
                    if (
                        "artifacts" not in journal
                        and capture is None
                        and (directory / ".log-head").exists()
                    ):
                        capture = LogCapture(
                            directory,
                            secrets=secrets,
                            emit=observation.log if observation else None,
                        )
                else:
                    capture = LogCapture(
                        directory,
                        secrets=secrets,
                        emit=observation.log if observation else None,
                    )
                    self.active_capture = capture
                    capture.write(b"Controlled training preparation started.\n")
                    if _is_plugin_run(assignment):
                        self._run_plugin_attempt(
                            journal,
                            directory,
                            capture,
                            observation,
                            config_digest=config_digest,
                            secrets=secrets,
                        )
                    else:
                        config = assignment["config"]
                        atomic_json(directory / "config.json", config)
                        try:
                            base = resolve_asset(self.models, config["base_model"])
                        except ValueError as exc:
                            raise InputMismatchError(
                                "Controlled base weights are missing or changed"
                            ) from exc
                        self.download(
                            assignment["dataset_url"],
                            directory / "dataset.zip",
                            config["dataset_sha256"],
                            24 * 1024**3,
                        )
                        try:
                            data_yaml = extract_dataset(
                                directory / "dataset.zip", directory / "dataset"
                            )
                        except ValueError as exc:
                            raise InputMismatchError(str(exc)) from exc
                        resume_path = None
                        initialization_path = None
                        if assignment["resume"]:
                            resume = assignment["resume"]
                            resume_path = directory / "resume.pt"
                            self.download(
                                resume["url"],
                                resume_path,
                                resume["sha256"],
                                2 * 1024**3,
                                exact_size=resume["size_bytes"],
                            )
                            epoch = resume.get("epoch")
                            if (
                                type(epoch) is not int
                                or not 0 < epoch < config["parameters"]["epochs"]
                            ):
                                raise InputMismatchError(
                                    "Resume checkpoint has no valid completed epoch"
                                )
                            checkpoint = immutable_copy(
                                resume_path, directory / f"checkpoint-{epoch:04d}.pt"
                            )
                            checkpoint.update(role="checkpoint", epoch=epoch)
                            atomic_json(directory / "checkpoint.json", checkpoint)
                        elif config.get("initialization"):
                            from training_contract.initialization import (
                                MAX_WEIGHTS_BYTES,
                                validate_pin,
                            )

                            pin = validate_pin(config["initialization"])
                            source = assignment.get("initialization")
                            if (
                                not source
                                or any(
                                    source.get(key) != value
                                    for key, value in pin["artifact"].items()
                                )
                                or source.get("source_digest") != pin["source_digest"]
                            ):
                                raise InputMismatchError(
                                    "Initialization assignment differs from its frozen source"
                                )
                            initialization_path = directory / "initialization.pt"
                            self.download(
                                source["url"],
                                initialization_path,
                                pin["artifact"]["sha256"],
                                MAX_WEIGHTS_BYTES,
                                exact_size=pin["artifact"]["size_bytes"],
                            )
                        spec = {
                            "run_id": assignment["run_id"],
                            "attempt_id": assignment["attempt_id"],
                            "framework": config["framework"],
                            "parameters": config["parameters"],
                            "base_path": str(base),
                            "resume_path": str(resume_path) if resume_path else None,
                            "initialization_path": str(initialization_path)
                            if initialization_path
                            else None,
                            "data_yaml": str(data_yaml),
                            "frozen_config": config,
                        }
                        atomic_json(directory / "spec.json", spec)
                        journal["phase"] = "training"
                        self.save(journal)
                        if self.stop_training.is_set() or self.shutdown.is_set():
                            raise StopTraining()
                        environment = {
                            key: value
                            for key, value in os.environ.items()
                            if not key.startswith("TRAINING_")
                        }
                        budget = assignment.get("native_budget")
                        if budget:
                            from training_contract.budget import validate_budget
                            from .process_budget import ProcessBudget

                            validate_budget(
                                budget, assignment["attempt_id"], config_digest
                            )
                            self.active_budget = ProcessBudget(
                                budget,
                                stop=stop_process,
                                live=live_group,
                                on_deadline=self.budget_deadline,
                            )
                            journal["spawn_intent"] = True
                            self.save(journal)

                        def spawn():
                            return subprocess.Popen(
                                [
                                    sys.executable,
                                    "-m",
                                    "training_executor.train",
                                    "--spec",
                                    str(directory / "spec.json"),
                                ],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                start_new_session=True,
                                pass_fds=(self.lock.fileno(),),
                                env=environment,
                            )

                        process = (
                            self.active_budget.launch(spawn) if budget else spawn()
                        )
                        self.active_process = process
                        capture.start(process.stdout)
                        journal["process"] = {
                            "pid": process.pid,
                            "started_at": psutil.Process(process.pid).create_time(),
                        }
                        if observation:
                            observation.sampler.attach(
                                process.pid, journal["process"]["started_at"]
                            )
                        self.save(journal)
                        while process.poll() is None:
                            if self.stop_training.is_set() or self.shutdown.is_set():
                                self.terminate_process(
                                    process,
                                    "shutdown" if self.shutdown.is_set() else "cancel",
                                )
                                break
                            time.sleep(0.2)
                        if live_group(process.pid):
                            self.terminate_process(process)
                        process.wait()
                        self.freeze_accounting(journal)
                        self.active_process = None
                        interrupted = self.stop_reason in {
                            "heartbeat_expired",
                            "heartbeat_unavailable",
                            "executor_authority_lost",
                        }
                        journal["outcome"] = (
                            "failed"
                            if interrupted
                            or self.stop_reason == "native_budget_exhausted"
                            else "cancelled"
                            if self.stop_training.is_set()
                            else "succeeded"
                            if process.returncode == 0
                            else "failed"
                        )
                        if journal["outcome"] == "failed":
                            journal["error"] = (
                                f"训练进程退出码 {process.returncode}；请下载脱敏日志查看原因"
                            )
                            native_failure = directory / "failure.json"
                            if (
                                native_failure.is_file()
                                and native_failure.stat().st_size <= 16 * 1024
                            ):
                                try:
                                    receipt = json.loads(native_failure.read_text())
                                    if (
                                        receipt.get("config_digest") == config_digest
                                        and receipt.get("code") in FAILURE_CODES
                                        and receipt.get("stage") in PHASES
                                        and receipt.get("source") == "native"
                                    ):
                                        journal["failure"] = failure_receipt(
                                            receipt["code"],
                                            receipt["stage"],
                                            config_digest,
                                            receipt["diagnostic"],
                                            exception_type=receipt.get(
                                                "exception_type"
                                            ),
                                            secrets=secrets,
                                            source="native",
                                        )
                                except (ValueError, TypeError, KeyError):
                                    pass
                            if (
                                interrupted
                                or self.stop_reason == "native_budget_exhausted"
                                or "failure" not in journal
                            ):
                                code = (
                                    "budget_exhausted"
                                    if self.stop_reason == "native_budget_exhausted"
                                    else "authority_lost"
                                    if self.stop_reason == "executor_authority_lost"
                                    else "execution_lost"
                                    if interrupted
                                    else "native_signal"
                                    if process.returncode < 0
                                    else "native_exception"
                                )
                                journal["failure"] = failure_receipt(
                                    code, "execution", config_digest, journal["error"]
                                )
                if (
                    self.shutdown.is_set()
                    and journal.get("outcome") == "succeeded"
                    and not (directory / "result.json").exists()
                ):
                    journal["outcome"] = "failed"
            except StopTraining:
                journal["outcome"] = "cancelled"
            except LeaseRetainedError:
                raise
            except Exception as exc:
                owner = getattr(self, "active_budget", None)
                if owner is not None and owner.process is not None:
                    process = owner.process
                elif getattr(self, "active_process", None) is not None:
                    process = self.active_process
                if process is not None:
                    self.terminate_process(process)
                if (
                    recovery
                    and journal.get("process")
                    and "pid" in journal["process"]
                    and journal["claim"]["boot_id"] == self.boot
                    and live_group(journal["process"]["pid"])
                ):
                    raise
                self.active_process = None
                journal["outcome"] = "failed"
                journal["error"] = f"训练准备失败：{type(exc).__name__}"
                journal["failure"] = failure_receipt(
                    "input_mismatch"
                    if isinstance(exc, InputMismatchError)
                    else "preparation_failed",
                    "preparing",
                    config_digest,
                    str(exc),
                    exception_type=type(exc).__name__,
                    secrets=secrets,
                )
            self.freeze_accounting(journal)
            self.save(journal)
            if capture and "artifacts" not in journal:
                redaction = capture.finish()
                self.active_capture = None
                if observation:
                    try:
                        observation.freeze(redaction)
                    except Exception:
                        pass
            if observation:
                observation.phase("uploading")
            self.save(journal)
            published = self.upload_and_finish(journal, directory)
        finally:
            self.heartbeat_done.set()
            self.heartbeat_thread.join(timeout=20)
            if observation:
                self.retire_observation(observation)
        if published:
            import shutil

            shutil.rmtree(directory)
            self.journal_path.unlink(missing_ok=True)
            self.active_budget = None

    def run(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda signum, frame: self.shutdown.set())
        from .preview_worker import PreviewSupervisor

        preview = PreviewSupervisor(self)
        thread = threading.Thread(target=preview.run, name="native-preview-cpu")
        thread.start()
        try:
            self.run_training()
        finally:
            self.shutdown.set()
            thread.join(timeout=90)
            if thread.is_alive():
                raise RuntimeError(
                    "CPU preview is still stopping; its lease remains held"
                )

    def run_training(self):
        lock_path = self.state / (
            "gpu-" + hashlib.sha256(self.resource.encode()).hexdigest()[:24] + ".lock"
        )
        self.lock = lock_path.open("a+")
        while not self.shutdown.is_set():
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                self.shutdown.wait(2)
        if self.shutdown.is_set():
            return
        container_runtime = getattr(self, "container_runtime", None)
        if container_runtime is not None:
            reconcile_orphan_containers(container_runtime, self.resource)
        while not self.shutdown.is_set():
            self.reap_observation()
            if self.journal_path.exists():
                journal = json.loads(self.journal_path.read_text())
                try:
                    if "assignment" not in journal:
                        journal["claim"]["recover_only"] = True
                        if not self.claim(journal):
                            self.journal_path.unlink()
                            continue
                    self.execute(
                        journal,
                        recovery=journal["claim"]["boot_id"] != self.boot
                        or journal["phase"] != "preparing",
                    )
                except Exception:
                    self.shutdown.wait(5)
                continue
            journal = {
                "claim": {
                    "executor_id": self.executor_id,
                    "boot_id": self.boot,
                    "claim_id": str(uuid.uuid4()),
                    "resource_id": self.resource,
                    "runtime": self.runtime,
                },
                "phase": "claiming",
            }
            self.save(journal)
            try:
                if self.claim(journal):
                    self.execute(journal)
                else:
                    self.journal_path.unlink(missing_ok=True)
                    self.shutdown.wait(5)
            except Exception:
                self.shutdown.wait(5)


def main():
    Supervisor().run()


if __name__ == "__main__":
    main()
