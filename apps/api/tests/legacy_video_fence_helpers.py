"""Fail-closed ownership and credential observations for the local legacy-video fixture.

This module is test-only. It never grants a Jobs retirement proof: it records the
observed stop and credential fence for one verifier-owned disposable environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url


_POSTGRES_IMAGE = "postgres:16-alpine"
_PG_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
_PG_NAME = re.compile(r"aap-module-data-[0-9a-f]{12}\Z")
_PG_ACCEPTANCE_ID = re.compile(r"[0-9a-f]{24}\Z")
_WORKER_ACCEPTANCE_ID = re.compile(r"[0-9a-f]{20}\Z")
_SOURCE_REVISION = re.compile(r"[0-9a-f]{7,64}\Z")
_ROLE_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_QUEUE_NAME = re.compile(r"legacy_video_fence_[0-9a-f]{20}\Z")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_WORKER_DATABASE_SOURCES = {
    "DATABASE_URL": "identity_api_consumer",
    "PREANNOTATION_RESULTS_DATABASE_URL_FILE": "preannotation_result_client",
    "INFERENCE_ROUTING_DATABASE_URL": "inference_routing_client",
    "LABELING_VIDEO_WRITER_DATABASE_URL": "labeling_video_writer_client",
}


class LocalFenceRefused(RuntimeError):
    """The fixture cannot prove exact local resource or credential ownership."""


S3_PROBE_BODY = b"owned-legacy-video-fence-probe\n"


def prove_s3_write_access(client, bucket: str, key: str) -> dict[str, Any]:
    """Establish the same authorized operation that must fail after revocation."""
    result = client.put_object(Bucket=bucket, Key=key, Body=S3_PROBE_BODY)
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        if body.read() != S3_PROBE_BODY:
            raise LocalFenceRefused("s3_probe_content_mismatch")
    finally:
        body.close()
    return {
        "operation": "PutObject",
        "key": key,
        "payload_sha256": sha256(S3_PROBE_BODY).hexdigest(),
        "succeeded_before_revoke": True,
        "version_id": result.get("VersionId"),
    }


def prove_s3_write_denied(client, bucket: str, key: str) -> str:
    from botocore.exceptions import ClientError

    try:
        client.put_object(Bucket=bucket, Key=key, Body=S3_PROBE_BODY)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in {"InvalidAccessKeyId", "AccessDenied", "AllAccessDisabled", "403"}:
            return str(code)
        raise LocalFenceRefused("stale_s3_credential_probe_inconclusive") from None
    raise LocalFenceRefused("stale_s3_credential_still_works")


def stop_fixture_workers(*workers) -> list[LocalFenceRefused]:
    errors = []
    for worker in workers:
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                errors.append(LocalFenceRefused("fixture_worker_cleanup_failed"))
    return errors


@dataclass(frozen=True)
class WorkerDatabaseCredential:
    source_variable: str
    role: str
    database: str
    container_host: str
    container_port: int
    secret_file: bool
    _url: URL = field(repr=False, compare=False)

    def evidence(self) -> dict[str, Any]:
        """Return identity metadata only; the DSN and password stay in memory."""
        return {
            "source_variable": self.source_variable,
            "role": self.role,
            "database": self.database,
            "container_host": self.container_host,
            "container_port": self.container_port,
            "secret_file": self.secret_file,
        }

    def host_url(self, *, host: str, port: int) -> URL:
        """Retarget this observed DSN to the verifier's loopback PG binding."""
        return self._url.set(
            drivername="postgresql+asyncpg",
            host=host,
            port=port,
        )


@dataclass(frozen=True)
class LocalPostgresIdentity:
    container_id: str
    container_name: str
    acceptance_id: str
    database: str
    host: str
    port: int
    database_oid: int
    cluster_system_identifier: int
    owner_role: str

    def evidence(self) -> dict[str, Any]:
        return {
            "container_id": self.container_id,
            "container_name": self.container_name,
            "acceptance_id": self.acceptance_id,
            "database": self.database,
            "host": self.host,
            "port": self.port,
            "database_oid": self.database_oid,
            "cluster_system_identifier": self.cluster_system_identifier,
            "owner_role": self.owner_role,
        }


@dataclass(frozen=True)
class WorkerCredentialInventory:
    identity: dict[str, Any]
    credentials: tuple[WorkerDatabaseCredential, ...] = field(repr=False)
    write_selectors: dict[str, Any] = field(default_factory=dict)

    def evidence(self) -> dict[str, Any]:
        return {
            "identity": dict(self.identity),
            "database_credentials": worker_credential_evidence(self.credentials),
            "write_selectors": dict(self.write_selectors),
        }


def worker_database_credentials(worker: Any) -> tuple[WorkerDatabaseCredential, ...]:
    """Observe DSNs from the exact running container, never from shared host env."""
    if not getattr(worker, "container_id", None):
        raise LocalFenceRefused("worker_container_unavailable")
    try:
        record = worker._inspect(worker.container_id)
        worker._verify_container_record(record)
        worker.assert_runtime()
    except Exception:
        raise LocalFenceRefused("worker_container_ownership_unavailable") from None
    config = record.get("Config") or {}
    environment = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for item in (config.get("Env") or [])
        if isinstance(item, str) and "=" in item
    }
    mounts = record.get("Mounts") or []

    def read_secret_file(path: str) -> str:
        matches = [
            mount
            for mount in mounts
            if mount.get("Destination") == path
            and mount.get("Source") == str(getattr(worker, "result_dsn_file", ""))
            and mount.get("Type") == "bind"
            and mount.get("RW") is False
        ]
        if path != "/run/secrets/preannotation_results_dsn" or len(matches) != 1:
            raise LocalFenceRefused("worker_secret_mount_ownership_mismatch")
        try:
            # Read only the container-owned, read-only mount. The DSN remains in
            # process memory and is never returned in diagnostics or evidence.
            return worker._docker(
                "exec",
                worker.container_id,
                "/opt/venv/bin/python",
                "-I",
                "-c",
                "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
                path,
            ).strip()
        except Exception:
            raise LocalFenceRefused("worker_secret_file_unavailable") from None

    database = str(getattr(worker, "_database_name", ""))
    return inventory_worker_database_credentials(
        environment,
        database=database,
        read_secret_file=read_secret_file,
    )


