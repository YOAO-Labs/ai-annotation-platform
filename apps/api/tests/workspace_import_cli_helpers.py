"""Owned one-shot API-image runner for the nuScenes import CLI acceptance."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import select
import secrets
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

from sqlalchemy.engine import URL, make_url

from tests.test_asset_storage_cleanup_runtime import (
    _shared_bootstrap_minio,
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_identity_sessions_live import _verify_redis
from tests.test_labeling_module_api_cold import (
    _minio_dependency,
    _verify_minio_identity,
)


ROOT = Path(__file__).resolve().parents[3]
_RETAINED_FIXTURE_CLIENTS = []
_EMPTY_SELECTOR_PREFIXES = (
    "IDENTITY_AUTH",
    "IDENTITY_KEY",
    "IDENTITY_SESSION",
    "IDENTITY_ACCOUNT",
    "IDENTITY_GROUP",
    "IDENTITY_DIRECTORY",
    "IDENTITY_ADMIN_PASSWORD",
    "IDENTITY_PLATFORM_ROLE",
    "IDENTITY_SUSPENSION",
    "IDENTITY_DELETION",
    "IDENTITY_OFFBOARDING",
    "IDENTITY_BRIEFS",
    "IDENTITY_INVITATION",
    "IDENTITY_DEACTIVATION",
    "IDENTITY_DEACTIVATION_OPERATOR",
    "IDENTITY_PRESENCE",
    "IDENTITY_RECOVERY",
    "IDENTITY_MAIL",
    "IDENTITY_MAIL_V2",
    "IDENTITY_MAIL_SETTINGS",
    "NOTIFICATION_PUBLISH",
    "WORKSPACE_MEMBERSHIP",
    "WORKSPACE_OWNER_TRANSFER",
    "WORKSPACE_CONFIGURATION",
    "WORKSPACE_TEMPLATE_QUERY",
    "WORKSPACE_PROJECT_QUERY",
    "WORKSPACE_DIRECTORY_QUERY",
    "LABELING_CLASS_QUERY",
    "LABELING_VIDEO_WRITER",
    "LABELING_MASK_GC",
    "INFERENCE_PIPELINES",
    "INFERENCE_BACKEND_PROBE",
    "INFERENCE_HEALTH_OBSERVATION",
    "INFERENCE_REGISTRY_INGRESS",
    "INFERENCE_ROUTING",
    "MEDIA_PROBE",
    "ASSETS_CATALOG",
    "ASSETS_SOURCE",
    "ASSETS_MEDIA",
    "ASSETS_MASK_CONTENT",
    "ASSETS_MASK_CLEANUP",
    "PREANNOTATION_RESULTS",
)
_EMPTY_SELECTOR_SUFFIXES = (
    "_URL",
    "_TOKEN",
    "_TOKEN_FILE",
    "_DATABASE_URL",
    "_DATABASE_URL_FILE",
)
_ORIGINAL_READER_KEYS = (
    "AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT",
    "AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET",
    "AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY",
    "AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY",
)


@dataclass(frozen=True)
class CliRun:
    exit_code: int
    stdout: str
    stderr: str
    diagnostics_path: str
    timed_out: bool
    externally_stopped: bool

    @property
    def evidence(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "diagnostics_path": self.diagnostics_path,
            "timed_out": self.timed_out,
            "externally_stopped": self.externally_stopped,
        }


class WorkspaceImportCli:
    """Run the installed CLI image with one read-only fixture and owned peers."""

    def __init__(
        self,
        test_engine,
        tmp_path: Path,
        *,
        source_root: Path,
        buckets: dict[str, str],
        minio_authority: str,
        minio_use_ssl: bool,
        s3_access_key: str,
        s3_secret_key: str,
        runtime_database_url: URL | None = None,
        identity_auth_database_url: URL | None = None,
        fixture_run_ids: frozenset[uuid.UUID] | None = None,
        fixture_family: str | None = None,
        original_reader: bool = False,
        managed_original_writer: bool = False,
    ) -> None:
        if (fixture_run_ids is None) != (fixture_family is None) or (
            fixture_run_ids is not None
            and (
                not fixture_run_ids
                or any(not isinstance(value, uuid.UUID) for value in fixture_run_ids)
                or fixture_family != "scale"
                or runtime_database_url is not None
                or identity_auth_database_url is not None
                or os.geteuid() == 0
            )
        ):
            raise ValueError("Private fixture CLI needs its exact family and run IDs")
        if original_reader and fixture_family is not None:
            raise ValueError("Original-reader and private owner profiles are separate")
        if managed_original_writer and (original_reader or fixture_family is not None):
            raise ValueError(
                "Managed-original writer requires its own consumer profile"
            )
        self.fixture_run_ids = fixture_run_ids
        self.fixture_family = fixture_family
        self.host_network = (
            fixture_family is not None or original_reader or managed_original_writer
        )
        self.original_reader_environment = {
            name: self._required(name) if original_reader else ""
            for name in _ORIGINAL_READER_KEYS
        }
        self.fixture_metadata: dict[Path, str] = {}
        self.fixture_resource_root: Path | None = None
        self.test_engine = test_engine
        self.source_root = Path(source_root).resolve(strict=True)
        if self.source_root.is_symlink() or not self.source_root.is_dir():
            raise RuntimeError("nuScenes fixture root must be an owned real directory")
        parsed_minio = urlsplit(f"http://{minio_authority}")
        if (
            not parsed_minio.hostname
            or parsed_minio.username
            or parsed_minio.password
            or parsed_minio.path not in {"", "/"}
            or parsed_minio.query
            or parsed_minio.fragment
        ):
            raise ValueError("Verifier supplied an invalid MinIO authority")
        shared_minio = _shared_bootstrap_minio(
            SimpleNamespace(
                minio_endpoint=minio_authority, minio_use_ssl=minio_use_ssl
            ),
            container_id=os.environ.get("AAP_MODULE_DATA_MINIO_CONTAINER_ID"),
        )
        if shared_minio is None and parsed_minio.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError(
                "Owned MinIO must have a verified bootstrap or loopback endpoint"
            )
        self.minio_authority = (
            parsed_minio.netloc if self.host_network else "workspace-import-minio:9000"
        )
        self.minio_use_ssl = minio_use_ssl
        self.buckets = dict(buckets)
        if (
            len(self.buckets) != 10
            or "MINIO_BUCKET" not in self.buckets
            or "MINIO_DATASETS_BUCKET" not in self.buckets
            or any(
                not name.startswith("MINIO_")
                or not name.endswith("_BUCKET")
                or not value
                for name, value in self.buckets.items()
            )
        ):
            raise RuntimeError("Verifier did not provide all ten owned bucket names")
        self.s3_access_key = s3_access_key
        self.s3_secret_key = s3_secret_key
        self.identity = uuid.uuid4().hex[:20]
        self.secret_key = secrets.token_urlsafe(32)
        self.network_name = f"aap-workspace-import-{self.identity}"
        self.directory = Path(tmp_path).resolve() / self.network_name
        self.environment_file = self.directory / "cli.env"
        self.network_id: str | None = None
        self.container_id: str | None = None
        self._current_container_stopped = False
        self._docker_environment = self._minimal_docker_environment()

        database_name = str(test_engine.url.database or "")
        if not database_name.endswith("_test"):
            raise RuntimeError("nuScenes CLI requires the disposable test database")
        _verify_postgres(test_engine.url)
        self.redis_owner_url = _verify_redis()
        self.minio = None
        if not self.host_network or managed_original_writer:
            resource_url = make_url(self._required("AAP_MODULE_DATA_ASSET_CLEANUP_URL"))
            if (resource_url.host, resource_url.port) != (
                test_engine.url.host,
                test_engine.url.port,
            ):
                raise RuntimeError(
                    "CLI database and owned store use different verifiers"
                )
            resources = _verified_resource_manifest(
                SimpleNamespace(url=resource_url), resource_url
            )
            if resources["buckets"] != self.buckets:
                raise RuntimeError(
                    "CLI buckets differ from the verified resource manifest"
                )
            self.minio = _minio_dependency(resources)
            if (parsed_minio.port, minio_use_ssl) != (
                self.minio["host_port"],
                self.minio["ssl"],
            ):
                raise RuntimeError("CLI MinIO endpoint differs from the verified store")
            if managed_original_writer:
                from app.config import settings

                if minio_authority != settings.minio_endpoint:
                    raise RuntimeError(
                        "CLI managed-original endpoint differs from its catalog"
                    )
        self.database_name = database_name
        self.database_url = self._runtime_database_url(runtime_database_url)
        self.identity_auth_database_url = ""
        if identity_auth_database_url is not None:
            auth = identity_auth_database_url
            if (
                auth.get_backend_name() != "postgresql"
                or auth.username != "yoao_identity_auth_client"
                or not auth.password
                or auth.query
                or (auth.host, auth.port, auth.database)
                != (
                    test_engine.url.host,
                    test_engine.url.port,
                    test_engine.url.database,
                )
            ):
                raise RuntimeError(
                    "Identity authentication must target the owned test DB"
                )
            self.identity_auth_database_url = auth.set(
                drivername="postgresql",
                host=self.database_url.host,
                port=self.database_url.port,
            ).render_as_string(hide_password=False)
        self.postgres_container = self._required("AAP_MODULE_DATA_PG_CONTAINER_ID")
        self.redis_container = self._required("AAP_MODULE_DATA_REDIS_CONTAINER_NAME")
        self.image = self._required("AAP_WORKSPACE_MODULE_API_IMAGE")
        self.image_identity = self._required("AAP_WORKSPACE_MODULE_API_IDENTITY")
        self.image_record = self._verify_image()
        self.redis_url = (
            self.redis_owner_url.render_as_string(hide_password=False)
            if self.host_network
            else self._docker_redis_url(self.redis_owner_url)
        )

    def start(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        if self.fixture_family is not None:
            self._prepare_private_fixture_context()
            self._assert_transport()
            return
        self._write_environment_file()
        if self.host_network:
            self.network_id = "host"
            self._assert_transport()
            return
        self.network_id = self._docker(
            "network",
            "create",
            "--label",
            "org.aap.acceptance=workspace-import-cli",
            "--label",
            f"org.aap.acceptance-id={self.identity}",
            self.network_name,
        )
        _verify_postgres(self.test_engine.url)
        _verify_redis()
        self._docker(
            "network",
            "connect",
            "--gw-priority",
            "-1",
            "--alias",
            "workspace-import-postgres",
            self.network_id,
            self.postgres_container,
        )
        self._docker(
            "network",
            "connect",
            "--gw-priority",
            "-1",
            "--alias",
            "workspace-import-redis",
            self.network_id,
            self.redis_container,
        )
        _verify_minio_identity(self._inspect(self.minio["id"]), self.minio)
        self._docker(
            "network",
            "connect",
            "--gw-priority",
            "-1",
            "--alias",
            "workspace-import-minio",
            self.network_id,
            self.minio["id"],
        )
        self.assert_private_network(
            {
                self._peer_id(self.postgres_container),
                self._peer_id(self.redis_container),
                self.minio["id"],
            }
        )

    def run(self, *, dataset_name: str) -> CliRun:
        if self.network_id is None:
            raise RuntimeError("nuScenes CLI network has not been started")
        if not dataset_name or "\n" in dataset_name or "\x00" in dataset_name:
            raise ValueError("Dataset name must be non-empty single-line text")
        return self.run_command(
            (
                "python",
                "-m",
                "scripts.import_nuscenes_scene",
                "--nuscenes-root",
                "/fixture",
                "--scene-tokens",
                "scene-0000,scene-0001",
                "--dataset-name",
                dataset_name,
                "--frame",
                "ego",
            )
        )

    def run_command(self, command: tuple[str, ...]) -> CliRun:
        """Run one explicit argv through the same owned one-shot container."""
        if self.network_id is None:
            raise RuntimeError("nuScenes CLI network has not been started")
        if not command or any(
            type(argument) is not str
            or not argument
            or "\n" in argument
            or "\x00" in argument
            for argument in command
        ):
            raise ValueError("CLI command must be non-empty single-line arguments")
        if self.fixture_family == "scale":
            if (
                command[:4] != ("python", "-m", "scripts.seed_scale", "--run-id")
                or len(command) < 5
                or command[4] not in {str(value) for value in self.fixture_run_ids}
                or command[5:] not in (("4", "2", "1"), ("--purge",))
            ):
                raise ValueError("Fixture owner may only seed or purge its exact runs")
        self._assert_transport()
        metadata_mounts = [
            value
            for path in self.fixture_metadata
            for value in ("--mount", f"type=bind,source={path},target={path},readonly")
        ]
        run_id = uuid.uuid4().hex[:12]
        container_name = f"{self.network_name}-{run_id}"
        container_id = self._docker(
            "create",
            "--name",
            container_name,
            "--label",
            "org.aap.acceptance=workspace-import-cli",
            "--label",
            f"org.aap.acceptance-id={self.identity}",
            "--label",
            f"org.aap.run-id={run_id}",
            "--label",
            f"org.aap.source-revision={self.image_record['source_revision']}",
            "--network",
            self.network_id,
            "--add-host",
            "host.docker.internal:host-gateway",
            "--env-file",
            str(self.environment_file),
            "--mount",
            f"type=bind,source={self.source_root},target=/fixture,readonly",
            *metadata_mounts,
            "--user",
            f"{os.geteuid()}:{os.getegid()}"
            if self.fixture_family is not None
            else "10003:10003",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "--workdir",
            "/app",
            self.image,
            *command,
        )
        self.container_id = container_id
        self._current_container_stopped = False
        start_result = None
        start_error = None
        timed_out = False
        try:
            self._verify_container(self._inspect(container_id), run_id=run_id)
            self._assert_transport()
            try:
                start_result = subprocess.run(
                    ["docker", "start", "--attach", container_id],
                    env=self._docker_environment,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                timed_out = True
                start_error = "container execution exceeded 180 seconds"
                if error.stdout:
                    stdout = self._decode_output(error.stdout)
                else:
                    stdout = ""
                if error.stderr:
                    stderr = self._decode_output(error.stderr)
                else:
                    stderr = ""
                self._docker("stop", "--time", "5", container_id, timeout=15)
                start_result = subprocess.CompletedProcess(
                    args=["docker", "start", "--attach", container_id],
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr,
                )

            wait_output = self._docker("wait", container_id, timeout=30)
            try:
                exit_code = int(wait_output)
            except ValueError:
                raise RuntimeError(
                    "Docker did not return the CLI process exit code"
                ) from None
            record = self._inspect(container_id)
            self._verify_container(record, run_id=run_id)
            self._assert_transport()
            stdout = self.redact(start_result.stdout or "")
            stderr = self.redact(start_result.stderr or "")
            if start_error:
                stderr = self.redact(f"{stderr}\n{start_error}".strip())
            diagnostics = {
                "container_id": record.get("Id"),
                "image_id": record.get("Image"),
                "run_id": run_id,
                "fixture_family": self.fixture_family,
                "fixture_run_ids": sorted(map(str, self.fixture_run_ids or ())),
                "docker_wait_exit_code": exit_code,
                "docker_attach_exit_code": start_result.returncode,
                "timed_out": timed_out,
                "externally_stopped": self._current_container_stopped,
                "stdout": stdout,
                "stderr": stderr,
            }
            path = (
                ROOT
                / ".worktree"
                / f"workspace-import-cli-{self.identity}-{run_id}.json"
            )
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            return CliRun(
                exit_code,
                stdout,
                stderr,
                str(path),
                timed_out,
                self._current_container_stopped,
            )
        finally:
            record = self._inspect(container_id, required=False)
            if record is not None:
                self._verify_container(record, run_id=run_id)
                if (record.get("State") or {}).get("Running"):
                    self._docker("stop", "--time", "5", container_id, timeout=15)
                self._docker("rm", container_id, timeout=30)
            self.container_id = None
            self._assert_transport()

    def running_database_client_address(self) -> str:
        """Return the verified transport endpoint used by the running CLI."""
        if self.container_id is None:
            raise RuntimeError("No nuScenes CLI container is currently running")
        record = self._inspect(self.container_id)
        self._verify_container(record)
        if not (record.get("State") or {}).get("Running"):
            raise RuntimeError("nuScenes CLI container has already exited")
        if self.host_network:
            self._assert_transport()
            postgres = self._inspect(self.postgres_container)
            bridge = (postgres.get("NetworkSettings", {}).get("Networks") or {}).get(
                "bridge", {}
            )
            address = bridge.get("Gateway")
            if not address:
                raise RuntimeError(
                    "Owned PostgreSQL has no verified host bridge endpoint"
                )
            return address
        network = record["NetworkSettings"]["Networks"][self.network_name]
        address = network.get("IPAddress")
        if not address:
            raise RuntimeError("nuScenes CLI has no private database client address")
        return address

    def stop_current_container(self) -> bool:
        """Stop only this helper's verified running CLI container."""
        if self.container_id is None:
            raise RuntimeError("No nuScenes CLI container is currently running")
        record = self._inspect(self.container_id)
        labels = (record.get("Config") or {}).get("Labels") or {}
        run_id = labels.get("org.aap.run-id")
        if not isinstance(run_id, str):
            raise RuntimeError("nuScenes CLI container has no owned run identity")
        self._verify_container(record, run_id=run_id)
        if not (record.get("State") or {}).get("Running"):
            return False
        self._assert_transport()
        self._current_container_stopped = True
        self._docker("stop", "--time", "5", self.container_id, timeout=15)
        return True

    def assert_private_network(self, expected: set[str]) -> None:
        if self.network_id is None or self.host_network:
            raise RuntimeError("nuScenes CLI network has not been created")
        network = self._inspect_network()
        attached = set((network.get("Containers") or {}).keys())
        if attached != expected:
            raise RuntimeError("nuScenes CLI network has missing or foreign peers")

    def close(self) -> None:
        if self.container_id is not None:
            record = self._inspect(self.container_id, required=False)
            if record is not None:
                self._verify_container(record)
                if (record.get("State") or {}).get("Running"):
                    self._docker("stop", "--time", "5", self.container_id, timeout=15)
                self._docker("rm", self.container_id, timeout=30)
            self.container_id = None
        if self.network_id is not None and self.host_network:
            self._assert_transport()
            self.network_id = None
        if self.network_id is not None:
            _verify_postgres(self.test_engine.url)
            _verify_redis()
            _verify_minio_identity(self._inspect(self.minio["id"]), self.minio)
            network = self._inspect_network()
            peers = network.get("Containers") or {}
            for peer in (
                self.postgres_container,
                self.redis_container,
                self.minio["id"],
            ):
                peer_id = self._peer_id(peer)
                if peer_id in peers:
                    self._docker("network", "disconnect", self.network_id, peer)
            if self._inspect_network().get("Containers") or {}:
                raise RuntimeError("nuScenes CLI network still contains foreign peers")
            self._docker("network", "rm", self.network_id)
            self.network_id = None
        if self.directory.exists():
            if self.directory.is_symlink():
                raise RuntimeError("nuScenes CLI secret directory became a symlink")
            for path in self.directory.iterdir():
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            self.directory.rmdir()

    def redact(self, value: str) -> str:
        secrets_to_hide = [
            self.s3_access_key,
            self.s3_secret_key,
            self.database_url.password or "",
            self.test_engine.url.password or "",
            self.redis_owner_url.password or "",
            self.secret_key,
        ]
        secrets_to_hide.extend(
            self.original_reader_environment[name]
            for name in _ORIGINAL_READER_KEYS
            if name.endswith(("ACCESS_KEY", "SECRET_KEY"))
        )
        if self.identity_auth_database_url:
            secrets_to_hide.append(
                make_url(self.identity_auth_database_url).password or ""
            )
        for secret in sorted(set(secrets_to_hide), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "<redacted>")
        value = re.sub(
            r"([?&]X-Amz-[A-Za-z0-9-]+=)[^&\s]+",
            r"\1<redacted>",
            value,
            flags=re.IGNORECASE,
        )
        return re.sub(
            r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
            "<redacted-jwt>",
            value,
        )

    def _verify_container(self, record: dict, *, run_id: str | None = None) -> None:
        config = record.get("Config") or {}
        host = record.get("HostConfig") or {}
        labels = config.get("Labels") or {}
        mounts = record.get("Mounts") or []
        binds = [mount for mount in mounts if mount.get("Type") == "bind"]
        extra_mounts = [mount for mount in mounts if mount.get("Type") != "bind"]
        expected_network = (
            record.get("NetworkSettings", {}).get("Networks") or {}
        ).get("host" if self.host_network else self.network_name)
        expected_mounts = {
            "/fixture": self.source_root,
            **{str(path): path for path in self.fixture_metadata},
        }
        actual_mounts = {
            mount.get("Destination"): Path(mount.get("Source", "")).resolve()
            for mount in binds
        }
        expected_user = (
            f"{os.geteuid()}:{os.getegid()}"
            if self.fixture_family is not None
            else "10003:10003"
        )
        if (
            labels.get("org.aap.acceptance") != "workspace-import-cli"
            or labels.get("org.aap.acceptance-id") != self.identity
            or labels.get("org.aap.source-revision")
            != self.image_record["source_revision"]
            or (run_id is not None and labels.get("org.aap.run-id") != run_id)
            or labels.get("com.docker.compose.project") is not None
            or record.get("Image") != self.image_record["id"]
            or config.get("Image") != self.image
            or config.get("User") != expected_user
            or host.get("NetworkMode") != self.network_id
            or host.get("Privileged") is True
            or host.get("PidMode") not in {"", "private"}
            or host.get("ReadonlyRootfs") is not True
            or "ALL" not in (host.get("CapDrop") or [])
            or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
            or host.get("Memory") != 512 * 1024 * 1024
            or host.get("NanoCpus") != 1_000_000_000
            or host.get("PidsLimit") != 64
            or len(binds) != len(expected_mounts)
            or actual_mounts != expected_mounts
            or any(mount.get("RW") is not False for mount in binds)
            or any(
                mount.get("Type") != "tmpfs" or mount.get("Destination") != "/tmp"
                for mount in extra_mounts
            )
            or expected_network is None
            or len(record.get("NetworkSettings", {}).get("Networks") or {}) != 1
        ):
            raise RuntimeError(
                "nuScenes CLI container does not match its owned runtime"
            )
        env = dict(
            item.split("=", 1) for item in (config.get("Env") or []) if "=" in item
        )
        database = make_url(env.get("DATABASE_URL", ""))
        if (
            database.username != self.database_url.username
            or database.password != self.database_url.password
            or database.host != self.database_url.host
            or database.port != self.database_url.port
            or database.database != self.database_name
            or env.get("DATABASE_URL_FILE")
            or env.get("REDIS_URL") != self.redis_url
            or env.get("CELERY_BROKER_URL") != self.redis_url
            or env.get("CELERY_RESULT_BACKEND") != self.redis_url
            or env.get("CELERY_BROKER_READ_URL") != self.redis_url
            or env.get("CELERY_BROKER_WRITE_URL") != self.redis_url
            or env.get("MIGRATION_DATABASE_URL")
            or env.get("ALEMBIC_AUTO_UPGRADE") != "false"
            or env.get("E2E_SEED_ENABLED")
            != ("true" if self.fixture_run_ids is not None else "false")
            or env.get("MINIO_ENDPOINT") != self.minio_authority
            or env.get("MINIO_ACCESS_KEY") != self.s3_access_key
            or env.get("MINIO_SECRET_KEY") != self.s3_secret_key
            or env.get("MINIO_USE_SSL") != ("true" if self.minio_use_ssl else "false")
            or env.get("IDENTITY_URL")
            or env.get("IDENTITY_TOKEN")
            or env.get("IDENTITY_AUTH_DATABASE_URL", "")
            != self.identity_auth_database_url
        ):
            raise RuntimeError("nuScenes CLI did not use its scoped operational inputs")
        fixture_environment = self._fixture_environment()
        if any(
            env.get(name, "") != fixture_environment.get(name, "")
            for name in (
                "PLAYWRIGHT_E2E_DATABASE_URL",
                "FIXTURE_RUNTIME_DATABASE_URL",
                "YOAO_WORKTREE_RESOURCE_ROOT",
                "YOAO_WORKTREE_MODE",
                "AAP_FIXTURE_TOOLS_ROOT",
            )
        ):
            raise RuntimeError("CLI fixture authority differs from its private profile")
        if any(
            env.get(name, "") != value
            for name, value in self.original_reader_environment.items()
        ) or env.get("CONNECTOR_ENCRYPTION_KEY"):
            raise RuntimeError("CLI original-reader authority differs from its profile")
        if any(
            name in env
            for name, value in self.original_reader_environment.items()
            if not value
        ):
            raise RuntimeError(
                "CLI without an original reader inherited target settings"
            )
        bucket_names = {
            name
            for name in env
            if name.startswith("MINIO_") and name.endswith("_BUCKET")
        }
        if bucket_names != set(self.buckets) or any(
            env.get(name) != value for name, value in self.buckets.items()
        ):
            raise RuntimeError(
                "nuScenes CLI bucket names are not bound to the owned resource manifest"
            )
        database_selectors = {
            name
            for name, value in env.items()
            if (
                name == "DATABASE_URL"
                or name.endswith(("_DATABASE_URL", "_DATABASE_URL_FILE"))
            )
            and value
        }
        expected_selectors = {"DATABASE_URL"}
        if self.identity_auth_database_url:
            expected_selectors.add("IDENTITY_AUTH_DATABASE_URL")
        if self.fixture_family is not None:
            expected_selectors.update(
                {"PLAYWRIGHT_E2E_DATABASE_URL", "FIXTURE_RUNTIME_DATABASE_URL"}
            )
        if database_selectors != expected_selectors:
            raise RuntimeError(
                "nuScenes CLI must use only its scoped runtime database URL"
            )
        for prefix in _EMPTY_SELECTOR_PREFIXES:
            for suffix in _EMPTY_SELECTOR_SUFFIXES:
                if prefix + suffix == "IDENTITY_AUTH_DATABASE_URL":
                    continue
                if env.get(prefix + suffix):
                    raise RuntimeError(
                        "nuScenes CLI selected a foreign service or database"
                    )
        for name, value in env.items():
            if name == "IDENTITY_AUTH_DATABASE_URL":
                continue
            if (
                name.startswith(
                    (
                        "IDENTITY_",
                        "WORKSPACE_",
                        "LABELING_",
                        "INFERENCE_",
                        "MEDIA_PROBE_",
                        "ASSETS_",
                        "NOTIFICATION_PUBLISH_",
                        "PREANNOTATION_RESULTS_",
                    )
                )
                and name.endswith(
                    ("_URL", "_TOKEN", "_DATABASE_URL", "_DATABASE_URL_FILE")
                )
                and value
            ):
                raise RuntimeError("nuScenes CLI inherited a foreign service selector")

    def _verify_image(self) -> dict[str, str]:
        records = json.loads(self._docker("image", "inspect", self.image))
        if not isinstance(records, list) or len(records) != 1:
            raise RuntimeError("nuScenes CLI image identity is not unique")
        record = records[0]
        labels = (record.get("Config") or {}).get("Labels") or {}
        revision = labels.get("org.aap.source-revision")
        if (
            labels.get("org.aap.acceptance") != "workspace-module-api"
            or labels.get("org.aap.acceptance-id") != self.image_identity
            or not isinstance(revision, str)
            or re.fullmatch(r"[0-9a-f]{7,64}", revision) is None
        ):
            raise RuntimeError(
                "nuScenes CLI image does not match the verifier artifact"
            )
        return {"id": record["Id"], "source_revision": revision}

    def _runtime_database_url(self, runtime_database_url: URL | None = None) -> URL:
        value = runtime_database_url or make_url(
            self._required("AAP_MODULE_DATA_LEGACY_RUNTIME_URL")
        )
        expected_role = (
            "legacy_runtime"
            if runtime_database_url is None
            else self.database_name + "_app"
        )
        if (
            value.drivername != "postgresql+asyncpg"
            or value.username != expected_role
            or not value.password
            or value.query
            or value.host != self.test_engine.url.host
            or value.port != self.test_engine.url.port
            or value.database != self.test_engine.url.database
            or not str(value.database or "").endswith("_test")
        ):
            raise RuntimeError("Runtime role does not target the owned test DB")
        if self.host_network:
            return value
        return value.set(
            host="workspace-import-postgres",
            port=5432,
        )

    def _write_environment_file(self) -> None:
        values = {
            "ENVIRONMENT": "development",
            "DEBUG": "false",
            "SECRET_KEY": self.secret_key,
            "DATABASE_URL": self.database_url.render_as_string(hide_password=False),
            "DATABASE_URL_FILE": "",
            "MIGRATION_DATABASE_URL": "",
            "MIGRATION_DATABASE_URL_FILE": "",
            "TEST_DATABASE_URL": "",
            "PLAYWRIGHT_E2E_DATABASE_URL": "",
            "ALEMBIC_AUTO_UPGRADE": "false",
            "E2E_SEED_ENABLED": "true" if self.fixture_run_ids is not None else "false",
            "PYTHONPATH": "/app",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "HOME": "/tmp",
            "REDIS_URL": self.redis_url,
            "CELERY_BROKER_URL": self.redis_url,
            "CELERY_RESULT_BACKEND": self.redis_url,
            "CELERY_BROKER_READ_URL": self.redis_url,
            "CELERY_BROKER_WRITE_URL": self.redis_url,
            "MINIO_ENDPOINT": self.minio_authority,
            "MINIO_ACCESS_KEY": self.s3_access_key,
            "MINIO_SECRET_KEY": self.s3_secret_key,
            "MINIO_USE_SSL": "true" if self.minio_use_ssl else "false",
            "MINIO_PUBLIC_URL": "",
            "IDENTITY_URL": "",
            "IDENTITY_TOKEN": "",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN": "",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": "",
            "WORKSPACE_INFERENCE_PROBE_URL": "",
            "ASSETS_CATALOG_URL": "",
            "ASSETS_CATALOG_TOKEN": "",
            "ASSETS_SOURCE_URL": "",
            "ASSETS_SOURCE_TOKEN": "",
            "ASSETS_MEDIA_URL": "",
            "ASSETS_MEDIA_TOKEN": "",
        }
        for prefix in _EMPTY_SELECTOR_PREFIXES:
            for suffix in _EMPTY_SELECTOR_SUFFIXES:
                values[prefix + suffix] = ""
        values["IDENTITY_AUTH_DATABASE_URL"] = self.identity_auth_database_url
        values.update(self._fixture_environment())
        values.update(
            (name, value)
            for name, value in self.original_reader_environment.items()
            if value
        )
        values["CONNECTOR_ENCRYPTION_KEY"] = ""
        for name in ("WORKSPACE_INFERENCE_PROBE",):
            for suffix in ("_URL", "_TOKEN", "_TOKEN_FILE"):
                values[name + suffix] = ""
        values["IDENTITY_URL"] = ""
        values["IDENTITY_TOKEN"] = ""
        values["IDENTITY_RECOVERY_MAIL_KEY"] = ""
        values["IDENTITY_RECOVERY_MAIL_KEY_FILE"] = ""
        values.update(self.buckets)
        lines = []
        for key, value in sorted(values.items()):
            if "\x00" in value or "\n" in value:
                raise ValueError("CLI environment values must be single-line text")
            lines.append(f"{key}={value}")
        self.environment_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.environment_file.chmod(0o600)

    def _fixture_environment(self) -> dict[str, str]:
        if self.fixture_family is None:
            return {}
        return {
            "PLAYWRIGHT_E2E_DATABASE_URL": self.test_engine.url.render_as_string(
                hide_password=False
            ),
            "FIXTURE_RUNTIME_DATABASE_URL": self.database_url.render_as_string(
                hide_password=False
            ),
            "YOAO_WORKTREE_RESOURCE_ROOT": str(self.fixture_resource_root),
            "YOAO_WORKTREE_MODE": "test",
            "AAP_FIXTURE_TOOLS_ROOT": "/opt/yoao-fixture-tools",
        }

    def _prepare_private_fixture_context(self) -> None:
        root = Path(self._required("YOAO_WORKTREE_RESOURCE_ROOT"))
        if (
            not root.is_absolute()
            or root != root.resolve(strict=True)
            or self._required("YOAO_WORKTREE_MODE") != "test"
        ):
            raise RuntimeError(
                "Private fixture metadata must use its canonical test root"
            )
        self.fixture_resource_root = root
        for path in (
            root / ".worktree/identity.json",
            root / ".worktree/test/resources.json",
        ):
            if not path.is_file() or path != path.resolve(strict=True):
                raise RuntimeError(
                    "Private fixture metadata is not an exact regular file"
                )
            self.fixture_metadata[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.network_id = "host"
        self._write_environment_file()
        environment = dict(os.environ)
        environment.update(
            {
                "DATABASE_URL": self.database_url.render_as_string(hide_password=False),
                "FIXTURE_RUNTIME_DATABASE_URL": self.database_url.render_as_string(
                    hide_password=False
                ),
                "PLAYWRIGHT_E2E_DATABASE_URL": self.test_engine.url.render_as_string(
                    hide_password=False
                ),
            }
        )
        requests = [
            {
                "operation": "prepare_fixture_suite",
                "data": {"suite_id": str(run_id), "family": self.fixture_family},
            }
            for run_id in sorted(self.fixture_run_ids, key=str)
        ]
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "apps/web/e2e/fixtures/seed-request.py")],
            cwd=ROOT / "apps/api",
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 180
        operation_id = None
        reply = None
        try:

            def exchange(envelope):
                process.stdin.write(json.dumps(envelope) + "\n")
                process.stdin.flush()
                if not select.select(
                    [process.stdout], [], [], max(0, deadline - time.monotonic())
                )[0]:
                    raise RuntimeError(
                        "Private fixture coordinator outcome is unresolved"
                    )
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError(
                        "Private fixture coordinator exited before replying"
                    )
                reply = json.loads(line)
                if reply.get("operation_id") != envelope["operation_id"]:
                    raise RuntimeError("Private fixture coordinator identity changed")
                return reply

            for request in requests:
                operation_id = str(uuid.uuid4())
                reply = None
                reply = exchange(
                    {
                        "action": "execute",
                        "operation_id": operation_id,
                        "payload": request,
                    }
                )
                if reply.get("state") == "pending":
                    raise RuntimeError("Private fixture coordinator outcome is pending")
                if reply.get("owner_error") in {
                    "OperationOutcomeUnresolved",
                    "OperationIdentityConflict",
                }:
                    raise RuntimeError(
                        f"Private fixture operation refused: {reply['owner_error']}"
                    )
                if any(
                    key in reply
                    and (
                        not isinstance(reply[key], str) or not reply[key].isidentifier()
                    )
                    for key in ("owner_error", "journal_error", "disposal_error")
                ):
                    raise RuntimeError(
                        "Private fixture coordinator returned an invalid error"
                    )
                if "operation_response" in reply and (
                    not isinstance(reply["operation_response"], dict)
                    or type(reply["operation_response"].get("status")) is not int
                    or not isinstance(reply["operation_response"].get("body"), str)
                ):
                    raise RuntimeError(
                        "Private fixture coordinator returned an invalid operation response"
                    )
                if reply.get("owner_error") is None and (
                    type(reply.get("status")) is not int
                    or not isinstance(reply.get("body"), str)
                    or (
                        reply["status"] == 200
                        and json.loads(reply["body"]) != request["data"]
                    )
                ):
                    raise RuntimeError(
                        "Private fixture coordinator returned a different scope"
                    )
                receipt = exchange({"action": "ack", "operation_id": operation_id})
                if receipt.get("state") != "acknowledged":
                    raise RuntimeError(
                        "Private fixture coordinator receipt is unresolved"
                    )
                if reply.get("disposal_error"):
                    causes = []
                    if "operation_response" in reply:
                        causes.append(
                            RuntimeError(
                                f"Fixture operation returned HTTP {reply['operation_response']['status']}"
                            )
                        )
                    if (
                        reply.get("owner_error")
                        and reply["owner_error"] != reply["disposal_error"]
                    ):
                        causes.append(
                            RuntimeError(
                                f"Fixture operation failed: {reply['owner_error']}"
                            )
                        )
                    causes.append(
                        RuntimeError(
                            f"Fixture database disposal failed: {reply['disposal_error']}"
                        )
                    )
                    if reply.get("journal_error"):
                        causes.append(
                            RuntimeError(
                                f"Fixture evidence journal failed: {reply['journal_error']}"
                            )
                        )
                    raise ExceptionGroup(
                        "Private fixture operation and disposal failed", causes
                    )
                if reply.get("journal_error"):
                    raise ExceptionGroup(
                        "Private fixture operation and evidence failed",
                        [
                            RuntimeError(
                                f"Fixture operation: {reply.get('owner_error', reply.get('status'))}"
                            ),
                            RuntimeError(
                                f"Fixture evidence journal: {reply['journal_error']}"
                            ),
                        ],
                    )
                if (
                    reply.get("owner_error")
                    or reply.get("status") != 200
                    or json.loads(reply["body"]) != request["data"]
                ):
                    raise RuntimeError(
                        "Private fixture coordinator returned a different scope"
                    )
            process.stdin.close()
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                raise RuntimeError(
                    "Private fixture coordinator refused CLI preparation"
                )
            process.stdout.close()
            process.stderr.close()
        except BaseException as error:
            _RETAINED_FIXTURE_CLIENTS.append(process)
            if reply is not None:
                error.fixture_response = reply
                error.add_note("Fixture operation response was received")
            error.fixture_operation_id = operation_id
            if operation_id:
                error.add_note(f"Fixture operation ID: {operation_id}")
            raise

    def _assert_transport(self) -> None:
        if self.host_network:
            _verify_postgres(self.test_engine.url)
            _verify_redis()
            if self.minio is not None:
                _verify_minio_identity(self._inspect(self.minio["id"]), self.minio)
            for path, digest in self.fixture_metadata.items():
                if (
                    path != path.resolve(strict=True)
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest
                ):
                    raise RuntimeError("Private fixture launcher metadata changed")
            if self.network_id != "host" or (
                self.fixture_family is not None and len(self.fixture_metadata) != 2
            ):
                raise RuntimeError("CLI host transport is not prepared")
            return
        _verify_minio_identity(self._inspect(self.minio["id"]), self.minio)
        expected = {
            self._peer_id(self.postgres_container),
            self._peer_id(self.redis_container),
            self.minio["id"],
        }
        if self.container_id is not None:
            record = self._inspect(self.container_id)
            if (record.get("State") or {}).get("Running"):
                expected.add(record["Id"])
        self.assert_private_network(expected)

    def _inspect(self, identity: str, *, required: bool = True) -> dict | None:
        result = subprocess.run(
            ["docker", "inspect", identity],
            env=self._docker_environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            if not required:
                return None
            raise RuntimeError("Docker inspection failed")
        values = json.loads(result.stdout)
        if not isinstance(values, list) or len(values) != 1:
            raise RuntimeError("Docker identity was not unique")
        return values[0]

    def _inspect_network(self) -> dict:
        records = json.loads(self._docker("network", "inspect", self.network_id))
        if not isinstance(records, list) or len(records) != 1:
            raise RuntimeError("nuScenes CLI network identity is not unique")
        network = records[0]
        labels = network.get("Labels") or {}
        if (
            network.get("Name") != self.network_name
            or labels.get("org.aap.acceptance") != "workspace-import-cli"
            or labels.get("org.aap.acceptance-id") != self.identity
        ):
            raise RuntimeError("nuScenes CLI network ownership did not match")
        return network

    def _peer_id(self, identity: str) -> str:
        record = self._inspect(identity)
        return record["Id"]

    def _docker(self, *args: str, timeout: int = 90) -> str:
        try:
            result = subprocess.run(
                ["docker", *args],
                env=self._docker_environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Owned nuScenes Docker operation timed out") from None
        if result.returncode != 0:
            raise RuntimeError("Owned nuScenes Docker operation failed")
        return result.stdout.strip()

    @staticmethod
    def _docker_redis_url(value: URL) -> str:
        return value.set(host="workspace-import-redis", port=6379).render_as_string(
            hide_password=False
        )

    @staticmethod
    def _decode_output(value: str | bytes) -> str:
        return (
            value.decode("utf-8", errors="replace")
            if isinstance(value, bytes)
            else value
        )

    @staticmethod
    def _minimal_docker_environment() -> dict[str, str]:
        names = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
        )
        return {name: os.environ[name] for name in names if os.environ.get(name)}

    @staticmethod
    def _required(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"nuScenes CLI verifier input {name} is missing")
        return value
