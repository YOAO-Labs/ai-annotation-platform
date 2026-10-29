"""Fixed Docker operations for one explicitly bound plugin installation.

No shell, caller-supplied command, privileged mode, Docker socket or arbitrary
mounts are accepted. Container identity is checked before every mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ai_annotation.errors import AAPError
from ai_annotation.plugin_models import _OCI_DIGEST_REFERENCE

_IMAGE = re.compile(_OCI_DIGEST_REFERENCE)
_GPU = re.compile(
    r"^GPU-[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
)
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_OWNER = re.compile(r"^[0-9a-f]{64}$")
_QUALIFICATION_TARGET = "/var/lib/aap-libreyolo-qualification/report.json"


@dataclass(frozen=True)
class ContainerConfig:
    name: str
    installation_id: str
    owner_hash: str
    image: str
    entrypoint: tuple[str, ...]
    gpu_uuid: str
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

    def validate(self) -> None:
        if not _NAME.fullmatch(self.name) or not _NAME.fullmatch(self.network):
            raise AAPError("Invalid container name or Docker network")
        if self.network == "host":
            raise AAPError("Host networking is not permitted")
        if not _NAME.fullmatch(self.installation_id) or not _OWNER.fullmatch(
            self.owner_hash
        ):
            raise AAPError("Invalid container ownership identity")
        if not _IMAGE.fullmatch(self.image):
            raise AAPError("Plugin image must be pinned to a sha256 digest")
        if (
            not isinstance(self.entrypoint, tuple)
            or len(self.entrypoint) != 6
            or not isinstance(self.entrypoint[0], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.entrypoint[0])
            or self.entrypoint[1:] != ("serve", "--host", "0.0.0.0", "--port", "8000")
        ):
            raise AAPError("Invalid approved plugin entrypoint")
        if not _GPU.fullmatch(self.gpu_uuid):
            raise AAPError("One physical NVIDIA GPU UUID is required")
        if not (1 <= self.host_port <= 65535 and 1 <= self.container_port <= 65535):
            raise AAPError("Invalid plugin port")
        if self.memory_mb <= 0 or not 0 < self.cpus <= 256:
            raise AAPError("Invalid container resource limits")
        if not self.env_file.is_absolute() or not self.data_dir.is_absolute():
            raise AAPError("Container files must have absolute paths")
        if not self.data_target.startswith("/") or ":" in self.data_target:
            raise AAPError("Invalid container data target")
        if "," in str(self.data_dir) or "," in self.data_target:
            raise AAPError("Docker mount paths must not contain commas")
        if "docker.sock" in str(self.data_dir) or "docker.sock" in self.data_target:
            raise AAPError("Docker socket mounts are not permitted")
        if self.qualification_file is not None and (
            not self.qualification_file.is_absolute()
            or "," in str(self.qualification_file)
        ):
            raise AAPError("Qualification file must have a safe absolute path")
        if self.candidate_token is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.candidate_token
        ):
            raise AAPError("Invalid candidate operation identity")

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


class Docker:
    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["docker", *args], capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise AAPError("Docker CLI is unavailable") from exc
        if result.returncode:
            # Docker errors may contain env values or other sensitive configuration.
            raise AAPError(f"Docker {args[0]} failed (exit {result.returncode})")
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
            raise AAPError("Container name is ambiguous")
        container_id = ids
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise AAPError("Container runtime returned an invalid full container ID")
        raw = self._run(
            "container", "inspect", "--format", "{{json .Config.Labels}}", container_id
        )
        try:
            labels = json.loads(raw)
        except ValueError as exc:
            raise AAPError("Unable to verify container ownership") from exc
        expected = {
            "aap.installation": config.installation_id,
            "aap.owner": config.owner_hash,
            "aap.image": config.image,
            "aap.config": config.identity(),
        }
        if config.candidate_token is not None:
            expected["aap.candidate"] = config.candidate_token
        if not isinstance(labels, dict) or any(
            labels.get(k) != v for k, v in expected.items()
        ):
            raise AAPError(
                "Container belongs to another installation or has a different configuration"
            )
        image = self._run(
            "container", "inspect", "--format", "{{.Config.Image}}", container_id
        )
        if image != config.image:
            raise AAPError("Container image does not match the installation receipt")
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
            raise AAPError("Unable to verify container resource configuration") from exc
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
            or not isinstance(devices, list)
            or len(devices) != 1
            or not isinstance(devices[0], dict)
            or devices[0].get("DeviceIDs") != [config.gpu_uuid]
            or devices[0].get("Capabilities") != [["gpu"]]
        ):
            raise AAPError("Container resources do not match the approved installation")
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
            raise AAPError(
                "Container port bindings do not match the approved installation"
            )
        expected_mounts = {
            (str(config.data_dir), config.data_target, True),
            (
                str(config.data_dir / "gpu-keyring.json"),
                f"{config.data_target}/gpu-keyring.json",
                False,
            ),
        }
        if config.qualification_file is not None:
            expected_mounts.add(
                (str(config.qualification_file), _QUALIFICATION_TARGET, False)
            )
        if not isinstance(mounts, list) or any(
            not isinstance(m, dict) or m.get("Type") != "bind" for m in mounts
        ):
            raise AAPError("Container mounts do not match the approved installation")
        actual_mounts = {
            (m.get("Source"), m.get("Destination"), m.get("RW")) for m in mounts
        }
        if len(mounts) != len(expected_mounts) or actual_mounts != expected_mounts:
            raise AAPError("Container mounts do not match the approved installation")
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
            raise AAPError("Unable to verify container entrypoint") from exc
        if (
            user != self._user()
            or entry != list(config.entrypoint)
            or cmd not in (None, [])
        ):
            raise AAPError(
                "Container user or entrypoint differs from the approved runtime"
            )
        return {"id": container_id, "name": config.name}

    def _verify_image(self, config: ContainerConfig) -> None:
        raw = self._run(
            "image", "inspect", "--format", "{{json .RepoDigests}}", config.image
        )
        try:
            digests = json.loads(raw)
        except ValueError as exc:
            raise AAPError("Unable to verify local image digest") from exc
        if not isinstance(digests, list) or config.image not in digests:
            raise AAPError("Local image does not have the approved digest")

    def image_id(self, config: ContainerConfig) -> str:
        config.validate()
        self._verify_image(config)
        image_id = self._run("image", "inspect", "--format", "{{.Id}}", config.image)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise AAPError("Docker returned an invalid image ID")
        return image_id

    @staticmethod
    def _user() -> str:
        if os.geteuid() == 0:
            raise AAPError("Run plugin deployment as a non-root Docker administrator")
        return f"{os.geteuid()}:{os.getegid()}"

    def verify_running(self, config: ContainerConfig) -> dict:
        config.validate()
        self._verify_image(config)
        existing = self._inspect(config)
        if existing is None:
            raise AAPError(
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
            raise AAPError("Running container does not use the verified local image")
        if (
            self._run(
                "container", "inspect", "--format", "{{.State.Running}}", existing["id"]
            )
            != "true"
        ):
            raise AAPError(
                "Active installation container is not running; unexpected exit requires owner recovery"
            )
        started_at = self._run(
            "container", "inspect", "--format", "{{.State.StartedAt}}", existing["id"]
        )
        if not started_at or started_at.startswith("0001-"):
            raise AAPError("Unable to verify container process start identity")
        return {**existing, "image_id": image_id, "started_at": started_at}

    def _state(self, container_id: str) -> dict:
        try:
            state = json.loads(
                self._run(
                    "container", "inspect", "--format", "{{json .State}}", container_id
                )
            )
        except (TypeError, ValueError) as exc:
            raise AAPError("Unable to verify container process state") from exc
        if (
            not isinstance(state, dict)
            or type(state.get("Running")) is not bool
            or state.get("Restarting") is not False
            or state.get("Paused") is not False
            or state.get("Dead") is not False
            or not isinstance(state.get("StartedAt"), str)
            or not isinstance(state.get("FinishedAt"), str)
        ):
            raise AAPError("Unable to verify container process state")
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
            raise AAPError("Candidate container ID differs from the recorded identity")
        state = self._state(container_id)
        if may_start and not self._never_finished(state):
            raise AAPError(
                "Candidate has previously exited; refusing an unproven restart"
            )
        if not state["Running"] and may_start:
            if state["StartedAt"] != "0001-01-01T00:00:00Z":
                raise AAPError("Candidate process exited; refusing an unproven restart")
            self._run("container", "start", container_id)
        elif not state["Running"]:
            raise AAPError("Candidate process exited; refusing an unproven restart")
        inspected = self.verify_running(config)
        state = self._state(container_id)
        if (
            not state["Running"]
            or state["StartedAt"] != inspected["started_at"]
            or (may_start and not self._never_finished(state))
        ):
            raise AAPError("Candidate process identity changed or previously exited")
        return inspected

    def _create(self, config: ContainerConfig) -> dict:
        config.validate()
        self._verify_image(config)
        if config.qualification_file is None:
            raise AAPError("GPU qualification report is required before starting")
        candidate_label = (
            ["--label", f"aap.candidate={config.candidate_token}"]
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
            f"aap.installation={config.installation_id}",
            "--label",
            f"aap.owner={config.owner_hash}",
            "--label",
            f"aap.image={config.image}",
            "--user",
            self._user(),
            "--network",
            config.network,
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
            "--publish",
            f"{config.host_port}:{config.container_port}",
            "--label",
            f"aap.config={config.identity()}",
            "--env-file",
            str(config.env_file),
            "--mount",
            f"type=bind,src={config.data_dir},dst={config.data_target}",
            "--mount",
            f"type=bind,src={config.data_dir / 'gpu-keyring.json'},dst={config.data_target}/gpu-keyring.json,readonly",
            "--mount",
            f"type=bind,src={config.qualification_file},dst={_QUALIFICATION_TARGET},readonly",
            "--",
            config.image,
        )
        existing = self._inspect(config)
        if existing is None:
            raise AAPError("Created container could not be verified")
        return existing

    def ensure_started(self, config: ContainerConfig) -> dict:
        config.validate()
        self._verify_image(config)
        if config.qualification_file is None:
            raise AAPError("GPU qualification report is required before starting")
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
            raise AAPError("Running container does not use the verified local image")
        state = self._run(
            "container", "inspect", "--format", "{{.State.Running}}", existing["id"]
        )
        if state == "false":
            self._run("container", "start", existing["id"])
        elif state != "true":
            raise AAPError("Unknown container state")
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
                    raise AAPError(
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
                        raise AAPError(
                            f"Plugin startup health returned HTTP {response.status_code}"
                        )
                time.sleep(0.25)
        raise AAPError(
            "Plugin startup timed out after 60 seconds; activation was not submitted"
        )

    def prepare_assets(self, config: ContainerConfig) -> None:
        config.validate()
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
            "aap-libreyolo",
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
            raise AAPError("Container ID differs from the proven container")
        state = self._state(proven_id)
        if not started_at or state["StartedAt"] != started_at:
            raise AAPError(
                "Container process identity differs from the quiescence proof"
            )
        if state["Running"]:
            self._run("container", "stop", proven_id)
            state = self._state(proven_id)
            if state["Running"] or state["StartedAt"] != started_at:
                raise AAPError("Container process identity changed after quiescence")
        self._run("container", "rm", proven_id)