def inspect_live_worker_credentials(
    worker: Any,
    *,
    expected_database: str,
    expected_s3_access_key: str,
    expected_media_token: str,
) -> WorkerCredentialInventory:
    """Observe the exact worker container and every active write credential source."""
    identity = owned_worker_identity(worker)
    if identity.get("database") != expected_database:
        raise LocalFenceRefused("worker_database_identity_mismatch")
    record = worker._inspect(worker.container_id)
    environment = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for item in ((record.get("Config") or {}).get("Env") or [])
        if isinstance(item, str) and "=" in item
    }
    credentials = worker_database_credentials(worker)
    write_selectors = inspect_worker_write_selectors(
        environment,
        expected_s3_access_key=expected_s3_access_key,
        expected_media_token=expected_media_token,
    )
    return WorkerCredentialInventory(
        identity=identity,
        credentials=credentials,
        write_selectors=write_selectors,
    )


async def owned_postgres_identity(test_engine: Any) -> LocalPostgresIdentity:
    """Verify the exact disposable module-data PG container before DB mutation."""
    from tests.labeling_class_query_helpers import assert_owned_test_database
    from tests.test_asset_storage_cleanup_runtime import (
        _inspect_docker,
        _required_env,
        _verify_postgres,
    )

    url = test_engine.url
    try:
        _verify_postgres(url)
        resource = await assert_owned_test_database(test_engine)
        container_id = _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID")
        container_name = _required_env("AAP_MODULE_DATA_PG_CONTAINER_NAME")
        acceptance_id = _required_env("AAP_MODULE_DATA_PG_CONTAINER_IDENTITY")
        record = _inspect_docker(container_id)
    except Exception:
        raise LocalFenceRefused("postgres_fixture_ownership_unavailable") from None
    container = validate_owned_postgres_container(
        record,
        container_id=container_id,
        container_name=container_name,
        acceptance_id=acceptance_id,
        host_url=url,
    )
    if (
        resource.get("owner_role") != "owner"
        or resource.get("database") != container["database"]
    ):
        raise LocalFenceRefused("postgres_database_owner_mismatch")
    try:
        async with test_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT current_user,current_database(),db.oid,"
                        "control.system_identifier,role.rolsuper "
                        "FROM pg_catalog.pg_database AS db "
                        "CROSS JOIN LATERAL pg_catalog.pg_control_system() AS control "
                        "JOIN pg_catalog.pg_roles AS role ON role.rolname=current_user "
                        "WHERE db.datname=pg_catalog.current_database()"
                    )
                )
            ).one()
    except Exception:
        raise LocalFenceRefused("postgres_owner_session_unavailable") from None
    if (
        row.current_user != "owner"
        or row.current_database != container["database"]
        or not row.rolsuper
    ):
        raise LocalFenceRefused("postgres_owner_role_not_exclusive")
    return LocalPostgresIdentity(
        container_id=container_id,
        container_name=container_name,
        acceptance_id=acceptance_id,
        database=container["database"],
        host=container["host"],
        port=container["port"],
        database_oid=int(row.oid),
        cluster_system_identifier=int(row.system_identifier),
        owner_role=row.current_user,
    )


def owned_worker_identity(worker: Any) -> dict[str, Any]:
    """Read the exact worker container and enforce the fixture-only queue."""
    if not getattr(worker, "container_id", None):
        raise LocalFenceRefused("worker_container_unavailable")
    try:
        record = worker._inspect(worker.container_id)
        runtime = worker.assert_runtime()
    except Exception:
        raise LocalFenceRefused("worker_container_ownership_unavailable") from None
    queue = str(getattr(worker, "queue", ""))
    image_record = getattr(worker, "_image_record", {})
    image_id = image_record.get("id")
    source_revision = image_record.get("source_revision")
    image_acceptance_id = str(getattr(worker, "image_identity", ""))
    container_name = str(getattr(worker, "container_name", ""))
    acceptance_id = str(getattr(worker, "identity", ""))
    identity = validate_owned_worker_container(
        record,
        container_id=worker.container_id,
        container_name=container_name,
        acceptance_id=acceptance_id,
        image_id=str(image_id or ""),
        source_revision=str(source_revision or ""),
        queue=queue,
    )
    if runtime.get("started_at") != (record.get("State") or {}).get("StartedAt"):
        raise LocalFenceRefused("worker_started_at_changed")
    try:
        broker_url = make_url(getattr(worker, "_redis_url", ""))
        external_broker_url = make_url(
            os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL", "")
        )
    except Exception:
        raise LocalFenceRefused("worker_broker_identity_unavailable") from None
    if (
        broker_url.drivername != "redis"
        or broker_url.username is not None
        or broker_url.password is not None
        or broker_url.query
        or broker_url.host != "worker-redis"
        or broker_url.port != 6379
        or broker_url.database != "0"
        or external_broker_url.drivername != "redis"
        or external_broker_url.host not in _LOCAL_HOSTS
        or external_broker_url.username is not None
        or external_broker_url.password is not None
        or external_broker_url.query
        or external_broker_url.database != "0"
        or not worker._same_redis_target(worker._redis_owner_url, external_broker_url)
    ):
        raise LocalFenceRefused("worker_broker_identity_mismatch")
    from tests.test_identity_sessions_live import _verify_redis

    try:
        verified_redis_url = _verify_redis()
        redis_container_name = os.environ["AAP_MODULE_DATA_REDIS_CONTAINER_NAME"]
        redis_owner = os.environ["AAP_MODULE_DATA_REDIS_OWNER"]
        redis_record = worker._inspect(redis_container_name)
    except Exception:
        raise LocalFenceRefused("redis_resource_ownership_unavailable") from None
    redis_labels = (redis_record.get("Config") or {}).get("Labels") or {}
    redis_ports = ((redis_record.get("NetworkSettings") or {}).get("Ports") or {}).get(
        "6379/tcp"
    ) or []
    if not (
        redis_labels.get("org.aap.worktree-owner") == redis_owner
        and redis_record.get("Id")
        and redis_record.get("Name") == f"/{redis_container_name}"
        and len(redis_ports) == 1
        and redis_ports[0].get("HostIp") == "127.0.0.1"
        and str(redis_ports[0].get("HostPort")) == str(verified_redis_url.port)
        and verified_redis_url.host in _LOCAL_HOSTS
        and worker._same_redis_target(worker._redis_owner_url, verified_redis_url)
    ):
        raise LocalFenceRefused("redis_resource_ownership_mismatch")
    try:
        worker.assert_private_network()
    except Exception:
        raise LocalFenceRefused("worker_private_network_identity_mismatch") from None
    identity["network_id"] = worker.network_id
    identity["network_name"] = worker.network_name
    identity["database"] = str(worker._database_name)
    identity["image_acceptance_id"] = image_acceptance_id
    identity["worker_started_at"] = runtime["started_at"]
    identity["broker"] = {
        "host_alias": broker_url.host,
        "port": broker_url.port,
        "database": broker_url.database,
        "redis_container_id": redis_record["Id"],
        "redis_container_name": redis_container_name,
        "redis_owner": redis_owner,
        "redis_loopback_host": verified_redis_url.host,
        "redis_loopback_port": verified_redis_url.port,
        "shared_unauthenticated": True,
        "external_consumers_unknown": True,
        "queue_is_fixture_specific": True,
        "queue_purge_scope": "exact_fixture_queue_only",
    }
    return identity


