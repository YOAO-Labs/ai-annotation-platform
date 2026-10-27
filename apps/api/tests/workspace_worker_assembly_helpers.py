"""Private, verifier-owned Docker resources for cold Celery worker acceptance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from urllib.parse import urlsplit
import uuid

from sqlalchemy.engine import URL, make_url

from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url


ROOT = Path(__file__).resolve().parents[3]


def _worker_queue_name(identity: str, queue_name: str | None = None) -> str:
    if queue_name is None:
        return f"aap_workspace_worker_{identity}"
    if queue_name not in {"default", "export", "media"}:
        raise ValueError(
            "Only the explicit default and export worker queues are supported"
        )
    return queue_name


class WorkspaceCeleryWorker:
    """Run an installed API artifact as a real worker on owned PG/Redis peers."""

    def __init__(
        self,
        test_engine,
        directory: Path,
        *,
        backend_token: str,
        object_store_endpoint: str,
        object_store_bucket: str,
        object_store_use_ssl: bool,
        object_store_access_key: str,
        object_store_secret_key: str,
        object_store_buckets: dict[str, str] | None = None,
        queue_name: str | None = None,
    ) -> None:
        self.test_engine = test_engine
        self.directory = Path(directory).resolve()
        self.secret_directory = self.directory / "workspace-worker-secrets"
        self.environment_file = self.secret_directory / "worker.env"
        self.result_dsn_file = self.secret_directory / "preannotation-results-dsn"
        self.backend_token = backend_token
        self.object_store_endpoint = self._docker_http_endpoint(object_store_endpoint)
        self.object_store_public_url = object_store_endpoint.rstrip("/")
        self.object_store_bucket = object_store_bucket
        self.object_store_use_ssl = object_store_use_ssl
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key
        self.object_store_buckets = dict(object_store_buckets or {})
        if object_store_buckets is not None and (
            len(self.object_store_buckets) != 10
            or self.object_store_buckets.get("MINIO_BUCKET") != object_store_bucket
            or "MINIO_EXPORT_BUCKET" not in self.object_store_buckets
            or any(
                not key.startswith("MINIO_") or not key.endswith("_BUCKET") or not value
                for key, value in self.object_store_buckets.items()
            )
        ):
            raise RuntimeError("Worker requires all ten owned object storage buckets")
        self.secret_key = secrets.token_urlsafe(32)
        self.identity = uuid.uuid4().hex[:20]
        self.network_name = f"aap-workspace-worker-{self.identity}"
        self.container_name = self.network_name
        self.queue = _worker_queue_name(self.identity, queue_name)
        self.node_name = f"{self.network_name}@worker"
        self.container_id: str | None = None
        self.network_id: str | None = None
        self._docker_environment = self._minimal_docker_environment()
        _verify_postgres(test_engine.url)
        self._redis_owner_url = _verify_redis()
        self.postgres_container = self._required("AAP_MODULE_DATA_PG_CONTAINER_ID")
        self.redis_container = self._required("AAP_MODULE_DATA_REDIS_CONTAINER_NAME")
        self.image = self._required("AAP_WORKSPACE_MODULE_API_IMAGE")
        self.image_identity = self._required("AAP_WORKSPACE_MODULE_API_IDENTITY")
        self._image_record = self._verify_image()
        self._database_name = str(test_engine.url.database or "")
        if not self._database_name.endswith("_test"):
            raise RuntimeError("Worker assembly requires the disposable test DB")
        self._api_database_url = self._api_consumer_url()
        self._result_database_url = self._role_dsn(
            "AAP_MODULE_DATA_PREANNOTATION_RESULT_URL",
            "preannotation_result_client",
        )
        broker_url = make_url(
            os.environ.get("CELERY_BROKER_URL") or self._required("REDIS_URL")
        )
        if not self._same_redis_target(self._redis_owner_url, broker_url):
            raise RuntimeError("Celery broker does not target the verified Redis peer")
        self._redis_url = self._docker_url(self._required("REDIS_URL"), "worker-redis")

    @property
    def api_database_url(self) -> URL:
        return self._api_database_url

    def start(self) -> None:
        self._prepare_files()
        self.network_id = self._docker(
            "network",
            "create",
            "--label",
            "org.aap.acceptance=workspace-worker-assembly",
            "--label",
            f"org.aap.acceptance-id={self.identity}",
            self.network_name,
        )
        _verify_postgres(self.test_engine.url)
        _verify_redis()
        self._docker(
            "network",
            "connect",
            "--alias",
            "worker-postgres",
            self.network_id,
            self.postgres_container,
        )
        self._docker(
            "network",
            "connect",
            "--alias",
            "worker-redis",
            self.network_id,
            self.redis_container,
        )
        self.container_id = self._docker(
            "create",
            "--name",
            self.container_name,
            "--label",
            "org.aap.acceptance=workspace-worker-assembly",
            "--label",
            f"org.aap.acceptance-id={self.identity}",
            "--label",
            f"org.aap.source-revision={self._image_record['source_revision']}",
            "--network",
            self.network_id,
            "--add-host",
            "host.docker.internal:host-gateway",
            "--env-file",
            str(self.environment_file),
            "--mount",
            f"type=bind,source={self.result_dsn_file},target=/run/secrets/preannotation_results_dsn,readonly",
            "--user",
            "10003:10003",
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
            self.image,
            "celery",
            "-A",
            "app.workers.celery_app:celery_app",
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--prefetch-multiplier=1",
            f"--queues={self.queue}",
            f"--hostname={self.node_name}",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=INFO",
        )
        self._docker("start", self.container_id)
        self.assert_runtime()
        self.assert_private_network()

    def restart(self) -> None:
        if self.container_id is None:
            raise RuntimeError("Worker container has not been created")
        self._verify_container_record(self._inspect(self.container_id))
        self._docker("restart", "--time", "20", self.container_id, timeout=45)
        self.assert_runtime()
        self.assert_private_network()

    def kill(self) -> dict:
        """Lose the owned worker process without running application cleanup."""
        before = self.assert_runtime()
        self.assert_private_network()
        self._docker("kill", "--signal", "KILL", self.container_id)
        if self._docker("wait", self.container_id, timeout=20) != "137":
            raise RuntimeError("Owned worker did not exit from SIGKILL")
        record = self._inspect(self.container_id)
        self._verify_container_record(record)
        state = record.get("State") or {}
        if state.get("Running") or state.get("ExitCode") != 137:
            raise RuntimeError("Owned worker did not terminate from SIGKILL")
        return {
            "container_id": record["Id"],
            "started_at": before["started_at"],
            "finished_at": state.get("FinishedAt"),
            "exit_code": state["ExitCode"],
        }

    def task_signature(self, task_name: str) -> dict:
        """Inspect the callable in the same immutable image as the live worker."""
        if task_name not in {
            "app.workers.cross_frame_job.run_cross_frame_job",
            "app.workers.export.run_export",
            "app.workers.video_tracker.run_video_tracker_job",
            "app.workers.predictions_retry.retry_failed_prediction",
            "app.workers.audit.persist_audit_entry",
        }:
            raise ValueError("Task is outside the qualified worker paths")
        self.assert_runtime()
        code = (
            "import importlib,inspect,json,sys; "
            "module,name=sys.argv[1].rsplit('.',1); "
            "task=getattr(importlib.import_module(module),name); "
            "print(json.dumps({'task':task.name,"
            "'parameters':list(inspect.signature(task.run).parameters)}))"
        )
        return json.loads(
            self._docker(
                "exec", self.container_id, "/opt/venv/bin/python", "-c", code, task_name
            )
        )

    def task_delivery_policy(self, task_name: str) -> dict:
        self.task_signature(task_name)
        code = (
            "import importlib,json,sys; "
            "module,name=sys.argv[1].rsplit('.',1); "
            "task=getattr(importlib.import_module(module),name); "
            "print(json.dumps({'acks_late':bool(task.acks_late),"
            "'reject_on_worker_lost':bool(task.reject_on_worker_lost)}))"
        )
        return json.loads(
            self._docker(
                "exec", self.container_id, "/opt/venv/bin/python", "-c", code, task_name
            )
        )

    def kill_prefork_child(self, pid: int) -> dict:
        if type(pid) is not int or pid <= 1:
            raise ValueError("Worker child PID must be an integer greater than one")
        before = self.assert_runtime()
        self.assert_private_network()
        code = (
            "import os,pathlib,signal,sys; "
            "pid=int(sys.argv[1]); path=pathlib.Path('/proc')/str(pid); "
            "status=dict(line.split(':',1) for line in (path/'status').read_text().splitlines() if ':' in line); "
            "assert int(status['PPid'])==1; "
            "assert path.stat().st_uid==os.getuid()==10003; "
            "assert b'celery' in (path/'cmdline').read_bytes(); "
            "os.kill(pid,signal.SIGKILL)"
        )
        self._docker(
            "exec", self.container_id, "/opt/venv/bin/python", "-c", code, str(pid)
        )
        after = self.assert_runtime()
        if after["started_at"] != before["started_at"]:
            raise RuntimeError("Losing the child also restarted the worker parent")
        return {
            "container_id": self.container_id,
            "child_pid": pid,
            "parent_unchanged": True,
        }

    def stop(self) -> None:
        if self.container_id is not None:
            record = self._inspect(self.container_id, required=False)
            if record is not None:
                self._verify_container_record(record)
                if (record.get("State") or {}).get("Running"):
                    self._docker("stop", "--time", "20", self.container_id, timeout=45)
                self._docker("rm", self.container_id)
            self.container_id = None
        if self.network_id is not None:
            _verify_postgres(self.test_engine.url)
            _verify_redis()
            for peer in (self.postgres_container, self.redis_container):
                record = self._inspect(peer)
                if self.network_name in (
                    record.get("NetworkSettings", {}).get("Networks") or {}
                ):
                    self._docker("network", "disconnect", self.network_id, peer)
            network = self._inspect_network(self.network_id)
            if network.get("Containers"):
                raise RuntimeError("Worker assembly network still has attached peers")
            self._docker("network", "rm", self.network_id)
            self.network_id = None

    def assert_runtime(self) -> dict:
        if self.container_id is None:
            raise RuntimeError("Worker container has not been created")
        record = self._inspect(self.container_id)
        self._verify_container_record(record)
        config = record.get("Config") or {}
        host = record.get("HostConfig") or {}
        if (
            (record.get("State") or {}).get("Running") is not True
            or config.get("Image") != self.image
            or config.get("User") != "10003:10003"
            or host.get("ReadonlyRootfs") is not True
            or "ALL" not in (host.get("CapDrop") or [])
            or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
            or host.get("Memory") != 512 * 1024 * 1024
            or host.get("NanoCpus") != 1_000_000_000
            or host.get("PidsLimit") != 64
        ):
            raise RuntimeError("Celery worker process does not match its owned runtime")
        env = dict(
            item.split("=", 1) for item in (config.get("Env") or []) if "=" in item
        )
        database_url = make_url(env.get("DATABASE_URL", ""))
        if (
            database_url.username != "identity_api_consumer"
            or database_url.host != "worker-postgres"
            or database_url.port != 5432
            or database_url.database != self._database_name
        ):
            raise RuntimeError("Worker did not use the restricted API database role")
        if (
            env.get("PREANNOTATION_RESULTS_DATABASE_URL")
            or env.get("PREANNOTATION_RESULTS_DATABASE_URL_FILE")
            != "/run/secrets/preannotation_results_dsn"
        ):
            raise RuntimeError("Worker result owner binding is not function-only")
        workspace_prefixes = (
            "WORKSPACE_MEMBERSHIP",
            "WORKSPACE_OWNER_TRANSFER",
            "WORKSPACE_CONFIGURATION",
            "WORKSPACE_TEMPLATE_QUERY",
            "WORKSPACE_PROJECT_QUERY",
            "WORKSPACE_DIRECTORY_QUERY",
        )
        for prefix in workspace_prefixes:
            for suffix in ("_URL", "_DATABASE_URL", "_DATABASE_URL_FILE"):
                if env.get(prefix + suffix):
                    raise RuntimeError(
                        "Worker has a competing Workspace owner selector"
                    )
        if env.get("IDENTITY_URL") or env.get("IDENTITY_TOKEN"):
            raise RuntimeError("Worker unexpectedly selects remote Identity")
        if (
            env.get("MINIO_ENDPOINT") != self.object_store_endpoint
            or env.get("MINIO_PUBLIC_URL") != self.object_store_public_url
            or env.get("MINIO_BUCKET") != self.object_store_bucket
            or env.get("MINIO_ACCESS_KEY") != self.object_store_access_key
            or env.get("MINIO_SECRET_KEY") != self.object_store_secret_key
            or env.get("MINIO_USE_SSL")
            != ("true" if self.object_store_use_ssl else "false")
        ):
            raise RuntimeError(
                "Worker object storage identity is not the scoped Guide identity"
            )
        if any(
            env.get(key) != value for key, value in self.object_store_buckets.items()
        ):
            raise RuntimeError("Worker bucket bindings differ from its owned resources")
        if env.get("WORKSPACE_INFERENCE_PROBE_URL"):
            raise RuntimeError("Worker unexpectedly selects a Workspace HTTP owner")
        mounts = record.get("Mounts") or []
        secrets = {
            mount.get("Destination")
            for mount in mounts
            if mount.get("Destination") == "/run/secrets/preannotation_results_dsn"
        }
        if secrets != {"/run/secrets/preannotation_results_dsn"}:
            raise RuntimeError("Worker result DSN secret mount is missing")
        bind_mounts = [mount for mount in mounts if mount.get("Type") == "bind"]
        if (
            len(bind_mounts) != 1
            or bind_mounts[0].get("Destination")
            != "/run/secrets/preannotation_results_dsn"
            or Path(bind_mounts[0].get("Source", "")).resolve()
            != self.result_dsn_file.resolve()
            or bind_mounts[0].get("RW") is not False
        ):
            raise RuntimeError("Worker has an unexpected bind or source-code mount")
        return {
            "container_id": record.get("Id"),
            "image_id": record.get("Image"),
            "restart_count": (record.get("RestartCount") or 0),
            "started_at": (record.get("State") or {}).get("StartedAt"),
            "database_client_address": record["NetworkSettings"]["Networks"][
                self.network_name
            ]["IPAddress"],
            "running": (record.get("State") or {}).get("Running") is True,
            "user": config.get("User"),
            "read_only": host.get("ReadonlyRootfs"),
            "mount_targets": sorted(mount.get("Destination", "") for mount in mounts),
            "environment_names": sorted(env),
            "limits": {
                "memory_bytes": host.get("Memory"),
                "nano_cpus": host.get("NanoCpus"),
                "pids_limit": host.get("PidsLimit"),
            },
        }

    def assert_private_network(self) -> None:
        if self.network_id is None or self.container_id is None:
            raise RuntimeError("Worker assembly network has not been created")
        record = self._inspect_network(self.network_id)
        attached = set((record.get("Containers") or {}).keys())
        postgres_id = self._inspect(self.postgres_container)["Id"]
        redis_id = self._inspect(self.redis_container)["Id"]
        if attached != {self.container_id, postgres_id, redis_id}:
            raise RuntimeError("Worker network contains missing or foreign peers")

    def capture_diagnostics(self) -> str:
        records = {"network": None, "container": None, "logs": ""}
        if self.network_id is not None:
            records["network"] = self._inspect_network(self.network_id)
        if self.container_id is not None:
            record = self._inspect(self.container_id, required=False)
            records["container"] = (
                {
                    "id": record.get("Id"),
                    "state": record.get("State"),
                    "image": record.get("Image"),
                }
                if record is not None
                else None
            )
            if record is not None:
                output = subprocess.run(
                    ["docker", "logs", "--tail", "250", self.container_id],
                    env=self._docker_environment,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                records["logs"] = self.redact(output.stdout + output.stderr)
                records["log_exit_code"] = output.returncode
        path = ROOT / ".worktree" / f"workspace-worker-{self.identity}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return str(path)

    def redact(self, value: str) -> str:
        secrets = [
            self.backend_token,
            self.secret_key,
            self.object_store_access_key,
            self.object_store_secret_key,
            self._api_database_url.password or "",
            self._result_database_url.password or "",
            make_url(self._required("REDIS_URL")).password or "",
            make_url(
                os.environ.get("CELERY_BROKER_URL") or self._required("REDIS_URL")
            ).password
            or "",
        ]
        for name, value_from_environment in getattr(
            self, "_tracker_environment", {}
        ).items():
            if (
                not isinstance(value_from_environment, str)
                or not value_from_environment
            ):
                continue
            if re.search(r"(?i)(?:TOKEN|KEY|SECRET|PASSWORD)", name):
                secrets.append(value_from_environment)
            if "DATABASE_URL" in name:
                secrets.append(value_from_environment)
                try:
                    secrets.append(make_url(value_from_environment).password or "")
                except Exception:
                    # The full selector is already scrubbed if it cannot be parsed.
                    pass
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "<redacted>")
        return re.sub(
            r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
            "<redacted-jwt>",
            value,
        )

    def _prepare_files(self) -> None:
        self.secret_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.secret_directory.is_symlink():
            raise RuntimeError("Worker secret directory must not be a symlink")
        self.secret_directory.chmod(0o700)
        self.result_dsn_file.write_text(
            self._result_database_url.render_as_string(hide_password=False),
            encoding="utf-8",
        )
        self.result_dsn_file.chmod(0o444)
        values = {
            "ENVIRONMENT": "development",
            "DEBUG": "false",
            "SECRET_KEY": self.secret_key,
            "DATABASE_URL": self._api_database_url.render_as_string(
                hide_password=False
            ),
            "MIGRATION_DATABASE_URL": "",
            "ALEMBIC_AUTO_UPGRADE": "false",
            "E2E_SEED_ENABLED": "false",
            "REDIS_URL": self._redis_url,
            "CELERY_BROKER_URL": self._redis_url,
            "PREANNOTATION_RESULTS_DATABASE_URL": "",
            "PREANNOTATION_RESULTS_DATABASE_URL_FILE": "/run/secrets/preannotation_results_dsn",
            "IDENTITY_URL": "",
            "IDENTITY_TOKEN": "",
            "MINIO_ENDPOINT": self.object_store_endpoint,
            "MINIO_PUBLIC_URL": self.object_store_public_url,
            "MINIO_ACCESS_KEY": self.object_store_access_key,
            "MINIO_SECRET_KEY": self.object_store_secret_key,
            "MINIO_BUCKET": self.object_store_bucket,
            "MINIO_USE_SSL": "true" if self.object_store_use_ssl else "false",
            "IDENTITY_AUTH_DATABASE_URL": "",
            "IDENTITY_AUTH_DATABASE_URL_FILE": "",
            "IDENTITY_BRIEFS_DATABASE_URL": "",
            "IDENTITY_BRIEFS_DATABASE_URL_FILE": "",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN": "",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": "",
            "WORKSPACE_INFERENCE_PROBE_URL": "",
            "HOME": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        for prefix in (
            "WORKSPACE_MEMBERSHIP",
            "WORKSPACE_OWNER_TRANSFER",
            "WORKSPACE_CONFIGURATION",
            "WORKSPACE_TEMPLATE_QUERY",
            "WORKSPACE_PROJECT_QUERY",
            "WORKSPACE_DIRECTORY_QUERY",
        ):
            values[f"{prefix}_URL"] = ""
            values[f"{prefix}_DATABASE_URL"] = ""
            values[f"{prefix}_DATABASE_URL_FILE"] = ""
        values.update(self.object_store_buckets)
        self._write_env_file(self.environment_file, values)

    def _write_env_file(self, path: Path, values: dict[str, str]) -> None:
        lines = []
        for key, value in sorted(values.items()):
            if "\x00" in value or "\n" in value:
                raise ValueError("Worker environment values must be single-line text")
            lines.append(f"{key}={value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def _verify_container_record(self, record: dict) -> None:
        labels = (record.get("Config") or {}).get("Labels") or {}
        if (
            labels.get("org.aap.acceptance") != "workspace-worker-assembly"
            or labels.get("org.aap.acceptance-id") != self.identity
            or record.get("Image") != self._image_record["id"]
            or labels.get("com.docker.compose.project") is not None
        ):
            raise RuntimeError("Worker container is not owned by this acceptance")

    def _verify_image(self) -> dict[str, str]:
        output = self._docker("image", "inspect", self.image)
        records = json.loads(output)
        if not isinstance(records, list) or len(records) != 1:
            raise RuntimeError("Worker image identity is not unique")
        labels = (records[0].get("Config") or {}).get("Labels") or {}
        revision = labels.get("org.aap.source-revision")
        if (
            labels.get("org.aap.acceptance") != "workspace-module-api"
            or labels.get("org.aap.acceptance-id") != self.image_identity
            or not isinstance(revision, str)
            or re.fullmatch(r"[0-9a-f]{7,64}", revision) is None
        ):
            raise RuntimeError("Worker image does not match the verifier artifact")
        return {"id": records[0]["Id"], "source_revision": revision}

    def _api_consumer_url(self) -> URL:
        host_url = make_url(self._required("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"))
        container_url = make_url(
            self._required("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_CONTAINER_URL")
        )
        if (
            host_url.username != "identity_api_consumer"
            or host_url.database != self._database_name
            or host_url.host != self.test_engine.url.host
            or host_url.port != self.test_engine.url.port
            or not self._database_name.endswith("_test")
            or container_url.username != host_url.username
            or container_url.password != host_url.password
            or container_url.database != host_url.database
            or container_url.host != "host.docker.internal"
            or container_url.port != host_url.port
        ):
            raise RuntimeError("Verifier API consumer identity does not match test DB")
        return container_url.set(
            host="worker-postgres",
            port=5432,
        )

    def _role_dsn(self, variable: str, role: str) -> URL:
        value = make_url(_role_url(variable, role))
        if (
            value.username != role
            or value.database != self._database_name
            or value.host != self.test_engine.url.host
            or value.port != self.test_engine.url.port
        ):
            raise RuntimeError(f"Verifier role URL {variable} does not match test DB")
        _verify_postgres(value)
        return value.set(host="worker-postgres", port=5432, drivername="postgresql")

    def _docker_url(self, value: str, host: str) -> str:
        try:
            parsed = make_url(value)
            if not parsed.host:
                raise ValueError
            return parsed.set(host=host, port=6379).render_as_string(
                hide_password=False
            )
        except (TypeError, ValueError):
            raise ValueError("Verifier supplied an invalid Redis URL") from None

    @staticmethod
    def _same_redis_target(left: URL, right: URL) -> bool:
        loopback = {"localhost", "127.0.0.1", "::1"}
        same_host = left.host == right.host or (
            left.host in loopback and right.host in loopback
        )
        return (
            same_host
            and left.port == right.port
            and left.database == right.database
            and left.username == right.username
            and left.password == right.password
        )

    @staticmethod
    def _docker_http_endpoint(value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Verifier supplied an invalid object storage endpoint")
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"host.docker.internal{port}"

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

    def _inspect_network(self, identity: str) -> dict:
        record = self._docker("network", "inspect", identity)
        values = json.loads(record)
        if not isinstance(values, list) or len(values) != 1:
            raise RuntimeError("Worker network identity is not unique")
        network = values[0]
        labels = network.get("Labels") or {}
        if (
            network.get("Name") != self.network_name
            or labels.get("org.aap.acceptance") != "workspace-worker-assembly"
            or labels.get("org.aap.acceptance-id") != self.identity
        ):
            raise RuntimeError("Worker network ownership did not match")
        return network

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
            raise RuntimeError("Owned worker Docker operation timed out") from None
        if result.returncode != 0:
            raise RuntimeError("Owned worker Docker operation failed")
        return result.stdout.strip()

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
            raise RuntimeError(f"Worker verifier input {name} is missing")
        return value
