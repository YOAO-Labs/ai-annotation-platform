"""Owned Compose assembly for the complete Workspace API consumer slice."""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
import uuid

import httpx
from sqlalchemy.engine import URL, make_url

from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url


ROOT = Path(__file__).resolve().parents[3]
_WORKSPACE_SERVICES = {
    "workspace-membership": 8172,
    "workspace-owner-transfer": 8173,
    "workspace-configuration": 8174,
    "workspace-template-query": 8175,
    "workspace-project-query": 8176,
    "workspace-directory-query": 8177,
}
_WORKSPACE_ROLES = {
    "membership": (
        "AAP_MODULE_DATA_WORKSPACE_MEMBERSHIP_URL",
        "workspace_membership_client",
        "membership_database_url",
    ),
    "owner_transfer": (
        "AAP_MODULE_DATA_WORKSPACE_OWNER_TRANSFER_URL",
        "workspace_owner_transfer_client",
        "owner_transfer_database_url",
    ),
    "configuration": (
        "AAP_MODULE_DATA_WORKSPACE_CONFIGURATION_URL",
        "workspace_configuration_client",
        "configuration_database_url",
    ),
    "template_query": (
        "AAP_MODULE_DATA_WORKSPACE_TEMPLATE_QUERY_URL",
        "workspace_template_query_client",
        "template_query_database_url",
    ),
    "project_query": (
        "AAP_MODULE_DATA_WORKSPACE_PROJECT_QUERY_URL",
        "workspace_project_query_client",
        "project_query_database_url",
    ),
    "directory_query": (
        "AAP_MODULE_DATA_WORKSPACE_DIRECTORY_QUERY_URL",
        "workspace_directory_query_client",
        "directory_query_database_url",
    ),
}
_API_WORKSPACE_BINDINGS = {
    "WORKSPACE_MEMBERSHIP": "http://workspace-membership:8172",
    "WORKSPACE_OWNER_TRANSFER": "http://workspace-owner-transfer:8173",
    "WORKSPACE_CONFIGURATION": "http://workspace-configuration:8174",
    "WORKSPACE_TEMPLATE_QUERY": "http://workspace-template-query:8175",
    "WORKSPACE_PROJECT_QUERY": "http://workspace-project-query:8176",
    "WORKSPACE_DIRECTORY_QUERY": "http://workspace-directory-query:8177",
}
_EXPECTED_SERVICES = {
    *_WORKSPACE_SERVICES,
    "api",
    "inference-probe",
    "workspace-guide-objects",
}


