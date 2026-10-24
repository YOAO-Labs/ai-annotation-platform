"""Single-resource supervisor with durable claims, heartbeats and upload recovery."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.metadata
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

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

from .files import atomic_json, digest_file, extract_dataset, immutable_copy


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


def runtime_identity():
    import torch
    from training_contract.catalog import supported_identities, instance_rle_capability
    from training_contract.preview import POLICY_DIGEST
    from training_contract.initialization import (
        POLICY_DIGEST as INITIALIZATION_POLICY_DIGEST,
        supported_policy_digests,
    )
    from training_contract.budget import POLICY_DIGEST as BUDGET_POLICY_DIGEST
    from training_contract.input_membership import POLICY_DIGEST as INPUT_POLICY_DIGEST

    gpu = os.environ["TRAINING_GPU_UUID"]
    devices = (
        subprocess.check_output(
            ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"], text=True
        )
        .strip()
        .splitlines()
    )
    if (
        devices != [gpu]
        or not torch.cuda.is_available()
        or torch.cuda.device_count() != 1
    ):
        raise RuntimeError("Executor must expose exactly the configured physical GPU")
    selected_image = re.fullmatch(
        r"[^@\s]+@(sha256:[0-9a-f]{64})",
        os.environ.get("TRAINING_IMAGE_REFERENCE", ""),
    )
    if selected_image is None:
        raise ValueError("An immutable executor image reference is required")
    image = selected_image[1]
    frameworks = {"ultralytics": importlib.metadata.version("ultralytics")}
    try:
        frameworks["libreyolo"] = importlib.metadata.version("libreyolo")
    except importlib.metadata.PackageNotFoundError:
        pass
    return {
        "executor_protocol": 2,
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
        "base_asset_sha256": available_asset_digests(
            os.environ.get("TRAINING_MODELS_DIR", "/models")
        ),
        "image_digest": image,
        "gpu_uuid": gpu,
        "frameworks": frameworks,
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "torchvision": importlib.metadata.version("torchvision"),
        "cuda": torch.version.cuda,
        "gpu_name": torch.cuda.get_device_name(0),
    }


def live_group(pgid: int):
    found = []
    for process in psutil.process_iter(["pid", "status"]):
        try:
            if (
                process.info["status"] != psutil.STATUS_ZOMBIE
                and os.getpgid(process.pid) == pgid
            ):
                found.append(process.pid)
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return found


def stop_process(process: subprocess.Popen):
    # The process group is created by this Popen and its inherited lock remains
    # held until every member exits. Never infer exit from heartbeat expiry.
    pgid = process.pid
    for sig, grace in ((signal.SIGINT, 10), (signal.SIGTERM, 10), (signal.SIGKILL, 10)):
        if not live_group(pgid):
            process.wait()
            return
        os.killpg(pgid, sig)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if not live_group(pgid):
                process.wait()
                return
            time.sleep(0.2)
    raise RuntimeError("Training process group did not stop; lease must remain held")


class Supervisor:
    def __init__(self):
        self.state = Path(os.environ.get("TRAINING_STATE_DIR", "/state"))
        self.state.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)
        self.models = Path(os.environ.get("TRAINING_MODELS_DIR", "/models"))
        self.boot = str(uuid.uuid4())
        self.runtime = runtime_identity()
        identity_path = self.state / "executor.json"
        if not identity_path.exists():
            atomic_json(identity_path, {"id": "executor-" + uuid.uuid4().hex})
        self.executor_id = json.loads(identity_path.read_text())["id"]
        self.resource = (
            os.environ.get("TRAINING_NODE_ID", "local") + "/" + self.runtime["gpu_uuid"]
        )
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
        # Use a separate transport: storage upload or preparation can block the
        # main thread without blocking cancellation and liveness reports.
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
        # Never send the executor credential to the object store.
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

    def inventory(self, journal, directory):
        artifacts = []
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
        # A definitive rejection proves this receipt was not accepted. The API
        # permits failed completion without complete/valid uploaded artifacts.
        # Preserve any frozen accounting while replacing the rejected receipt.
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
                    # Replays never repeat authorization or modify a receipt:
                    # the platform may have accepted it before a lost response.
                    self.save(journal)
                    self.api(self.endpoint(journal, "finish"), journal["finish_body"])
                    return True
                if "artifacts" not in journal:
                    journal["artifacts"] = self.inventory(journal, directory)
                    journal["phase"] = "uploading"
                    self.save(journal)
                # The child has exited. Flush every durable event before final
                # publication, including the last epoch inside a heartbeat gap.
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
                    response.raise_for_status()
                journal["finish_body"] = self.finish_body(journal)
                # Persist on every retry: a previous save may have failed after
                # finish_body was assigned in memory but before atomic durability.
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
                        # No acknowledgement means no lease release or cleanup.
                        # Stop retrying an invalid terminal request; repair the
                        # configuration before restarting this retained journal.
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
            # Keep the owner until its threads exit; do not start another optional
            # session or let its shutdown override execution/publication truth.
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

    def execute(self, journal, *, recovery=False):
        from .log_capture import LogCapture
        from .observability import ObservationSession

        assignment = journal["assignment"]
        directory = self.state / "attempts" / assignment["attempt_id"]
        directory.mkdir(parents=True, exist_ok=True)
        config_digest = assignment.get("config_digest", "")
        secrets = getattr(self, "redaction_secrets", ())
        observation = None
        self.reap_observation()
        capture = getattr(self, "active_capture", None)
        published = False
        process = getattr(self, "active_process", None)
        budget_owner = getattr(self, "active_budget", None)
        if budget_owner is not None and budget_owner.process is not None:
            process = budget_owner.process
        if process is not None:
            # Retain the actual Popen across a failed stop/journal write, reap it,
            # and prove every inherited-lock holder exited before publication.
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
                    previous = journal.get("process")
                    if (
                        previous
                        and journal["claim"]["boot_id"] == self.boot
                        and live_group(previous["pid"])
                    ):
                        raise RuntimeError(
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
                            validate_pin,
                            MAX_WEIGHTS_BYTES,
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

                        validate_budget(budget, assignment["attempt_id"], config_digest)
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

                    process = self.active_budget.launch(spawn) if budget else spawn()
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
                        if interrupted or self.stop_reason == "native_budget_exhausted"
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
                                        exception_type=receipt.get("exception_type"),
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
            except Exception as exc:
                owner = getattr(self, "active_budget", None)
                if owner is not None and owner.process is not None:
                    process = owner.process
                if process:
                    self.terminate_process(process)
                if (
                    recovery
                    and journal.get("process")
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
                # Logs are a required training artifact. Retry this packaging
                # gate from durable segments without restarting native training.
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
            # Only remove this attempt after the platform acknowledged durable
            # publication or fenced it out in favor of a later attempt.
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
            # Persist BEFORE the HTTP request. A lost claim response can be
            # recovered by the same request identity without an orphaned lease.
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
