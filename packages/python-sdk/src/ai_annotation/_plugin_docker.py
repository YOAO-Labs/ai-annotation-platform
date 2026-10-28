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
from dataclasses import dataclass
from pathlib import Path

from ai_annotation.errors import AAPError

_IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}$")
_GPU = re.compile(r"^GPU-[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$")
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_OWNER = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ContainerConfig:
    name: str
    installation_id: str
    owner_hash: str
    image: str
    gpu_uuid: str
    network: str
    host_port: int
    container_port: int
    memory_mb: int
    cpus: float
    env_file: Path
    data_dir: Path
    data_target: str

    def validate(self) -> None:
        if not _NAME.fullmatch(self.name) or not _NAME.fullmatch(self.network):
            raise AAPError("Invalid container name or Docker network")
        if self.network == "host":
            raise AAPError("Host networking is not permitted")
        if not _NAME.fullmatch(self.installation_id) or not _OWNER.fullmatch(self.owner_hash):
            raise AAPError("Invalid container ownership identity")
        if not _IMAGE.fullmatch(self.image):
            raise AAPError("Plugin image must be pinned to a sha256 digest")
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

    def identity(self) -> str:
        fields = (
            self.installation_id, self.owner_hash, self.image, self.gpu_uuid,
            self.network, str(self.host_port), str(self.container_port),
            str(self.memory_mb), str(self.cpus), str(self.env_file),
            str(self.data_dir), self.data_target,
        )
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


