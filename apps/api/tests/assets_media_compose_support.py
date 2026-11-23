"""Start the published Media services on verifier-owned resources."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from tests.test_asset_storage_cleanup_runtime import (
    _container_s3_endpoint,
    _docker,
    _inspect_docker,
    _required_env,
    _verify_postgres,
)

_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE = _ROOT / "docker-compose.assets-media.yml"
_PROJECT_LABEL = "com.docker.compose.project"
_SERVICE_LABEL = "com.docker.compose.service"
_OWNER_LABEL = "org.aap.acceptance-id"
_DATABASE_ALIAS = "media-test-postgres"


@dataclass(frozen=True)
class _Role:
    service: str
    profile: str
    module: str
    database_setting: str
    secret_prefix: str
    cpu: int
    memory: int
    stop_seconds: int
    selector: str | None = None


CONTROL = _Role(
    "assets-media-control",
    "assets-media-control",
    "yoao_assets_media_runtime",
    "ASSETS_MEDIA_CONTROL_DATABASE_URL",
    "media_control",
    500_000_000,
    512 * 1024**2,
    30,
)
VIDEO = _Role(
    "assets-media-worker",
    "assets-video-executor",
    "yoao_assets_media_runtime.worker",
    "ASSETS_MEDIA_WORKER_DATABASE_URL",
    "media_worker",
    2_000_000_000,
    2 * 1024**3,
    60,
    "ASSETS_VIDEO_EXECUTOR",
)
IMAGE = _Role(
    "assets-images-worker",
    "assets-image-executor",
    "yoao_assets_images_runtime.worker",
    "ASSETS_IMAGES_DATABASE_URL",
    "images",
    4_000_000_000,
    16 * 1024**3,
    120,
    "ASSETS_IMAGE_EXECUTOR",
)


class MediaCompose:
    def __init__(self, directory, settings, buckets, record_property, *, policy=None):
        self.project = _required_env("AAP_ASSETS_MEDIA_COMPOSE_PROJECT")
        assert re.fullmatch(r"aap-media-q-[0-9a-f]{12}", self.project)
        self.owner = uuid4().hex
        self.directory = directory / "media-compose"
        self.directory.mkdir(mode=0o700)
        self.override = self.directory / "ownership.json"
        self.record = record_property
        self.pg = _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID")
        self.network = None
        self.roles = {}
        self.services = {}
        self.secret_paths = {}
        self._secret_values = []
        self.containers = {}
        self.image_ids = {}
        self.public_s3 = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
        self.env = (
            {
                name: os.environ[name]
                for name in ("PATH", "HOME", "LANG")
                if name in os.environ
            }
            | {
                "ASSETS_MEDIA_S3_ENDPOINT": _container_s3_endpoint(settings),
                "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": self.public_s3,
                "ASSETS_MEDIA_DATASETS_BUCKET": buckets["MINIO_DATASETS_BUCKET"],
                "ASSETS_MEDIA_CACHE_BUCKET": buckets["MINIO_MEDIA_CACHE_BUCKET"],
                "ASSETS_MEDIA_INPUTS_BUCKET": buckets["MINIO_INPUTS_BUCKET"],
                "ASSETS_IMAGES_LEGACY_BUCKET": buckets["MINIO_BUCKET"],
                "ASSETS_MEDIA_POLL_SECONDS": "0.2",
                "ASSETS_IMAGES_POLL_SECONDS": "0.2",
            }
            | (policy or {})
        )

    def _secret(self, key, setting, value):
        path = self.directory / key
        with open(path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            stream.write(value)
        subprocess.run(
            ["setfacl", "-m", "u:10005:r--", str(path)],
            capture_output=True,
            check=True,
            timeout=5,
        )
        self.env[setting + "_FILE"] = str(path)
        self._secret_values.append(value)
        return path

    def add(
        self, role, database_url, access_key, secret_key, *, token=None, signer=None
    ):
        _verify_postgres(database_url)
        self.roles[role.service] = role
        self.secret_paths[role.service] = files = {}
        database = database_url.set(
            drivername="postgresql", host=_DATABASE_ALIAS, port=5432
        ).render_as_string(hide_password=False)
        prefix = f"ASSETS_{role.secret_prefix.upper()}_S3"
        for suffix, setting, value in (
            ("database_url", role.database_setting, database),
            ("s3_access_key", prefix + "_ACCESS_KEY", access_key),
            ("s3_secret_key", prefix + "_SECRET_KEY", secret_key),
        ):
            key = f"{role.secret_prefix}_{suffix}"
            files[f"/run/secrets/{key}"] = self._secret(key, setting, value)
        service = self.services[role.service] = {"labels": {_OWNER_LABEL: self.owner}}
        if role is CONTROL:
            assert token is not None
            files["/run/secrets/media_token"] = self._secret(
                "media_token", "ASSETS_MEDIA_TOKEN", token
            )
            service["ports"] = ["127.0.0.1::8768"]
        if role.selector:
            self.env[role.selector] = "split"
        if signer is not None:
            assert role in (CONTROL, IMAGE)
            signer.assert_running()
            binding, signer_token = (
                ("ASSETS_MEDIA_NATIVE_IMAGE_STORAGE", signer.reader_token)
                if role is CONTROL
                else ("ASSETS_IMAGES_NATIVE_STORAGE", signer.writer_token)
            )
            key = f"{role.secret_prefix}_native_storage_token"
            target = f"/run/secrets/{key}"
            files[target] = self._secret(key, binding + "_TOKEN", signer_token)
            service["environment"] = {
                binding + "_URL": signer.url,
                binding + "_TOKEN_FILE": target,
            }
            service["secrets"] = [key]

    def _compose(self, *arguments, timeout=90):
        flags = [
            arg for role in self.roles.values() for arg in ("--profile", role.profile)
        ]
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                "/dev/null",
                "-p",
                self.project,
                "-f",
                str(_COMPOSE),
                "-f",
                str(self.override),
                *flags,
                *arguments,
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            diagnostic = self._redact(result.stderr)[-3000:]
            raise AssertionError(f"media Compose {arguments[0]} failed: {diagnostic}")
        return result.stdout.strip()

    def _redact(self, diagnostic):
        for value in self._secret_values:
            diagnostic = diagnostic.replace(value, "<redacted>")
        return re.sub(r"(?i)(X-Amz-[\w-]+)=[^&\s]+", r"\1=<redacted>", diagnostic)

    def _owned_labels(self, observed, service=None):
        labels = observed.get("Config", observed).get("Labels") or {}
        assert labels.get(_PROJECT_LABEL) == self.project
        assert labels.get(_OWNER_LABEL) == self.owner
        if service:
            assert labels.get(_SERVICE_LABEL) == service
            assert labels.get("com.docker.compose.project.config_files", "").split(
                ","
            ) == [str(_COMPOSE), str(self.override)]

    def create(self):
        assert self.roles and not self.override.exists()
        assert not _docker(
            "ps", "-aq", "--filter", f"label={_PROJECT_LABEL}={self.project}"
        )
        assert not _docker(
            "network", "ls", "-q", "--filter", f"label={_PROJECT_LABEL}={self.project}"
        )
        secrets = {
            path.name: {"file": str(path)}
            for service, spec in self.services.items()
            for path in self.secret_paths[service].values()
            if path.name in spec.get("secrets", [])
        }
        self.override.write_text(
            json.dumps(
                {
                    "services": self.services,
                    "secrets": secrets,
                    "networks": {"default": {"labels": {_OWNER_LABEL: self.owner}}},
                }
            ),
            encoding="utf-8",
        )
        for service in self.roles:
            ref = f"{self.project}-{service}:latest"
            image = json.loads(_docker("image", "inspect", ref))[0]
            labels = image["Config"]["Labels"]
            assert (
                labels[_PROJECT_LABEL] == self.project
                and labels[_SERVICE_LABEL] == service
            )
            self.image_ids[service] = image["Id"]
        self._compose("create", "--no-build", "--pull", "never", *self.roles)
        network = _inspect_docker(f"{self.project}_default")
        self._owned_labels(network)
        assert network["Driver"] == "bridge" and not network["Containers"]
        self.network = network["Id"]
        _docker("network", "connect", "--alias", _DATABASE_ALIAS, self.network, self.pg)
        self.record("media_compose_project", self.project)
        self.record("media_compose_network", self.network)

    def start(self, role):
        self._compose("up", "-d", "--no-build", "--pull", "never", role.service)
        container_id = self._compose("ps", "--no-trunc", "-q", role.service)
        assert re.fullmatch(r"[0-9a-f]{64}", container_id)
        self.containers[role.service] = container_id
        observed = self.verify(role)
        self.record(
            role.service,
            json.dumps(
                {
                    "id": container_id,
                    "image": observed["Image"],
                    "profile": role.profile,
                    "user": observed["Config"]["User"],
                    "cpu": role.cpu,
                    "memory": role.memory,
                    "secret_files": sorted(self.secret_paths[role.service]),
                },
                sort_keys=True,
            ),
        )
        return container_id

    def verify(self, role):
        container_id = self.containers[role.service]
        observed = _inspect_docker(container_id)
        self._owned_labels(observed, role.service)
        config, host = observed["Config"], observed["HostConfig"]
        assert (
            observed["Id"] == container_id
            and observed["Image"] == self.image_ids[role.service]
        )
        assert config["User"] == "10005:10005"
        assert config["Cmd"] == ["python", "-m", role.module]
        assert config["Labels"]["com.docker.compose.oneoff"].lower() == "false"
        assert config["StopTimeout"] == role.stop_seconds
        assert host["NanoCpus"] == role.cpu and host["Memory"] == role.memory
        assert (
            host["ReadonlyRootfs"]
            and not host["Privileged"]
            and host["CapDrop"] == ["ALL"]
        )
        assert "no-new-privileges:true" in host["SecurityOpt"]
        assert set(host["Tmpfs"]) == {"/tmp"}
        env = dict(value.split("=", 1) for value in config["Env"])
        assert not set(env) & {
            "DATABASE_URL",
            "ASSETS_MEDIA_TOKEN",
            "ASSETS_MEDIA_CONTROL_DATABASE_URL",
            "ASSETS_MEDIA_WORKER_DATABASE_URL",
            "ASSETS_IMAGES_DATABASE_URL",
            "ASSETS_MEDIA_S3_ACCESS_KEY",
            "ASSETS_MEDIA_S3_SECRET_KEY",
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN",
            "ASSETS_IMAGES_NATIVE_STORAGE_TOKEN",
        }
        assert not any(
            name.startswith(("MINIO_", "AWS_", "CELERY_", "REDIS_", "APP_"))
            for name in env
        )
        if role.selector:
            assert env[role.selector] == "split"
        files = self.secret_paths[role.service]
        assert set(files) == {
            value for value in env.values() if value.startswith("/run/secrets/")
        }
        mounts = observed["Mounts"]
        assert {mount["Destination"] for mount in mounts} == set(files)
        assert all(
            not mount["RW"]
            and mount["Type"] == "bind"
            and mount["Source"] == str(files[mount["Destination"]])
            for mount in mounts
        )
        networks = observed["NetworkSettings"]["Networks"]
        assert [network["NetworkID"] for network in networks.values()] == [self.network]
        ports = {
            key: value
            for key, value in (observed["NetworkSettings"]["Ports"] or {}).items()
            if value
        }
        if role is CONTROL:
            assert list(ports) == ["8768/tcp"]
            assert (
                len(ports["8768/tcp"]) == 1
                and ports["8768/tcp"][0]["HostIp"] == "127.0.0.1"
            )
        else:
            assert not ports
        probe = _docker(
            "exec",
            container_id,
            "python",
            "-c",
            "import importlib, os, pathlib; "
            f"importlib.import_module('{role.module}'); "
            f"assert all(p.read_bytes() for p in map(pathlib.Path, {list(files)!r})); "
            "s=open('/proc/self/status').read().splitlines(); "
            "print(os.getuid(), os.getgid(), "
            "next(x.split()[1] for x in s if x.startswith('CapEff:')), "
            "next(x.split()[1] for x in s if x.startswith('NoNewPrivs:')))",
        )
        uid, gid, capabilities, no_new_privileges = probe.split()
        assert (uid, gid, int(capabilities, 16), no_new_privileges) == (
            "10005",
            "10005",
            0,
            "1",
        )
        return observed

    def origin(self):
        observed = _inspect_docker(self.containers[CONTROL.service])
        port = observed["NetworkSettings"]["Ports"]["8768/tcp"][0]["HostPort"]
        return f"http://127.0.0.1:{int(port)}"

    def stop(self, role):
        self._owned_labels(_inspect_docker(self.containers[role.service]), role.service)
        self._compose("stop", role.service, timeout=role.stop_seconds + 20)

    def restart(self, role):
        container_id = self.containers[role.service]
        previous = _inspect_docker(container_id)
        self._owned_labels(previous, role.service)
        self._compose("restart", role.service, timeout=role.stop_seconds + 20)
        current = self.verify(role)
        assert current["State"]["StartedAt"] != previous["State"]["StartedAt"]
        self.record(f"{role.service}_restarted", container_id)

    def logs(self, container_id):
        captured = subprocess.run(
            ["docker", "logs", "--tail", "80", container_id],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return self._redact(captured.stdout + captured.stderr)

    def close(self):
        if not self.override.exists():
            return
        filters = ("--filter", f"label={_PROJECT_LABEL}={self.project}")
        ids = _docker("ps", "-aq", *filters).splitlines()
        for container_id in ids:
            observed = _inspect_docker(container_id)
            self._owned_labels(observed)
            service = observed["Config"]["Labels"][_SERVICE_LABEL]
            assert service in self.roles
            self._owned_labels(observed, service)
        for network_id in _docker("network", "ls", "-q", *filters).splitlines():
            network = _inspect_docker(network_id)
            self._owned_labels(network)
            assert network["Name"] == f"{self.project}_default"
            if self.pg in network["Containers"]:
                _docker("network", "disconnect", network["Id"], self.pg)
        self._compose("down", "--volumes", timeout=160)
        assert not _docker("ps", "-aq", *filters)
        assert not _docker("network", "ls", "-q", *filters)
        for files in self.secret_paths.values():
            for path in files.values():
                path.unlink()
        self.record("media_compose_removed", self.project)