class WorkspaceModuleStack:
    """Run six same-artifact Workspace owners and one isolated API process."""

    def __init__(
        self,
        test_engine,
        directory: Path,
        *,
        api_secret: str,
        control_token: str,
        inference_token: str,
        guide_token: str,
        guide_bucket: str,
        guide_access_key: str,
        guide_secret_key: str,
    ) -> None:
        self.test_engine = test_engine
        self.directory = Path(directory).resolve()
        self._secret_directory = self.directory / "workspace-module-secrets"
        self._compose_env_file = self._secret_directory / "compose.env"
        self._api_env_file = self._secret_directory / "api.env"
        self._override_file = self._secret_directory / "compose.override.yml"
        self._log_file = self._secret_directory / "compose.log"
        self._api_secret = api_secret
        self._control_token = control_token
        self._inference_token = inference_token
        self._guide_token = guide_token
        self._guide_bucket = guide_bucket
        self._guide_access_key = guide_access_key
        self._guide_secret_key = guide_secret_key
        _verify_postgres(test_engine.url)
        _verify_redis()
        self._postgres_name = self._required("AAP_MODULE_DATA_PG_CONTAINER_NAME")
        self._postgres_container = self._required("AAP_MODULE_DATA_PG_CONTAINER_ID")
        self._redis_name = self._required("AAP_MODULE_DATA_REDIS_CONTAINER_NAME")
        self._workspace_image = self._required("AAP_WORKSPACE_MODULE_IMAGE")
        self._workspace_identity = self._required("AAP_WORKSPACE_MODULE_IDENTITY")
        self._api_image = self._required("AAP_WORKSPACE_MODULE_API_IMAGE")
        self._api_identity = self._required("AAP_WORKSPACE_MODULE_API_IDENTITY")
        self._inference_image = self._required("AAP_WORKSPACE_MODULE_INFERENCE_IMAGE")
        self._inference_identity = self._required(
            "AAP_WORKSPACE_MODULE_INFERENCE_IDENTITY"
        )
        self._assets_image = self._required("AAP_WORKSPACE_MODULE_ASSETS_SOURCE_IMAGE")
        self._assets_identity = self._required(
            "AAP_WORKSPACE_MODULE_ASSETS_SOURCE_IDENTITY"
        )
        self.project_name = f"aap-workspace-assembly-{uuid.uuid4().hex[:12]}"
        self._compose_environment = self._docker_environment()
        self._database_name = str(test_engine.url.database or "")
        self._validate_database_name(self._database_name)
        self._database_urls: dict[str, URL] = {
            key: self._role_dsn(variable, role)
            for key, (variable, role, _) in _WORKSPACE_ROLES.items()
        }
        self._briefs_dsn: URL = self._role_dsn(
            "AAP_MODULE_DATA_IDENTITY_BRIEFS_URL", "identity_briefs_client"
        )
        self._identity_auth_dsn: URL = self._role_dsn(
            "AAP_MODULE_DATA_IDENTITY_AUTH_URL", "identity_auth_client"
        )
        self._inference_dsn: URL = self._role_dsn(
            "AAP_MODULE_DATA_INFERENCE_BACKEND_PROBE_URL",
            "inference_backend_probe_client",
        )
        api_host_url = make_url(
            self._required("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL")
        )
        api_container_url = make_url(
            self._required("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_CONTAINER_URL")
        )
        if (
            api_host_url.username != "identity_api_consumer"
            or api_host_url.database != self._database_name
            or api_host_url.host != test_engine.url.host
            or api_host_url.port != test_engine.url.port
            or not self._database_name.endswith("_test")
            or api_container_url.username != api_host_url.username
            or api_container_url.password != api_host_url.password
            or api_container_url.database != api_host_url.database
            or api_container_url.host != "host.docker.internal"
            or api_container_url.port != api_host_url.port
        ):
            raise RuntimeError("Verifier API consumer identity does not match test DB")
        self._api_host_url = api_host_url
        self._api_database_url = api_container_url.set(
            host=self._postgres_name, port=5432
        )
        self._redis_url = self._docker_url(self._required("REDIS_URL"))
        self._celery_url = self._docker_url(
            os.environ.get("CELERY_BROKER_URL") or self._required("REDIS_URL")
        )
        self._service_ids: dict[str, str] = {}
        self._inspection_evidence: dict[str, dict] = {}
        self._origins: dict[str, str] = {}
        self._image_ids = {
            "workspace": self._verify_image(
                self._workspace_image,
                "workspace-module-owner",
                self._workspace_identity,
            ),
            "api": self._verify_image(
                self._api_image, "workspace-module-api", self._api_identity
            ),
            "inference": self._verify_image(
                self._inference_image,
                "workspace-module-inference",
                self._inference_identity,
            ),
            "assets_source": self._verify_image(
                self._assets_image,
                "workspace-module-assets-source",
                self._assets_identity,
            ),
        }

    @property
    def api_origin(self) -> str:
        return self._origins["api"]

    @property
    def owner_origins(self) -> dict[str, str]:
        return {key: value for key, value in self._origins.items() if key != "api"}

    @property
    def inspection_evidence(self) -> dict[str, dict]:
        return json.loads(json.dumps(self._inspection_evidence))

    def redact(self, value: str) -> str:
        secrets = [
            self._api_secret,
            self._control_token,
            self._inference_token,
            self._guide_token,
            self._guide_access_key,
            self._guide_secret_key,
            os.environ.get("AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY", ""),
            os.environ.get("AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY", ""),
        ]
        secrets.extend(url.password or "" for url in self._database_urls.values())
        secrets.extend(
            [
                self._briefs_dsn.password or "",
                self._identity_auth_dsn.password or "",
                self._inference_dsn.password or "",
                self._api_database_url.password or "",
            ]
        )
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "<redacted>")
        return re.sub(
            r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
            "<redacted-jwt>",
            value,
        )

    async def start(self) -> None:
        self._prepare_files()
        self._verify_project_ownership()
        self._compose("create", timeout=180)
        self._connect_dependencies()
        self._compose("up", "--detach", timeout=180)
        self._verify_project_ownership()
        self._refresh_origins()
        for service in _EXPECTED_SERVICES:
            await self._wait_service_ready(service)
        self._assert_private_network()
        self._refresh_inspection_evidence()

    async def stop(self) -> None:
        if not self._compose_env_file.is_file() or not self._override_file.is_file():
            return
        containers = self._verify_project_ownership(allow_partial=True)
        if containers:
            self._compose("stop", timeout=120)
        self._disconnect_dependencies()
        self._compose("down", timeout=120)
        self._service_ids = {}

    async def stop_service(self, service: str) -> None:
        self._validate_service(service)
        self._verify_project_ownership()
        self._compose("stop", service, timeout=90)
        await self._wait_stopped(service)
        self._refresh_inspection_evidence()

    async def start_service(self, service: str) -> None:
        self._validate_service(service)
        self._verify_project_ownership()
        self._compose("start", service, timeout=90)
        self._refresh_origins()
        await self._wait_service_ready(service)
        self._refresh_inspection_evidence()

    async def restart_service(self, service: str) -> None:
        self._validate_service(service)
        self._verify_project_ownership()
        self._compose("restart", service, timeout=90)
        self._refresh_origins()
        await self._wait_service_ready(service)
        self._refresh_inspection_evidence()

    async def switch_database(self, database_name: str) -> None:
        self._validate_database_name(database_name)
        await self.stop()
        self._database_name = database_name
        self._api_database_url = self._api_database_url.set(database=database_name)
        self._api_host_url = self._api_host_url.set(database=database_name)
        await self.start()

    def capture_diagnostics(self) -> str:
        records = {}
        prepared = self._compose_env_file.is_file() and self._override_file.is_file()
        containers = (
            self._verify_project_ownership(allow_partial=True) if prepared else {}
        )
        if not prepared:
            records["setup"] = {"compose_files_complete": False}
        for service, details in containers.items():
            result = subprocess.run(
                ["docker", "logs", "--tail", "120", details["Id"]],
                env=self._compose_environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            records[service] = {
                "state": details.get("State"),
                "logs": self.redact(result.stdout + result.stderr),
            }
        path = ROOT / ".worktree" / f"workspace-module-{self.project_name}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return str(path)

    def sample_runtime(self) -> dict:
        """Sample only the current verified assembly's live resource usage."""
        self._refresh_inspection_evidence()
        expected = set(self._service_ids.values())
        if len(expected) != len(_EXPECTED_SERVICES):
            raise RuntimeError("Workspace capacity sample has an incomplete assembly")
        output = self._run_docker(
            [
                "stats",
                "--no-stream",
                "--no-trunc",
                "--format",
                "{{json .}}",
                *sorted(expected),
            ],
            operation="resource sample",
            timeout=20,
        )
        rows = [json.loads(line) for line in output.splitlines() if line]
        if len(rows) != len(expected) or {row.get("ID") for row in rows} != expected:
            raise RuntimeError("Workspace capacity sample has missing or foreign IDs")
        by_id = {row["ID"]: row for row in rows}
        services = {}
        for service, details in self._inspection_evidence.items():
            if (
                not details["running"]
                or details["oom_killed"]
                or details["health"] != "healthy"
                or details["restart_count"] != 0
            ):
                raise RuntimeError(f"{service} became unhealthy under Workspace load")
            row = by_id[details["container_id"]]
            cpu = float(row["CPUPerc"].removesuffix("%"))
            memory = float(row["MemPerc"].removesuffix("%"))
            pids = int(row["PIDs"])
            if (
                not all(math.isfinite(value) and value >= 0 for value in (cpu, memory))
                or not 0 < pids <= details["limits"]["pids_limit"]
            ):
                raise RuntimeError(f"{service} returned invalid resource statistics")
            services[service] = {
                "container_id": details["container_id"],
                "image_id": details["image_id"],
                "started_at": details["started_at"],
                "health": details["health"],
                "oom_killed": details["oom_killed"],
                "restart_count": details["restart_count"],
                "limits": details["limits"],
                "cpu_percent": cpu,
                "memory_percent": memory,
                "memory_usage": row["MemUsage"],
                "pids": pids,
                "network_io": row["NetIO"],
                "block_io": row["BlockIO"],
            }
        return {"monotonic_seconds": time.monotonic(), "services": services}

    def service_origin(self, service: str) -> str:
        if service not in self._origins:
            raise ValueError("Unknown Workspace assembly service")
        return self._origins[service]

    def _prepare_files(self) -> None:
        self._secret_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._secret_directory.is_symlink():
            raise RuntimeError("Workspace secret directory must not be a symlink")
        self._secret_directory.chmod(0o700)

        paths: dict[str, Path] = {}
        for key, (_, _, secret_name) in _WORKSPACE_ROLES.items():
            paths[key] = self._write_secret(
                secret_name,
                self._database_urls[key]
                .set(database=self._database_name)
                .render_as_string(hide_password=False),
            )
        paths["briefs"] = self._write_secret(
            "identity_briefs_database_url",
            self._briefs_dsn.set(database=self._database_name).render_as_string(
                hide_password=False
            ),
        )
        paths["identity_auth"] = self._write_secret(
            "identity_auth_database_url",
            self._identity_auth_dsn.set(database=self._database_name).render_as_string(
                hide_password=False
            ),
        )
        paths["control"] = self._write_secret(
            "membership_control_token", self._control_token
        )
        paths["inference_dsn"] = self._write_secret(
            "inference_backend_probe_database_url",
            self._inference_dsn.set(database=self._database_name).render_as_string(
                hide_password=False
            ),
        )
        paths["inference_token"] = self._write_secret(
            "inference_backend_probe_control_token", self._inference_token
        )
        paths["guide_token"] = self._write_secret(
            "guide_objects_token", self._guide_token
        )
        paths["guide_source_token"] = self._write_secret(
            "guide_source_token", self._guide_token
        )
        paths["guide_access"] = self._write_secret(
            "guide_source_access_key", self._guide_access_key
        )
        paths["guide_secret"] = self._write_secret(
            "guide_source_secret_key", self._guide_secret_key
        )

        self._write_environment_file(self._api_env_file, self._api_environment())
        compose_values = {
            "AAP_WORKSPACE_MODULE_IMAGE": self._workspace_image,
            "AAP_WORKSPACE_MODULE_API_IMAGE": self._api_image,
            "AAP_WORKSPACE_MODULE_INFERENCE_IMAGE": self._inference_image,
            "AAP_WORKSPACE_MODULE_ASSETS_SOURCE_IMAGE": self._assets_image,
            "WORKSPACE_MODULE_STACK_ID": self.project_name,
            "WORKSPACE_API_ENV_FILE": str(self._api_env_file),
            "WORKSPACE_GUIDE_BUCKET": self._guide_bucket,
            "WORKSPACE_GUIDE_SOURCE_TOKEN_FILE": str(paths["guide_source_token"]),
            "WORKSPACE_GUIDE_SOURCE_ACCESS_KEY_FILE": str(paths["guide_access"]),
            "WORKSPACE_GUIDE_SOURCE_SECRET_KEY_FILE": str(paths["guide_secret"]),
            "WORKSPACE_INFERENCE_PROBE_URL": "http://inference-probe:8768",
            "WORKSPACE_RUNTIME_MEMBERSHIP_DATABASE_URL_FILE": str(paths["membership"]),
            "WORKSPACE_RUNTIME_OWNER_TRANSFER_DATABASE_URL_FILE": str(
                paths["owner_transfer"]
            ),
            "WORKSPACE_RUNTIME_CONFIGURATION_DATABASE_URL_FILE": str(
                paths["configuration"]
            ),
            "WORKSPACE_RUNTIME_TEMPLATE_QUERY_DATABASE_URL_FILE": str(
                paths["template_query"]
            ),
            "WORKSPACE_RUNTIME_PROJECT_QUERY_DATABASE_URL_FILE": str(
                paths["project_query"]
            ),
            "WORKSPACE_RUNTIME_DIRECTORY_QUERY_DATABASE_URL_FILE": str(
                paths["directory_query"]
            ),
            "WORKSPACE_RUNTIME_PROJECT_QUERY_BRIEFS_DATABASE_URL_FILE": str(
                paths["briefs"]
            ),
            "WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE": str(paths["control"]),
            "WORKSPACE_RUNTIME_IDENTITY_AUTH_DATABASE_URL_FILE": str(
                paths["identity_auth"]
            ),
            "WORKSPACE_RUNTIME_INFERENCE_PROBE_TOKEN_FILE": str(
                paths["inference_token"]
            ),
            "INFERENCE_RUNTIME_BACKEND_PROBE_DATABASE_URL_FILE": str(
                paths["inference_dsn"]
            ),
            "INFERENCE_RUNTIME_BACKEND_PROBE_CONTROL_TOKEN_FILE": str(
                paths["inference_token"]
            ),
            "WORKSPACE_GUIDE_OBJECTS_URL": "http://workspace-guide-objects:8767",
            "WORKSPACE_GUIDE_OBJECTS_TOKEN_FILE": str(paths["guide_token"]),
            "ML_HEALTH_TIMEOUT": "10",
            "VIDEO_SEGMENT_SIZE_FRAMES": "18000",
            "VIDEO_TRACKER_WINDOW_SIZE_FRAMES": "300",
            "VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES": "16",
            "WORKSPACE_TIMEOUT_SECONDS": "20",
            "INFERENCE_MAX_CONCURRENCY": "4",
        }
        self._write_environment_file(self._compose_env_file, compose_values)
        self._write_override_file()

    def _api_environment(self) -> dict[str, str]:
        api_container = self._api_database_url.render_as_string(hide_password=False)
        minio_access = self._required("AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY")
        minio_secret = self._required("AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY")
        values = {
            "ENVIRONMENT": "development",
            "DEBUG": "false",
            "SECRET_KEY": self._api_secret,
            "DATABASE_URL": api_container,
            "MIGRATION_DATABASE_URL": "",
            "ALEMBIC_AUTO_UPGRADE": "false",
            "E2E_SEED_ENABLED": "false",
            "REDIS_URL": self._redis_url,
            "CELERY_BROKER_URL": self._celery_url,
            "AUDIT_ASYNC": "true",
            "IDENTITY_URL": "",
            "IDENTITY_TOKEN": "",
            "IDENTITY_AUTH_DATABASE_URL": "",
            "IDENTITY_AUTH_DATABASE_URL_FILE": "/run/secrets/identity_auth_database_url",
            "IDENTITY_BRIEFS_DATABASE_URL": "",
            "IDENTITY_BRIEFS_DATABASE_URL_FILE": "/run/secrets/identity_briefs_database_url",
            "IDENTITY_TIMEOUT_SECONDS": "10",
            "MINIO_ENDPOINT": urlsplit(
                self._docker_http_endpoint(self._required("MINIO_ENDPOINT"))
            ).netloc,
            "MINIO_ACCESS_KEY": minio_access,
            "MINIO_SECRET_KEY": minio_secret,
            "MINIO_USE_SSL": self._required_or("MINIO_USE_SSL", "false"),
            "MINIO_PUBLIC_URL": self._docker_public_url(
                os.environ.get("MINIO_PUBLIC_URL", "")
            ),
            "SENTRY_DSN": "",
            "TURNSTILE_ENABLED": "false",
            "TRAINING_ENABLED": "false",
            "IDENTITY_RECOVERY_MAIL_KEY": "",
            "IDENTITY_RECOVERY_MAIL_KEY_FILE": "",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN": "",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": "/run/secrets/membership_control_token",
        }
        for variable, default in (
            ("MINIO_BUCKET", "annotations"),
            ("MINIO_DATASETS_BUCKET", "datasets"),
            ("MINIO_BUG_REPORTS_BUCKET", "bug-reports"),
            ("MINIO_MEDIA_CACHE_BUCKET", "media-cache"),
            ("MINIO_AUDIT_ARCHIVE_BUCKET", "audit-archive"),
            ("MINIO_IMPORT_BUCKET", "import"),
            ("MINIO_EXPORT_BUCKET", "export"),
            ("MINIO_AVATARS_BUCKET", "avatars"),
            ("MINIO_TRAINING_BUCKET", "training"),
            ("MINIO_INPUTS_BUCKET", "accepted-inputs"),
        ):
            values[variable] = os.environ.get(variable, default)

        for prefix, url in _API_WORKSPACE_BINDINGS.items():
            values[f"{prefix}_URL"] = url
            values[f"{prefix}_DATABASE_URL"] = ""
            values[f"{prefix}_DATABASE_URL_FILE"] = ""
        values.update(
            {
                "WORKSPACE_MEMBERSHIP_TIMEOUT_SECONDS": "20",
                "WORKSPACE_OWNER_TRANSFER_TIMEOUT_SECONDS": "20",
                "WORKSPACE_CONFIGURATION_TIMEOUT_SECONDS": "20",
                "WORKSPACE_TEMPLATE_QUERY_TIMEOUT_SECONDS": "20",
                "WORKSPACE_PROJECT_QUERY_TIMEOUT_SECONDS": "20",
                "WORKSPACE_DIRECTORY_QUERY_TIMEOUT_SECONDS": "20",
                "WORKSPACE_GUIDE_OBJECTS_URL": "",
                "WORKSPACE_GUIDE_OBJECTS_TOKEN": "",
                "WORKSPACE_GUIDE_OBJECTS_TOKEN_FILE": "",
            }
        )
        return values

    def _write_override_file(self) -> None:
        api_env_file = json.dumps(str(self._api_env_file))
        self._override_file.write_text(
            f"""services:
  workspace-membership:
    image: ${{AAP_WORKSPACE_MODULE_IMAGE:?}}
    build: !reset null
    ports: !override ["127.0.0.1::8172"]
    labels: &workspace-module-owner-labels
      org.aap.acceptance: workspace-module-assembly
      org.aap.acceptance-id: ${{WORKSPACE_MODULE_STACK_ID:?}}
  workspace-owner-transfer:
    image: ${{AAP_WORKSPACE_MODULE_IMAGE:?}}
    build: !reset null
    ports: !override ["127.0.0.1::8173"]
    labels: *workspace-module-owner-labels
  workspace-configuration:
    image: ${{AAP_WORKSPACE_MODULE_IMAGE:?}}
    build: !reset null
    ports: !override ["127.0.0.1::8174"]
    depends_on:
      inference-probe:
        condition: service_healthy
      workspace-guide-objects:
        condition: service_healthy
    labels: *workspace-module-owner-labels
  workspace-template-query:
    image: ${{AAP_WORKSPACE_MODULE_IMAGE:?}}
    build: !reset null
    ports: !override ["127.0.0.1::8175"]
    labels: *workspace-module-owner-labels
  workspace-project-query:
    image: ${{AAP_WORKSPACE_MODULE_IMAGE:?}}
    build: !reset null
    ports: !override ["127.0.0.1::8176"]
    labels: *workspace-module-owner-labels
  workspace-directory-query:
    image: ${{AAP_WORKSPACE_MODULE_IMAGE:?}}
    build: !reset null
    ports: !override ["127.0.0.1::8177"]
    labels: *workspace-module-owner-labels
  inference-probe:
    image: ${{AAP_WORKSPACE_MODULE_INFERENCE_IMAGE:?}}
    build: !reset null
    ports: !override ["127.0.0.1::8768"]
    extra_hosts: ["host.docker.internal:host-gateway"]
    labels:
      org.aap.acceptance: workspace-module-assembly
      org.aap.acceptance-id: ${{WORKSPACE_MODULE_STACK_ID:?}}
  workspace-guide-objects:
    image: ${{AAP_WORKSPACE_MODULE_ASSETS_SOURCE_IMAGE:?}}
    build: !reset null
    command: ["python", "-m", "aap_assets_source_runtime"]
    user: "10003:10003"
    environment:
      ASSETS_SOURCE_MODE: guide-only
      ASSETS_SOURCE_HOST: 0.0.0.0
      ASSETS_SOURCE_PORT: 8767
      ASSETS_SOURCE_TOKEN_FILE: /run/secrets/guide_source_token
      ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT: "4"
      ASSETS_SOURCE_GUIDE_S3_ENDPOINT: {json.dumps(self._docker_http_endpoint(self._required("MINIO_ENDPOINT")))}
      ASSETS_SOURCE_GUIDE_S3_PUBLIC_URL: /minio
      ASSETS_SOURCE_GUIDE_S3_BUCKET: ${{WORKSPACE_GUIDE_BUCKET:?}}
      ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY_FILE: /run/secrets/guide_source_access_key
      ASSETS_SOURCE_GUIDE_S3_SECRET_KEY_FILE: /run/secrets/guide_source_secret_key
    secrets:
      - guide_source_token
      - guide_source_access_key
      - guide_source_secret_key
    ports: ["127.0.0.1::8767"]
    extra_hosts: ["host.docker.internal:host-gateway"]
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    pids_limit: 64
    mem_limit: 256m
    cpus: 1
    tmpfs: ["/tmp:rw,noexec,nosuid,size=16m"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8767/ready', timeout=2)"]
      interval: 1s
      timeout: 3s
      start_period: 5s
      retries: 10
    labels:
      org.aap.acceptance: workspace-module-assembly
      org.aap.acceptance-id: ${{WORKSPACE_MODULE_STACK_ID:?}}
  api:
    image: ${{AAP_WORKSPACE_MODULE_API_IMAGE:?}}
    build: !reset null
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "3"]
    user: "10003:10003"
    env_file: [{api_env_file}]
    secrets:
      - membership_control_token
      - identity_auth_database_url
      - identity_briefs_database_url
    depends_on:
      workspace-membership: {{condition: service_healthy}}
      workspace-owner-transfer: {{condition: service_healthy}}
      workspace-configuration: {{condition: service_healthy}}
      workspace-template-query: {{condition: service_healthy}}
      workspace-project-query: {{condition: service_healthy}}
      workspace-directory-query: {{condition: service_healthy}}
    ports: !override ["127.0.0.1::8000"]
    extra_hosts: ["host.docker.internal:host-gateway"]
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    pids_limit: 64
    mem_limit: 768m
    cpus: 1
    tmpfs: ["/tmp:rw,noexec,nosuid,size=32m"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; [urllib.request.urlopen('http://127.0.0.1:8000/health/' + component, timeout=2) for component in ('db', 'redis', 'minio')]"]
      interval: 1s
      timeout: 3s
      start_period: 10s
      retries: 20
    labels:
      org.aap.acceptance: workspace-module-assembly
      org.aap.acceptance-id: ${{WORKSPACE_MODULE_STACK_ID:?}}

secrets:
  membership_control_token:
    file: ${{WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE:?}}
  identity_auth_database_url:
    file: ${{WORKSPACE_RUNTIME_IDENTITY_AUTH_DATABASE_URL_FILE:?}}
  identity_briefs_database_url:
    file: ${{WORKSPACE_RUNTIME_PROJECT_QUERY_BRIEFS_DATABASE_URL_FILE:?}}
  inference_backend_probe_database_url:
    file: ${{INFERENCE_RUNTIME_BACKEND_PROBE_DATABASE_URL_FILE:?}}
  inference_backend_probe_control_token:
    file: ${{INFERENCE_RUNTIME_BACKEND_PROBE_CONTROL_TOKEN_FILE:?}}
  guide_source_token:
    file: ${{WORKSPACE_GUIDE_SOURCE_TOKEN_FILE:?}}
  guide_source_access_key:
    file: ${{WORKSPACE_GUIDE_SOURCE_ACCESS_KEY_FILE:?}}
  guide_source_secret_key:
    file: ${{WORKSPACE_GUIDE_SOURCE_SECRET_KEY_FILE:?}}
""",
            encoding="utf-8",
        )
        self._override_file.chmod(0o600)

    def _compose_arguments(self) -> list[str]:
        return [
            "compose",
            "--project-name",
            self.project_name,
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(self._compose_env_file),
            "-f",
            str(ROOT / "docker-compose.workspace.yml"),
            "-f",
            str(ROOT / "docker-compose.workspace-owner-transfer.yml"),
            "-f",
            str(ROOT / "docker-compose.workspace-configuration.yml"),
            "-f",
            str(ROOT / "docker-compose.workspace-template-query.yml"),
            "-f",
            str(ROOT / "docker-compose.workspace-project-query.yml"),
            "-f",
            str(ROOT / "docker-compose.workspace-directory-query.yml"),
            "-f",
            str(ROOT / "docker-compose.workspace-class-rename.yml"),
            "-f",
            str(ROOT / "docker-compose.workspace-inference-probe.yml"),
            "-f",
            str(ROOT / "docker-compose.workspace-guide-assets.yml"),
            "-f",
            str(ROOT / "docker-compose.inference-probe.yml"),
            "-f",
            str(self._override_file),
        ]

    def _compose(self, *arguments: str, timeout: int = 90) -> str:
        result = self._run_docker(
            self._compose_arguments() + list(arguments),
            operation="Compose operation",
            timeout=timeout,
        )
        return result

    def _run_docker(
        self, arguments: list[str], *, operation: str, timeout: int = 90
    ) -> str:
        try:
            result = subprocess.run(
                ["docker", *arguments],
                env=self._compose_environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._write_log(f"{operation}: timed out")
            raise RuntimeError(
                f"Owned Workspace {operation} timed out; see {self._log_file}"
            ) from None
        if result.returncode != 0:
            self._write_log(
                f"{operation}: Docker exited {result.returncode}: "
                f"{self.redact(result.stderr)[-2000:]}"
            )
            raise RuntimeError(
                f"Owned Workspace {operation} failed; see {self._log_file}"
            )
        return result.stdout.strip()

    def _inspect(self, container_id: str) -> dict:
        output = self._run_docker(
            ["inspect", container_id], operation="container inspection", timeout=20
        )
        try:
            records = json.loads(output)
        except json.JSONDecodeError:
            raise RuntimeError("Docker returned an invalid container record") from None
        if not isinstance(records, list) or len(records) != 1:
            raise RuntimeError("Container identity is not unique")
        return records[0]

    def _verify_project_ownership(
        self, *, allow_partial: bool = False
    ) -> dict[str, dict]:
        raw = self._compose("ps", "--all", "--quiet", timeout=20)
        containers = {}
        for container_id in (value for value in raw.splitlines() if value):
            details = self._inspect(container_id)
            labels = (details.get("Config") or {}).get("Labels") or {}
            if (
                labels.get("com.docker.compose.project") != self.project_name
                or labels.get("org.aap.acceptance") != "workspace-module-assembly"
                or labels.get("org.aap.acceptance-id") != self.project_name
            ):
                raise RuntimeError("Compose project contains a foreign container")
            service = labels.get("com.docker.compose.service")
            if not service or service in containers:
                raise RuntimeError("Compose service identity is ambiguous")
            containers[service] = details
        if containers and set(containers) != _EXPECTED_SERVICES and not allow_partial:
            raise RuntimeError("Compose service set differs from Workspace assembly")
        self._service_ids = {
            service: details["Id"]
            for service, details in containers.items()
            if details.get("Id")
        }
        return containers

    def _verify_image(self, image: str, accepted_as: str, identity: str) -> str:
        output = self._run_docker(
            ["image", "inspect", image], operation="image inspection", timeout=30
        )
        try:
            records = json.loads(output)
        except json.JSONDecodeError:
            raise RuntimeError("Docker returned an invalid image record") from None
        if not isinstance(records, list) or len(records) != 1:
            raise RuntimeError("Runtime image identity is not unique")
        labels = (records[0].get("Config") or {}).get("Labels") or {}
        revision = labels.get("org.aap.source-revision")
        if (
            labels.get("org.aap.acceptance") != accepted_as
            or labels.get("org.aap.acceptance-id") != identity
            or not isinstance(revision, str)
            or re.fullmatch(r"[0-9a-f]{7,64}", revision) is None
        ):
            raise RuntimeError("Runtime image artifact labels do not match verifier")
        image_id = records[0].get("Id")
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise RuntimeError("Runtime image did not have a content identity")
        return image_id

    def _dependency_network(self) -> str | None:
        name = self.project_name + "_default"
        output = self._run_docker(
            ["network", "ls", "--filter", f"name=^{name}$", "--quiet"],
            operation="owned network lookup",
            timeout=20,
        )
        if not output:
            return None
        ids = output.splitlines()
        if len(ids) != 1:
            raise RuntimeError("Owned Compose network identity is ambiguous")
        record = json.loads(
            self._run_docker(
                ["network", "inspect", ids[0]],
                operation="owned network inspection",
                timeout=20,
            )
        )[0]
        if (
            record["Name"] != name
            or record.get("Labels", {}).get("com.docker.compose.project")
            != self.project_name
        ):
            raise RuntimeError("Network is not owned by this Compose project")
        return name

    def _connect_dependencies(self) -> None:
        _verify_postgres(self.test_engine.url)
        _verify_redis()
        network = self._dependency_network()
        if network is None:
            raise RuntimeError("Compose did not create its owned network")
        for container in (self._postgres_container, self._redis_name):
            details = self._inspect(container)
            if network not in (
                details.get("NetworkSettings", {}).get("Networks") or {}
            ):
                self._run_docker(
                    ["network", "connect", network, container],
                    operation="owned dependency attachment",
                )

    def _disconnect_dependencies(self) -> None:
        network = self._dependency_network()
        if network is None:
            return
        _verify_postgres(self.test_engine.url)
        _verify_redis()
        for container in (self._postgres_container, self._redis_name):
            details = self._inspect(container)
            if network in (details.get("NetworkSettings", {}).get("Networks") or {}):
                self._run_docker(
                    ["network", "disconnect", network, container],
                    operation="owned dependency detachment",
                )

    def _assert_private_network(self) -> None:
        network = self._dependency_network()
        if network is None:
            raise RuntimeError("Workspace assembly network is missing")
        records = json.loads(
            self._run_docker(
                ["network", "inspect", network],
                operation="owned network membership inspection",
                timeout=20,
            )
        )
        if not isinstance(records, list) or len(records) != 1:
            raise RuntimeError("Workspace assembly network identity is ambiguous")
        attached = set((records[0].get("Containers") or {}).keys())
        expected = set(self._service_ids.values()) | {
            self._postgres_container,
            self._inspect(self._redis_name)["Id"],
        }
        if attached != expected:
            raise RuntimeError("Private Workspace network has missing or foreign peers")

    def _refresh_origins(self) -> None:
        origins = {}
        for service, port in _WORKSPACE_SERVICES.items():
            origins[service] = self._published_origin(service, port)
        origins["api"] = self._published_origin("api", 8000)
        origins["inference-probe"] = self._published_origin("inference-probe", 8768)
        origins["workspace-guide-objects"] = self._published_origin(
            "workspace-guide-objects", 8767
        )
        self._origins = origins

    def _published_origin(self, service: str, target_port: int) -> str:
        output = self._compose("port", service, str(target_port), timeout=20)
        values = [line.strip() for line in output.splitlines() if line.strip()]
        if len(values) != 1:
            raise RuntimeError("Owned loopback port mapping is not unique")
        parsed = urlsplit("//" + values[0])
        if parsed.hostname != "127.0.0.1" or parsed.port is None:
            raise RuntimeError("Owned service is not published on loopback")
        return f"http://127.0.0.1:{parsed.port}"

    async def _wait_service_ready(self, service: str, timeout: float = 90) -> None:
        origin = self._origins[service]
        path = (
            "/health/db"
            if service == "api"
            else ("/health" if service == "inference-probe" else "/ready")
        )
        await self._wait_http(origin + path, service, timeout=timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            details = self._inspect(self._service_ids[service])
            state = details.get("State") or {}
            health = (state.get("Health") or {}).get("Status")
            if state.get("Running") and health == "healthy":
                return
            if state.get("Status") == "exited" or state.get("OOMKilled"):
                break
            await asyncio.sleep(0.2)
        raise RuntimeError(f"Owned Workspace service {service} did not become healthy")

    async def _wait_http(self, url: str, operation: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                state = self._inspect(self._service_ids[operation]).get("State") or {}
                if state.get("Status") == "exited" or state.get("OOMKilled"):
                    break
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        raise RuntimeError(f"Owned Workspace {operation} endpoint did not become ready")

    async def _wait_stopped(self, service: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            details = self._inspect(self._service_ids[service])
            if (details.get("State") or {}).get("Running") is False:
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"Owned Workspace service {service} did not stop")

    def _refresh_inspection_evidence(self) -> None:
        evidence = {}
        for service, details in self._verify_project_ownership().items():
            config = details.get("Config") or {}
            host = details.get("HostConfig") or {}
            state = details.get("State") or {}
            env = dict(
                item.split("=", 1) for item in (config.get("Env") or []) if "=" in item
            )
            evidence[service] = {
                "container_id": details.get("Id"),
                "image_id": details.get("Image"),
                "user": config.get("User"),
                "read_only": host.get("ReadonlyRootfs"),
                "running": state.get("Running") is True,
                "health": (state.get("Health") or {}).get("Status"),
                "oom_killed": state.get("OOMKilled") is True,
                "restart_count": details.get("RestartCount"),
                "started_at": state.get("StartedAt"),
                "environment_names": sorted(env),
                "mount_targets": sorted(
                    mount.get("Destination", "")
                    for mount in details.get("Mounts") or []
                ),
                "limits": {
                    "memory_bytes": host.get("Memory"),
                    "nano_cpus": host.get("NanoCpus"),
                    "pids_limit": host.get("PidsLimit"),
                    "cap_drop": sorted(host.get("CapDrop") or []),
                    "security_options": sorted(host.get("SecurityOpt") or []),
                },
            }
            expected_image = (
                self._image_ids["workspace"]
                if service in _WORKSPACE_SERVICES
                else self._image_ids["api"]
                if service == "api"
                else self._image_ids["inference"]
                if service == "inference-probe"
                else self._image_ids["assets_source"]
            )
            if details.get("Image") != expected_image:
                raise RuntimeError(f"{service} uses an unexpected image artifact")
            expected_user = (
                "10004:10004"
                if service in _WORKSPACE_SERVICES
                else "10003:10003"
                if service in {"api", "workspace-guide-objects"}
                else "10008:10008"
            )
            if config.get("User") != expected_user:
                raise RuntimeError(f"{service} runs as an unexpected UID/GID")
            expected_memory = (
                768 * 1024 * 1024 if service == "api" else 256 * 1024 * 1024
            )
            if (
                host.get("ReadonlyRootfs") is not True
                or "ALL" not in (host.get("CapDrop") or [])
                or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
                or host.get("PidsLimit") != 64
                or host.get("NanoCpus") != 1_000_000_000
                or host.get("Memory") != expected_memory
            ):
                raise RuntimeError(f"{service} does not match the owned runtime limits")
            secret_targets = {
                mount.get("Destination")
                for mount in details.get("Mounts") or []
                if str(mount.get("Destination", "")).startswith("/run/secrets/")
            }
            expected_secrets = {
                "workspace-membership": {
                    "membership_database_url",
                    "membership_control_token",
                },
                "workspace-owner-transfer": {
                    "owner_transfer_database_url",
                    "membership_control_token",
                },
                "workspace-configuration": {
                    "configuration_database_url",
                    "membership_control_token",
                    "project_query_database_url",
                    "identity_briefs_database_url",
                    "workspace_inference_probe_token",
                    "guide_objects_token",
                },
                "workspace-template-query": {
                    "template_query_database_url",
                    "membership_control_token",
                },
                "workspace-project-query": {
                    "project_query_database_url",
                    "identity_briefs_database_url",
                    "membership_control_token",
                },
                "workspace-directory-query": {
                    "directory_query_database_url",
                    "identity_briefs_database_url",
                    "membership_control_token",
                },
                "inference-probe": {
                    "inference_backend_probe_database_url",
                    "inference_backend_probe_control_token",
                },
                "workspace-guide-objects": {
                    "guide_source_token",
                    "guide_source_access_key",
                    "guide_source_secret_key",
                },
                "api": {
                    "membership_control_token",
                    "identity_auth_database_url",
                    "identity_briefs_database_url",
                },
            }[service]
            if secret_targets != {f"/run/secrets/{name}" for name in expected_secrets}:
                raise RuntimeError(f"{service} received a cross-scope secret mount")
            for mount in details.get("Mounts") or []:
                if mount.get("Type") == "bind" and mount.get("RW") is not False:
                    raise RuntimeError(f"{service} has a writable host mount")
                source = mount.get("Source")
                if source and Path(source).resolve().is_relative_to(ROOT):
                    raise RuntimeError(f"{service} mounts the source tree")
            if service == "api":
                api_database = make_url(env.get("DATABASE_URL", ""))
                if (
                    api_database.username != "identity_api_consumer"
                    or api_database.host != self._postgres_name
                    or api_database.port != 5432
                    or api_database.database != self._database_name
                ):
                    raise RuntimeError(
                        "API does not use the restricted Workspace consumer"
                    )
                for prefix, expected_url in _API_WORKSPACE_BINDINGS.items():
                    if env.get(f"{prefix}_URL") != expected_url:
                        raise RuntimeError(
                            f"API {prefix} URL does not select service DNS"
                        )
                    if env.get(f"{prefix}_DATABASE_URL") or env.get(
                        f"{prefix}_DATABASE_URL_FILE"
                    ):
                        raise RuntimeError(f"API {prefix} has a competing direct DSN")
                if env.get("IDENTITY_URL") or env.get("IDENTITY_TOKEN"):
                    raise RuntimeError("API fell back to a remote Identity gateway")
                if env.get("IDENTITY_AUTH_DATABASE_URL") or not env.get(
                    "IDENTITY_AUTH_DATABASE_URL_FILE"
                ):
                    raise RuntimeError("API Identity AUTH is not function-only bound")
                if env.get("IDENTITY_BRIEFS_DATABASE_URL") or not env.get(
                    "IDENTITY_BRIEFS_DATABASE_URL_FILE"
                ):
                    raise RuntimeError("API Identity Briefs is not function-only bound")
                if (
                    env.get("WORKSPACE_MEMBERSHIP_CONTROL_TOKEN")
                    or env.get("WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE")
                    != "/run/secrets/membership_control_token"
                ):
                    raise RuntimeError(
                        "API does not use the scoped Workspace control secret"
                    )
                if not env.get("MINIO_ACCESS_KEY") or not env.get("MINIO_SECRET_KEY"):
                    raise RuntimeError("API bootstrap S3 credentials are missing")
                if env.get("SECRET_KEY") != self._api_secret:
                    raise RuntimeError("API signer differs from this verifier")
            if state.get("OOMKilled") is True:
                raise RuntimeError(f"Owned Workspace service {service} was OOM-killed")
        if set(evidence) != _EXPECTED_SERVICES:
            raise RuntimeError("Compose evidence omitted an assembled service")
        self._inspection_evidence = evidence

    def _write_secret(self, name: str, value: str) -> Path:
        if not isinstance(value, str) or "\x00" in value or "\n" in value:
            raise ValueError("Workspace secret values must be single-line text")
        path = self._secret_directory / name
        if path.is_symlink():
            raise RuntimeError("Workspace secret path must not be a symlink")
        if path.exists():
            if path.read_text(encoding="utf-8") == value:
                path.chmod(0o444)
                return path
            path.chmod(0o600)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o444)
        return path

    @staticmethod
    def _write_environment_file(path: Path, values: dict[str, str]) -> None:
        lines = []
        for name, raw in sorted(values.items()):
            value = str(raw)
            if "\x00" in value or "\n" in value:
                raise ValueError("Compose values must be single-line text")
            lines.append(f"{name}={json.dumps(value, ensure_ascii=False)}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def _write_log(self, message: str) -> None:
        self._log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._log_file.open("a", encoding="utf-8") as log:
            log.write(self.redact(message) + "\n")
        self._log_file.chmod(0o600)

    def _role_dsn(self, variable: str, role: str) -> URL:
        value = make_url(_role_url(variable, role))
        if (
            value.username != role
            or value.database != self._database_name
            or value.host != self.test_engine.url.host
            or value.port != self.test_engine.url.port
            or not self._database_name.endswith("_test")
        ):
            raise RuntimeError(f"Verifier role URL {variable} does not match test DB")
        _verify_postgres(value)
        return value.set(host=self._postgres_name, port=5432, drivername="postgresql")

    def _validate_service(self, service: str) -> None:
        if service not in _EXPECTED_SERVICES:
            raise ValueError("Unknown Workspace assembly service")

    def _validate_database_name(self, name: str) -> None:
        if (
            not name.endswith("_test")
            or re.fullmatch(r"[a-zA-Z0-9_]{1,63}", name) is None
        ):
            raise RuntimeError("Workspace assembly requires the disposable test DB")

    def _docker_url(self, value: str) -> str:
        try:
            url = make_url(value)
            if not url.host:
                raise ValueError
            return url.set(host=self._redis_name, port=6379).render_as_string(
                hide_password=False
            )
        except (TypeError, ValueError):
            raise ValueError("Verifier supplied an invalid Redis URL") from None

    @staticmethod
    def _docker_http_endpoint(value: str) -> str:
        default_scheme = (
            "https"
            if os.environ.get("MINIO_USE_SSL", "false").lower() in {"1", "true", "yes"}
            else "http"
        )
        parsed = urlsplit(value if "://" in value else f"{default_scheme}://{value}")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Verifier supplied an invalid object storage endpoint")
        scheme = parsed.scheme or "http"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{scheme}://host.docker.internal{port}"

    @classmethod
    def _docker_public_url(cls, value: str) -> str:
        if not value.startswith("http"):
            return value
        parsed = urlsplit(value)
        if (
            not parsed.scheme
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Verifier supplied an invalid public S3 URL")
        endpoint = cls._docker_http_endpoint(f"{parsed.scheme}://{parsed.netloc}")
        return f"{endpoint}{parsed.path}"

    @staticmethod
    def _docker_environment() -> dict[str, str]:
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
            raise RuntimeError(f"Verifier input {name} is missing")
        return value

    @staticmethod
    def _required_or(name: str, default: str) -> str:
        return os.environ.get(name, default)
