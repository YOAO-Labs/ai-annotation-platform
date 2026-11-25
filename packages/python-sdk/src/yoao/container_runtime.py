"""Backend-neutral container runtime port and local Docker CLI adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Protocol, runtime_checkable

from yoao.errors import SDKError
from yoao_inference_contract.plugin import validate_oci_digest_reference

_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_GPU_UUID = re.compile(
    r"^GPU-[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
)
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_USER = re.compile(r"^[a-zA-Z0-9_.-]+(?::[a-zA-Z0-9_.-]+)?$")


_FORBIDDEN_SOCKET_NAMES = ("docker.sock", "podman.sock", "containerd.sock")


class _ContainerNotFoundError(SDKError):
    """Raised internally when the container runtime reports a missing container."""


def _validate_safe_host_path(path: Path, label: str) -> None:
    text = str(path)
    lowered = text.lower()
    if (
        not path.is_absolute()
        or "," in text
        or '"' in text
        or any(socket_name in lowered for socket_name in _FORBIDDEN_SOCKET_NAMES)
        or any(ch in text for ch in "\x00\r\n")
    ):
        raise SDKError(f"{label} must have a safe absolute path")


def _validate_safe_target_path(target: str, label: str) -> None:
    lowered = target.lower() if isinstance(target, str) else ""
    if (
        not isinstance(target, str)
        or not target.startswith("/")
        or ":" in target
        or "," in target
        or '"' in target
        or any(socket_name in lowered for socket_name in _FORBIDDEN_SOCKET_NAMES)
        or any(ch in target for ch in "\x00\r\n")
    ):
        raise SDKError(f"{label} must be a safe container mount target")


def _validate_resource_limits(
    *,
    memory_mb: int,
    cpus: float,
    pids_limit: int | None = None,
    shm_size_mb: int | None = None,
) -> None:
    if (
        type(memory_mb) is not int
        or memory_mb <= 0
        or isinstance(cpus, bool)
        or not isinstance(cpus, (int, float))
        or not math.isfinite(cpus)
        or not 0 < cpus <= 256
    ):
        raise SDKError("Invalid container resource limits")
    if pids_limit is not None and (type(pids_limit) is not int or pids_limit <= 0):
        raise SDKError("Invalid container pids limit")
    if shm_size_mb is not None and (type(shm_size_mb) is not int or shm_size_mb <= 0):
        raise SDKError("Invalid container shared memory limit")


def _validate_env_pairs(pairs: Sequence[tuple[str, str]]) -> None:
    for key, value in pairs:
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or not isinstance(value, str)
            or any(ch in key or ch in value for ch in "\x00\r\n")
        ):
            raise SDKError("Invalid container environment entry")


def _validate_command_vector(command: Sequence[str]) -> None:
    if not command or any(
        not isinstance(arg, str) or not arg or any(ch in arg for ch in "\x00\r\n")
        for arg in command
    ):
        raise SDKError("Container command must be a nonempty argument vector")


@dataclass(frozen=True)
class ContainerMount:
    """Explicit host-to-container bind mount."""

    source: Path
    target: str
    readonly: bool = False

    def validate(self) -> None:
        _validate_safe_host_path(self.source, "Container mount source")
        _validate_safe_target_path(self.target, "Container mount target")

    def to_cli_arg(self) -> str:
        suffix = ",readonly" if self.readonly else ""
        return f"type=bind,src={self.source},dst={self.target}{suffix}"


@dataclass(frozen=True)
class ContainerPortBinding:
    """Explicit single TCP port publish rule."""

    host_port: int
    container_port: int

    def validate(self) -> None:
        if not (
            type(self.host_port) is int
            and type(self.container_port) is int
            and 1 <= self.host_port <= 65535
            and 1 <= self.container_port <= 65535
        ):
            raise SDKError("Invalid container port binding")


@dataclass(frozen=True)
class OneShotContainerSpec:
    """Isolated ephemeral container execution specification."""

    image: str
    command: tuple[str, ...]
    network: str = "none"
    memory_mb: int = 512
    cpus: float = 1.0
    user: str | None = None
    gpu_uuid: str | None = None
    env_file: Path | None = None
    env: tuple[tuple[str, str], ...] = ()
    mounts: tuple[ContainerMount, ...] = ()
    pids_limit: int | None = None
    shm_size_mb: int | None = None

    def validate(self) -> None:
        try:
            validate_oci_digest_reference(self.image)
        except ValueError as exc:
            raise SDKError("Container image must be pinned to a sha256 digest") from exc
        if not isinstance(self.command, tuple):
            raise SDKError("Container command must be a tuple")
        _validate_command_vector(self.command)
        if not _NAME.fullmatch(self.network) or self.network == "host":
            raise SDKError("Host networking is not permitted")
        _validate_resource_limits(
            memory_mb=self.memory_mb,
            cpus=self.cpus,
            pids_limit=self.pids_limit,
            shm_size_mb=self.shm_size_mb,
        )
        if self.user is not None and not _USER.fullmatch(self.user):
            raise SDKError("Invalid container user identity")
        if self.gpu_uuid is not None and not _GPU_UUID.fullmatch(self.gpu_uuid):
            raise SDKError("One physical NVIDIA GPU UUID is required")
        if self.env_file is not None:
            _validate_safe_host_path(self.env_file, "Container env file")
        _validate_env_pairs(self.env)
        for mount in self.mounts:
            mount.validate()


@dataclass(frozen=True)
class DetachedContainerSpec:
    """Managed long-running or staged container creation specification."""

    name: str
    image: str
    network: str
    memory_mb: int
    cpus: float
    user: str
    labels: tuple[tuple[str, str], ...] = ()
    gpu_uuid: str | None = None
    ports: tuple[ContainerPortBinding, ...] = ()
    env_file: Path | None = None
    env: tuple[tuple[str, str], ...] = ()
    mounts: tuple[ContainerMount, ...] = ()
    command: tuple[str, ...] | None = None
    pids_limit: int | None = None
    shm_size_mb: int | None = None

    def validate(self) -> None:
        if not _NAME.fullmatch(self.name) or not _NAME.fullmatch(self.network):
            raise SDKError("Invalid container name or network")
        if self.network == "host":
            raise SDKError("Host networking is not permitted")
        try:
            validate_oci_digest_reference(self.image)
        except ValueError as exc:
            raise SDKError("Container image must be pinned to a sha256 digest") from exc
        _validate_resource_limits(
            memory_mb=self.memory_mb,
            cpus=self.cpus,
            pids_limit=self.pids_limit,
            shm_size_mb=self.shm_size_mb,
        )
        if not _USER.fullmatch(self.user):
            raise SDKError("Invalid container user identity")
        if self.gpu_uuid is not None and not _GPU_UUID.fullmatch(self.gpu_uuid):
            raise SDKError("One physical NVIDIA GPU UUID is required")
        for key, value in self.labels:
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or not isinstance(value, str)
                or any(ch in key or ch in value for ch in "\x00\r\n")
            ):
                raise SDKError("Invalid container label")
        for port in self.ports:
            port.validate()
        if self.env_file is not None:
            _validate_safe_host_path(self.env_file, "Container env file")
        _validate_env_pairs(self.env)
        for mount in self.mounts:
            mount.validate()
        if self.command is not None:
            if not isinstance(self.command, tuple):
                raise SDKError("Container command must be a tuple")
            _validate_command_vector(self.command)


@dataclass(frozen=True)
class ContainerImageInfo:
    """Verified local OCI image inspection summary."""

    image_id: str
    repo_digests: tuple[str, ...]
    labels: Mapping[str, str]


@dataclass(frozen=True)
class ContainerStateInfo:
    """Normalized runtime state for an inspected container."""

    running: bool
    restarting: bool
    paused: bool
    dead: bool
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class ContainerInspection:
    """Normalized ownership, configuration, and state of one container."""

    container_id: str
    name: str
    image_id: str
    image_ref: str
    labels: Mapping[str, str]
    host_config: Mapping[str, Any]
    mounts: tuple[Mapping[str, Any], ...]
    user: str
    entrypoint: tuple[str, ...] | None
    cmd: tuple[str, ...] | None
    state: ContainerStateInfo


@runtime_checkable
class ContainerRuntimePort(Protocol):
    """Narrow backend-neutral container runtime contract for CLI and supervisors."""

    def inspect_image(self, image: str) -> ContainerImageInfo: ...

    def run_oneshot(self, spec: OneShotContainerSpec) -> str: ...

    def run_detached(
        self, spec: DetachedContainerSpec, *, start: bool = True
    ) -> str: ...

    def start_container(self, container_id: str) -> None: ...

    def inspect_container(
        self,
        *,
        name: str | None = None,
        container_id: str | None = None,
    ) -> ContainerInspection | None: ...

    def exec_in_container(
        self,
        container_id: str,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> str: ...

    def stop_and_remove(
        self,
        container_id: str,
        *,
        expected_started_at: str | None = None,
    ) -> None: ...


class LocalDockerRuntime:
    """Default local Docker/OCI CLI implementation of ContainerRuntimePort."""

    def __init__(self, *, executable: str = "docker") -> None:
        if not isinstance(executable, str) or not _NAME.fullmatch(executable):
            raise SDKError("Invalid container runtime executable")
        self.executable = executable

    def _run(self, *args: str, input: str | None = None) -> str:
        try:
            result = subprocess.run(
                [self.executable, *args],
                input=input,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SDKError("Container runtime CLI is unavailable") from exc
        if result.returncode:
            stderr = (result.stderr or "").strip().lower()
            if args[:2] == ("container", "inspect") and (
                "no such" in stderr or "not found" in stderr
            ):
                raise _ContainerNotFoundError(
                    f"Container runtime {args[0]} failed (exit {result.returncode})"
                )
            raise SDKError(
                f"Container runtime {args[0]} failed (exit {result.returncode})"
            )
        return result.stdout.strip()

    @staticmethod
    def _validate_container_id(container_id: str) -> str:
        if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(
            container_id
        ):
            raise SDKError("Container runtime returned an invalid full container ID")
        return container_id

    def _resolve_id_by_name(self, name: str) -> str | None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SDKError("Invalid container name")
        ids = self._run(
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            f"name=^/{name}$",
            "--format",
            "{{.ID}}",
        )
        if not ids:
            return None
        if len(ids.splitlines()) != 1:
            raise SDKError("Container name is ambiguous")
        return self._validate_container_id(ids)

    @staticmethod
    def _parse_state(raw_state: object) -> ContainerStateInfo:
        if (
            not isinstance(raw_state, dict)
            or type(raw_state.get("Running")) is not bool
            or type(raw_state.get("Restarting")) is not bool
            or type(raw_state.get("Paused")) is not bool
            or type(raw_state.get("Dead")) is not bool
            or not isinstance(raw_state.get("StartedAt"), str)
            or not isinstance(raw_state.get("FinishedAt"), str)
        ):
            raise SDKError("Unable to verify container process state")
        return ContainerStateInfo(
            running=raw_state["Running"],
            restarting=raw_state["Restarting"],
            paused=raw_state["Paused"],
            dead=raw_state["Dead"],
            started_at=raw_state["StartedAt"],
            finished_at=raw_state["FinishedAt"],
        )

    def inspect_image(self, image: str) -> ContainerImageInfo:
        try:
            validate_oci_digest_reference(image)
        except ValueError as exc:
            raise SDKError("Plugin image must be pinned to a sha256 digest") from exc
        raw = self._run("image", "inspect", "--format", "{{json .}}", image)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise SDKError("Unable to verify local image digest") from exc
        if not isinstance(payload, dict):
            raise SDKError("Unable to verify local image digest")
        digests = payload.get("RepoDigests")
        if not isinstance(digests, list) or image not in digests:
            raise SDKError("Local image does not have the approved digest")
        image_id = payload.get("Id")
        if isinstance(image_id, str) and _CONTAINER_ID.fullmatch(image_id):
            image_id = f"sha256:{image_id}"
        if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
            raise SDKError("Container runtime returned an invalid image ID")
        config = payload.get("Config")
        raw_labels = config.get("Labels") if isinstance(config, dict) else None
        if raw_labels is None:
            labels: dict[str, str] = {}
        elif isinstance(raw_labels, dict) and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_labels.items()
        ):
            labels = dict(raw_labels)
        else:
            raise SDKError("Local image returned invalid labels")
        return ContainerImageInfo(
            image_id=image_id,
            repo_digests=tuple(str(item) for item in digests if isinstance(item, str)),
            labels=labels,
        )

    def run_oneshot(self, spec: OneShotContainerSpec) -> str:
        spec.validate()
        args = ["container", "run", "--rm", "--network", spec.network]
        if spec.user is not None:
            args.extend(["--user", spec.user])
        if spec.gpu_uuid is not None:
            args.extend(["--gpus", f"device={spec.gpu_uuid}"])
        args.extend(
            [
                "--memory",
                f"{spec.memory_mb}m",
                "--cpus",
                str(spec.cpus),
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
            ]
        )
        if spec.pids_limit is not None:
            args.extend(["--pids-limit", str(spec.pids_limit)])
        if spec.shm_size_mb is not None:
            args.extend(["--shm-size", f"{spec.shm_size_mb}m"])
        if spec.env_file is not None:
            args.extend(["--env-file", str(spec.env_file)])
        for key, value in spec.env:
            args.extend(["--env", f"{key}={value}"])
        for mount in spec.mounts:
            args.extend(["--mount", mount.to_cli_arg()])
        args.extend(
            ["--entrypoint", spec.command[0], "--", spec.image, *spec.command[1:]]
        )
        return self._run(*args)

    def run_detached(self, spec: DetachedContainerSpec, *, start: bool = True) -> str:
        spec.validate()
        args = ["container", "create", "--name", spec.name]
        for key, value in spec.labels:
            args.extend(["--label", f"{key}={value}"])
        args.extend(["--user", spec.user, "--network", spec.network])
        if spec.gpu_uuid is not None:
            args.extend(["--gpus", f"device={spec.gpu_uuid}"])
        args.extend(
            [
                "--memory",
                f"{spec.memory_mb}m",
                "--cpus",
                str(spec.cpus),
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
            ]
        )
        if spec.pids_limit is not None:
            args.extend(["--pids-limit", str(spec.pids_limit)])
        if spec.shm_size_mb is not None:
            args.extend(["--shm-size", f"{spec.shm_size_mb}m"])
        for port in spec.ports:
            args.extend(["--publish", f"{port.host_port}:{port.container_port}"])
        if spec.env_file is not None:
            args.extend(["--env-file", str(spec.env_file)])
        for key, value in spec.env:
            args.extend(["--env", f"{key}={value}"])
        for mount in spec.mounts:
            args.extend(["--mount", mount.to_cli_arg()])
        if spec.command is not None:
            args.extend(["--entrypoint", spec.command[0]])
        args.extend(["--", spec.image])
        if spec.command is not None:
            args.extend(spec.command[1:])
        created = self._run(*args)
        container_id = (
            created
            if _CONTAINER_ID.fullmatch(created)
            else self._resolve_id_by_name(spec.name)
        )
        if container_id is None:
            raise SDKError("Created container could not be verified")
        if start:
            self.start_container(container_id)
        return container_id

    def start_container(self, container_id: str) -> None:
        validated = self._validate_container_id(container_id)
        self._run("container", "start", validated)

    def inspect_container(
        self,
        *,
        name: str | None = None,
        container_id: str | None = None,
    ) -> ContainerInspection | None:
        if (name is None) == (container_id is None):
            raise SDKError("Specify exactly one of name or container_id")
        resolved_id = (
            self._resolve_id_by_name(name)
            if name is not None
            else self._validate_container_id(container_id)
        )
        if resolved_id is None:
            return None
        try:
            raw = self._run(
                "container", "inspect", "--format", "{{json .}}", resolved_id
            )
        except _ContainerNotFoundError:
            return None
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise SDKError("Unable to verify container configuration") from exc
        if not isinstance(payload, dict):
            raise SDKError("Unable to verify container configuration")
        config = payload.get("Config")
        host_config = payload.get("HostConfig")
        mounts = payload.get("Mounts")
        if (
            not isinstance(config, dict)
            or not isinstance(host_config, dict)
            or not isinstance(mounts, list)
        ):
            raise SDKError("Unable to verify container configuration")
        raw_labels = config.get("Labels")
        if raw_labels is None:
            labels: dict[str, str] = {}
        elif isinstance(raw_labels, dict):
            labels = dict(raw_labels)
        else:
            raise SDKError("Unable to verify container ownership")
        raw_entrypoint = config.get("Entrypoint")
        if raw_entrypoint is None:
            entrypoint: tuple[str, ...] | None = None
        elif isinstance(raw_entrypoint, list) and all(
            isinstance(item, str) for item in raw_entrypoint
        ):
            entrypoint = tuple(raw_entrypoint)
        else:
            raise SDKError("Unable to verify container entrypoint")
        raw_cmd = config.get("Cmd")
        if raw_cmd is None:
            cmd: tuple[str, ...] | None = None
        elif isinstance(raw_cmd, list) and all(
            isinstance(item, str) for item in raw_cmd
        ):
            cmd = tuple(raw_cmd)
        else:
            raise SDKError("Unable to verify container entrypoint")
        image_ref = config.get("Image")
        image_id = payload.get("Image")
        user = config.get("User")
        if (
            not isinstance(image_ref, str)
            or not isinstance(image_id, str)
            or not isinstance(user, str)
        ):
            raise SDKError("Unable to verify container configuration")
        state = self._parse_state(payload.get("State"))
        raw_name = payload.get("Name")
        inspected_name = (
            raw_name.lstrip("/")
            if isinstance(raw_name, str) and raw_name
            else (name or "")
        )
        return ContainerInspection(
            container_id=resolved_id,
            name=inspected_name,
            image_id=image_id,
            image_ref=image_ref,
            labels=labels,
            host_config=host_config,
            mounts=tuple(mounts),
            user=user,
            entrypoint=entrypoint,
            cmd=cmd,
            state=state,
        )

    def exec_in_container(
        self,
        container_id: str,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> str:
        validated = self._validate_container_id(container_id)
        if not command or any(
            not isinstance(arg, str) or not arg or "\x00" in arg for arg in command
        ):
            raise SDKError("Invalid container exec command")
        args = ["exec"]
        if input_text is not None:
            args.append("--interactive")
        args.extend([validated, *command])
        return self._run(*args, input=input_text)

    def stop_and_remove(
        self,
        container_id: str,
        *,
        expected_started_at: str | None = None,
    ) -> None:
        validated = self._validate_container_id(container_id)
        info = self.inspect_container(container_id=validated)
        if info is None:
            return
        state = info.state
        if state.restarting or state.paused or state.dead:
            raise SDKError("Unable to verify container process state")
        if expected_started_at is not None and state.started_at != expected_started_at:
            raise SDKError(
                "Container process identity differs from the quiescence proof"
            )
        if state.running:
            self._run("container", "stop", validated)
            after = self.inspect_container(container_id=validated)
            if after is None:
                return
            if (
                after.state.restarting
                or after.state.paused
                or after.state.dead
                or after.state.running
                or (
                    expected_started_at is not None
                    and after.state.started_at != expected_started_at
                )
            ):
                raise SDKError("Container process identity changed after quiescence")
        self._run("container", "rm", validated)
