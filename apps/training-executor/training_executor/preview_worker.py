"""One independent CPU preview slot inside the existing executor process."""

from __future__ import annotations

from collections.abc import Mapping
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

import httpx
import psutil
from training_contract.observability import redact_text
from training_contract.preview import LIMITS
from training_contract.trainer_plugin import (
    TRAINER_PROTOCOL_VERSION,
    PreviewReportV1,
    PreviewSourceV1,
    PreviewSpecV1,
    atomic_write_json,
)

from .files import atomic_json, digest_file, immutable_copy
from .plugin_runner import (
    JobHandle,
    LeaseRetainedError,
    build_preview_container_spec,
    launch_container_job,
    launch_local_process,
    load_local_plugins_allowlist,
    preview_container_labels,
    preview_container_name,
    reconcile_container_by_id_or_name,
    reconcile_orphan_containers,
    resolve_local_plugin_entrypoint,
)
from .worker import (
    RetiredAttempt,
    StopTraining,
    Supervisor,
    live_group,
    require_upload_response,
    retryable_publication_response,
    stop_process,
)

_LOCAL_CPU_PREVIEW_ENV = {
    "NVIDIA_VISIBLE_DEVICES": "void",
    "CUDA_VISIBLE_DEVICES": "",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "POLARS_MAX_THREADS": "1",
    "NUMEXPR_MAX_THREADS": "1",
}


