"""Dual-mode plugin job runners for isolated containers and local process groups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Protocol, runtime_checkable

import psutil
from training_contract.observability import InputMismatchError
from yoao.container_runtime import (
    ContainerInspection,
    ContainerMount,
    ContainerRuntimePort,
    DetachedContainerSpec,
    LocalDockerRuntime,
)
from yoao.errors import SDKError
from yoao_inference_contract.plugin import validate_oci_digest_reference

_EXECUTABLE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_SHELL_EXECUTABLES = frozenset(
    {
        "sh",
        "bash",
        "dash",
        "ash",
        "zsh",
        "ksh",
        "fish",
        "csh",
        "tcsh",
        "cmd",
        "cmd.exe",
        "powershell",
        "pwsh",
        "env",
        "busybox",
        "toybox",
        "xargs",
        "sudo",
        "su",
        "doas",
        "nohup",
        "timeout",
        "setsid",
    }
)
_SAFE_PYTHON_FLAGS = frozenset({"-u", "-B", "-I", "-s", "-S", "-E", "-O", "-OO"})
_VALUE_PYTHON_FLAGS = frozenset({"-W", "-X"})
_FORBIDDEN_NODE_FLAGS = frozenset({"-e", "--eval", "-p", "--print"})
_SAFE_LOCAL_ENV_KEYS = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "CUDA_VISIBLE_DEVICES",
)
_CPU_PREVIEW_ENV = (
    ("NVIDIA_VISIBLE_DEVICES", "void"),
    ("CUDA_VISIBLE_DEVICES", ""),
    ("OMP_NUM_THREADS", "1"),
    ("OPENBLAS_NUM_THREADS", "1"),
    ("MKL_NUM_THREADS", "1"),
    ("POLARS_MAX_THREADS", "1"),
    ("NUMEXPR_MAX_THREADS", "1"),
)

DEFAULT_MEMORY_MB = 4096
MIN_MEMORY_MB = 16
MAX_MEMORY_MB = 65536
DEFAULT_CPUS = 2.0
DEFAULT_PIDS_LIMIT = 256
MIN_PIDS_LIMIT = 16
MAX_PIDS_LIMIT = 4096
DEFAULT_SHM_SIZE_MB = 512
MIN_SHM_SIZE_MB = 16
MAX_SHM_SIZE_MB = 16384

PREVIEW_MEMORY_MB = 16384
PREVIEW_CPUS = 1.0
PREVIEW_PIDS_LIMIT = 128
PREVIEW_SHM_SIZE_MB = 256


class LeaseRetainedError(RuntimeError):
    """Raised when a managed job fails to confirm exit so its lease must remain held."""


@runtime_checkable
class JobHandle(Protocol):
    pid: int | str
    stdout: Any

    @property
    def returncode(self) -> int | None: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def is_alive(self) -> bool: ...

    def stop(self) -> None: ...


def live_group(pgid: int) -> list[int]:
    found: list[int] = []
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


def stop_process_group(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: tuple[float, float, float] = (10.0, 10.0, 10.0),
) -> None:
    pgid = process.pid
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)
    for sig, grace in zip(signals, grace_seconds, strict=True):
        if not live_group(pgid):
            process.wait()
            return
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            if not live_group(pgid):
                process.wait()
                return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if not live_group(pgid):
                process.wait()
                return
            time.sleep(0.05)
    if not live_group(pgid):
        process.wait()
        return
    raise LeaseRetainedError(
        "Training process group did not stop; lease must remain held"
    )


def validate_plugin_entrypoint(
    entrypoint: Sequence[str],
    *,
    allow_absolute_head: bool = True,
) -> tuple[str, ...]:
    if (
        isinstance(entrypoint, (str, bytes))
        or not isinstance(entrypoint, Sequence)
        or not 1 <= len(entrypoint) <= 64
    ):
        raise InputMismatchError("Plugin entrypoint must contain 1..64 arguments")
    items: list[str] = []
    for arg in entrypoint:
        if (
            not isinstance(arg, str)
            or not arg
            or not arg.strip()
            or any(ch in arg for ch in "\x00\r\n")
        ):
            raise InputMismatchError(
                "Plugin entrypoint arguments must be non-empty single-line strings"
            )
        items.append(arg)
    head = items[0]
    if os.path.isabs(head):
        if not allow_absolute_head:
            raise InputMismatchError(
                "Plugin entrypoint executable must not be an absolute path"
            )
        head_path = Path(head)
        if ".." in head_path.parts or not _EXECUTABLE_NAME.fullmatch(head_path.name):
            raise InputMismatchError("Plugin entrypoint executable path is invalid")
        head_name = head_path.name.lower()
    else:
        if _EXECUTABLE_NAME.fullmatch(head) is None:
            raise InputMismatchError("Plugin entrypoint executable is invalid")
        head_name = head.lower()
    if head_name in _SHELL_EXECUTABLES:
        raise InputMismatchError(
            "Plugin entrypoint must not invoke a shell or wrapper interpreter"
        )
    if head_name.startswith("python") or head_name.startswith("pypy"):
        args = items[1:]
        idx = 0
        while idx < len(args):
            arg = args[idx]
            if arg == "--" or not arg.startswith("-"):
                break
            if arg == "-m":
                if idx + 1 >= len(args) or args[idx + 1].startswith("-"):
                    raise InputMismatchError(
                        "Plugin entrypoint python -m requires a module name"
                    )
                break
            if arg in _VALUE_PYTHON_FLAGS:
                if idx + 1 >= len(args):
                    raise InputMismatchError(
                        f"Plugin entrypoint option {arg} requires a value"
                    )
                idx += 2
                continue
            if arg in _SAFE_PYTHON_FLAGS:
                idx += 1
                continue
            raise InputMismatchError(
                "Plugin entrypoint must not invoke python -c or unsupported interpreter options"
            )
    elif head_name in {"node", "nodejs"}:
        for arg in items[1:]:
            if arg == "--" or not arg.startswith("-"):
                break
            if arg.split("=", 1)[0] in _FORBIDDEN_NODE_FLAGS:
                raise InputMismatchError(
                    "Plugin entrypoint must not invoke inline node evaluation"
                )
    return tuple(items)


def _parse_local_plugins_payload(raw_text: str) -> dict[str, dict[str, Any]]:
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise ValueError("TRAINING_LOCAL_PLUGINS payload must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("TRAINING_LOCAL_PLUGINS payload must be a JSON object")
    allowlist: dict[str, dict[str, Any]] = {}
    for plugin_id, entry in parsed.items():
        if (
            not isinstance(plugin_id, str)
            or not plugin_id.strip()
            or plugin_id != plugin_id.strip()
            or len(plugin_id) > 128
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in plugin_id)
        ):
            raise ValueError("Local plugin identifier is invalid")
        if not isinstance(entry, dict) or "entrypoint" not in entry:
            raise ValueError(f"Local plugin {plugin_id!r} must declare an entrypoint")
        validated = validate_plugin_entrypoint(
            entry["entrypoint"], allow_absolute_head=True
        )
        allowlist[plugin_id] = {**entry, "entrypoint": list(validated)}
    return allowlist


def load_local_plugins_allowlist(
    env: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    source = os.environ if env is None else env
    allowlist: dict[str, dict[str, Any]] = {}
    file_path = source.get("TRAINING_LOCAL_PLUGINS_FILE", "").strip()
    if file_path:
        allowlist.update(_parse_local_plugins_payload(Path(file_path).read_text()))
    inline = source.get("TRAINING_LOCAL_PLUGINS_JSON", "").strip()
    if inline:
        allowlist.update(_parse_local_plugins_payload(inline))
    return allowlist


def resolve_local_plugin_entrypoint(
    plugin_id: str,
    allowlist: Mapping[str, Mapping[str, Any]],
    *,
    requested_entrypoint: Sequence[str] | None = None,
) -> tuple[str, ...]:
    entry = allowlist.get(plugin_id)
    if entry is None:
        raise InputMismatchError(
            f"Local plugin {plugin_id!r} is not present in the host allowlist"
        )
    allowed = validate_plugin_entrypoint(entry["entrypoint"], allow_absolute_head=True)
    if requested_entrypoint is not None:
        requested = validate_plugin_entrypoint(
            requested_entrypoint, allow_absolute_head=True
        )
        if requested != allowed:
            raise InputMismatchError(
                f"Local plugin {plugin_id!r} entrypoint differs from the host allowlist"
            )
    return allowed


def resolve_container_runtime(
    env: Mapping[str, str] | None = None,
) -> ContainerRuntimePort | None:
    source = os.environ if env is None else env
    raw = source.get("TRAINING_CONTAINER_RUNTIME")
    if raw is None or not raw.strip():
        return (
            LocalDockerRuntime(executable="docker") if shutil.which("docker") else None
        )
    cleaned = raw.strip()
    if cleaned.lower() == "none":
        return None
    return LocalDockerRuntime(executable=cleaned)


def scrubbed_local_env(
    *,
    requires_gpu: bool,
    gpu_uuid: str | None = None,
    extra: Mapping[str, str] | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if base_env is None else base_env
    env = {key: source[key] for key in _SAFE_LOCAL_ENV_KEYS if key in source}
    if requires_gpu:
        if gpu_uuid:
            env["CUDA_VISIBLE_DEVICES"] = gpu_uuid
    else:
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["NVIDIA_VISIBLE_DEVICES"] = "void"
    if extra:
        env.update(extra)
    return env


def trainer_container_name(resource_id: str, attempt_id: str) -> str:
    digest = hashlib.sha256(f"{resource_id}:{attempt_id}".encode()).hexdigest()[:16]
    return f"yoao-trainer-{digest}"


def trainer_container_labels(
    *,
    resource_id: str,
    attempt_id: str,
    run_id: str,
    plugin_id: str,
) -> dict[str, str]:
    return {
        "yoao.trainer.managed": "true",
        "yoao.trainer.gpu_resource_id": resource_id,
        "yoao.trainer.attempt_id": attempt_id,
        "yoao.trainer.run_id": run_id,
        "yoao.trainer.plugin_id": plugin_id,
    }


def preview_container_name(resource_id: str, preview_id: str) -> str:
    digest = hashlib.sha256(f"{resource_id}:{preview_id}".encode()).hexdigest()[:16]
    return f"yoao-trainer-preview-{digest}"


def preview_container_labels(
    *,
    resource_id: str,
    preview_id: str,
    plugin_id: str,
) -> dict[str, str]:
    return {
        "yoao.trainer.managed": "true",
        "yoao.trainer.preview": "true",
        "yoao.trainer.gpu_resource_id": resource_id,
        "yoao.trainer.preview_id": preview_id,
        "yoao.trainer.plugin_id": plugin_id,
    }


def _bounded_memory_mb(resource: Mapping[str, Any] | None) -> int:
    raw = (
        DEFAULT_MEMORY_MB
        if not resource
        else resource.get("memory_mb", DEFAULT_MEMORY_MB)
    )
    if type(raw) is not int or not MIN_MEMORY_MB <= raw <= MAX_MEMORY_MB:
        raise InputMismatchError(
            "Plugin container memory_mb must be an integer in 16..65536"
        )
    return raw


def _bounded_cpus(resource: Mapping[str, Any] | None) -> float:
    raw = DEFAULT_CPUS if not resource else resource.get("cpus", DEFAULT_CPUS)
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or not 0 < float(raw) <= 256
    ):
        raise InputMismatchError("Plugin container cpus must be finite in (0, 256]")
    return float(raw)


def _bounded_positive_int(
    resource: Mapping[str, Any] | None,
    key: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int,
) -> int:
    raw = default if not resource else resource.get(key, default)
    if type(raw) is not int or not (minimum <= raw <= maximum):
        raise InputMismatchError(
            f"Plugin container {key} must be an integer in {minimum}..{maximum}"
        )
    return raw


def _current_uid_gid() -> str:
    return f"{os.getuid()}:{os.getgid()}"


def build_train_container_spec(
    *,
    resource_id: str,
    attempt_id: str,
    run_id: str,
    plugin_id: str,
    image: str,
    entrypoint: Sequence[str],
    input_dir: Path,
    output_dir: Path,
    models_dir: Path | None = None,
    resource: Mapping[str, Any] | None = None,
    requires_gpu: bool = True,
    slot_gpu_uuid: str | None = None,
) -> DetachedContainerSpec:
    try:
        validate_oci_digest_reference(image)
    except ValueError as exc:
        raise InputMismatchError(
            "Plugin container image must be pinned to a sha256 digest"
        ) from exc
    validated_entrypoint = validate_plugin_entrypoint(
        entrypoint, allow_absolute_head=True
    )
    if requires_gpu and not slot_gpu_uuid:
        raise InputMismatchError(
            "Plugin requires a GPU slot but executor has no physical GPU"
        )
    mounts = [
        ContainerMount(input_dir.resolve(), "/work/input", readonly=True),
        ContainerMount(output_dir.resolve(), "/work/output", readonly=False),
    ]
    if models_dir is not None and models_dir.is_dir():
        mounts.append(ContainerMount(models_dir.resolve(), "/models", readonly=True))
    env = (
        ()
        if requires_gpu
        else (("NVIDIA_VISIBLE_DEVICES", "void"), ("CUDA_VISIBLE_DEVICES", ""))
    )
    labels = trainer_container_labels(
        resource_id=resource_id,
        attempt_id=attempt_id,
        run_id=run_id,
        plugin_id=plugin_id,
    )
    spec = DetachedContainerSpec(
        name=trainer_container_name(resource_id, attempt_id),
        image=image,
        network="none",
        memory_mb=_bounded_memory_mb(resource),
        cpus=_bounded_cpus(resource),
        user=_current_uid_gid(),
        labels=tuple(sorted(labels.items())),
        gpu_uuid=slot_gpu_uuid if requires_gpu else None,
        env=env,
        mounts=tuple(mounts),
        command=(
            *validated_entrypoint,
            "train",
            "--spec",
            "/work/input/spec.json",
            "--output",
            "/work/output",
        ),
        pids_limit=_bounded_positive_int(
            resource,
            "pids_limit",
            DEFAULT_PIDS_LIMIT,
            minimum=MIN_PIDS_LIMIT,
            maximum=MAX_PIDS_LIMIT,
        ),
        shm_size_mb=_bounded_positive_int(
            resource,
            "shm_size_mb",
            DEFAULT_SHM_SIZE_MB,
            minimum=MIN_SHM_SIZE_MB,
            maximum=MAX_SHM_SIZE_MB,
        ),
    )
    spec.validate()
    return spec


def build_preview_container_spec(
    *,
    resource_id: str,
    preview_id: str,
    plugin_id: str,
    image: str,
    entrypoint: Sequence[str],
    input_dir: Path,
    output_dir: Path,
    models_dir: Path | None = None,
) -> DetachedContainerSpec:
    try:
        validate_oci_digest_reference(image)
    except ValueError as exc:
        raise InputMismatchError(
            "Preview plugin image must be pinned to a sha256 digest"
        ) from exc
    validated_entrypoint = validate_plugin_entrypoint(
        entrypoint, allow_absolute_head=True
    )
    mounts = [
        ContainerMount(input_dir.resolve(), "/work/input", readonly=True),
        ContainerMount(output_dir.resolve(), "/work/output", readonly=False),
    ]
    if models_dir is not None and models_dir.is_dir():
        mounts.append(ContainerMount(models_dir.resolve(), "/models", readonly=True))
    labels = preview_container_labels(
        resource_id=resource_id,
        preview_id=preview_id,
        plugin_id=plugin_id,
    )
    spec = DetachedContainerSpec(
        name=preview_container_name(resource_id, preview_id),
        image=image,
        network="none",
        memory_mb=PREVIEW_MEMORY_MB,
        cpus=PREVIEW_CPUS,
        user=_current_uid_gid(),
        labels=tuple(sorted(labels.items())),
        gpu_uuid=None,
        env=_CPU_PREVIEW_ENV,
        mounts=tuple(mounts),
        command=(
            *validated_entrypoint,
            "preview",
            "--spec",
            "/work/input/preview-spec.json",
            "--output",
            "/work/output",
        ),
        pids_limit=PREVIEW_PIDS_LIMIT,
        shm_size_mb=PREVIEW_SHM_SIZE_MB,
    )
    spec.validate()
    return spec


def _container_state_active(info: ContainerInspection | None) -> bool:
    return bool(
        info is not None
        and (
            info.state.running
            or info.state.restarting
            or info.state.paused
            or info.state.dead
        )
    )


def _stop_and_verify_container(
    runtime: ContainerRuntimePort,
    container_id: str,
    *,
    term_grace_seconds: float = 10.0,
    kill_grace_seconds: float = 10.0,
    poll_interval_seconds: float = 0.2,
) -> ContainerInspection | None:
    try:
        info = runtime.inspect_container(container_id=container_id)
        if not _container_state_active(info):
            return info
        for sig, grace in (("TERM", term_grace_seconds), ("KILL", kill_grace_seconds)):
            runtime.kill_container(container_id, signal=sig)
            deadline = time.monotonic() + grace
            while True:
                info = runtime.inspect_container(container_id=container_id)
                if not _container_state_active(info):
                    return info
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(poll_interval_seconds, remaining))
        info = runtime.inspect_container(container_id=container_id)
    except SDKError as exc:
        raise LeaseRetainedError(
            "Unable to verify managed trainer container exit; lease must remain held"
        ) from exc
    if _container_state_active(info):
        raise LeaseRetainedError(
            "Managed trainer container did not stop; lease must remain held"
        )
    return info


def reconcile_orphan_containers(
    runtime: ContainerRuntimePort,
    resource_id: str,
    *,
    extra_labels: Mapping[str, str] | None = None,
    term_grace_seconds: float = 10.0,
    kill_grace_seconds: float = 10.0,
    poll_interval_seconds: float = 0.2,
) -> list[str]:
    labels = {
        "yoao.trainer.managed": "true",
        "yoao.trainer.gpu_resource_id": resource_id,
    }
    if extra_labels:
        labels.update(extra_labels)
    try:
        containers = runtime.list_containers(labels=labels)
    except SDKError as exc:
        raise LeaseRetainedError(
            "Unable to enumerate managed trainer containers; lease must remain held"
        ) from exc
    reaped: list[str] = []
    for item in containers:
        _stop_and_verify_container(
            runtime,
            item.container_id,
            term_grace_seconds=term_grace_seconds,
            kill_grace_seconds=kill_grace_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        try:
            runtime.remove_container(item.container_id, force=True)
        except SDKError as exc:
            raise LeaseRetainedError(
                "Unable to remove stopped trainer container; lease must remain held"
            ) from exc
        reaped.append(item.container_id)
    return reaped


def reconcile_container_by_id_or_name(
    runtime: ContainerRuntimePort,
    *,
    container_id: str | None = None,
    name: str | None = None,
    term_grace_seconds: float = 10.0,
    kill_grace_seconds: float = 10.0,
    poll_interval_seconds: float = 0.2,
) -> bool:
    info: ContainerInspection | None = None
    try:
        if container_id:
            info = runtime.inspect_container(container_id=container_id)
        if info is None and name:
            info = runtime.inspect_container(name=name)
    except SDKError as exc:
        raise LeaseRetainedError(
            "Unable to inspect managed trainer container; lease must remain held"
        ) from exc
    if info is None:
        return False
    _stop_and_verify_container(
        runtime,
        info.container_id,
        term_grace_seconds=term_grace_seconds,
        kill_grace_seconds=kill_grace_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    try:
        runtime.remove_container(info.container_id, force=True)
    except SDKError as exc:
        raise LeaseRetainedError(
            "Unable to remove reconciled trainer container; lease must remain held"
        ) from exc
    return True


class LocalProcessJobHandle:
    def __init__(
        self,
        process: subprocess.Popen[Any],
        *,
        grace_seconds: tuple[float, float, float] = (10.0, 10.0, 10.0),
    ) -> None:
        self._process = process
        self.pid: int = process.pid
        self.stdout = process.stdout
        self._grace_seconds = grace_seconds

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def poll(self) -> int | None:
        return self._process.poll()

    def is_alive(self) -> bool:
        return bool(live_group(self.pid))

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def stop(self) -> None:
        stop_process_group(self._process, grace_seconds=self._grace_seconds)


def launch_local_process(
    *,
    plugin_id: str,
    allowlist: Mapping[str, Mapping[str, Any]],
    args: Sequence[str],
    requested_entrypoint: Sequence[str] | None = None,
    requires_gpu: bool = False,
    gpu_uuid: str | None = None,
    lock_fd: int | None = None,
    stdout: Any = subprocess.PIPE,
    env_overrides: Mapping[str, str] | None = None,
    grace_seconds: tuple[float, float, float] = (10.0, 10.0, 10.0),
) -> LocalProcessJobHandle:
    entrypoint = resolve_local_plugin_entrypoint(
        plugin_id, allowlist, requested_entrypoint=requested_entrypoint
    )
    if requires_gpu and not gpu_uuid:
        raise InputMismatchError(
            "Plugin requires a GPU slot but executor has no physical GPU"
        )
    env = scrubbed_local_env(
        requires_gpu=requires_gpu, gpu_uuid=gpu_uuid, extra=env_overrides
    )
    pass_fds = (lock_fd,) if lock_fd is not None else ()
    process = subprocess.Popen(
        [*entrypoint, *args],
        stdout=stdout,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        pass_fds=pass_fds,
        env=env,
    )
    return LocalProcessJobHandle(process, grace_seconds=grace_seconds)


class ContainerJobHandle:
    def __init__(
        self,
        runtime: ContainerRuntimePort,
        container_id: str,
        *,
        name: str,
        log_proc: subprocess.Popen[bytes] | None = None,
        term_grace_seconds: float = 10.0,
        kill_grace_seconds: float = 10.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self.runtime = runtime
        self.container_id = container_id
        self.pid: str = container_id
        self.name = name
        self._log_proc = log_proc
        self.stdout = log_proc.stdout if log_proc is not None else None
        self._term_grace_seconds = term_grace_seconds
        self._kill_grace_seconds = kill_grace_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._returncode: int | None = None
        self._removed = False
        self._lock = threading.RLock()

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def _close_log_stream(self) -> None:
        log_proc = self._log_proc
        if log_proc is None:
            return
        try:
            if log_proc.poll() is None:
                try:
                    log_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log_proc.terminate()
                    try:
                        log_proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        log_proc.kill()
                        log_proc.wait(timeout=2)
        except OSError:
            pass

    def _remove_once(self) -> None:
        if not self._removed:
            self.runtime.remove_container(self.container_id, force=True)
            self._removed = True

    def is_alive(self) -> bool:
        info = self.runtime.inspect_container(container_id=self.container_id)
        if _container_state_active(info):
            return True
        if info is not None and self._returncode is None:
            self._returncode = info.state.exit_code
        return False

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        info = self.runtime.inspect_container(container_id=self.container_id)
        if info is None:
            return self._returncode
        if _container_state_active(info):
            return None
        self._returncode = info.state.exit_code
        return self._returncode

    def stop(self) -> None:
        with self._lock:
            final = _stop_and_verify_container(
                self.runtime,
                self.container_id,
                term_grace_seconds=self._term_grace_seconds,
                kill_grace_seconds=self._kill_grace_seconds,
                poll_interval_seconds=self._poll_interval_seconds,
            )
            if final is not None:
                self._returncode = final.state.exit_code
            elif self._returncode is None:
                self._returncode = -9
            self._close_log_stream()
            if final is not None:
                self._remove_once()
            else:
                self._removed = True

    def wait(self, timeout: float | None = None) -> int:
        with self._lock:
            cid = self.container_id
            already_removed = self._removed
            cached = self._returncode
        if already_removed and cached is not None:
            return int(cached)
        try:
            info = self.runtime.inspect_container(container_id=cid)
            waited_code: int | None = None
            if _container_state_active(info):
                waited_code = self.runtime.wait_container(cid, timeout=timeout)
            after = self.runtime.inspect_container(container_id=cid)
        except SDKError as exc:
            raise LeaseRetainedError(
                "Unable to confirm trainer container exit; lease must remain held"
            ) from exc
        if _container_state_active(after):
            raise LeaseRetainedError(
                "Managed trainer container is still running; lease must remain held"
            )
        with self._lock:
            if after is not None:
                self._returncode = after.state.exit_code
            elif waited_code is not None:
                self._returncode = waited_code
            elif info is not None:
                self._returncode = info.state.exit_code
            elif self._returncode is None:
                self._returncode = -1
            self._close_log_stream()
            if after is not None:
                try:
                    self._remove_once()
                except SDKError as exc:
                    raise LeaseRetainedError(
                        "Unable to remove exited trainer container; lease must remain held"
                    ) from exc
            else:
                self._removed = True
            return int(self._returncode)


def launch_container_job(
    runtime: ContainerRuntimePort,
    spec: DetachedContainerSpec,
    *,
    open_logs: bool = True,
    term_grace_seconds: float = 10.0,
    kill_grace_seconds: float = 10.0,
    poll_interval_seconds: float = 0.2,
) -> ContainerJobHandle:
    container_id: str | None = None
    try:
        container_id = runtime.run_detached(spec, start=True)
        log_proc: subprocess.Popen[bytes] | None = None
        if open_logs:
            log_proc = runtime.open_log_stream(container_id)
    except Exception:
        reconcile_container_by_id_or_name(
            runtime,
            container_id=container_id,
            name=spec.name,
            term_grace_seconds=term_grace_seconds,
            kill_grace_seconds=kill_grace_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        raise
    return ContainerJobHandle(
        runtime,
        container_id,
        name=spec.name,
        log_proc=log_proc,
        term_grace_seconds=term_grace_seconds,
        kill_grace_seconds=kill_grace_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
