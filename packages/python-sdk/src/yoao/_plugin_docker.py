"""Fixed Docker operations for one explicitly bound plugin installation.

No shell, caller-supplied command, privileged mode, Docker socket or arbitrary
mounts are accepted. Container identity is checked before every mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from collections.abc import Sequence
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from yoao.errors import SDKError
from yoao_inference_contract.plugin import (
    PLUGIN_MANIFEST_OCI_LABEL,
    PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL,
    PluginManifest,
    parse_oci_plugin_manifest,
    parse_oci_qualification_command,
    validate_oci_digest_reference,
    validate_oci_qualification_command,
)

_GPU = re.compile(
    r"^GPU-[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
)
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_OWNER = re.compile(r"^[0-9a-f]{64}$")
_LIBREYOLO_ENTRYPOINT = "yoao-libreyolo"

_LABEL_INSTALLATION = "yoao.installation"
_LABEL_OWNER = "yoao.owner"
_LABEL_IMAGE = "yoao.image"
_LABEL_CONFIG = "yoao.config"
_LABEL_CANDIDATE = "yoao.candidate"

QUALIFICATION_CONTEXT_TARGET = "/var/lib/yoao-qualification/context.json"
QUALIFICATION_OUTPUT_DIR_TARGET = "/var/lib/yoao-qualification/output"
QUALIFICATION_OUTPUT_FILE_TARGET = f"{QUALIFICATION_OUTPUT_DIR_TARGET}/report.json"
QUALIFICATION_FIXTURE_TARGET = "/var/lib/yoao-qualification/fixture"


def _validate_entrypoint(entrypoint: Sequence[str], container_port: int) -> None:
    if (
        not entrypoint
        or any(
            not isinstance(arg, str) or not arg or any(c in arg for c in "\x00\r\n")
            for arg in entrypoint
        )
        or not _NAME.fullmatch(entrypoint[0])
        or entrypoint[0].lower() in {"sh", "bash", "dash", "ash", "zsh", "ksh", "fish"}
        or (
            entrypoint[0].lower().startswith("python")
            and any(
                arg == "-c"
                or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:])
                for arg in entrypoint[1:]
            )
        )
    ):
        raise ValueError("Invalid approved plugin entrypoint")
    if "--port" in entrypoint:
        index = entrypoint.index("--port")
        if (
            entrypoint.count("--port") != 1
            or index + 1 >= len(entrypoint)
            or entrypoint[index + 1] != str(container_port)
        ):
            raise ValueError("Plugin entrypoint port differs from the approved port")


@dataclass(frozen=True)
class ContainerConfig:
    name: str
    installation_id: str
    owner_hash: str
    image: str
    entrypoint: tuple[str, ...]
    gpu_uuid: str | None
    network: str
    host_port: int
    container_port: int
    memory_mb: int
    cpus: float
    env_file: Path
    data_dir: Path
    data_target: str
    qualification_file: Path | None = None
    candidate_token: str | None = None

    @property
    def qualification_target(self) -> str:
        return f"{self.data_target}-qualification/report.json"

    def validate(self) -> None:
        if not _NAME.fullmatch(self.name) or not _NAME.fullmatch(self.network):
            raise SDKError("Invalid container name or Docker network")
        if self.network == "host":
            raise SDKError("Host networking is not permitted")
        if not _NAME.fullmatch(self.installation_id) or not _OWNER.fullmatch(
            self.owner_hash
        ):
            raise SDKError("Invalid container ownership identity")
        try:
            validate_oci_digest_reference(self.image)
        except ValueError as exc:
            raise SDKError("Plugin image must be pinned to a sha256 digest") from exc
        if not isinstance(self.entrypoint, tuple):
            raise SDKError("Invalid approved plugin entrypoint")
        try:
            _validate_entrypoint(self.entrypoint, self.container_port)
        except ValueError as exc:
            raise SDKError(str(exc)) from exc
        if self.gpu_uuid is not None and not _GPU.fullmatch(self.gpu_uuid):
            raise SDKError("One physical NVIDIA GPU UUID is required")
        if not (1 <= self.host_port <= 65535 and 1 <= self.container_port <= 65535):
            raise SDKError("Invalid plugin port")
        if self.memory_mb <= 0 or not 0 < self.cpus <= 256:
            raise SDKError("Invalid container resource limits")
        if not self.env_file.is_absolute() or not self.data_dir.is_absolute():
            raise SDKError("Container files must have absolute paths")
        if not self.data_target.startswith("/") or ":" in self.data_target:
            raise SDKError("Invalid container data target")
        if "," in str(self.data_dir) or "," in self.data_target:
            raise SDKError("Docker mount paths must not contain commas")
        if "docker.sock" in str(self.data_dir) or "docker.sock" in self.data_target:
            raise SDKError("Docker socket mounts are not permitted")
        if self.qualification_file is not None and (
            not self.qualification_file.is_absolute()
            or "," in str(self.qualification_file)
        ):
            raise SDKError("Qualification file must have a safe absolute path")
        if self.candidate_token is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.candidate_token
        ):
            raise SDKError("Invalid candidate operation identity")

    def identity(self) -> str:
        fields = (
            self.installation_id,
            self.owner_hash,
            self.image,
            self.entrypoint,
            self.gpu_uuid,
            self.network,
            str(self.host_port),
            str(self.container_port),
            str(self.memory_mb),
            str(self.cpus),
            str(self.env_file),
            str(self.data_dir),
            self.data_target,
            str(self.qualification_file),
        )
        return hashlib.sha256(
            json.dumps(fields, separators=(",", ":")).encode()
        ).hexdigest()


def _validate_mount_path(path: Path, label: str) -> None:
    text = str(path)
    if (
        not path.is_absolute()
        or "," in text
        or "docker.sock" in text
        or any(c in text for c in "\x00\r\n")
    ):
        raise SDKError(f"{label} must have a safe absolute path")


@dataclass(frozen=True)
class QualificationRunConfig:
    image: str
    command: tuple[str, ...]
    gpu_uuid: str
    memory_mb: int
    cpus: float
    env_file: Path
    context_file: Path
    output_dir: Path
    data_target: str
    assets_dir: Path | None = None
    fixture_file: Path | None = None

    def validate(self) -> None:
        try:
            validate_oci_digest_reference(self.image)
        except ValueError as exc:
            raise SDKError("Plugin image must be pinned to a sha256 digest") from exc
        if not isinstance(self.command, tuple) or not all(
            isinstance(arg, str) for arg in self.command
        ):
            raise SDKError("Invalid approved qualification command")
        try:
            validate_oci_qualification_command(self.command)
        except ValueError as exc:
            raise SDKError("Invalid approved qualification command") from exc
        if not isinstance(self.gpu_uuid, str) or not _GPU.fullmatch(self.gpu_uuid):
            raise SDKError("One physical NVIDIA GPU UUID is required")
        if (
            type(self.memory_mb) is not int
            or self.memory_mb <= 0
            or isinstance(self.cpus, bool)
            or not isinstance(self.cpus, (int, float))
            or not math.isfinite(self.cpus)
            or not 0 < self.cpus <= 256
        ):
            raise SDKError("Invalid container resource limits")
        if (
            not self.data_target.startswith("/")
            or ":" in self.data_target
            or "," in self.data_target
            or "docker.sock" in self.data_target
            or any(c in self.data_target for c in "\x00\r\n")
        ):
            raise SDKError("Invalid container data target")
        _validate_mount_path(self.env_file, "Qualification env file")
        _validate_mount_path(self.context_file, "Qualification context file")
        _validate_mount_path(self.output_dir, "Qualification output directory")
        if self.assets_dir is not None:
            _validate_mount_path(self.assets_dir, "Qualification asset directory")
        if self.fixture_file is not None:
            _validate_mount_path(self.fixture_file, "Qualification fixture file")


class Docker:
    def _run(self, *args: str, input: str | None = None) -> str:
        try:
            result = subprocess.run(
                ["docker", *args],
                input=input,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SDKError("Docker CLI is unavailable") from exc
        if result.returncode:
            raise SDKError(f"Docker {args[0]} failed (exit {result.returncode})")
        return result.stdout.strip()

    def _inspect(self, config: ContainerConfig) -> dict | None:
        ids = self._run(
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            f"name=^/{config.name}$",
            "--format",
            "{{.ID}}",
        )
        if not ids:
            return None
        if len(ids.splitlines()) != 1:
            raise SDKError("Container name is ambiguous")
        container_id = ids
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise SDKError("Container runtime returned an invalid full container ID")
        raw = self._run(
            "container", "inspect", "--format", "{{json .Config.Labels}}", container_id
        )
        try:
            labels = json.loads(raw)
        except ValueError as exc:
            raise SDKError("Unable to verify container ownership") from exc
        expected = {
            _LABEL_INSTALLATION: config.installation_id,
            _LABEL_OWNER: config.owner_hash,
            _LABEL_IMAGE: config.image,
            _LABEL_CONFIG: config.identity(),
        }
        if config.candidate_token is not None:
            expected[_LABEL_CANDIDATE] = config.candidate_token
        if not isinstance(labels, dict) or any(
            labels.get(k) != v for k, v in expected.items()
        ):
            raise SDKError(
                "Container belongs to another installation or has a different configuration"
            )
        image = self._run(
            "container", "inspect", "--format", "{{.Config.Image}}", container_id
        )
        if image != config.image:
            raise SDKError("Container image does not match the installation receipt")
        raw_config = self._run(
            "container", "inspect", "--format", "{{json .HostConfig}}", container_id
        )
        raw_mounts = self._run(
            "container", "inspect", "--format", "{{json .Mounts}}", container_id
        )
        try:
            host = json.loads(raw_config)
            mounts = json.loads(raw_mounts)
        except ValueError as exc:
            raise SDKError("Unable to verify container resource configuration") from exc
        devices = host.get("DeviceRequests") if isinstance(host, dict) else None
        if (
            not isinstance(host, dict)
            or host.get("Privileged") is not False
            or host.get("PidMode") != ""
            or host.get("NetworkMode") != config.network
            or host.get("Memory") != config.memory_mb * 1024 * 1024
            or host.get("NanoCpus") != int(config.cpus * 1_000_000_000)
            or host.get("CapAdd") not in (None, [])
            or host.get("Devices") not in (None, [])
            or "ALL" not in (host.get("CapDrop") or [])
            or not any(
                str(item).startswith("no-new-privileges")
                for item in (host.get("SecurityOpt") or [])
            )
            or (
                devices not in (None, [])
                if config.gpu_uuid is None
                else (
                    not isinstance(devices, list)
                    or len(devices) != 1
                    or not isinstance(devices[0], dict)
                    or devices[0].get("DeviceIDs") != [config.gpu_uuid]
                    or devices[0].get("Capabilities") != [["gpu"]]
                )
            )
        ):
            raise SDKError("Container resources do not match the approved installation")
        ports = host.get("PortBindings") or {}
        bindings = (
            ports.get(f"{config.container_port}/tcp")
            if isinstance(ports, dict)
            else None
        )
        if (
            not isinstance(bindings, list)
            or len(bindings) != 1
            or not isinstance(bindings[0], dict)
            or bindings[0].get("HostPort") != str(config.host_port)
            or set(ports) != {f"{config.container_port}/tcp"}
        ):
            raise SDKError(
                "Container port bindings do not match the approved installation"
            )
        expected_mounts = {
            (str(config.data_dir), config.data_target, True),
        }
        if config.gpu_uuid is not None:
            expected_mounts.add(
                (
                    str(config.data_dir / "gpu-keyring.json"),
                    f"{config.data_target}/gpu-keyring.json",
                    False,
                )
            )
        if config.qualification_file is not None:
            expected_mounts.add(
                (str(config.qualification_file), config.qualification_target, False)
            )
        if not isinstance(mounts, list) or any(
            not isinstance(m, dict) or m.get("Type") != "bind" for m in mounts
        ):
            raise SDKError("Container mounts do not match the approved installation")
        actual_mounts = {
            (m.get("Source"), m.get("Destination"), m.get("RW")) for m in mounts
        }
        if len(mounts) != len(expected_mounts) or actual_mounts != expected_mounts:
            raise SDKError("Container mounts do not match the approved installation")
        user = self._run(
            "container", "inspect", "--format", "{{.Config.User}}", container_id
        )
        entrypoint = self._run(
            "container",
            "inspect",
            "--format",
            "{{json .Config.Entrypoint}}",
            container_id,
        )
        command = self._run(
            "container", "inspect", "--format", "{{json .Config.Cmd}}", container_id
        )
        try:
            entry = json.loads(entrypoint)
            cmd = json.loads(command)
        except ValueError as exc:
            raise SDKError("Unable to verify container entrypoint") from exc
        if (
            user != self._user()
            or entry != list(config.entrypoint)
            or cmd not in (None, [])
        ):
            raise SDKError(
                "Container user or entrypoint differs from the approved runtime"
            )
        return {"id": container_id, "name": config.name}

    def _verify_image_reference(self, image: str) -> None:
        raw = self._run("image", "inspect", "--format", "{{json .RepoDigests}}", image)
        try:
            digests = json.loads(raw)
        except ValueError as exc:
            raise SDKError("Unable to verify local image digest") from exc
        if not isinstance(digests, list) or image not in digests:
            raise SDKError("Local image does not have the approved digest")

    def _verify_image(self, config: ContainerConfig) -> None:
        self._verify_image_reference(config.image)

    def inspect_image_labels(self, image: str) -> tuple[str, dict[str, str]]:
        try:
            validate_oci_digest_reference(image)
        except ValueError as exc:
            raise SDKError("Plugin image must be pinned to a sha256 digest") from exc
        self._verify_image_reference(image)
        image_id = self._run("image", "inspect", "--format", "{{.Id}}", image)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise SDKError("Docker returned an invalid image ID")
        raw_labels = self._run(
            "image", "inspect", "--format", "{{json .Config.Labels}}", image
        )
        try:
            labels = json.loads(raw_labels)
        except ValueError as exc:
            raise SDKError("Unable to read local image labels") from exc
        if labels is None:
            return image_id, {}
        if not isinstance(labels, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in labels.items()
        ):
            raise SDKError("Local image returned invalid labels")
        return image_id, labels

    def inspect_image_manifest(
        self, image: str
    ) -> tuple[PluginManifest, str, tuple[str, ...] | None]:
        image_id, labels = self.inspect_image_labels(image)
        raw_manifest = labels.get(PLUGIN_MANIFEST_OCI_LABEL)
        if raw_manifest is None:
            raise SDKError(f"Local image does not declare {PLUGIN_MANIFEST_OCI_LABEL}")
        try:
            manifest = parse_oci_plugin_manifest(raw_manifest, artifact=image)
        except ValueError as exc:
            raise SDKError(
                f"Local image declares an invalid {PLUGIN_MANIFEST_OCI_LABEL} label"
            ) from exc
        raw_command = labels.get(PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL)
        qualification_command: tuple[str, ...] | None = None
        if raw_command is not None:
            try:
                qualification_command = parse_oci_qualification_command(raw_command)
            except ValueError as exc:
                raise SDKError(
                    f"Local image declares an invalid {PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL} label"
                ) from exc
        return manifest, image_id, qualification_command

    def run_qualification(self, config: QualificationRunConfig) -> None:
        config.validate()
        self._verify_image_reference(config.image)
        mounts = [
            "--mount",
            f"type=bind,src={config.context_file},dst={QUALIFICATION_CONTEXT_TARGET},readonly",
            "--mount",
            f"type=bind,src={config.output_dir},dst={QUALIFICATION_OUTPUT_DIR_TARGET}",
        ]
        if config.assets_dir is not None:
            mounts.extend(
                [
                    "--mount",
                    f"type=bind,src={config.assets_dir},dst={config.data_target}/assets,readonly",
                ]
            )
        if config.fixture_file is not None:
            mounts.extend(
                [
                    "--mount",
                    f"type=bind,src={config.fixture_file},dst={QUALIFICATION_FIXTURE_TARGET},readonly",
                ]
            )
        self._run(
            "container",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            self._user(),
            "--gpus",
            f"device={config.gpu_uuid}",
            "--memory",
            f"{config.memory_mb}m",
            "--cpus",
            str(config.cpus),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--env-file",
            str(config.env_file),
            *mounts,
            "--entrypoint",
            config.command[0],
            "--",
            config.image,
            *config.command[1:],
        )

    def image_id(self, config: ContainerConfig) -> str:
        config.validate()
        self._verify_image(config)
        image_id = self._run("image", "inspect", "--format", "{{.Id}}", config.image)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise SDKError("Docker returned an invalid image ID")
        return image_id

    @staticmethod
    def _user() -> str:
        if os.geteuid() == 0:
            raise SDKError("Run plugin deployment as a non-root Docker administrator")
        return f"{os.geteuid()}:{os.getegid()}"

    def verify_running(self, config: ContainerConfig) -> dict:
        config.validate()
        self._verify_image(config)
        existing = self._inspect(config)
        if existing is None:
            raise SDKError(
                "Active installation has no owned container; unexpected exit requires owner recovery"
            )
        image_id = self._run("image", "inspect", "--format", "{{.Id}}", config.image)
        container_image = self._run(
            "container", "inspect", "--format", "{{.Image}}", existing["id"]
        )
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or image_id != container_image
        ):
            raise SDKError("Running container does not use the verified local image")
        if (
            self._run(
                "container", "inspect", "--format", "{{.State.Running}}", existing["id"]
            )
            != "true"
        ):
            raise SDKError(
                "Active installation container is not running; unexpected exit requires owner recovery"
            )
        started_at = self._run(
            "container", "inspect", "--format", "{{.State.StartedAt}}", existing["id"]
        )
        if not started_at or started_at.startswith("0001-"):
            raise SDKError("Unable to verify container process start identity")
        return {**existing, "image_id": image_id, "started_at": started_at}

    def measure_assets(
        self, config: ContainerConfig, container_id: str, assets: list[dict]
    ) -> list[str]:
        """Hash files in the running container, not a replaceable host pathname."""
        probe = Path(__file__).with_name("_plugin_asset_measure.py").read_text()
        output = self._run(
            "exec",
            "--interactive",
            container_id,
            "python3",
            "-I",
            "-S",
            "-c",
            probe,
            input=json.dumps({"root": config.data_target, "assets": assets}),
        )
        try:
            measured = json.loads(output)
        except ValueError as exc:
            raise SDKError("Container returned an invalid asset measurement") from exc
        if not isinstance(measured, list) or any(
            not isinstance(path, str) for path in measured
        ):
            raise SDKError("Container returned an invalid asset measurement")
        return measured

    def _state(self, container_id: str) -> dict:
        try:
            state = json.loads(
                self._run(
                    "container", "inspect", "--format", "{{json .State}}", container_id
                )
            )
        except (TypeError, ValueError) as exc:
            raise SDKError("Unable to verify container process state") from exc
        if (
            not isinstance(state, dict)
            or type(state.get("Running")) is not bool
            or state.get("Restarting") is not False
            or state.get("Paused") is not False
            or state.get("Dead") is not False
            or not isinstance(state.get("StartedAt"), str)
            or not isinstance(state.get("FinishedAt"), str)
        ):
            raise SDKError("Unable to verify container process state")
        return state

    @staticmethod
    def _never_finished(state: dict) -> bool:
        return state["FinishedAt"] == "0001-01-01T00:00:00Z"

    def create_candidate(self, config: ContainerConfig) -> dict:
        """Replay only the container marked by this private candidate intent."""
        config.validate()
        existing = self._inspect(config)
        return existing if existing is not None else self._create(config)

    def start_candidate(
        self, config: ContainerConfig, container_id: str, *, may_start: bool
    ) -> dict:
        config.validate()
        self._verify_image(config)
        existing = self._inspect(config)
        if existing is None or existing["id"] != container_id:
            raise SDKError("Candidate container ID differs from the recorded identity")
        state = self._state(container_id)
        if may_start and not self._never_finished(state):
            raise SDKError(
                "Candidate has previously exited; refusing an unproven restart"
            )
        if not state["Running"] and may_start:
            if state["StartedAt"] != "0001-01-01T00:00:00Z":
                raise SDKError("Candidate process exited; refusing an unproven restart")
            self._run("container", "start", container_id)
        elif not state["Running"]:
            raise SDKError("Candidate process exited; refusing an unproven restart")
        inspected = self.verify_running(config)
        state = self._state(container_id)
        if (
            not state["Running"]
            or state["StartedAt"] != inspected["started_at"]
            or (may_start and not self._never_finished(state))
        ):
            raise SDKError("Candidate process identity changed or previously exited")
        return inspected

    def _create(self, config: ContainerConfig) -> dict:
        config.validate()
        self._verify_image(config)
        if config.gpu_uuid is not None and config.qualification_file is None:
            raise SDKError("GPU qualification report is required before starting")
        candidate_label = (
            ["--label", f"{_LABEL_CANDIDATE}={config.candidate_token}"]
            if config.candidate_token
            else []
        )
        self._run(
            "container",
            "create",
            "--name",
            config.name,
            *candidate_label,
            "--label",
            f"{_LABEL_INSTALLATION}={config.installation_id}",
            "--label",
            f"{_LABEL_OWNER}={config.owner_hash}",
            "--label",
            f"{_LABEL_IMAGE}={config.image}",
            "--user",
            self._user(),
            "--network",
            config.network,
            *(["--gpus", f"device={config.gpu_uuid}"] if config.gpu_uuid else []),
            "--memory",
            f"{config.memory_mb}m",
            "--cpus",
            str(config.cpus),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--publish",
            f"{config.host_port}:{config.container_port}",
            "--label",
            f"{_LABEL_CONFIG}={config.identity()}",
            "--env-file",
            str(config.env_file),
            "--mount",
            f"type=bind,src={config.data_dir},dst={config.data_target}",
            *(
                [
                    "--mount",
                    f"type=bind,src={config.data_dir / 'gpu-keyring.json'},dst={config.data_target}/gpu-keyring.json,readonly",
                ]
                if config.gpu_uuid
                else []
            ),
            *(
                [
                    "--mount",
                    f"type=bind,src={config.qualification_file},dst={config.qualification_target},readonly",
                ]
                if config.qualification_file
                else []
            ),
            "--",
            config.image,
        )
        existing = self._inspect(config)
        if existing is None:
            raise SDKError("Created container could not be verified")
        return existing

    def ensure_started(self, config: ContainerConfig) -> dict:
        config.validate()
        self._verify_image(config)
        if config.gpu_uuid is not None and config.qualification_file is None:
            raise SDKError("GPU qualification report is required before starting")
        existing = self._inspect(config)
        if existing is None:
            existing = self._create(config)
        image_id = self._run("image", "inspect", "--format", "{{.Id}}", config.image)
        container_image = self._run(
            "container", "inspect", "--format", "{{.Image}}", existing["id"]
        )
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or image_id != container_image
        ):
            raise SDKError("Running container does not use the verified local image")
        state = self._run(
            "container", "inspect", "--format", "{{.State.Running}}", existing["id"]
        )
        if state == "false":
            self._run("container", "start", existing["id"])
        elif state != "true":
            raise SDKError("Unknown container state")
        return self.verify_running(config)

    def wait_ready(self, config: ContainerConfig, container_id: str) -> None:
        """Wait for process startup, without replacing the core's authenticated probe."""
        deadline = time.monotonic() + 60
        with httpx.Client(timeout=1, trust_env=False, follow_redirects=False) as client:
            while time.monotonic() < deadline:
                if (
                    self._run(
                        "container",
                        "inspect",
                        "--format",
                        "{{.State.Running}}",
                        container_id,
                    )
                    != "true"
                ):
                    raise SDKError(
                        "Plugin process exited before its health endpoint became ready"
                    )
                try:
                    response = client.get(f"http://127.0.0.1:{config.host_port}/health")
                except (
                    httpx.NetworkError,
                    httpx.TimeoutException,
                    httpx.RemoteProtocolError,
                ):
                    pass
                else:
                    if response.status_code == 200:
                        return
                    if response.status_code != 503:
                        raise SDKError(
                            f"Plugin startup health returned HTTP {response.status_code}"
                        )
                time.sleep(0.25)
        raise SDKError(
            "Plugin startup timed out after 60 seconds; activation was not submitted"
        )

    def prepare_assets(self, config: ContainerConfig) -> None:
        config.validate()
        if config.entrypoint[0] != _LIBREYOLO_ENTRYPOINT:
            raise SDKError(
                "Selected LibreYOLO image manifest does not declare the "
                "yoao-libreyolo entrypoint; use its original tools for historical "
                "images or select a new compatible image"
            )
        self._verify_image(config)
        self._run(
            "container",
            "run",
            "--rm",
            "--network",
            config.network,
            "--user",
            self._user(),
            "--memory",
            f"{config.memory_mb}m",
            "--cpus",
            str(config.cpus),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--env-file",
            str(config.env_file),
            "--mount",
            f"type=bind,src={config.data_dir},dst={config.data_target}",
            "--entrypoint",
            _LIBREYOLO_ENTRYPOINT,
            "--",
            config.image,
            "prepare-assets",
            "--size",
            "t",
            "--asset-dir",
            f"{config.data_target}/assets",
        )

    def exists(self, config: ContainerConfig) -> bool:
        config.validate()
        return self._inspect(config) is not None

    def stop_and_remove(
        self, config: ContainerConfig, proven_id: str, started_at: str | None
    ) -> None:
        config.validate()
        existing = self._inspect(config)
        if existing is None:
            return
        if existing["id"] != proven_id:
            raise SDKError("Container ID differs from the proven container")
        state = self._state(proven_id)
        if not started_at or state["StartedAt"] != started_at:
            raise SDKError(
                "Container process identity differs from the quiescence proof"
            )
        if state["Running"]:
            self._run("container", "stop", proven_id)
            state = self._state(proven_id)
            if state["Running"] or state["StartedAt"] != started_at:
                raise SDKError("Container process identity changed after quiescence")
        self._run("container", "rm", proven_id)