def _extract_preview_image(plugin: Mapping[str, Any]) -> str | None:
    for candidate in (plugin.get("image"), plugin.get("artifact")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, Mapping):
            for key in ("image", "reference", "oci_reference"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _build_plugin_preview_report(
    assignment: Mapping[str, Any],
    spec: Mapping[str, Any],
    report: PreviewReportV1,
) -> dict[str, Any]:
    details = report.details
    if "mosaic_closed" in details and "policy" in details:
        outputs = []
        for index, item in enumerate(report.outputs):
            meta = item.metadata
            outputs.append(
                {
                    "index": index,
                    "image": f"preview-{index}.png",
                    "sha256": item.sha256,
                    "width": item.width,
                    "height": item.height,
                    "boxes": meta.get("boxes", []),
                    "source_draws": meta.get("source_draws", []),
                    "sources_read": meta.get("sources_read", []),
                    "operations": meta.get("operations", []),
                    "filtered": meta.get("filtered", 0),
                    "clipped": meta.get("clipped", 0),
                }
            )
        return {
            "revision": 1,
            "preview_id": str(assignment["preview_id"]),
            "input_digest": report.input_digest,
            "config_digest": spec["config_digest"],
            "policy": details["policy"],
            "policy_digest": report.policy_digest,
            "seed": spec["seed"],
            "epoch": spec["epoch"],
            "mosaic_closed": bool(details["mosaic_closed"]),
            "native_parameters": details.get("native_parameters")
            or spec.get("native_values")
            or {},
            "transform_parameters": details.get("transform_parameters") or {},
            "source_pool": spec["sources"],
            "pool_summary": spec.get("pool_summary") or {},
            "outputs": outputs,
            "runtime": {**report.runtime, "cuda_initialized": False},
        }
    dumped = report.model_dump(mode="json")
    for index, item in enumerate(dumped["outputs"]):
        item["filename"] = f"preview-{index}.png"
    return dumped


class PreviewSupervisor:
    def __init__(self, training):
        self.state = training.state
        self.models = training.models
        self.runtime = training.runtime
        self.container_runtime = getattr(training, "container_runtime", None)
        self.local_plugins = getattr(training, "local_plugins", None)
        self.executor_id, self.boot = training.executor_id, training.boot
        self.resource = (
            os.environ.get("TRAINING_NODE_ID", "local") + "/augmentation-preview/cpu-0"
        )
        self.shutdown = training.shutdown
        self.stop_training = threading.Event()
        self.heartbeat_done = threading.Event()
        self.stop_reason = None
        self.journal_path = self.state / "preview-assignment.json"
        self.client = httpx.Client(
            base_url=training.client.base_url,
            headers=training.client.headers,
            timeout=15,
            trust_env=False,
        )
        self.lock = None
        self.process = None
        self.process_exited = True
        self.publication_blocked = False
        self.preview_deadline_seconds = LIMITS["child_seconds"]

    def api(self, endpoint, payload):
        response = self.client.post("previews/" + endpoint, json=payload)
        if response.status_code in {404, 409}:
            detail = response.json().get("detail")
            if (
                response.status_code == 404
                or isinstance(detail, dict)
                and detail.get("code") == "stale_preview"
            ):
                raise RetiredAttempt("CPU preview was retired")
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
        return journal["assignment"]["preview_id"] + "/" + suffix

    def claim(self, journal):
        assignment = self.api("claim", journal["claim"])["assignment"]
        if assignment:
            journal["assignment"], journal["phase"] = assignment, "preparing"
            self.save(journal)
        return assignment

    def heartbeat(self, journal):
        last_success = time.monotonic()
        with httpx.Client(
            base_url=self.client.base_url,
            headers=self.client.headers,
            timeout=10,
            trust_env=False,
        ) as client:
            while not self.heartbeat_done.is_set():
                try:
                    response = client.post(
                        "previews/" + self.endpoint(journal, "heartbeat"),
                        json=self.fence(journal),
                    )
                    if response.status_code in {403, 404}:
                        self.stop_reason = "原生预览执行权限已失效"
                        self.stop_training.set()
                        return
                    response.raise_for_status()
                    if response.json()["stop"]:
                        self.stop_reason = (
                            response.json().get("reason") or "已请求取消原生预览"
                        )
                        self.stop_training.set()
                    last_success = time.monotonic()
                except Exception:
                    if time.monotonic() - last_success > 15:
                        self.stop_reason = "CPU 预览心跳不可用，已停止执行"
                        self.stop_training.set()
                self.heartbeat_done.wait(2)

    def inventory(self, journal, directory):
        names = [
            f"preview-{index}.png"
            for index in range(journal["assignment"]["spec"]["count"])
        ] + ["report.json"]
        artifacts = []
        for name in names:
            path = directory / name
            bound = (
                LIMITS["report_bytes"]
                if name == "report.json"
                else LIMITS["output_bytes"]
            )
            if not path.is_file() or not 0 < path.stat().st_size <= bound:
                raise ValueError(
                    "Native preview output is missing or exceeds its bound"
                )
            artifacts.append(
                {
                    "name": name,
                    "sha256": digest_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
        return artifacts

    def finish_body(self, journal):
        return {
            **self.fence(journal),
            "status": journal["outcome"],
            "process_exited": True,
            "error_message": journal.get("error"),
        }

    def reject_publication(self, journal, stage, status=None):
        journal["outcome"] = "failed"
        journal["error"] = f"原生预览产物发布失败：{stage}" + (
            f" 返回 HTTP {status}" if status is not None else " 产物校验未通过"
        )
        journal["publication_rejection"] = {"stage": stage, "status_code": status}
        journal["finish_body"] = self.finish_body(journal)
        journal["phase"] = "finishing"
        self.save(journal)

    def publish(self, journal, directory):
        while not self.shutdown.is_set() and not self.publication_blocked:
            stage = "finish" if "finish_body" in journal else "inventory"
            try:
                if "finish_body" in journal:
                    self.save(journal)
                    self.api(self.endpoint(journal, "finish"), journal["finish_body"])
                    return True
                if self.stop_training.is_set() and journal["outcome"] == "succeeded":
                    journal["outcome"], journal["error"] = (
                        "cancelled",
                        self.stop_reason,
                    )
                if journal["outcome"] == "succeeded":
                    if "artifacts" not in journal:
                        journal["artifacts"] = self.inventory(journal, directory)
                        self.save(journal)
                    stage = "uploads"
                    urls = self.api(
                        self.endpoint(journal, "uploads"),
                        {**self.fence(journal), "artifacts": journal["artifacts"]},
                    )
                    for artifact in journal["artifacts"]:
                        if self.stop_training.is_set() or self.shutdown.is_set():
                            raise StopTraining()
                        stage = "artifact"
                        path = directory / artifact["name"]
                        if digest_file(path) != artifact["sha256"]:
                            raise ValueError("Local preview output changed")
                        target = urls[artifact["name"]]
                        with path.open("rb") as source:
                            stage = "storage"
                            response = httpx.put(
                                target["url"],
                                content=source,
                                headers={
                                    **target["headers"],
                                    "Content-Length": str(artifact["size_bytes"]),
                                },
                                timeout=15,
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
            except StopTraining:
                journal["outcome"], journal["error"] = (
                    "cancelled",
                    self.stop_reason or "已取消原生预览",
                )
                self.save(journal)
                continue
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                transient = retryable_publication_response(
                    exc.response, storage=stage == "storage"
                )
                if 400 <= status < 500 and not transient:
                    if stage == "finish" and "publication_rejection" in journal:
                        print(
                            f"Failed CPU preview terminal publication rejected (HTTP {status}); "
                            "stopping CPU preview supervisor and retaining its journal "
                            "and lease for operator repair.",
                            flush=True,
                        )
                        self.publication_blocked = True
                        return False
                    self.reject_publication(journal, stage, status)
                    continue
            except (ValueError, FileNotFoundError):
                if stage in {"inventory", "artifact"}:
                    self.reject_publication(journal, stage)
                    continue
            except Exception:
                pass
            self.shutdown.wait(2)
        return False

    def confirm_exit(self):
        if self.process is not None:
            if isinstance(self.process, JobHandle):
                if self.process.is_alive():
                    self.process.stop()
                if self.process.is_alive():
                    raise LeaseRetainedError(
                        "CPU preview job is still alive; retaining lease"
                    )
                self.process.wait()
            else:
                if live_group(self.process.pid):
                    stop_process(self.process)
                if live_group(self.process.pid):
                    raise LeaseRetainedError(
                        "CPU preview group is still alive; retaining lease"
                    )
                self.process.wait()
            self.process = None
            self.process_exited = True
        if not self.process_exited:
            raise LeaseRetainedError("CPU preview exit is unconfirmed; retaining lease")

    def execute(self, journal, *, recovery=False):
        self.confirm_exit()
        if recovery:
            container_id = journal.get("container_id")
            container_name = journal.get("container_name")
            if container_id or container_name:
                container_runtime = getattr(self, "container_runtime", None)
                if container_runtime is None:
                    raise LeaseRetainedError(
                        "Container runtime unavailable during preview recovery; retaining lease"
                    )
                reconcile_container_by_id_or_name(
                    container_runtime,
                    container_id=container_id,
                    name=container_name,
                )
            elif (
                journal["claim"]["boot_id"] == self.boot
                and journal.get("process")
                and "pid" in journal["process"]
                and live_group(journal["process"]["pid"])
            ):
                raise LeaseRetainedError(
                    "Previous CPU preview group is alive; retaining lease"
                )
        assignment = journal["assignment"]
        directory = self.state / "previews" / assignment["preview_id"]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stop_training.clear()
        self.stop_reason = None
        self.heartbeat_done.clear()
        heartbeat = threading.Thread(
            target=self.heartbeat, args=(journal,), daemon=True
        )
        heartbeat.start()
        try:
            self.execute_owned(journal, directory, recovery=recovery)
        finally:
            self.heartbeat_done.set()
            heartbeat.join(timeout=15)

    def _run_plugin_preview(self, journal: dict[str, Any], directory: Path) -> None:
        assignment = journal["assignment"]
        spec = assignment["spec"]
        plugin = spec["trainer_plugin"]
        if not isinstance(plugin, Mapping):
            raise ValueError("Preview trainer_plugin descriptor must be a mapping")
        plugin_id = plugin.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise ValueError("Preview trainer_plugin is missing plugin_id")
        plugin_id = plugin_id.strip()
        execution_mode = plugin.get("execution_mode")
        if execution_mode not in {"container", "local_process"}:
            raise ValueError(
                "Preview execution_mode must be container or local_process"
            )
        container_runtime = getattr(self, "container_runtime", None)
        if execution_mode == "container" and container_runtime is None:
            raise ValueError("Container runtime is unavailable for plugin preview")
        local_allowlist = getattr(self, "local_plugins", None)
        if local_allowlist is None:
            local_allowlist = load_local_plugins_allowlist()
        if execution_mode == "local_process":
            resolve_local_plugin_entrypoint(
                plugin_id,
                local_allowlist,
                requested_entrypoint=plugin.get("entrypoint"),
            )
        input_dir = directory / "input"
        output_dir = directory / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        preview_sources: list[PreviewSourceV1] = []
        for member in spec["sources"]:
            image_rel = member.get("image_path") or member["filename"]
            image_dst = input_dir / image_rel
            if not image_dst.resolve().is_relative_to(input_dir.resolve()):
                raise ValueError("Preview input path escapes its owner")
            image_dst.parent.mkdir(parents=True, exist_ok=True)
            Supervisor.download(
                self,
                assignment["source_urls"][member["task_id"]],
                image_dst,
                member["sha256"],
                LIMITS["source_bytes"],
                exact_size=member["size_bytes"],
            )
            label_path = member.get("label_path")
            label_sha256 = member.get("label_sha256")
            if label_path is not None:
                label_dst = input_dir / label_path
                if not label_dst.resolve().is_relative_to(input_dir.resolve()):
                    raise ValueError("Preview label path escapes its owner")
                label_dst.parent.mkdir(parents=True, exist_ok=True)
                label_text = member.get("labels", "")
                label_dst.write_text(label_text)
                if label_sha256 is not None and digest_file(label_dst) != label_sha256:
                    raise ValueError("Preview source label digest mismatch")
                box_count = (
                    member["box_count"]
                    if "box_count" in member
                    else len([line for line in label_text.splitlines() if line.strip()])
                )
            else:
                box_count = member.get("box_count", 0)
            preview_sources.append(
                PreviewSourceV1(
                    task_id=str(member["task_id"]),
                    filename=image_rel,
                    sha256=member["sha256"],
                    width=member["width"],
                    height=member["height"],
                    size_bytes=member["size_bytes"],
                    box_count=box_count,
                    label_path=label_path,
                    label_sha256=label_sha256,
                )
            )
        config = spec.get("config") or {}
        base_model = config.get("base_model")
        architecture_id = (
            spec.get("architecture_id")
            or (base_model.get("id") if isinstance(base_model, Mapping) else None)
            or "default"
        )
        preview_spec = PreviewSpecV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            preview_id=str(assignment["preview_id"]),
            framework_id=str(
                spec.get("framework_id")
                or config.get("framework_id")
                or config.get("framework")
                or plugin.get("framework_id")
                or plugin_id
            ),
            task=str(spec.get("task") or config.get("task") or "detect"),
            architecture_id=str(architecture_id),
            input_digest=str(assignment["input_digest"]),
            config_digest=str(spec["config_digest"]),
            dataset_snapshot_digest=str(spec["dataset_snapshot_digest"]),
            policy_digest=str(spec["policy_digest"]),
            seed=spec["seed"],
            epoch=spec["epoch"],
            count=spec["count"],
            sources=preview_sources,
            parameters=dict(spec.get("parameters") or config.get("parameters") or {}),
            native_values=dict(spec.get("native_values") or {}),
            pool_summary=dict(spec.get("pool_summary") or {}),
        )
        atomic_write_json(input_dir / "preview-spec.json", preview_spec)
        container_spec = None
        if execution_mode == "container":
            image = _extract_preview_image(plugin)
            if not image:
                raise ValueError("Container preview plugin is missing pinned image")
            entrypoint = plugin.get("entrypoint")
            if not entrypoint:
                raise ValueError("Container preview plugin is missing entrypoint")
            container_spec = build_preview_container_spec(
                resource_id=self.resource,
                preview_id=str(assignment["preview_id"]),
                plugin_id=plugin_id,
                image=image,
                entrypoint=entrypoint,
                input_dir=input_dir,
                output_dir=output_dir,
                models_dir=self.models,
            )
        journal["phase"] = "transforming"
        journal["execution_mode"] = execution_mode
        if execution_mode == "container":
            journal["container_name"] = preview_container_name(
                self.resource, str(assignment["preview_id"])
            )
            journal["container_labels"] = preview_container_labels(
                resource_id=self.resource,
                preview_id=str(assignment["preview_id"]),
                plugin_id=plugin_id,
            )
        self.save(journal)
        if self.shutdown.is_set() or self.stop_training.is_set():
            raise StopTraining()
        deadline = time.monotonic() + getattr(
            self, "preview_deadline_seconds", LIMITS["child_seconds"]
        )
        handle: JobHandle
        if execution_mode == "container":
            self.process_exited = False
            try:
                handle = self.process = launch_container_job(
                    container_runtime, container_spec, open_logs=False
                )
            except LeaseRetainedError:
                raise
            except Exception:
                reconcile_container_by_id_or_name(
                    container_runtime,
                    container_id=journal.get("container_id"),
                    name=journal.get("container_name"),
                )
                self.process_exited = True
                raise
            journal["container_id"] = handle.container_id
            journal["process"] = {
                "container_id": handle.container_id,
                "container_name": handle.name,
            }
            self.save(journal)
            while handle.poll() is None:
                if (
                    self.shutdown.is_set()
                    or self.stop_training.is_set()
                    or time.monotonic() > deadline
                ):
                    self.stop_reason = (
                        self.stop_reason or "原生预览已停止或超过执行时限"
                    )
                    self.stop_training.set()
                    handle.stop()
                    break
                self.shutdown.wait(0.05)
            if handle.is_alive():
                handle.stop()
            handle.wait()
            self.process = None
            self.process_exited = True
        else:
            with (directory / "preview.log").open("ab") as log:
                self.process_exited = False
                try:
                    handle = self.process = launch_local_process(
                        plugin_id=plugin_id,
                        allowlist=local_allowlist,
                        requested_entrypoint=plugin.get("entrypoint"),
                        args=[
                            "preview",
                            "--spec",
                            str((input_dir / "preview-spec.json").resolve()),
                            "--output",
                            str(output_dir.resolve()),
                        ],
                        requires_gpu=False,
                        gpu_uuid=None,
                        lock_fd=self.lock.fileno() if self.lock is not None else None,
                        stdout=log,
                        env_overrides=_LOCAL_CPU_PREVIEW_ENV,
                    )
                except Exception:
                    self.process_exited = True
                    raise
                try:
                    started_at = psutil.Process(handle.pid).create_time()
                except psutil.NoSuchProcess:
                    started_at = time.time()
                journal["process"] = {
                    "pid": handle.pid,
                    "started_at": started_at,
                }
                self.save(journal)
                while handle.poll() is None:
                    if (
                        self.shutdown.is_set()
                        or self.stop_training.is_set()
                        or time.monotonic() > deadline
                    ):
                        self.stop_reason = (
                            self.stop_reason or "原生预览已停止或超过执行时限"
                        )
                        self.stop_training.set()
                        handle.stop()
                        break
                    self.shutdown.wait(0.05)
                if handle.is_alive():
                    handle.stop()
                handle.wait()
                self.process = None
                self.process_exited = True
        if self.stop_training.is_set():
            journal["outcome"] = "cancelled"
            journal["error"] = self.stop_reason or "已取消原生预览"
            return
        if handle.returncode != 0:
            journal["outcome"] = "failed"
            journal["error"] = (
                f"原生预览进程退出码 {handle.returncode}；请检查冻结素材与配置"
            )
            return
        report_path = output_dir / "report.json"
        if (
            report_path.is_symlink()
            or not report_path.is_file()
            or not 0 < report_path.stat().st_size <= LIMITS["report_bytes"]
        ):
            raise ValueError("Plugin preview report.json is missing or exceeds bound")
        report = PreviewReportV1.model_validate(json.loads(report_path.read_text()))
        if report.status != "succeeded":
            journal["outcome"] = "failed"
            journal["error"] = redact_text(
                report.failure_message or report.failure_code or "插件预览失败"
            )[:1000]
            return
        if report.cuda_initialized is not False:
            raise ValueError("Plugin preview unexpectedly initialized CUDA")
        if report.input_digest != assignment["input_digest"]:
            raise ValueError("Plugin preview report input_digest mismatch")
        if report.policy_digest != spec["policy_digest"]:
            raise ValueError("Plugin preview report policy_digest mismatch")
        if len(report.outputs) != spec["count"]:
            raise ValueError("Plugin preview report output count mismatch")
        resolved_output = output_dir.resolve()
        for index, item in enumerate(report.outputs):
            src = output_dir / item.filename
            if (
                src.is_symlink()
                or not src.is_file()
                or not src.resolve().is_relative_to(resolved_output)
            ):
                raise ValueError(
                    f"Plugin preview output is missing or unsafe: {item.filename}"
                )
            if src.stat().st_size != item.size_bytes or digest_file(src) != item.sha256:
                raise ValueError(
                    f"Plugin preview output digest or size mismatch: {item.filename}"
                )
            immutable_copy(src, directory / f"preview-{index}.png")
        projected = _build_plugin_preview_report(assignment, spec, report)
        atomic_json(directory / "report.json", projected)
        if (directory / "report.json").stat().st_size > LIMITS["report_bytes"]:
            raise ValueError("Projected preview report exceeds its bound")
        journal["outcome"] = "succeeded"

    def execute_owned(self, journal, directory, *, recovery):
        assignment = journal["assignment"]
        process = None
        try:
            if recovery:
                journal.setdefault("outcome", "failed")
                journal.setdefault("error", "执行器重启；已确认旧 CPU 预览进程退出")
            else:
                if assignment["stop"]:
                    raise StopTraining()
                spec = assignment["spec"]
                if "trainer_plugin" in spec:
                    self._run_plugin_preview(journal, directory)
                else:
                    base = self.models / spec["config"]["base_model"]["filename"]
                    if (
                        not base.is_file()
                        or digest_file(base) != spec["config"]["base_model"]["sha256"]
                    ):
                        raise ValueError(
                            "Controlled preview base weights are missing or changed"
                        )
                    for member in spec["sources"]:
                        image, label = (
                            directory / member["image_path"],
                            directory / member["label_path"],
                        )
                        for path in (image, label):
                            if not path.resolve().is_relative_to(directory.resolve()):
                                raise ValueError("Preview input path escapes its owner")
                            path.parent.mkdir(parents=True, exist_ok=True)
                        Supervisor.download(
                            self,
                            assignment["source_urls"][member["task_id"]],
                            image,
                            member["sha256"],
                            LIMITS["source_bytes"],
                            exact_size=member["size_bytes"],
                        )
                        label.write_text(member["labels"])
                    native_spec = {
                        **spec,
                        "preview_id": assignment["preview_id"],
                        "input_digest": assignment["input_digest"],
                        "base_path": str(base),
                    }
                    atomic_json(directory / "spec.json", native_spec)
                    journal["phase"] = "transforming"
                    self.save(journal)
                    if self.shutdown.is_set() or self.stop_training.is_set():
                        raise StopTraining()
                    environment = {
                        key: value
                        for key, value in os.environ.items()
                        if not key.startswith("TRAINING_")
                    }
                    environment.update(
                        CUDA_VISIBLE_DEVICES="",
                        OMP_NUM_THREADS="1",
                        OPENBLAS_NUM_THREADS="1",
                        MKL_NUM_THREADS="1",
                        POLARS_MAX_THREADS="1",
                        NUMEXPR_MAX_THREADS="1",
                        YOLO_CONFIG_DIR=str(directory / ".cache/ultralytics"),
                        MPLCONFIGDIR=str(directory / ".cache/matplotlib"),
                    )
                    with (directory / "preview.log").open("ab") as log:
                        self.process_exited = False
                        try:
                            process = self.process = subprocess.Popen(
                                [
                                    sys.executable,
                                    "-m",
                                    "training_executor.preview",
                                    "--spec",
                                    str(directory / "spec.json"),
                                ],
                                env=environment,
                                stdout=log,
                                stderr=subprocess.STDOUT,
                                start_new_session=True,
                                pass_fds=(self.lock.fileno(),),
                            )
                        except Exception:
                            self.process_exited = True
                            raise
                        journal["process"] = {
                            "pid": process.pid,
                            "started_at": psutil.Process(process.pid).create_time(),
                        }
                        self.save(journal)
                        deadline = time.monotonic() + LIMITS["child_seconds"] + 2
                        while process.poll() is None:
                            if (
                                self.shutdown.is_set()
                                or self.stop_training.is_set()
                                or time.monotonic() > deadline
                            ):
                                self.stop_reason = (
                                    self.stop_reason or "原生预览已停止或超过执行时限"
                                )
                                self.stop_training.set()
                                stop_process(process)
                                break
                            self.shutdown.wait(0.2)
                        if live_group(process.pid):
                            stop_process(process)
                    journal["outcome"] = (
                        "cancelled"
                        if self.stop_training.is_set()
                        else "succeeded"
                        if process.returncode == 0
                        else "failed"
                    )
                    if journal["outcome"] != "succeeded":
                        journal["error"] = (
                            self.stop_reason
                            or f"原生预览进程退出码 {process.returncode}；请检查冻结素材与配置"
                        )
        except StopTraining:
            journal["outcome"], journal["error"] = (
                "cancelled",
                self.stop_reason or "已取消原生预览",
            )
        except LeaseRetainedError:
            raise
        except Exception as exc:
            self.confirm_exit()
            journal["outcome"], journal["error"] = (
                "failed",
                f"原生预览准备失败：{type(exc).__name__}",
            )
        self.confirm_exit()
        self.save(journal)
        published = self.publish(journal, directory)
        if published:
            shutil.rmtree(directory)
            self.journal_path.unlink(missing_ok=True)

    def run(self):
        lock_path = self.state / (
            "preview-cpu-"
            + hashlib.sha256(self.resource.encode()).hexdigest()[:24]
            + ".lock"
        )
        with lock_path.open("a+") as self.lock:
            while not self.shutdown.is_set():
                try:
                    fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.shutdown.wait(2)
            if not self.shutdown.is_set():
                container_runtime = getattr(self, "container_runtime", None)
                if container_runtime is not None:
                    reconcile_orphan_containers(
                        container_runtime,
                        self.resource,
                        extra_labels={"yoao.trainer.preview": "true"},
                    )
            while not self.shutdown.is_set() and not self.publication_blocked:
                if self.journal_path.exists():
                    journal = json.loads(self.journal_path.read_text())
                    try:
                        if "assignment" not in journal:
                            journal["claim"]["recover_only"] = True
                            if not self.claim(journal):
                                self.journal_path.unlink(missing_ok=True)
                                continue
                        self.execute(
                            journal,
                            recovery=journal["claim"]["boot_id"] != self.boot
                            or journal["phase"] != "preparing",
                        )
                    except RetiredAttempt:
                        self.journal_path.unlink(missing_ok=True)
                    except Exception:
                        self.shutdown.wait(2)
                    continue
                journal = {
                    "claim": {
                        "purpose": "augmentation-preview",
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
                        self.shutdown.wait(2)
                except Exception:
                    self.shutdown.wait(2)
        self.client.close()
