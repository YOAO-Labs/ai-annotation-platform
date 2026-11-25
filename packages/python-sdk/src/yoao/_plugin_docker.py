"""Fixed container operations for one explicitly bound plugin installation.

No shell, caller-supplied command, privileged mode, container socket or arbitrary
mounts are accepted. Container identity is checked before every mutation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

import httpx

from yoao.container_runtime import (
    ContainerInspection,
    ContainerMount,
    ContainerPortBinding,
    ContainerRuntimePort,
    ContainerStateInfo,
    DetachedContainerSpec,
    LocalDockerRuntime,
    OneShotContainerSpec,
)
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

PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL = "io.yoao.plugin.prepare-assets-command"

_GPU = re.compile(
    r"^GPU-[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
)
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_OWNER = re.compile(r"^[0-9a-f]{64}$")

_LABEL_INSTALLATION = "yoao.installation"
_LABEL_OWNER = "yoao.owner"
_LABEL_IMAGE = "yoao.image"
_LABEL_CONFIG = "yoao.config"
_LABEL_CANDIDATE = "yoao.candidate"

QUALIFICATION_CONTEXT_TARGET = "/var/lib/yoao-qualification/context.json"
QUALIFICATION_OUTPUT_DIR_TARGET = "/var/lib/yoao-qualification/output"
QUALIFICATION_OUTPUT_FILE_TARGET = f"{QUALIFICATION_OUTPUT_DIR_TARGET}/report.json"
QUALIFICATION_FIXTURE_TARGET = "/var/lib/yoao-qualification/fixture"


def validate_oci_prepare_assets_command(command: Sequence[object]) -> tuple[str, ...]:
    """Validate an asset-preparation argument vector as a fixed non-shell command."""
    try:
        return validate_oci_qualification_command(command)
    except ValueError as exc:
        raise ValueError(
            str(exc).replace(
                PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL,
                PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL,
            )
        ) from None


def parse_oci_prepare_assets_command(raw: object) -> tuple[str, ...]:
    """Parse an OCI prepare-assets command label as a fixed non-shell argument vector."""
    try:
        return parse_oci_qualification_command(raw)
    except ValueError as exc:
        raise ValueError(
            str(exc).replace(
                PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL,
                PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL,
            )
        ) from None


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
class InspectedPluginImage:
    """Verified OCI plugin image manifest and optional lifecycle command labels."""

    manifest: PluginManifest
    image_id: str
    qualification_command: tuple[str, ...] | None
    prepare_assets_command: tuple[str, ...] | None


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
    """Plugin deployment container controller backed by a ContainerRuntimePort."""

    def __init__(self, runtime: ContainerRuntimePort | None = None) -> None:
        self.runtime: ContainerRuntimePort = runtime or LocalDockerRuntime()

    @staticmethod
    def _user() -> str:
        if os.geteuid() == 0:
            raise SDKError(
                "Run plugin deployment as a non-root container administrator"
            )
        return f"{os.geteuid()}:{os.getegid()}"

    @staticmethod
    def _verify_clean_state(state: ContainerStateInfo) -> ContainerStateInfo:
        if state.restarting or state.paused or state.dead:
            raise SDKError("Unable to verify container process state")
        return state

    @staticmethod
    def _never_finished(state: ContainerStateInfo) -> bool:
        return state.finished_at == "0001-01-01T00:00:00Z"

    def _verify_owned_inspection(
        self, config: ContainerConfig, info: ContainerInspection
    ) -> dict:
        if not re.fullmatch(r"[0-9a-f]{64}", info.container_id):
            raise SDKError("Container runtime returned an invalid full container ID")
        expected = {
            _LABEL_INSTALLATION: config.installation_id,
            _LABEL_OWNER: config.owner_hash,
            _LABEL_IMAGE: config.image,
            _LABEL_CONFIG: config.identity(),
        }
        if config.candidate_token is not None:
            expected[_LABEL_CANDIDATE] = config.candidate_token
        if not isinstance(info.labels, dict) or any(
            info.labels.get(k) != v for k, v in expected.items()
        ):
            raise SDKError(
                "Container belongs to another installation or has a different configuration"
            )
        if info.image_ref != config.image:
            raise SDKError("Container image does not match the installation receipt")
        host = info.host_config
        mounts = info.mounts
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
        if any(not isinstance(m, dict) or m.get("Type") != "bind" for m in mounts):
            raise SDKError("Container mounts do not match the approved installation")
        actual_mounts = {
            (m.get("Source"), m.get("Destination"), m.get("RW")) for m in mounts
        }
        if len(mounts) != len(expected_mounts) or actual_mounts != expected_mounts:
            raise SDKError("Container mounts do not match the approved installation")
        if (
            info.user != self._user()
            or info.entrypoint != config.entrypoint
            or info.cmd not in (None, ())
        ):
            raise SDKError(
                "Container user or entrypoint differs from the approved runtime"
            )
        return {"id": info.container_id, "name": config.name}

    def _inspect(self, config: ContainerConfig) -> dict | None:
        info = self.runtime.inspect_container(name=config.name)
        if info is None:
            return None
        return self._verify_owned_inspection(config, info)

    def _state(self, container_id: str) -> ContainerStateInfo:
        info = self.runtime.inspect_container(container_id=container_id)
        if info is None:
            raise SDKError("Unable to verify container process state")
        return self._verify_clean_state(info.state)

    def inspect_image_labels(self, image: str) -> tuple[str, dict[str, str]]:
        info = self.runtime.inspect_image(image)
        return info.image_id, dict(info.labels)

    def inspect_image_metadata(self, image: str) -> InspectedPluginImage:
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
        raw_prepare_assets = labels.get(PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL)
        prepare_assets_command: tuple[str, ...] | None = None
        if raw_prepare_assets is not None:
            try:
                prepare_assets_command = parse_oci_prepare_assets_command(
                    raw_prepare_assets
                )
            except ValueError as exc:
                raise SDKError(
                    f"Local image declares an invalid {PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL} label"
                ) from exc
        return InspectedPluginImage(
            manifest=manifest,
            image_id=image_id,
            qualification_command=qualification_command,
            prepare_assets_command=prepare_assets_command,
        )

    def inspect_image_manifest(
        self, image: str
    ) -> tuple[PluginManifest, str, tuple[str, ...] | None]:
        inspected = self.inspect_image_metadata(image)
        return (
            inspected.manifest,
            inspected.image_id,
            inspected.qualification_command,
        )

    def run_qualification(self, config: QualificationRunConfig) -> None:
        config.validate()
        self.runtime.inspect_image(config.image)
        mounts: list[ContainerMount] = [
            ContainerMount(
                source=config.context_file,
                target=QUALIFICATION_CONTEXT_TARGET,
                readonly=True,
            ),
            ContainerMount(
                source=config.output_dir,
                target=QUALIFICATION_OUTPUT_DIR_TARGET,
                readonly=False,
            ),
        ]
        if config.assets_dir is not None:
            mounts.append(
                ContainerMount(
                    source=config.assets_dir,
                    target=f"{config.data_target}/assets",
                    readonly=True,
                )
            )
        if config.fixture_file is not None:
            mounts.append(
                ContainerMount(
                    source=config.fixture_file,
                    target=QUALIFICATION_FIXTURE_TARGET,
                    readonly=True,
                )
            )
        self.runtime.run_oneshot(
            OneShotContainerSpec(
                image=config.image,
                command=config.command,
                network="none",
                memory_mb=config.memory_mb,
                cpus=config.cpus,
                user=self._user(),
                gpu_uuid=config.gpu_uuid,
                env_file=config.env_file,
                mounts=tuple(mounts),
            )
        )

    def image_id(self, config: ContainerConfig) -> str:
        config.validate()
        return self.runtime.inspect_image(config.image).image_id

    def verify_running(self, config: ContainerConfig) -> dict:
        config.validate()
        image_id = self.runtime.inspect_image(config.image).image_id
        info = self.runtime.inspect_container(name=config.name)
        if info is None:
            raise SDKError(
                "Active installation has no owned container; unexpected exit requires owner recovery"
            )
        existing = self._verify_owned_inspection(config, info)
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or image_id != info.image_id
        ):
            raise SDKError("Running container does not use the verified local image")
        if not info.state.running:
            raise SDKError(
                "Active installation container is not running; unexpected exit requires owner recovery"
            )
        started_at = info.state.started_at
        if not started_at or started_at.startswith("0001-"):
            raise SDKError("Unable to verify container process start identity")
        return {**existing, "image_id": image_id, "started_at": started_at}

    def measure_assets(
        self, config: ContainerConfig, container_id: str, assets: list[dict]
    ) -> list[str]:
        """Hash files in the running container, not a replaceable host pathname."""
        probe = Path(__file__).with_name("_plugin_asset_measure.py").read_text()
        output = self.runtime.exec_in_container(
            container_id,
            ("python3", "-I", "-S", "-c", probe),
            input_text=json.dumps({"root": config.data_target, "assets": assets}),
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

    def create_candidate(self, config: ContainerConfig) -> dict:
        """Replay only the container marked by this private candidate intent."""
        config.validate()
        existing = self._inspect(config)
        return existing if existing is not None else self._create(config)

    def start_candidate(
        self, config: ContainerConfig, container_id: str, *, may_start: bool
    ) -> dict:
        config.validate()
        self.runtime.inspect_image(config.image)
        existing = self._inspect(config)
        if existing is None or existing["id"] != container_id:
            raise SDKError("Candidate container ID differs from the recorded identity")
        state = self._state(container_id)
        if may_start and not self._never_finished(state):
            raise SDKError(
                "Candidate has previously exited; refusing an unproven restart"
            )
        if not state.running and may_start:
            if state.started_at != "0001-01-01T00:00:00Z":
                raise SDKError("Candidate process exited; refusing an unproven restart")
            self.runtime.start_container(container_id)
        elif not state.running:
            raise SDKError("Candidate process exited; refusing an unproven restart")
        inspected = self.verify_running(config)
        state = self._state(container_id)
        if (
            not state.running
            or state.started_at != inspected["started_at"]
            or (may_start and not self._never_finished(state))
        ):
            raise SDKError("Candidate process identity changed or previously exited")
        return inspected

    def _create(self, config: ContainerConfig) -> dict:
        config.validate()
        self.runtime.inspect_image(config.image)
        if config.gpu_uuid is not None and config.qualification_file is None:
            raise SDKError("GPU qualification report is required before starting")
        labels: list[tuple[str, str]] = []
        if config.candidate_token:
            labels.append((_LABEL_CANDIDATE, config.candidate_token))
        labels.extend(
            [
                (_LABEL_INSTALLATION, config.installation_id),
                (_LABEL_OWNER, config.owner_hash),
                (_LABEL_IMAGE, config.image),
                (_LABEL_CONFIG, config.identity()),
            ]
        )
        mounts: list[ContainerMount] = [
            ContainerMount(
                source=config.data_dir,
                target=config.data_target,
                readonly=False,
            )
        ]
        if config.gpu_uuid is not None:
            mounts.append(
                ContainerMount(
                    source=config.data_dir / "gpu-keyring.json",
                    target=f"{config.data_target}/gpu-keyring.json",
                    readonly=True,
                )
            )
        if config.qualification_file is not None:
            mounts.append(
                ContainerMount(
                    source=config.qualification_file,
                    target=config.qualification_target,
                    readonly=True,
                )
            )
        self.runtime.run_detached(
            DetachedContainerSpec(
                name=config.name,
                image=config.image,
                network=config.network,
                memory_mb=config.memory_mb,
                cpus=config.cpus,
                user=self._user(),
                labels=tuple(labels),
                gpu_uuid=config.gpu_uuid,
                ports=(
                    ContainerPortBinding(
                        host_port=config.host_port,
                        container_port=config.container_port,
                    ),
                ),
                env_file=config.env_file,
                mounts=tuple(mounts),
            ),
            start=False,
        )
        existing = self._inspect(config)
        if existing is None:
            raise SDKError("Created container could not be verified")
        return existing

    def ensure_started(self, config: ContainerConfig) -> dict:
        config.validate()
        image_id = self.runtime.inspect_image(config.image).image_id
        if config.gpu_uuid is not None and config.qualification_file is None:
            raise SDKError("GPU qualification report is required before starting")
        existing = self._inspect(config)
        if existing is None:
            existing = self._create(config)
        info = self.runtime.inspect_container(container_id=existing["id"])
        if info is None:
            raise SDKError("Created container could not be verified")
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or image_id != info.image_id
        ):
            raise SDKError("Running container does not use the verified local image")
        if not info.state.running:
            self.runtime.start_container(existing["id"])
        return self.verify_running(config)

    def wait_ready(self, config: ContainerConfig, container_id: str) -> None:
        """Wait for process startup, without replacing the core's authenticated probe."""
        deadline = time.monotonic() + 60
        with httpx.Client(timeout=1, trust_env=False, follow_redirects=False) as client:
            while time.monotonic() < deadline:
                info = self.runtime.inspect_container(container_id=container_id)
                if info is None or not info.state.running:
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

    def prepare_assets(self, config: ContainerConfig, command: Sequence[str]) -> None:
        """Run the image-declared non-shell asset preparation command."""
        config.validate()
        try:
            validated_command = validate_oci_prepare_assets_command(command)
        except ValueError as exc:
            raise SDKError("Invalid approved asset-preparation command") from exc
        self.runtime.inspect_image(config.image)
        self.runtime.run_oneshot(
            OneShotContainerSpec(
                image=config.image,
                command=validated_command,
                network=config.network,
                memory_mb=config.memory_mb,
                cpus=config.cpus,
                user=self._user(),
                env_file=config.env_file,
                mounts=(
                    ContainerMount(
                        source=config.data_dir,
                        target=config.data_target,
                        readonly=False,
                    ),
                ),
            )
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
        if not started_at:
            raise SDKError(
                "Container process identity differs from the quiescence proof"
            )
        self.runtime.stop_and_remove(proven_id, expected_started_at=started_at)