class Docker:
    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
        except OSError as exc:
            raise AAPError("Docker CLI is unavailable") from exc
        if result.returncode:
            # Docker errors may contain env values or other sensitive configuration.
            raise AAPError(f"Docker {args[0]} failed (exit {result.returncode})")
        return result.stdout.strip()

    def _inspect(self, config: ContainerConfig) -> dict | None:
        ids = self._run("container", "ls", "-a", "--filter", f"name=^/{config.name}$", "--format", "{{.ID}}")
        if not ids:
            return None
        if len(ids.splitlines()) != 1:
            raise AAPError("Container name is ambiguous")
        raw = self._run("container", "inspect", "--format", '{{json .Config.Labels}}', config.name)
        try:
            labels = json.loads(raw)
        except ValueError as exc:
            raise AAPError("Unable to verify container ownership") from exc
        expected = {"aap.installation": config.installation_id, "aap.owner": config.owner_hash, "aap.image": config.image, "aap.config": config.identity()}
        if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in expected.items()):
            raise AAPError("Container belongs to another installation or has a different configuration")
        image = self._run("container", "inspect", "--format", "{{.Config.Image}}", config.name)
        if image != config.image:
            raise AAPError("Container image does not match the installation receipt")
        raw_config = self._run("container", "inspect", "--format", "{{json .HostConfig}}", config.name)
        raw_mounts = self._run("container", "inspect", "--format", "{{json .Mounts}}", config.name)
        try:
            host = json.loads(raw_config)
            mounts = json.loads(raw_mounts)
        except ValueError as exc:
            raise AAPError("Unable to verify container resource configuration") from exc
        devices = host.get("DeviceRequests") if isinstance(host, dict) else None
        if (not isinstance(host, dict) or host.get("Privileged") is not False
                or host.get("NetworkMode") != config.network
                or host.get("Memory") != config.memory_mb * 1024 * 1024
                or host.get("NanoCpus") != int(config.cpus * 1_000_000_000)
                or host.get("CapAdd") not in (None, [])
                or host.get("Devices") not in (None, [])
                or "ALL" not in (host.get("CapDrop") or [])
                or not any(str(item).startswith("no-new-privileges") for item in (host.get("SecurityOpt") or []))
                or not isinstance(devices, list) or len(devices) != 1
                or not isinstance(devices[0], dict)
                or devices[0].get("DeviceIDs") != [config.gpu_uuid]
                or devices[0].get("Capabilities") != [["gpu"]]):
            raise AAPError("Container resources do not match the approved installation")
        ports = host.get("PortBindings") or {}
        bindings = ports.get(f"{config.container_port}/tcp") if isinstance(ports, dict) else None
        if (not isinstance(bindings, list) or len(bindings) != 1
                or not isinstance(bindings[0], dict)
                or bindings[0].get("HostPort") != str(config.host_port)
                or set(ports) != {f"{config.container_port}/tcp"}):
            raise AAPError("Container port bindings do not match the approved installation")
        if (not isinstance(mounts, list) or len(mounts) != 1
                or not isinstance(mounts[0], dict)
                or mounts[0].get("Type") != "bind"
                or mounts[0].get("Source") != str(config.data_dir)
                or mounts[0].get("Destination") != config.data_target
                or mounts[0].get("RW") is not True):
            raise AAPError("Container mounts do not match the approved installation")
        user = self._run("container", "inspect", "--format", "{{.Config.User}}", config.name)
        entrypoint = self._run("container", "inspect", "--format", "{{json .Config.Entrypoint}}", config.name)
        command = self._run("container", "inspect", "--format", "{{json .Config.Cmd}}", config.name)
        try:
            entry = json.loads(entrypoint)
            cmd = json.loads(command)
        except ValueError as exc:
            raise AAPError("Unable to verify container entrypoint") from exc
        if (user != self._user() or entry != ["aap-libreyolo", "serve", "--host", "0.0.0.0", "--port", "8000"]
                or cmd not in (None, [])):
            raise AAPError("Container user or entrypoint differs from the approved runtime")
        container_id = self._run("container", "inspect", "--format", "{{.Id}}", config.name)
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise AAPError("Container runtime returned an invalid full container ID")
        return {"id": container_id, "name": config.name}

    def _verify_image(self, config: ContainerConfig) -> None:
        raw = self._run("image", "inspect", "--format", "{{json .RepoDigests}}", config.image)
        try:
            digests = json.loads(raw)
        except ValueError as exc:
            raise AAPError("Unable to verify local image digest") from exc
        if not isinstance(digests, list) or config.image not in digests:
            raise AAPError("Local image does not have the approved digest")

    @staticmethod
    def _user() -> str:
        if os.geteuid() == 0:
            raise AAPError("Run plugin deployment as a non-root Docker administrator")
        return f"{os.geteuid()}:{os.getegid()}"

    def verify_running(self, config: ContainerConfig) -> None:
        config.validate()
        self._verify_image(config)
        if self._inspect(config) is None:
            raise AAPError("Active installation has no owned container; refusing to restart without requalification")
        image_id = self._run("image", "inspect", "--format", "{{.Id}}", config.image)
        container_image = self._run("container", "inspect", "--format", "{{.Image}}", config.name)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or image_id != container_image:
            raise AAPError("Running container does not use the verified local image")
        if self._run("container", "inspect", "--format", "{{.State.Running}}", config.name) != "true":
            raise AAPError("Active installation container is not running; refusing to restart without requalification")

    def ensure_started(self, config: ContainerConfig) -> dict:
        config.validate()
        self._verify_image(config)
        existing = self._inspect(config)
        if existing is None:
            self._run(
                "container", "create", "--name", config.name,
                "--label", f"aap.installation={config.installation_id}",
                "--label", f"aap.owner={config.owner_hash}",
                "--label", f"aap.image={config.image}",
                "--user", self._user(),
                "--network", config.network,
                "--gpus", f"device={config.gpu_uuid}",
                "--memory", f"{config.memory_mb}m", "--cpus", str(config.cpus),
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--publish", f"{config.host_port}:{config.container_port}",
                "--label", f"aap.config={config.identity()}",
                "--env-file", str(config.env_file),
                "--mount", f"type=bind,src={config.data_dir},dst={config.data_target}",
                config.image,
            )
            existing = self._inspect(config)
            if existing is None:
                raise AAPError("Created container could not be verified")
        image_id = self._run("image", "inspect", "--format", "{{.Id}}", config.image)
        container_image = self._run("container", "inspect", "--format", "{{.Image}}", config.name)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or image_id != container_image:
            raise AAPError("Running container does not use the verified local image")
        state = self._run("container", "inspect", "--format", "{{.State.Running}}", config.name)
        if state == "false":
            self._run("container", "start", config.name)
        elif state != "true":
            raise AAPError("Unknown container state")
        return {**existing, "image_id": image_id}

    def prepare_assets(self, config: ContainerConfig) -> None:
        config.validate()
        self._verify_image(config)
        self._run(
            "container", "run", "--rm", "--network", config.network,
            "--user", self._user(),
            "--memory", f"{config.memory_mb}m", "--cpus", str(config.cpus),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--env-file", str(config.env_file),
            "--mount", f"type=bind,src={config.data_dir},dst={config.data_target}",
            "--entrypoint", "aap-libreyolo", config.image,
            "prepare-assets", "--size", "t", "--asset-dir", f"{config.data_target}/assets",
        )

    def exists(self, config: ContainerConfig) -> bool:
        config.validate()
        return self._inspect(config) is not None

    def stop_and_remove(self, config: ContainerConfig) -> None:
        config.validate()
        if self._inspect(config) is None:
            return
        state = self._run("container", "inspect", "--format", "{{.State.Running}}", config.name)
        if state == "true":
            self._run("container", "stop", config.name)
        elif state != "false":
            raise AAPError("Unknown container state")
        self._run("container", "rm", config.name)
