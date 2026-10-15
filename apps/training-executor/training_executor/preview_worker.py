"""One independent CPU preview slot inside the existing executor process."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

import httpx
import psutil
from training_contract.preview import LIMITS

from .files import atomic_json, digest_file
from .worker import RetiredAttempt, StopTraining, Supervisor, live_group, stop_process


class PreviewSupervisor:
    def __init__(self, training):
        self.state = training.state
        self.models = training.models
        self.runtime = training.runtime
        self.executor_id, self.boot = training.executor_id, training.boot
        self.resource = (
            os.environ.get("TRAINING_NODE_ID", "local") + "/augmentation-preview/cpu-0"
        )
        self.shutdown = training.shutdown
        # Never share training's cancellation, heartbeat, journal or flock.
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
        # Retain the Popen handle even when persisting its PID fails. Holding a
        # flock across a same-boot retry does not prove the child has exited.
        self.process = None
        self.process_exited = True

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

    def publish(self, journal, directory):
        while not self.shutdown.is_set():
            try:
                if "finish_body" not in journal:
                    if (
                        self.stop_training.is_set()
                        and journal["outcome"] == "succeeded"
                    ):
                        journal["outcome"], journal["error"] = (
                            "cancelled",
                            self.stop_reason,
                        )
                    if journal["outcome"] == "succeeded":
                        if "artifacts" not in journal:
                            journal["artifacts"] = self.inventory(journal, directory)
                            self.save(journal)
                        urls = self.api(
                            self.endpoint(journal, "uploads"),
                            {**self.fence(journal), "artifacts": journal["artifacts"]},
                        )
                        for artifact in journal["artifacts"]:
                            if self.stop_training.is_set() or self.shutdown.is_set():
                                raise StopTraining()
                            path = directory / artifact["name"]
                            if digest_file(path) != artifact["sha256"]:
                                raise ValueError("Local preview output changed")
                            target = urls[artifact["name"]]
                            with path.open("rb") as source:
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
                            response.raise_for_status()
                    journal["finish_body"] = {
                        **self.fence(journal),
                        "status": journal["outcome"],
                        "process_exited": True,
                        "error_message": journal.get("error"),
                    }
                    # Exact receipt survives a lost finish response.
                    self.save(journal)
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
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {403, 422} and "finish_body" in journal:
                    # These explicit rejections occur before accepting a terminal
                    # receipt. A stopped, invalid result can acknowledge failure.
                    journal.pop("finish_body")
                    journal["outcome"], journal["error"] = (
                        "failed",
                        "原生预览产物或当前权限未通过平台校验",
                    )
                    self.save(journal)
                elif exc.response.status_code in {403, 409, 422}:
                    journal["outcome"], journal["error"] = (
                        "cancelled",
                        self.stop_reason or "原生预览上传授权已失效",
                    )
                    self.save(journal)
                self.shutdown.wait(2)
            except ValueError:
                if "finish_body" not in journal:
                    journal["outcome"], journal["error"] = (
                        "failed",
                        "原生预览产物缺失或超过上限",
                    )
                    self.save(journal)
                self.shutdown.wait(2)
            except Exception:
                # Keep the CPU lease and receipt through transport outages.
                self.shutdown.wait(2)
        return False

    def confirm_exit(self):
        if self.process is not None:
            if live_group(self.process.pid):
                stop_process(self.process)
            if live_group(self.process.pid):
                raise RuntimeError("CPU preview group is still alive; retaining lease")
            self.process.wait()
            self.process = None
            self.process_exited = True
        if not self.process_exited:
            raise RuntimeError("CPU preview exit is unconfirmed; retaining lease")

    def execute(self, journal, *, recovery=False):
        self.confirm_exit()
        if (
            recovery
            and journal["claim"]["boot_id"] == self.boot
            and journal.get("process")
            and live_group(journal["process"]["pid"])
        ):
            raise RuntimeError("Previous CPU preview group is alive; retaining lease")
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

    def execute_owned(self, journal, directory, *, recovery):
        assignment = journal["assignment"]
        process = None
        try:
            if recovery:
                # A new boot reacquires the inherited flock. Same-boot retries
                # must additionally pass confirm_exit before entering here.
                journal.setdefault("outcome", "failed")
                journal.setdefault("error", "执行器重启；已确认旧 CPU 预览进程退出")
            else:
                if assignment["stop"]:
                    raise StopTraining()
                spec = assignment["spec"]
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
                        # Popen reaps a child whose exec fails before returning.
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
            while not self.shutdown.is_set():
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