def stop_owned_worker(worker: Any, expected: Mapping[str, Any]) -> dict[str, Any]:
    """Stop/remove only the exact verified fixture worker; never signal by port."""
    if not getattr(worker, "container_id", None):
        raise LocalFenceRefused("worker_container_unavailable")
    current = owned_worker_identity(worker)
    if any(
        current.get(key) != expected.get(key)
        for key in (
            "container_id",
            "container_name",
            "acceptance_id",
            "image_id",
            "image_acceptance_id",
            "source_revision",
            "database",
            "network_id",
            "network_name",
            "queue",
            "worker_started_at",
        )
    ):
        raise LocalFenceRefused("worker_identity_changed_before_stop")
    container_id = worker.container_id
    before = worker._inspect(container_id)
    try:
        worker._verify_container_record(before)
    except Exception:
        raise LocalFenceRefused(
            "worker_container_ownership_changed_before_stop"
        ) from None
    if (
        before.get("Id") != expected.get("container_id")
        or worker.network_id != expected.get("network_id")
        or worker.network_name != expected.get("network_name")
        or (before.get("State") or {}).get("Running") is not True
    ):
        raise LocalFenceRefused("worker_not_running_at_stop")
    network_id = worker.network_id
    network_name = worker.network_name
    try:
        worker._docker("stop", "--time", "20", container_id, timeout=45)
        waited = worker._docker("wait", container_id, timeout=30)
        stopped = worker._inspect(container_id)
        state = stopped.get("State") or {}
        if (
            state.get("Running")
            or str(state.get("ExitCode")) != str(waited)
            or state.get("StartedAt") != expected["worker_started_at"]
            or (stopped.get("RestartCount") or 0) != 0
            or ((stopped.get("HostConfig") or {}).get("RestartPolicy") or {}).get(
                "Name", ""
            )
            not in {"", "no"}
        ):
            raise LocalFenceRefused("worker_stop_boundary_unverified")
        # WorkspaceCeleryWorker.stop validates this exact ID/acceptance label
        # again, removes the container, and detaches only its private network.
        worker.stop()
        if worker._inspect(container_id, required=False) is not None:
            raise LocalFenceRefused("worker_container_removal_unverified")
    except LocalFenceRefused:
        raise
    except Exception:
        raise LocalFenceRefused("owned_worker_stop_failed") from None
    return {
        "container_id": container_id,
        "container_name": expected["container_name"],
        "acceptance_id": expected["acceptance_id"],
        "image_id": expected["image_id"],
        "image_acceptance_id": expected["image_acceptance_id"],
        "source_revision": expected["source_revision"],
        "database": expected["database"],
        "network_id": network_id,
        "network_name": network_name,
        "started_at": expected["worker_started_at"],
        "exit_code": int(waited),
        "restart_count": 0,
        "restart_policy": "no",
        "container_removed": True,
        "queue": expected["queue"],
    }


