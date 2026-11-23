"""Private Compose deployment for the standalone cleanup runtime acceptance."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


_LABEL = "org.aap.acceptance"
_ID_LABEL = "org.aap.acceptance-id"
_OWNER = "module-data-boundary"
_PROJECT_LABEL = "com.docker.compose.project"
_SERVICES = {"assets-cleanup": "10001:10001", "assets-source": "10003:10003"}
_FILES = (
    "docker-compose.assets-cleanup.yml",
    "docker-compose.assets-source.yml",
    "docker-compose.assets-native.yml",
)


class CleanupCompose:
    def __init__(self, root: Path, tmpdir: Path, record_property):
        self.directory = tempfile.TemporaryDirectory(
            dir=tmpdir, prefix="assets-cleanup-"
        )
        self.root = root
        self.path = Path(self.directory.name)
        self.identity = uuid.uuid4().hex[:24]
        self.project = f"aap-cleanup-{self.identity[:12]}"
        self.environment = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.path),
            "DOCKER_CONFIG": str(self.path),
            "LANG": "C.UTF-8",
        }
        self.images = {
            service: f"{self.project}-{service}:test" for service in _SERVICES
        }
        self.secrets: dict[str, set[Path]] = {service: set() for service in _SERVICES}
        self.containers: dict[str, str] = {}
        self.postgres_id: str | None = None
        self.record_property = record_property
        self.receipt: dict = {
            "project": self.project,
            "compose_files": list(_FILES),
            "published_compose_sha256": {
                file: sha256((root / file).read_bytes()).hexdigest() for file in _FILES
            },
        }
        self.files = [*(root / file for file in _FILES), self.path / "private.json"]

    def _docker(self, *args: str, timeout: int = 30) -> str:
        result = subprocess.run(
            ["docker", *args],
            env=self.environment,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            kind = args[0]
            if kind == "compose" and str(self.files[-1]) in args:
                kind += f" {args[args.index(str(self.files[-1])) + 1]}"
            failure = {"command": kind, "exit": result.returncode}
            self.receipt.setdefault("failed_docker_commands", []).append(failure)
            diagnostic = result.stderr
            for paths in self.secrets.values():
                for path in paths:
                    value = path.read_text(encoding="utf-8").strip()
                    diagnostic = diagnostic.replace(value, "<redacted>")
            raise RuntimeError(
                f"owned cleanup Docker {kind} failed (exit {result.returncode}): "
                f"{diagnostic[-2000:]}"
            )
        return (result.stdout + (result.stderr if args[0] == "logs" else "")).strip()

    def _compose(self, *args: str, timeout: int = 90) -> str:
        files = [arg for path in self.files for arg in ("-f", str(path))]
        return self._docker(
            "compose",
            "--env-file",
            "/dev/null",
            "-p",
            self.project,
            *files,
            *args,
            timeout=timeout,
        )

    def _inspect(self, kind: str, identifier: str) -> dict:
        records = json.loads(self._docker(kind, "inspect", identifier))
        if len(records) != 1:
            raise RuntimeError(f"cleanup {kind} identity is not unique")
        return records[0]

    def secret(self, service: str, setting: str, value: str) -> None:
        if not value or "\n" in value or "\r" in value:
            raise RuntimeError(f"invalid cleanup fixture secret for {setting}")
        path = self.path / setting
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o444)
        self.secrets[service].add(path)
        self.environment[setting + "_FILE"] = str(path)

    def _owned(self, labels: dict) -> bool:
        return labels.get(_LABEL) == _OWNER and labels.get(_ID_LABEL) == self.identity

    def _owned_containers(self) -> dict[str, dict]:
        containers = {}
        for identifier in self._docker(
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label={_PROJECT_LABEL}={self.project}",
        ).splitlines():
            observed = self._inspect("container", identifier)
            labels = observed["Config"]["Labels"]
            service = labels.get("com.docker.compose.service")
            if (
                not self._owned(labels)
                or labels.get(_PROJECT_LABEL) != self.project
                or labels.get("com.docker.compose.project.config_files", "").split(",")
                != [str(path) for path in self.files]
                or service not in _SERVICES
                or service in containers
                or observed["Id"] != identifier
            ):
                raise RuntimeError(
                    "refusing to operate on an unowned cleanup container"
                )
            containers[service] = observed
        return containers

    def _owned_network(self) -> dict | None:
        ids = self._docker(
            "network",
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label={_PROJECT_LABEL}={self.project}",
        ).splitlines()
        if not ids:
            return None
        network = self._inspect("network", ids[0])
        labels = network["Labels"]
        if (
            len(ids) != 1
            or network["Id"] != ids[0]
            or network["Name"] != f"{self.project}_default"
            or network["Driver"] != "bridge"
            or not self._owned(labels)
            or labels.get(_PROJECT_LABEL) != self.project
            or labels.get("com.docker.compose.network") != "default"
        ):
            raise RuntimeError("refusing to operate on an unowned cleanup network")
        return network

    def create(self, postgres_id: str) -> None:
        if self._owned_containers() or self._owned_network():
            raise RuntimeError("cleanup Compose project already exists")
        self.receipt["compose_version"] = self._docker("compose", "version", "--short")
        labels = {_LABEL: _OWNER, _ID_LABEL: self.identity}
        override = {
            "services": {
                **{
                    service: {
                        "image": image,
                        "labels": labels,
                        "build": {"labels": labels},
                    }
                    for service, image in self.images.items()
                },
                **{
                    service: {"profiles": ["outside-cleanup-proof"]}
                    for service in (
                        "assets-media-control",
                        "assets-images-worker",
                        "celery-worker-image-pyramid",
                    )
                },
            },
            "networks": {"default": {"labels": labels}},
        }
        self.files[-1].write_text(json.dumps(override), encoding="utf-8")
        self.receipt["private_override_sha256"] = sha256(
            self.files[-1].read_bytes()
        ).hexdigest()
        self._compose("create", "--build", *self.images, timeout=900)
        containers = self._owned_containers()
        if containers.keys() != _SERVICES.keys():
            raise RuntimeError("Compose did not create both cleanup and signer")
        self.containers = {
            service: observed["Id"] for service, observed in containers.items()
        }
        self.receipt["created_containers"] = self.containers.copy()
        network = self._owned_network()
        if network is None or set(network.get("Containers") or {}) - set(
            self.containers.values()
        ):
            raise RuntimeError("cleanup Compose network was not exclusively owned")
        self.postgres_id = postgres_id
        self.receipt["bridge_network"] = network["Id"]
        self.receipt["postgres_container"] = postgres_id
        self._docker(
            "network", "connect", "--alias", "cleanup-db", network["Id"], postgres_id
        )
        self._compose("start", "assets-source", "assets-cleanup")

    @staticmethod
    def _status(observed: dict) -> dict:
        state = observed["State"]
        return {
            "running": state["Running"],
            "status": state["Status"],
            "exit_code": state["ExitCode"],
            "oom_killed": state["OOMKilled"],
            "health": state.get("Health", {}).get("Status"),
        }

    async def wait_ready(self, service: str) -> None:
        deadline = time.monotonic() + 120
        identifier = self.containers[service]
        while time.monotonic() < deadline:
            observed = await asyncio.to_thread(self._inspect, "container", identifier)
            status = self._status(observed)
            self.receipt.setdefault("readiness", {})[service] = status
            if status["running"] and status["health"] == "healthy":
                return
            if not status["running"] or status["health"] == "unhealthy":
                break
            await asyncio.sleep(0.5)
        raise RuntimeError(f"{service} did not become ready: {status}")

    def verify(self) -> str:
        network = self._owned_network()
        containers = self._owned_containers()
        members = set(self.containers.values()) | {self.postgres_id}
        if network is None or set(network["Containers"]) != members:
            raise RuntimeError("cleanup Compose network membership did not match")
        evidence = self.receipt.setdefault("services", {})
        for service, observed in containers.items():
            config = observed["Config"]
            host = observed["HostConfig"]
            labels = config["Labels"]
            image = self._inspect("image", self.images[service])
            mounted = [
                mount for mount in observed["Mounts"] if mount["Source"] != "/dev/null"
            ]
            environment = dict(entry.split("=", 1) for entry in config["Env"])
            if not (
                config["User"] == _SERVICES[service]
                and config["Image"] == self.images[service]
                and image["Id"] == observed["Image"]
                and self._owned(image["Config"]["Labels"])
                and host["ReadonlyRootfs"]
                and host["CapDrop"] == ["ALL"]
                and "no-new-privileges:true" in host["SecurityOpt"]
                and host["Privileged"] is False
                and not host["PortBindings"]
                and set(observed["NetworkSettings"]["Networks"]) == {network["Name"]}
                and {Path(mount["Source"]) for mount in mounted}
                == self.secrets[service]
                and all(
                    mount["Type"] == "bind"
                    and not mount["RW"]
                    and mount["Destination"].startswith("/run/secrets/")
                    for mount in mounted
                )
                and all(
                    setting + "_FILE" in environment and setting not in environment
                    for setting in (path.name for path in self.secrets[service])
                )
                and not any(
                    key.startswith(
                        ("DATABASE_URL", "REDIS_URL", "CELERY_", "MINIO_", "AWS_")
                    )
                    for key in environment
                )
            ):
                raise RuntimeError(
                    f"{service} does not match the standalone Compose contract"
                )
            evidence[service] = {
                "container": observed["Id"],
                "image": image["Id"],
                "compose_config_hash": labels["com.docker.compose.config-hash"],
                "uid_gid": config["User"],
                "published_ports": [],
                "secret_mounts": sorted(mount["Destination"] for mount in mounted),
            }
        source = containers["assets-source"]["NetworkSettings"]["Networks"][
            network["Name"]
        ]
        address = ipaddress.ip_address(source["IPAddress"])
        if not address.is_private or address.version != 4:
            raise RuntimeError("the Native signer is not on an owned private address")
        return f"http://{address}:8767"

    def stop_worker(self) -> None:
        observed = self._owned_containers()["assets-cleanup"]
        self._compose("stop", "assets-cleanup")
        status = self._status(self._inspect("container", observed["Id"]))
        self.receipt["worker_stopped_for_recovery"] = status
        if (
            status["running"]
            or status["oom_killed"]
            or status["exit_code"] not in {0, 143}
        ):
            raise RuntimeError("cleanup worker did not stop cleanly")

    def restart_worker(self) -> None:
        self._owned_containers()
        self._compose("start", "assets-cleanup")
        if (
            self._owned_containers()["assets-cleanup"]["Id"]
            != self.containers["assets-cleanup"]
        ):
            raise RuntimeError("Compose did not restart the same cleanup container")
        self.receipt["restarted_same_container"] = True

    def worker_log_contains(self, message: str) -> bool:
        return message in self._docker(
            "logs", "--tail", "100", self.containers["assets-cleanup"]
        )

    def close(self) -> None:
        try:
            if not self.files[-1].exists():
                return
            containers = self._owned_containers()
            network = self._owned_network()
            if network is not None and set(network.get("Containers") or {}) - (
                {observed["Id"] for observed in containers.values()}
                | {self.postgres_id}
            ):
                raise RuntimeError(
                    "refusing to remove cleanup network with unowned endpoints"
                )
            if containers:
                self._compose("stop", *containers)
                self.receipt["service_exit_status"] = {
                    service: self._status(observed)
                    for service, observed in self._owned_containers().items()
                }
            if network is not None and self.postgres_id in (
                network.get("Containers") or {}
            ):
                self._docker("network", "disconnect", network["Id"], self.postgres_id)
            if containers or network is not None:
                self._compose("down")
            if self._owned_containers() or self._owned_network():
                raise RuntimeError(
                    "cleanup Compose containers or network were not retired"
                )
            for image_name in self.images.values():
                if self._docker("image", "ls", "-q", image_name):
                    image = self._inspect("image", image_name)
                    if (
                        not self._owned(image["Config"]["Labels"])
                        or image_name not in image["RepoTags"]
                    ):
                        raise RuntimeError(
                            "refusing to remove an unowned cleanup image"
                        )
                    self._docker("image", "rm", image_name)
                if self._docker("image", "ls", "-q", image_name):
                    raise RuntimeError("cleanup Compose image was not retired")
            self.receipt["retired_compose_resources"] = True
        finally:
            self.record_property(
                "cleanup_compose", json.dumps(self.receipt, sort_keys=True)
            )
            self.directory.cleanup()