async def fence_disposable_database_logins(
    test_engine: Any,
    postgres: LocalPostgresIdentity,
    credentials: tuple[WorkerDatabaseCredential, ...],
) -> dict[str, Any]:
    """Disable every non-owner LOGIN in this one verifier-owned PG container.

    This intentionally cannot be called with a worktree-host PostgreSQL URL or a
    production/remote server: ``owned_postgres_identity`` only yields an identity
    for the verifier's uniquely labelled disposable container.
    """
    if (
        test_engine.url.host not in _LOCAL_HOSTS
        or test_engine.url.database != postgres.database
        or test_engine.url.username != postgres.owner_role
    ):
        raise LocalFenceRefused("postgres_target_changed_before_mutation")
    fresh_postgres = await owned_postgres_identity(test_engine)
    if fresh_postgres != postgres:
        raise LocalFenceRefused("postgres_identity_changed_before_mutation")
    observed_roles = {item.role for item in credentials}
    if not observed_roles:
        raise LocalFenceRefused("worker_database_inventory_empty")
    try:
        async with test_engine.begin() as connection:
            identity = (
                await connection.execute(
                    text(
                        "SELECT current_user,current_database(),role.rolsuper "
                        "FROM pg_catalog.pg_roles AS role "
                        "WHERE role.rolname=current_user"
                    )
                )
            ).one()
            if (
                identity.current_user != postgres.owner_role
                or identity.current_database != postgres.database
                or not identity.rolsuper
            ):
                raise LocalFenceRefused("postgres_owner_changed_before_mutation")
            # The owner is the only LOGIN retained. All other LOGIN roles are
            # inside the verifier-owned, single-run PostgreSQL container.
            login_roles = (
                (
                    await connection.execute(
                        text(
                            "SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,"
                            "rolreplication,rolbypassrls "
                            "FROM pg_catalog.pg_roles WHERE rolcanlogin "
                            "AND rolname<>:owner ORDER BY rolname"
                        ),
                        {"owner": postgres.owner_role},
                    )
                )
                .mappings()
                .all()
            )
            if not observed_roles.issubset({row["rolname"] for row in login_roles}):
                raise LocalFenceRefused("worker_database_role_inventory_mismatch")
            if any(
                row["rolsuper"]
                or row["rolcreatedb"]
                or row["rolcreaterole"]
                or row["rolreplication"]
                or row["rolbypassrls"]
                for row in login_roles
            ):
                raise LocalFenceRefused("unexpected_privileged_local_login")
            if not all(_ROLE_NAME.fullmatch(row["rolname"]) for row in login_roles):
                raise LocalFenceRefused("unexpected_local_login_name")
            database_name = test_engine.dialect.identifier_preparer.quote_identifier(
                postgres.database
            )
            await connection.exec_driver_sql(
                f"REVOKE CONNECT ON DATABASE {database_name} FROM PUBLIC"
            )
            for row in login_roles:
                role_name = test_engine.dialect.identifier_preparer.quote_identifier(
                    row["rolname"]
                )
                await connection.exec_driver_sql(
                    f"ALTER ROLE {role_name} NOLOGIN NOINHERIT"
                )
                await connection.exec_driver_sql(
                    f"REVOKE CONNECT ON DATABASE {database_name} FROM {role_name}"
                )
            sessions = (
                (
                    await connection.execute(
                        text(
                            "SELECT pid FROM pg_catalog.pg_stat_activity "
                            "WHERE datname=:database AND usesysid<>"
                            "(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=:owner) "
                            "ORDER BY pid"
                        ),
                        {"database": postgres.database, "owner": postgres.owner_role},
                    )
                )
                .scalars()
                .all()
            )
            terminated = 0
            for pid in sessions:
                stopped = await connection.scalar(
                    text("SELECT pg_catalog.pg_terminate_backend(:pid)"),
                    {"pid": pid},
                )
                if stopped is not True:
                    raise LocalFenceRefused("postgres_session_termination_failed")
                terminated += 1
    except LocalFenceRefused:
        raise
    except Exception:
        raise LocalFenceRefused("postgres_login_fence_failed") from None

    try:
        async with test_engine.connect() as connection:
            login_states = (
                (
                    await connection.execute(
                        text(
                            "SELECT rolname,rolcanlogin,rolinherit "
                            "FROM pg_catalog.pg_roles "
                            "WHERE rolname=ANY(:roles) ORDER BY rolname"
                        ),
                        {"roles": [row["rolname"] for row in login_roles]},
                    )
                )
                .mappings()
                .all()
            )
            connect_states = (
                (
                    await connection.execute(
                        text(
                            "SELECT role.role_name,pg_catalog.has_database_privilege("
                            "role.role_name,:database,'CONNECT') AS can_connect "
                            "FROM pg_catalog.unnest(CAST(:roles AS text[])) "
                            "AS role(role_name) ORDER BY role.role_name"
                        ),
                        {
                            "database": postgres.database,
                            "roles": [row["rolname"] for row in login_roles],
                        },
                    )
                )
                .mappings()
                .all()
            )
            remaining_sessions = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                    "WHERE datname=:database AND usesysid<>"
                    "(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=:owner)"
                ),
                {"database": postgres.database, "owner": postgres.owner_role},
            )
            remaining_logins = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_roles "
                    "WHERE rolcanlogin AND rolname<>:owner"
                ),
                {"owner": postgres.owner_role},
            )
    except Exception:
        raise LocalFenceRefused("postgres_login_fence_verification_failed") from None
    if (
        len(login_states) != len(login_roles)
        or any(row["rolcanlogin"] or row["rolinherit"] for row in login_states)
        or any(row["can_connect"] for row in connect_states)
        or remaining_sessions != 0
        or remaining_logins != 0
    ):
        raise LocalFenceRefused("postgres_login_fence_incomplete")
    return {
        "roles_disabled": [row["rolname"] for row in login_roles],
        "observed_worker_roles": sorted(observed_roles),
        "sessions_terminated": terminated,
        "non_owner_sessions_remaining": int(remaining_sessions),
        "all_non_owner_logins_disabled": True,
        "all_non_owner_connect_revoked": True,
        "database": postgres.database,
        "container_id": postgres.container_id,
        "non_owner_logins_remaining": int(remaining_logins),
    }


def validate_local_fence_preflight(value: Mapping[str, Any]) -> None:
    """Validate the complete sanitized preflight before any destructive action."""
    required = {
        "schema",
        "scope",
        "postgres",
        "worker",
        "worker_database_credentials",
        "worker_write_selectors",
        "storage",
        "media_peer",
        "backend_peer",
        "broker",
    }
    if (
        set(value) != required
        or value.get("schema") != "aap-local-video-fence-preflight/v1"
    ):
        raise LocalFenceRefused("local_fence_preflight_contract_mismatch")
    postgres = value.get("postgres")
    worker = value.get("worker")
    scope = value.get("scope")
    credentials = value.get("worker_database_credentials")
    selectors = value.get("worker_write_selectors")
    storage = value.get("storage")
    media = value.get("media_peer")
    backend = value.get("backend_peer")
    broker = value.get("broker")
    if (
        not all(
            isinstance(part, Mapping)
            for part in (postgres, worker, scope, storage, media, backend, broker)
        )
        or not isinstance(credentials, list)
        or not isinstance(selectors, Mapping)
    ):
        raise LocalFenceRefused("local_fence_preflight_contract_mismatch")
    write_probe = storage.get("write_probe")
    if not isinstance(write_probe, Mapping):
        raise LocalFenceRefused("local_fence_preflight_contract_mismatch")

    def local_origin(value: object) -> bool:
        try:
            origin = urlsplit(str(value))
            return (
                origin.scheme in {"http", "https"}
                and origin.hostname in _LOCAL_HOSTS
                and origin.username is None
                and origin.password is None
                and origin.port is not None
                and 1 <= origin.port <= 65535
                and origin.path in {"", "/"}
                and not origin.query
                and not origin.fragment
            )
        except ValueError:
            return False

    if (
        not _PG_CONTAINER.fullmatch(str(postgres.get("container_id", "")))
        or not _PG_NAME.fullmatch(str(postgres.get("container_name", "")))
        or not _PG_ACCEPTANCE_ID.fullmatch(str(postgres.get("acceptance_id", "")))
        or postgres.get("host") not in _LOCAL_HOSTS
        or not str(postgres.get("database", "")).endswith("_test")
        or postgres.get("owner_role") != "owner"
        or type(postgres.get("port")) is not int
        or postgres.get("port") < 1
        or type(postgres.get("database_oid")) is not int
        or postgres.get("database_oid") < 1
        or type(postgres.get("cluster_system_identifier")) is not int
        or postgres.get("cluster_system_identifier") < 1
        or not _PG_CONTAINER.fullmatch(str(worker.get("container_id", "")))
        or not _WORKER_ACCEPTANCE_ID.fullmatch(str(worker.get("acceptance_id", "")))
        or worker.get("container_name")
        != f"aap-workspace-worker-{worker.get('acceptance_id')}"
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(worker.get("image_id", "")))
        or not worker.get("image_acceptance_id")
        or not _PG_CONTAINER.fullmatch(str(worker.get("network_id", "")))
        or worker.get("network_name")
        != f"aap-workspace-worker-{worker.get('acceptance_id')}"
        or worker.get("database") != postgres.get("database")
        or not _SOURCE_REVISION.fullmatch(str(worker.get("source_revision", "")))
        or not _QUEUE_NAME.fullmatch(str(worker.get("queue", "")))
        or not worker.get("worker_started_at")
        or worker.get("restart_policy") != "no"
        or worker.get("restart_count") != 0
        or not credentials
        or {
            (item.get("source_variable"), item.get("role"))
            for item in credentials
            if isinstance(item, Mapping)
        }
        != set(_WORKER_DATABASE_SOURCES.items())
        or scope.get("owner") != "tracker:" + str(scope.get("job_id", ""))
        or scope.get("operation") != "tracker"
        or scope.get("job_status") != "running"
        or not scope.get("job_started_at")
        or scope.get("input_referenced") is not True
        or scope.get("deletion_guard_still_409") is not True
        or scope.get("closed") is not False
        or scope.get("state") != "active"
        or scope.get("attempt_count") != 0
        or scope.get("permit_count") != 0
        or scope.get("no_live_admission") is not True
        or scope.get("retirement_phase") is not None
        or scope.get("jobs_execution_present") is not False
        or scope.get("jobs_journal_present") is not False
        or scope.get("deletion_clearance_present") is not False
        or storage.get("owner_verified") is not True
        or storage.get("worker_identity_matches") is not True
        or storage.get("user_created") is not True
        or storage.get("policy_created") is not True
        or storage.get("access_key_id") is None
        or storage.get("policy_id") is None
        or not storage.get("bucket")
        or not _PG_CONTAINER.fullmatch(str(storage.get("container_id", "")))
        or storage.get("resource_owner") is None
        or write_probe.get("operation") != "PutObject"
        or not re.fullmatch(
            r"projects/[0-9a-f-]{36}/guide/legacy-video-fence-[0-9a-f]{32}\.txt",
            str(write_probe.get("key", "")),
        )
        or write_probe.get("payload_sha256") != sha256(S3_PROBE_BODY).hexdigest()
        or write_probe.get("succeeded_before_revoke") is not True
        or selectors.get("s3_principal_id") != storage.get("access_key_id")
        or selectors.get("media_control_token")
        != "present_and_bound_to_owned_media_peer"
        or set(selectors.get("database_roles", []))
        != set(_WORKER_DATABASE_SOURCES.values())
        or selectors.get("direct_routing_token") != "absent"
        or selectors.get("direct_writer_control_token") != "absent"
        or media.get("owned_process_count") != 1
        or media.get("running") is not True
        or not isinstance(media.get("process_id"), int)
        or not local_origin(media.get("endpoint"))
        or not isinstance(media.get("completed_materializer_process_ids"), list)
        or not media.get("completed_materializer_process_ids")
        or any(
            type(process_id) is not int or process_id < 1
            for process_id in media.get("completed_materializer_process_ids", [])
        )
        or backend.get("registry_id") is None
        or backend.get("management_available") is not True
        or backend.get("active_requests") != 0
        or backend.get("registry_id") != scope.get("registry_id")
        or not local_origin(backend.get("endpoint"))
        or broker.get("queue") != worker.get("queue")
        or not _PG_CONTAINER.fullmatch(str(broker.get("redis_container_id", "")))
        or broker.get("redis_container_name") is None
        or broker.get("redis_owner") is None
        or broker.get("redis_loopback_host") not in _LOCAL_HOSTS
        or broker.get("redis_loopback_port") is None
        or broker.get("shared_unauthenticated") is not True
        or broker.get("external_consumers_unknown") is not True
        or broker.get("queue_is_fixture_specific") is not True
        or broker.get("queue_purge_scope") != "exact_fixture_queue_only"
    ):
        raise LocalFenceRefused("local_fence_preflight_incomplete")
    validate_sanitized_local_fence_evidence(value)


def build_local_fence_evidence(
    preflight: Mapping[str, Any], observations: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a structured fixture-scoped receipt only from verified observations."""
    validate_local_fence_preflight(preflight)
    required = {
        "worker_stop",
        "scope_retirement",
        "media_peer_stop",
        "backend_peer_stop",
        "database_write_fence",
        "stale_login_denials",
        "s3_revocation",
        "stale_redelivery",
        "stale_probe_worker_stop",
        "job_state_after",
    }
    if set(observations) != required:
        raise LocalFenceRefused("local_fence_observation_contract_mismatch")
    stop = observations.get("worker_stop")
    scope = observations.get("scope_retirement")
    media = observations.get("media_peer_stop")
    backend = observations.get("backend_peer_stop")
    database = observations.get("database_write_fence")
    denied = observations.get("stale_login_denials")
    storage = observations.get("s3_revocation")
    redelivery = observations.get("stale_redelivery")
    stale_stop = observations.get("stale_probe_worker_stop")
    job = observations.get("job_state_after")
    if not all(
        isinstance(part, Mapping)
        for part in (
            stop,
            scope,
            media,
            backend,
            database,
            storage,
            redelivery,
            stale_stop,
            job,
        )
    ) or not isinstance(denied, list):
        raise LocalFenceRefused("local_fence_observation_contract_mismatch")
    if (
        stop.get("container_id") != preflight["worker"].get("container_id")
        or stop.get("container_name") != preflight["worker"].get("container_name")
        or stop.get("acceptance_id") != preflight["worker"].get("acceptance_id")
        or stop.get("image_id") != preflight["worker"].get("image_id")
        or stop.get("image_acceptance_id")
        != preflight["worker"].get("image_acceptance_id")
        or stop.get("source_revision") != preflight["worker"].get("source_revision")
        or stop.get("database") != preflight["worker"].get("database")
        or stop.get("network_id") != preflight["worker"].get("network_id")
        or stop.get("network_name") != preflight["worker"].get("network_name")
        or stop.get("queue") != preflight["worker"].get("queue")
        or stop.get("container_removed") is not True
        or stop.get("started_at") != preflight["worker"].get("worker_started_at")
        or stop.get("exit_code") not in {0, 137}
        or stop.get("restart_policy") != "no"
        or stop.get("restart_count") != 0
        or scope.get("scope_id") != preflight["scope"].get("scope_id")
        or scope.get("registry_id") != preflight["scope"].get("registry_id")
        or not scope.get("retirement_id")
        or not scope.get("authority_epoch")
        or scope.get("phase") != "completed"
        or scope.get("state") != "reconciled"
        or scope.get("closed") is not True
        or scope.get("scope_retired") is not True
        or scope.get("backend_incarnation")
        != preflight["backend_peer"].get("backend_incarnation")
        or media.get("process_exited") is not True
        or media.get("endpoint_unavailable") is not True
        or backend.get("peer_stopped") is not True
        or backend.get("endpoint_unavailable") is not True
        or backend.get("active_requests_at_stop") != 0
        or database.get("all_non_owner_logins_disabled") is not True
        or database.get("all_non_owner_connect_revoked") is not True
        or database.get("non_owner_logins_remaining") != 0
        or database.get("non_owner_sessions_remaining") != 0
        or {item.get("role") for item in denied if isinstance(item, Mapping)}
        != set(_WORKER_DATABASE_SOURCES.values())
        or storage.get("user_removed") is not True
        or storage.get("policy_removed") is not True
        or storage.get("old_credential_denied") is not True
        or storage.get("access_key_id") != preflight["storage"].get("access_key_id")
        or storage.get("policy_id") != preflight["storage"].get("policy_id")
        or storage.get("bucket") != preflight["storage"].get("bucket")
        or storage.get("container_id") != preflight["storage"].get("container_id")
        or storage.get("write_probe") != preflight["storage"].get("write_probe")
        or storage.get("denial_code")
        not in {"InvalidAccessKeyId", "AccessDenied", "AllAccessDisabled", "403"}
        or redelivery.get("task_state") != "FAILURE"
        or redelivery.get("worker_executed") is not True
        or redelivery.get("queue") != preflight["worker"].get("queue")
        or redelivery.get("stale_worker_db_access") is not False
        or stale_stop.get("container_removed") is not True
        or stale_stop.get("restart_count") != 0
        or stale_stop.get("container_id") != redelivery.get("worker_container_id")
        or stale_stop.get("acceptance_id") != redelivery.get("worker_acceptance_id")
        or stale_stop.get("image_id") != redelivery.get("worker_image_id")
        or stale_stop.get("image_acceptance_id")
        != redelivery.get("worker_image_acceptance_id")
        or stale_stop.get("source_revision") != redelivery.get("worker_source_revision")
        or stale_stop.get("network_id") != redelivery.get("worker_network_id")
        or stale_stop.get("network_name") != redelivery.get("worker_network_name")
        or stale_stop.get("started_at") != redelivery.get("worker_started_at")
        or stale_stop.get("exit_code") not in {0, 137}
        or stale_stop.get("restart_policy") != "no"
        or stale_stop.get("container_name") != redelivery.get("worker_container_name")
        or stale_stop.get("queue") != redelivery.get("queue")
        or database.get("container_id") != preflight["postgres"].get("container_id")
        or database.get("database") != preflight["postgres"].get("database")
        or job.get("business_status_unchanged") is not True
        or job.get("status") != preflight["scope"].get("job_status")
        or job.get("started_at") != preflight["scope"].get("job_started_at")
        or job.get("execution_absent") is not True
        or job.get("journal_absent") is not True
        or job.get("clearance_absent") is not True
        or job.get("input_referenced") is not True
        or job.get("deletion_guard_still_409") is not True
    ):
        raise LocalFenceRefused("local_fence_observation_incomplete")
    result = {
        "schema": "aap-local-video-fence/v1",
        "scope": "one verifier-owned disposable test database and its fixture peers",
        "preflight": dict(preflight),
        "observations": dict(observations),
        "broker_boundary": {
            "shared_unauthenticated_broker": True,
            "external_consumers_unknown": True,
            "global_broker_isolation_proven": False,
            "only_fixture_queue_cleanup_allowed": True,
        },
        "jobs_legacy_retirement_proof_created": False,
        "journal_or_clearance_created": False,
        "production_or_shared_runtime_qualified": False,
    }
    validate_sanitized_local_fence_evidence(result)
    return result


async def run_fence_after_preflight(
    preflight: Mapping[str, Any],
    fence_operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Do not invoke a mutator unless the owned fixture preflight is complete."""
    validate_local_fence_preflight(preflight)
    return await fence_operation()


async def prove_stale_worker_dsns_denied(
    credentials: tuple[WorkerDatabaseCredential, ...],
    *,
    host: str,
    port: int,
    database: str,
) -> list[dict[str, Any]]:
    """Verify every observed stale worker DSN fails with a PostgreSQL auth denial."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    denied: list[dict[str, Any]] = []
    for item in credentials:
        url = item.host_url(host=host, port=port)
        if url.database != database:
            raise LocalFenceRefused("stale_worker_database_target_changed")
        engine = create_async_engine(
            url,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"timeout": 3, "command_timeout": 3},
        )
        try:
            try:
                async with engine.connect() as connection:
                    await connection.exec_driver_sql("SELECT 1")
            except Exception as exc:
                origin = getattr(exc, "orig", exc)
                sqlstate = getattr(origin, "sqlstate", None) or getattr(
                    origin, "pgcode", None
                )
                if sqlstate not in {"28000", "28P01"}:
                    raise LocalFenceRefused(
                        "stale_worker_login_probe_inconclusive"
                    ) from None
            else:
                raise LocalFenceRefused("stale_worker_login_still_succeeds")
        except LocalFenceRefused:
            raise
        except Exception:
            raise LocalFenceRefused("stale_worker_login_probe_failed") from None
        finally:
            await engine.dispose()
        denied.append({"source_variable": item.source_variable, "role": item.role})
    return denied


def inventory_worker_database_credentials(
    environment: Mapping[str, str],
    *,
    database: str,
    read_secret_file: Callable[[str], str],
    container_host: str = "worker-postgres",
    container_port: int = 5432,
) -> tuple[WorkerDatabaseCredential, ...]:
    """Parse every actual worker DB DSN, including read-only secret-file mounts.

    Unknown nonempty database URL selectors are refused. Empty URL/file settings
    do not represent credentials and are ignored.
    """
    observed: dict[str, WorkerDatabaseCredential] = {}
    for source, raw in sorted(environment.items()):
        if "DATABASE_URL" not in source:
            continue
        if not isinstance(raw, str):
            raise LocalFenceRefused("worker_database_environment_invalid")
        if not raw:
            continue
        if source.endswith("_FILE"):
            if source != "PREANNOTATION_RESULTS_DATABASE_URL_FILE":
                raise LocalFenceRefused("unrecognized_worker_database_secret_file")
            dsn_text = read_secret_file(raw)
            secret_file = True
        else:
            if source not in _WORKER_DATABASE_SOURCES:
                raise LocalFenceRefused("unrecognized_worker_database_selector")
            dsn_text = raw
            secret_file = False
        expected_role = _WORKER_DATABASE_SOURCES.get(source)
        if expected_role is None:
            raise LocalFenceRefused("unrecognized_worker_database_selector")
        try:
            url = make_url(dsn_text)
        except Exception:
            raise LocalFenceRefused("worker_database_identity_invalid") from None
        if (
            url.drivername not in {"postgresql", "postgresql+asyncpg"}
            or url.username != expected_role
            or not url.password
            or url.host != container_host
            or url.port != container_port
            or url.database != database
            or url.query
            or not _ROLE_NAME.fullmatch(url.username or "")
        ):
            raise LocalFenceRefused("worker_database_identity_mismatch")
        if source in observed:
            raise LocalFenceRefused("duplicate_worker_database_selector")
        observed[source] = WorkerDatabaseCredential(
            source_variable=source,
            role=expected_role,
            database=database,
            container_host=container_host,
            container_port=container_port,
            secret_file=secret_file,
            _url=url,
        )

    if set(observed) != set(_WORKER_DATABASE_SOURCES):
        raise LocalFenceRefused("incomplete_worker_database_inventory")
    roles = [item.role for item in observed.values()]
    if len(roles) != len(set(roles)):
        raise LocalFenceRefused("ambiguous_worker_database_inventory")
    return tuple(observed[source] for source in sorted(observed))


def validate_owned_postgres_container(
    record: Mapping[str, Any],
    *,
    container_id: str,
    container_name: str,
    acceptance_id: str,
    host_url: URL,
) -> dict[str, Any]:
    """Verify the exact verifier-owned, loopback-only disposable PG container."""
    labels = (record.get("Config") or {}).get("Labels") or {}
    bindings = ((record.get("NetworkSettings") or {}).get("Ports") or {}).get(
        "5432/tcp"
    ) or []
    if not (
        _PG_CONTAINER.fullmatch(container_id)
        and _PG_NAME.fullmatch(container_name)
        and _PG_ACCEPTANCE_ID.fullmatch(acceptance_id)
        and record.get("Id") == container_id
        and record.get("Name") == f"/{container_name}"
        and (record.get("Config") or {}).get("Image") == _POSTGRES_IMAGE
        and labels.get("org.aap.acceptance") == "module-data-boundary"
        and labels.get("org.aap.acceptance-id") == acceptance_id
        and (record.get("State") or {}).get("Running") is True
        and host_url.drivername == "postgresql+asyncpg"
        and host_url.host in _LOCAL_HOSTS
        and host_url.port is not None
        and host_url.database
        and host_url.database.endswith("_test")
        and host_url.username == "owner"
        and len(bindings) == 1
        and bindings[0].get("HostIp") == "127.0.0.1"
        and str(bindings[0].get("HostPort")) == str(host_url.port)
    ):
        raise LocalFenceRefused("postgres_resource_ownership_mismatch")
    return {
        "container_id": container_id,
        "container_name": container_name,
        "acceptance_id": acceptance_id,
        "database": host_url.database,
        "host": host_url.host,
        "port": host_url.port,
        "image": _POSTGRES_IMAGE,
    }


def validate_owned_worker_container(
    record: Mapping[str, Any],
    *,
    container_id: str,
    container_name: str,
    acceptance_id: str,
    image_id: str,
    source_revision: str,
    queue: str,
) -> dict[str, Any]:
    """Verify one exact installed worker, image, restart policy, and queue."""
    labels = (record.get("Config") or {}).get("Labels") or {}
    config = record.get("Config") or {}
    host = record.get("HostConfig") or {}
    restart_policy = host.get("RestartPolicy") or {}
    command = config.get("Cmd") or []
    queue_flags = [item for item in command if item.startswith("--queues=")]
    if not (
        _WORKER_ACCEPTANCE_ID.fullmatch(acceptance_id)
        and record.get("Id") == container_id
        and record.get("Name") == f"/{container_name}"
        and labels.get("org.aap.acceptance") == "workspace-worker-assembly"
        and labels.get("org.aap.acceptance-id") == acceptance_id
        and labels.get("org.aap.source-revision") == source_revision
        and _SOURCE_REVISION.fullmatch(source_revision)
        and record.get("Image") == image_id
        and (record.get("State") or {}).get("Running") is True
        and restart_policy.get("Name", "") in {"", "no"}
        and (record.get("RestartCount") or 0) == 0
        and _QUEUE_NAME.fullmatch(queue)
        and queue_flags == [f"--queues={queue}"]
    ):
        raise LocalFenceRefused("worker_resource_ownership_mismatch")
    return {
        "container_id": container_id,
        "container_name": container_name,
        "acceptance_id": acceptance_id,
        "image_id": image_id,
        "source_revision": source_revision,
        "queue": queue,
        "restart_policy": restart_policy.get("Name") or "no",
        "restart_count": record.get("RestartCount") or 0,
    }


def validate_sanitized_local_fence_evidence(value: Mapping[str, Any]) -> None:
    """Reject accidental persistence of DSNs, passwords, tokens, or S3 secrets."""
    forbidden_keys = {
        "dsn",
        "database_url",
        "password",
        "secret",
        "secret_key",
        "access_key_secret",
        "token",
        "authorization",
    }

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                normalized = str(key).lower()
                if normalized in forbidden_keys or normalized.endswith("_dsn"):
                    raise LocalFenceRefused("unsanitized_local_fence_evidence")
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
        elif isinstance(node, str):
            if re.search(r"(?i)(?:postgres(?:ql)?(?:\+asyncpg)?|redis)://", node):
                raise LocalFenceRefused("unsanitized_local_fence_evidence")
            if node.lower().startswith(("http://", "https://")):
                try:
                    endpoint = urlsplit(node)
                except ValueError:
                    raise LocalFenceRefused(
                        "unsanitized_local_fence_evidence"
                    ) from None
                if endpoint.username is not None or endpoint.password is not None:
                    raise LocalFenceRefused("unsanitized_local_fence_evidence")

    visit(value)


def worker_credential_evidence(
    credentials: tuple[WorkerDatabaseCredential, ...],
) -> list[dict[str, Any]]:
    """Serialize observed credential identities without serializing credentials."""
    return [item.evidence() for item in credentials]


def inspect_worker_write_selectors(
    environment: Mapping[str, str],
    *,
    expected_s3_access_key: str,
    expected_media_token: str,
) -> dict[str, Any]:
    """Bind the worker's non-DB write selectors to the fixture-owned peers."""
    if not expected_s3_access_key or not expected_media_token:
        raise LocalFenceRefused("worker_write_selector_identity_missing")
    sensitive = {
        key: value
        for key, value in environment.items()
        if re.search(r"(?i)(?:TOKEN|KEY|SECRET|PASSWORD)", key)
        and isinstance(value, str)
        and value
        # Python's base image publishes its GPG verification fingerprint.
        and not (key == "GPG_KEY" and re.fullmatch(r"[0-9A-F]{40}", value))
    }
    allowed = {
        "SECRET_KEY",
        "ASSETS_MEDIA_TOKEN",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
    }
    if set(sensitive) != allowed:
        raise LocalFenceRefused("unrecognized_worker_write_selector")
    if (
        sensitive.get("ASSETS_MEDIA_TOKEN") != expected_media_token
        or sensitive.get("MINIO_ACCESS_KEY") != expected_s3_access_key
        or not sensitive.get("MINIO_SECRET_KEY")
        or not sensitive.get("SECRET_KEY")
        or environment.get("INFERENCE_ROUTING_TOKEN")
        or environment.get("LABELING_VIDEO_WRITER_CONTROL_TOKEN")
        or environment.get("INFERENCE_ROUTING_TOKEN_FILE")
        or environment.get("LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE")
    ):
        raise LocalFenceRefused("worker_write_selector_mismatch")
    return {
        "database_roles": sorted(_WORKER_DATABASE_SOURCES.values()),
        "media_control_token": "present_and_bound_to_owned_media_peer",
        "s3_principal_id": expected_s3_access_key,
        "process_secret": "present_local_worker_secret",
        "direct_routing_token": "absent",
        "direct_writer_control_token": "absent",
    }


__all__ = [
    "LocalPostgresIdentity",
    "LocalFenceRefused",
    "WorkerCredentialInventory",
    "WorkerDatabaseCredential",
    "build_local_fence_evidence",
    "fence_disposable_database_logins",
    "inventory_worker_database_credentials",
    "inspect_live_worker_credentials",
    "inspect_worker_write_selectors",
    "owned_postgres_identity",
    "owned_worker_identity",
    "prove_stale_worker_dsns_denied",
    "run_fence_after_preflight",
    "stop_owned_worker",
    "validate_owned_postgres_container",
    "validate_owned_worker_container",
    "validate_local_fence_preflight",
    "validate_sanitized_local_fence_evidence",
    "worker_credential_evidence",
]
