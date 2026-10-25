"""Cold consumer proof for the immutable API image and installed Labeling package."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from aap_identity.security import create_access_token
from app.config import settings
from app.db.models.annotation import Annotation
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.factory import create_project, create_task, create_user
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _required_env,
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_assets_catalog_runtime import _write_environment
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url as _verified_role_url
from tests.notifications_publisher_acceptance_helpers import (
    connect_owned_dependency,
    disconnect_owned_dependency,
    inspect_docker as inspect_owned_docker,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_LABELING_COLD_API_ACCEPTANCE") != "1"
        or os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned API image and disposable module-data resources",
    ),
]

_IMAGE_ACCEPTANCE = "labeling-module-current-api-image"
_CONTAINER_ACCEPTANCE = "labeling-cold-api-consumer"
_ACCEPTANCE_LABEL = "org.aap.acceptance"
_ACCEPTANCE_ID_LABEL = "org.aap.acceptance-id"
_WORKTREE_OWNER_LABEL = "org.aap.worktree-owner"
_SOURCE_REVISION_LABEL = "org.aap.source-revision"
_AUTH_SECRET_TARGET = "/run/secrets/identity-auth-database-url"
_POSTGRES_ALIAS = "labeling-cold-postgres"
_REDIS_ALIAS = "labeling-cold-redis"
_MINIO_ALIAS = "labeling-cold-minio"
_API_PORT = 8000
_API_BUCKET_NAMES = (
    "MINIO_BUCKET",
    "MINIO_DATASETS_BUCKET",
    "MINIO_BUG_REPORTS_BUCKET",
    "MINIO_MEDIA_CACHE_BUCKET",
    "MINIO_AUDIT_ARCHIVE_BUCKET",
    "MINIO_IMPORT_BUCKET",
    "MINIO_EXPORT_BUCKET",
    "MINIO_AVATARS_BUCKET",
    "MINIO_TRAINING_BUCKET",
    "MINIO_INPUTS_BUCKET",
)


def _required(name: str) -> str:
    value = os.environ.get(name) or _required_env(name)
    if not value or "\n" in value or "\r" in value:
        pytest.fail(f"the verifier did not provide {name}", pytrace=False)
    return value


def _plain(value: object) -> str:
    getter = getattr(value, "get_secret_value", None)
    return getter() if callable(getter) else str(value)


def _image_artifact() -> tuple[str, str, dict]:
    image_ref = _required("AAP_LABELING_COLD_API_IMAGE_REF")
    expected_image_id = _required("AAP_LABELING_COLD_API_IMAGE_ID")
    artifact_path = Path(_required("AAP_LABELING_COLD_API_IMAGE_ARTIFACT"))
    if not artifact_path.is_absolute() or artifact_path.is_symlink():
        pytest.fail("the API image artifact path was not an owned regular file")
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pytest.fail("the qualified API image artifact was unavailable", pytrace=False)
    offline = artifact.get("offline_proof")
    if not (
        artifact.get("status") == "passed"
        and artifact.get("image") == image_ref
        and artifact.get("image_id") == expected_image_id
        and isinstance(artifact.get("identity"), str)
        and isinstance(artifact.get("source_commit"), str)
        and isinstance(offline, dict)
        and offline.get("status") == "passed"
        and offline.get("source_hashes_match") is True
    ):
        pytest.fail("the current API image artifact did not prove its source bytes")
    return image_ref, expected_image_id, artifact


def _verify_image(image_ref: str, expected_id: str, artifact: dict) -> None:
    image = inspect_owned_docker("image", image_ref)
    config = image.get("Config", {})
    labels = config.get("Labels") or {}
    if not (
        image.get("Id") == expected_id
        and image.get("Id", "").startswith("sha256:")
        and labels.get(_ACCEPTANCE_LABEL) == _IMAGE_ACCEPTANCE
        and labels.get(_ACCEPTANCE_ID_LABEL) == artifact["identity"]
        and labels.get(_SOURCE_REVISION_LABEL) == artifact["source_commit"]
    ):
        pytest.fail("API image ID or provenance labels did not match the artifact")


def _role_url(
    variable: str,
    role: str,
    test_engine,
    *,
    internal_network: bool = False,
) -> URL:
    value = make_url(_verified_role_url(variable, role))
    if value.database != test_engine.url.database:
        pytest.fail(f"verifier role URL {variable} escaped the test database")
    updates = {"drivername": "postgresql+asyncpg"}
    if internal_network:
        updates.update(host=_POSTGRES_ALIAS, port=5432)
    return value.set(**updates)


def _write_private_file(path: Path, value: str, *, mode: int) -> None:
    if path.is_symlink() or not value or "\0" in value or "\r" in value:
        raise RuntimeError("cold API file input was unsafe")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.write("\n")
    path.chmod(mode)


def _redis_dependency(resources: dict, test_engine) -> tuple[str, str, URL]:
    progress = _verify_redis()
    name, owner = resources["redis_name"], resources["owner"]
    details = _inspect_docker(name)
    labels = details.get("Config", {}).get("Labels") or {}
    if not (
        details.get("Name") == f"/{name}"
        and labels.get(_WORKTREE_OWNER_LABEL) == owner
        and isinstance(details.get("Id"), str)
    ):
        pytest.fail("the Redis container was not this worktree's test resource")
    return details["Id"], owner, progress.set(host=_REDIS_ALIAS, port=6379)


def _minio_dependency(resources: dict) -> dict:
    tmpdir = Path(_required("TMPDIR")).resolve()
    manifest_path = tmpdir.parents[2] / ".worktree" / "test" / "resources.json"
    if manifest_path.is_symlink():
        pytest.fail("the resource manifest must not be a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pytest.fail("the resource manifest MinIO endpoint was unavailable")
    if not isinstance(manifest, dict):
        pytest.fail("the resource manifest MinIO endpoint was unavailable")
    minio = manifest.get("minio")
    if not (
        manifest.get("resources") == resources
        and isinstance(minio, dict)
        and minio.get("endpoint") == _plain(settings.minio_endpoint)
        and minio.get("ssl") is settings.minio_use_ssl
    ):
        pytest.fail("MinIO settings did not match the verified resource manifest")
    scheme = "https" if minio["ssl"] else "http"
    endpoint = urlsplit(f"{scheme}://{minio['endpoint']}")
    if (
        endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
        or endpoint.port is None
    ):
        pytest.fail("verifier MinIO endpoint was not loopback scoped")

    candidates = _docker(
        "container",
        "ls",
        "--filter",
        "status=running",
        "--filter",
        f"publish={endpoint.port}",
        "--format",
        "{{.ID}}",
    ).splitlines()
    matches = {}
    for candidate in candidates:
        details = _inspect_docker(candidate)
        config = details.get("Config", {})
        if "minio" not in str(config.get("Image", "")).lower():
            continue
        ports = details.get("NetworkSettings", {}).get("Ports") or {}
        for container_port, bindings in ports.items():
            service_port = int(container_port.split("/", 1)[0])
            if any(
                binding.get("HostIp") in {"127.0.0.1", "0.0.0.0", "::1", "::"}
                and int(binding.get("HostPort", 0)) == endpoint.port
                for binding in bindings or []
            ):
                matches[(details.get("Id"), service_port)] = details
    if len(matches) != 1:
        pytest.fail("the shared MinIO container was not uniquely identified")
    (container_id, service_port), details = next(iter(matches.items()))
    if not (
        isinstance(container_id, str)
        and details.get("Name")
        and isinstance(details.get("Image"), str)
        and details["Image"].startswith("sha256:")
        and details.get("State", {}).get("Running") is True
    ):
        pytest.fail("the shared MinIO container identity was incomplete")
    return {
        "id": container_id,
        "name": details["Name"],
        "image_id": details["Image"],
        "host_port": endpoint.port,
        "service_port": service_port,
        "endpoint": f"{_MINIO_ALIAS}:{service_port}",
        "ssl": minio["ssl"],
    }


def _verify_minio_identity(details: dict, minio: dict) -> None:
    ports = details.get("NetworkSettings", {}).get("Ports") or {}
    binding = ports.get(f"{minio['service_port']}/tcp") or []
    if not (
        details.get("Id") == minio["id"]
        and details.get("Name") == minio["name"]
        and details.get("Image") == minio["image_id"]
        and details.get("State", {}).get("Running") is True
        and any(
            item.get("HostIp") in {"127.0.0.1", "0.0.0.0", "::1", "::"}
            and int(item.get("HostPort", 0)) == minio["host_port"]
            for item in binding
        )
    ):
        raise RuntimeError("shared MinIO container identity changed")


def _network_details(network_name: str, expected_id: str, run_id: str) -> dict:
    details = inspect_owned_docker("network", network_name)
    labels = details.get("Labels") or {}
    if not (
        details.get("Id") == expected_id
        and details.get("Name") == network_name
        and details.get("Driver") == "bridge"
        and labels.get(_ACCEPTANCE_LABEL) == _CONTAINER_ACCEPTANCE
        and labels.get(_ACCEPTANCE_ID_LABEL) == run_id
    ):
        raise RuntimeError("cold API network ownership changed")
    return details


def _find_created_container_id(container_name: str) -> str | None:
    matches = _docker(
        "container",
        "ls",
        "--all",
        "--filter",
        f"name=^/{container_name}$",
        "--format",
        "{{.ID}}",
    ).splitlines()
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError("cold API container name was not unique")
    details = _inspect_docker(matches[0])
    if details.get("Name") != f"/{container_name}":
        raise RuntimeError("cold API container name did not match")
    container_id = details.get("Id")
    if not isinstance(container_id, str) or not container_id:
        raise RuntimeError("cold API container identity was missing")
    return container_id


def _find_created_network_id(network_name: str) -> str | None:
    matches = _docker(
        "network",
        "ls",
        "--filter",
        f"name=^{network_name}$",
        "--format",
        "{{.ID}}",
    ).splitlines()
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError("cold API network name was not unique")
    details = inspect_owned_docker("network", matches[0])
    if details.get("Name") != network_name:
        raise RuntimeError("cold API network name did not match")
    network_id = details.get("Id")
    if not isinstance(network_id, str) or not network_id:
        raise RuntimeError("cold API network identity was missing")
    return network_id


def _container_environment(details: dict) -> dict[str, str]:
    return dict(
        entry.split("=", 1)
        for entry in (details.get("Config", {}).get("Env") or [])
        if "=" in entry
    )


def _verify_api_container(
    container_id: str,
    *,
    image_id: str,
    network_name: str,
    network_id: str,
    run_id: str,
    resource_owner: str,
    container_name: str,
    auth_secret_file: Path,
    database_url: URL,
    auth_url: URL,
    minio_endpoint: str,
    minio_access_key: str,
    minio_secret_key: str,
) -> str:
    details = _inspect_docker(container_id)
    config = details.get("Config", {})
    host = details.get("HostConfig", {})
    labels = config.get("Labels") or {}
    if not (
        details.get("Id") == container_id
        and details.get("Name") == f"/{container_name}"
        and details.get("Image") == image_id
        and config.get("User") == "10001:10001"
        and labels.get(_ACCEPTANCE_LABEL) == _CONTAINER_ACCEPTANCE
        and labels.get(_ACCEPTANCE_ID_LABEL) == run_id
        and labels.get(_WORKTREE_OWNER_LABEL) == resource_owner
        and host.get("NetworkMode") == network_name
        and host.get("ReadonlyRootfs") is True
        and details.get("State", {}).get("Running") is True
    ):
        raise RuntimeError("cold API container identity changed")
    mounts = details.get("Mounts") or []
    binds = [mount for mount in mounts if mount.get("Type") == "bind"]
    tmpfs_mounts = [mount for mount in mounts if mount.get("Type") == "tmpfs"]
    if not (
        len(binds) == 1
        and len(tmpfs_mounts) <= 1
        and all(mount.get("Destination") == "/tmp" for mount in tmpfs_mounts)
        and len(binds) + len(tmpfs_mounts) == len(mounts)
        and binds[0].get("Destination") == _AUTH_SECRET_TARGET
        and Path(binds[0].get("Source", "")).resolve() == auth_secret_file.resolve()
        and binds[0].get("RW") is False
    ):
        raise RuntimeError(
            "cold API container has a source mount or unsafe secret mount"
        )
    environment = _container_environment(details)
    try:
        observed_database_url = make_url(environment.get("DATABASE_URL", ""))
        observed_auth_url = make_url(
            auth_secret_file.read_text(encoding="utf-8").strip()
        )
    except Exception:
        raise RuntimeError("cold API database bindings were invalid") from None
    if not (
        observed_database_url.username == "identity_api_consumer"
        and observed_database_url.host == _POSTGRES_ALIAS
        and observed_database_url.port == 5432
        and observed_database_url.database == database_url.database
        and observed_database_url.password == database_url.password
        and observed_auth_url.username == "identity_auth_client"
        and observed_auth_url.host == _POSTGRES_ALIAS
        and observed_auth_url.port == 5432
        and observed_auth_url.database == auth_url.database
        and observed_auth_url.password == auth_url.password
        and environment.get("IDENTITY_AUTH_DATABASE_URL") == ""
        and environment.get("IDENTITY_AUTH_DATABASE_URL_FILE") == _AUTH_SECRET_TARGET
        and environment.get("IDENTITY_URL") == ""
        and environment.get("IDENTITY_TOKEN") == ""
        and environment.get("MIGRATION_DATABASE_URL") == ""
        and environment.get("ALEMBIC_AUTO_UPGRADE") == "false"
        and environment.get("MINIO_ENDPOINT") == minio_endpoint
        and environment.get("MINIO_ACCESS_KEY") == minio_access_key
        and environment.get("MINIO_SECRET_KEY") == minio_secret_key
        and environment.get("MINIO_ACCESS_KEY") != _plain(settings.minio_access_key)
        and environment.get("MINIO_SECRET_KEY") != _plain(settings.minio_secret_key)
    ):
        raise RuntimeError("cold API was not bound to the verifier consumer roles")
    networks = details.get("NetworkSettings", {}).get("Networks") or {}
    if not (
        set(networks) == {network_name}
        and networks[network_name].get("NetworkID") == network_id
        and networks[network_name].get("EndpointID")
    ):
        raise RuntimeError("cold API is attached outside its private test network")
    bindings = (details.get("NetworkSettings", {}).get("Ports") or {}).get(
        f"{_API_PORT}/tcp"
    ) or []
    if len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1":
        raise RuntimeError("cold API did not publish a loopback-only HTTP port")
    return f"http://127.0.0.1:{int(bindings[0]['HostPort'])}"


async def _wait_for_api(
    origin: str,
    container_id: str,
    *,
    secrets_to_redact: list[str],
    timeout: float = 75,
) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline:
            details = _inspect_docker(container_id)
            if details.get("State", {}).get("Running") is not True:
                break
            try:
                response = await client.get(origin + "/health/db")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    try:
        logs = _docker("logs", "--tail", "100", container_id, timeout=15)
    except Exception:
        logs = ""
    for value in sorted(
        (value for value in secrets_to_redact if value), key=len, reverse=True
    ):
        logs = logs.replace(value, "[redacted]")
    logs = re.sub(
        r"(?:postgresql(?:\+\w+)?|redis|https?)://[^\s\"'<>]+", "[endpoint]", logs
    )
    pytest.fail(
        f"cold API did not become database-ready:\n{logs[-4000:]}", pytrace=False
    )


async def _container_identity_probe(container_id: str) -> dict[str, str]:
    program = """
import asyncio, json, os, sysconfig
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def current_user(url):
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            result = await db.execute(text("SELECT current_user,current_database()"))
            return tuple(result.one())
    finally:
        await engine.dispose()

async def main():
    auth_path = Path(os.environ["IDENTITY_AUTH_DATABASE_URL_FILE"])
    api_identity = await current_user(os.environ["DATABASE_URL"])
    auth_identity = await current_user(auth_path.read_text().strip())
    import aap_labeling.annotation_write_contract as contract
    origin = Path(contract.__file__).resolve()
    site = Path(sysconfig.get_paths()["purelib"]).resolve()
    print(json.dumps({"database_role": api_identity[0], "database": api_identity[1],
                      "auth_role": auth_identity[0], "auth_database": auth_identity[1],
                      "package_origin": str(origin), "site_packages": str(site),
                      "package_is_installed": origin.is_relative_to(site)}))

asyncio.run(main())
"""
    try:
        payload = json.loads(
            _docker("exec", container_id, "python", "-c", program, timeout=30)
        )
    except (json.JSONDecodeError, RuntimeError):
        raise RuntimeError(
            "cold API database/package probe did not return evidence"
        ) from None
    if not (
        payload.get("database_role") == "identity_api_consumer"
        and payload.get("auth_role") == "identity_auth_client"
        and payload.get("database") == payload.get("auth_database")
        and payload.get("package_is_installed") is True
        and "/site-packages/" in payload.get("package_origin", "")
    ):
        raise RuntimeError(
            "cold API did not use the installed Labeling package and roles"
        )
    return payload


async def _wait_for_task_lock_waiter(test_engine, blocker_pid: int) -> bool:
    deadline = time.monotonic() + 8
    statement = text(
        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
        "WHERE datname=current_database() AND usename='identity_api_consumer' "
        "AND state='active' "
        "AND wait_event_type='Lock' "
        "AND :blocker_pid = ANY(pg_catalog.pg_blocking_pids(pid)))"
    )
    while time.monotonic() < deadline:
        async with test_engine.connect() as db:
            if await db.scalar(statement, {"blocker_pid": blocker_pid}):
                return True
        await asyncio.sleep(0.05)
    return False


async def _cleanup_rows(
    test_engine,
    *,
    actor_id: uuid.UUID | None,
    outsider_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
) -> None:
    if not any((actor_id, outsider_id, project_id, task_id)):
        return
    async with test_engine.begin() as db:
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        if actor_id is not None or outsider_id is not None:
            await db.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE actor_id IN "
                    "(CAST(:actor_id AS uuid),CAST(:outsider_id AS uuid))"
                ),
                {"actor_id": actor_id, "outsider_id": outsider_id},
            )
        if task_id is not None:
            await db.execute(
                text("DELETE FROM public.task_locks WHERE task_id=:task_id"),
                {"task_id": task_id},
            )
            await db.execute(
                text("DELETE FROM public.annotation_operations WHERE task_id=:task_id"),
                {"task_id": task_id},
            )
            await db.execute(
                text("DELETE FROM public.annotations WHERE task_id=:task_id"),
                {"task_id": task_id},
            )
            await db.execute(
                text("DELETE FROM public.tasks WHERE id=:task_id"),
                {"task_id": task_id},
            )
        if project_id is not None:
            await db.execute(
                text("DELETE FROM public.projects WHERE id=:project_id"),
                {"project_id": project_id},
            )
        for user_id in (actor_id, outsider_id):
            if user_id is not None:
                await db.execute(
                    text("DELETE FROM public.users WHERE id=:user_id"),
                    {"user_id": user_id},
                )


async def test_current_cold_api_reads_writes_and_persists_labeling_data(
    test_engine, tmp_path, record_property
):
    image_ref, expected_image_id, artifact = _image_artifact()
    _verify_image(image_ref, expected_image_id, artifact)
    api_db_host_url = _role_url(
        "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
        "identity_api_consumer",
        test_engine,
    )
    _verify_postgres(api_db_host_url)
    resources = _verified_resource_manifest(test_engine, api_db_host_url)

    postgres_id = _required("AAP_MODULE_DATA_PG_CONTAINER_ID")
    postgres_identity = _required("AAP_MODULE_DATA_PG_CONTAINER_IDENTITY")
    redis_id, resource_owner, redis_host_url = _redis_dependency(resources, test_engine)
    minio = _minio_dependency(resources)
    api_s3_access = _required("AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY")
    api_s3_secret = _required("AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY")
    if api_s3_access == _plain(settings.minio_access_key) or api_s3_secret == _plain(
        settings.minio_secret_key
    ):
        pytest.fail(
            "cold API storage startup was given the MinIO administrator identity"
        )

    run_id = uuid.uuid4().hex[:24]
    network_name = f"aap-labeling-cold-{run_id[:12]}"
    container_name = f"{network_name}-api"
    api_database_url = _role_url(
        "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
        "identity_api_consumer",
        test_engine,
        internal_network=True,
    )
    auth_database_url = _role_url(
        "AAP_MODULE_DATA_IDENTITY_AUTH_URL",
        "identity_auth_client",
        test_engine,
        internal_network=True,
    )
    auth_secret_file = tmp_path / f"identity-auth-{run_id}"
    environment_file = tmp_path / f"api-env-{run_id}"
    api_secret = secrets.token_urlsafe(48)
    redis_internal_url = redis_host_url.render_as_string(hide_password=False)
    api_environment = {
        "ENVIRONMENT": "development",
        "DEBUG": "false",
        "SECRET_KEY": api_secret,
        "DATABASE_URL": api_database_url.render_as_string(hide_password=False),
        "MIGRATION_DATABASE_URL": "",
        "ALEMBIC_AUTO_UPGRADE": "false",
        "E2E_SEED_ENABLED": "false",
        "REDIS_URL": redis_internal_url,
        "CELERY_BROKER_URL": redis_internal_url,
        "AUDIT_ASYNC": "false",
        "IDENTITY_URL": "",
        "IDENTITY_TOKEN": "",
        "IDENTITY_AUTH_DATABASE_URL": "",
        "IDENTITY_AUTH_DATABASE_URL_FILE": _AUTH_SECRET_TARGET,
        "IDENTITY_TIMEOUT_SECONDS": "10",
        "MINIO_ENDPOINT": minio["endpoint"],
        "MINIO_ACCESS_KEY": api_s3_access,
        "MINIO_SECRET_KEY": api_s3_secret,
        "MINIO_USE_SSL": "true" if minio["ssl"] else "false",
    }
    for bucket_name in _API_BUCKET_NAMES:
        bucket = resources.get("buckets", {}).get(bucket_name)
        if not isinstance(bucket, str) or not bucket:
            pytest.fail(f"the verifier omitted its owned {bucket_name}")
        api_environment[bucket_name] = bucket

    network_id: str | None = None
    api_container_id: str | None = None
    connected_dependencies = (postgres_id, redis_id)
    actor_id: uuid.UUID | None = None
    outsider_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    secret_values = [api_secret, api_s3_access, api_s3_secret]
    secret_values.extend(
        [
            api_database_url.render_as_string(hide_password=False),
            auth_database_url.render_as_string(hide_password=False),
            api_db_host_url.render_as_string(hide_password=False),
        ]
    )

    try:
        _write_private_file(
            auth_secret_file,
            auth_database_url.render_as_string(hide_password=False),
            mode=0o444,
        )
        _write_environment(environment_file, api_environment)
        network_id = _docker(
            "network",
            "create",
            "--driver",
            "bridge",
            "--label",
            f"{_ACCEPTANCE_LABEL}={_CONTAINER_ACCEPTANCE}",
            "--label",
            f"{_ACCEPTANCE_ID_LABEL}={run_id}",
            network_name,
        )
        if re.fullmatch(r"[0-9a-f]{64}", network_id) is None:
            raise RuntimeError("Docker did not return the private network identity")
        _network_details(network_name, network_id, run_id)

        connect_owned_dependency(network_name, network_id, postgres_id, _POSTGRES_ALIAS)
        connect_owned_dependency(network_name, network_id, redis_id, _REDIS_ALIAS)
        _verify_minio_identity(_inspect_docker(minio["id"]), minio)
        connect_owned_dependency(network_name, network_id, minio["id"], _MINIO_ALIAS)

        api_container_id = _docker(
            "create",
            "--name",
            container_name,
            "--label",
            f"{_ACCEPTANCE_LABEL}={_CONTAINER_ACCEPTANCE}",
            "--label",
            f"{_ACCEPTANCE_ID_LABEL}={run_id}",
            "--label",
            f"{_WORKTREE_OWNER_LABEL}={resource_owner}",
            "--network",
            network_name,
            "--publish",
            f"127.0.0.1::{_API_PORT}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--pids-limit",
            "128",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--stop-timeout",
            "30",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount",
            f"type=bind,source={auth_secret_file.resolve()},target={_AUTH_SECRET_TARGET},readonly",
            "--env-file",
            str(environment_file.resolve()),
            image_ref,
        )
        if re.fullmatch(r"[0-9a-f]{64}", api_container_id) is None:
            raise RuntimeError("Docker did not return the cold API container identity")
        _docker("start", api_container_id, timeout=60)
        origin = _verify_api_container(
            api_container_id,
            image_id=expected_image_id,
            network_name=network_name,
            network_id=network_id,
            run_id=run_id,
            resource_owner=resource_owner,
            container_name=container_name,
            auth_secret_file=auth_secret_file,
            database_url=api_database_url,
            auth_url=auth_database_url,
            minio_endpoint=minio["endpoint"],
            minio_access_key=api_s3_access,
            minio_secret_key=api_s3_secret,
        )
        await _wait_for_api(
            origin,
            api_container_id,
            secrets_to_redact=secret_values,
        )
        runtime_probe = await _container_identity_probe(api_container_id)
        assert runtime_probe["database"] == test_engine.url.database

        async with httpx.AsyncClient(
            base_url=origin, trust_env=False, timeout=30
        ) as client:
            async with async_sessionmaker(
                test_engine, expire_on_commit=False
            ).begin() as db:
                suffix = uuid.uuid4().hex
                actor = await create_user(
                    db,
                    "super_admin",
                    f"labeling-cold-api-{suffix}@example.test",
                    "Cold Labeling API owner",
                )
                outsider = await create_user(
                    db,
                    "employee",
                    f"labeling-cold-api-outsider-{suffix}@example.test",
                    "Cold Labeling API outsider",
                )
                project = await create_project(
                    db,
                    owner_id=actor.id,
                    name=f"Cold Labeling API {suffix}",
                )
                task = await create_task(
                    db, project_id=project.id, status="in_progress"
                )
                actor_id, outsider_id = actor.id, outsider.id
                project_id, task_id = project.id, task.id
            actor_token = create_access_token(
                str(actor_id), "super_admin", secret=api_secret, expire_minutes=60
            )
            outsider_token = create_access_token(
                str(outsider_id), "employee", secret=api_secret, expire_minutes=60
            )
            authorized = {"Authorization": f"Bearer {actor_token}"}
            unauthorized = {"Authorization": f"Bearer {outsider_token}"}
            payload = {
                "annotation_type": "bbox",
                "tool_unit_id": "bbox",
                "class_name": "car",
                "geometry": {
                    "type": "bbox",
                    "x": 0.1,
                    "y": 0.2,
                    "w": 0.3,
                    "h": 0.4,
                },
            }
            read_path = f"/api/v1/tasks/{task_id}/annotations"
            missing_auth = await client.get(read_path)
            assert missing_auth.status_code == 401, missing_auth.text
            outsider_read = await client.get(read_path, headers=unauthorized)
            assert outsider_read.status_code == 404, outsider_read.text
            outsider_write = await client.post(
                read_path, headers=unauthorized, json=payload
            )
            assert outsider_write.status_code == 404, outsider_write.text

            before = await client.get(read_path, headers=authorized)
            assert before.status_code == 200, before.text
            assert before.json() == []

            idempotency_key = "cold-api-" + uuid.uuid4().hex
            write_headers = {
                **authorized,
                "Idempotency-Key": idempotency_key,
            }
            async with test_engine.begin() as lock_session:
                await lock_session.execute(
                    text("SELECT id FROM public.tasks WHERE id=:task_id FOR UPDATE"),
                    {"task_id": task_id},
                )
                pending_write = asyncio.create_task(
                    client.post(read_path, headers=write_headers, json=payload)
                )
                blocker_pid = await lock_session.scalar(text("SELECT pg_backend_pid()"))
                lock_wait_observed = await _wait_for_task_lock_waiter(
                    test_engine, blocker_pid
                )
            created = await asyncio.wait_for(pending_write, timeout=30)
            assert lock_wait_observed, "API write bypassed the Task row lock"
            assert created.status_code == 201, created.text
            created_json = created.json()
            annotation_id = uuid.UUID(created_json["id"])

            replay = await client.post(read_path, headers=write_headers, json=payload)
            assert replay.status_code == 201, replay.text
            assert replay.json() == created_json
            after_write = await client.get(read_path, headers=authorized)
            assert after_write.status_code == 200, after_write.text
            assert [item["id"] for item in after_write.json()] == [str(annotation_id)]
            assert after_write.json()[0]["class_name"] == "car"

            async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
                stored = await db.get(Annotation, annotation_id)
                assert stored is not None
                assert stored.task_id == task_id and stored.project_id == project_id
                assert stored.class_name == "car"
                assert stored.geometry["type"] == "bbox"

            try:
                async with test_engine.begin() as check_fk:
                    await check_fk.execute(
                        text("DELETE FROM public.tasks WHERE id=:task_id"),
                        {"task_id": task_id},
                    )
            except DBAPIError as exc:
                sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
                    exc.orig, "pgcode", None
                )
                assert sqlstate == "23503"
            else:
                pytest.fail("annotation write did not preserve its Task foreign key")

            _verify_api_container(
                api_container_id,
                image_id=expected_image_id,
                network_name=network_name,
                network_id=network_id,
                run_id=run_id,
                resource_owner=resource_owner,
                container_name=container_name,
                auth_secret_file=auth_secret_file,
                database_url=api_database_url,
                auth_url=auth_database_url,
                minio_endpoint=minio["endpoint"],
                minio_access_key=api_s3_access,
                minio_secret_key=api_s3_secret,
            )
            await client.aclose()
            _docker("restart", api_container_id, timeout=60)
            restarted_origin = _verify_api_container(
                api_container_id,
                image_id=expected_image_id,
                network_name=network_name,
                network_id=network_id,
                run_id=run_id,
                resource_owner=resource_owner,
                container_name=container_name,
                auth_secret_file=auth_secret_file,
                database_url=api_database_url,
                auth_url=auth_database_url,
                minio_endpoint=minio["endpoint"],
                minio_access_key=api_s3_access,
                minio_secret_key=api_s3_secret,
            )
            await _wait_for_api(
                restarted_origin,
                api_container_id,
                secrets_to_redact=secret_values,
            )
            async with httpx.AsyncClient(
                base_url=restarted_origin, trust_env=False, timeout=30
            ) as restarted_client:
                after_restart = await restarted_client.get(
                    read_path, headers=authorized
                )
                assert after_restart.status_code == 200, after_restart.text
                assert [item["id"] for item in after_restart.json()] == [
                    str(annotation_id)
                ]
                assert after_restart.json()[0]["class_name"] == "car"

        record_property("labeling_cold_api_image_id", expected_image_id)
        record_property("labeling_cold_api_image_identity", artifact["identity"])
        record_property("labeling_cold_api_source_revision", artifact["source_commit"])
        record_property("labeling_cold_api_database_role", "identity_api_consumer")
        record_property("labeling_cold_api_identity_auth_role", "identity_auth_client")
        record_property(
            "labeling_cold_api_package_origin", runtime_probe["package_origin"]
        )
        record_property("labeling_cold_api_task_lock_observed", True)
        record_property("labeling_cold_api_restarted", True)
    finally:
        cleanup_errors: list[str] = []
        if api_container_id is None:
            try:
                api_container_id = _find_created_container_id(container_name)
            except BaseException:
                cleanup_errors.append(
                    "partial cold API container could not be inspected"
                )
        if network_id is None:
            try:
                network_id = _find_created_network_id(network_name)
            except BaseException:
                cleanup_errors.append("partial private network could not be inspected")
        if api_container_id is not None:
            try:
                details = _inspect_docker(api_container_id)
                labels = details.get("Config", {}).get("Labels") or {}
                if (
                    details.get("Id") == api_container_id
                    and details.get("Image") == expected_image_id
                    and labels.get(_ACCEPTANCE_LABEL) == _CONTAINER_ACCEPTANCE
                    and labels.get(_ACCEPTANCE_ID_LABEL) == run_id
                    and labels.get(_WORKTREE_OWNER_LABEL) == resource_owner
                ):
                    _docker("rm", "--force", api_container_id, timeout=30)
                else:
                    cleanup_errors.append("cold API container ownership changed")
            except BaseException:
                cleanup_errors.append("cold API container could not be removed")

        if network_id is not None:
            try:
                _network_details(network_name, network_id, run_id)
            except BaseException:
                cleanup_errors.append("private cold API network ownership changed")
            else:
                for dependency_id in connected_dependencies:
                    try:
                        details = _inspect_docker(dependency_id)
                        if network_name not in (
                            details.get("NetworkSettings", {}).get("Networks") or {}
                        ):
                            continue
                        labels = details.get("Config", {}).get("Labels") or {}
                        if dependency_id == postgres_id:
                            is_owned = (
                                labels.get(_ACCEPTANCE_LABEL) == "module-data-boundary"
                                and labels.get(_ACCEPTANCE_ID_LABEL)
                                == postgres_identity
                            )
                        else:
                            is_owned = (
                                labels.get(_WORKTREE_OWNER_LABEL) == resource_owner
                            )
                        if not is_owned:
                            cleanup_errors.append(
                                "refused to detach an unowned dependency"
                            )
                            continue
                        disconnect_owned_dependency(
                            network_name, network_id, dependency_id
                        )
                    except BaseException:
                        cleanup_errors.append(
                            "owned dependency could not be detached from private network"
                        )
                try:
                    details = _inspect_docker(minio["id"])
                    _verify_minio_identity(details, minio)
                    endpoint = (
                        details.get("NetworkSettings", {})
                        .get("Networks", {})
                        .get(network_name)
                    )
                    if endpoint:
                        if endpoint.get("NetworkID") != network_id:
                            cleanup_errors.append(
                                "refused to detach MinIO outside the test network"
                            )
                        else:
                            disconnect_owned_dependency(
                                network_name, network_id, minio["id"]
                            )
                except BaseException:
                    cleanup_errors.append(
                        "shared MinIO endpoint could not be detached from test network"
                    )
                try:
                    network = _network_details(network_name, network_id, run_id)
                    if network.get("Containers"):
                        cleanup_errors.append(
                            "private cold API network still has endpoints"
                        )
                    else:
                        _docker("network", "rm", network_name, timeout=30)
                except BaseException:
                    cleanup_errors.append(
                        "private cold API network could not be removed"
                    )

        try:
            await _cleanup_rows(
                test_engine,
                actor_id=actor_id,
                outsider_id=outsider_id,
                project_id=project_id,
                task_id=task_id,
            )
        except Exception as exc:
            cause = getattr(getattr(exc, "orig", exc), "__cause__", None)
            cleanup_errors.append(
                f"cold API fixture cleanup failed: {type(exc).__name__} "
                f"sqlstate={getattr(cause, 'sqlstate', None)} "
                f"constraint={getattr(cause, 'constraint_name', None)}"
            )
        for path in (auth_secret_file, environment_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append("cold API temporary secret could not be removed")
        if cleanup_errors:
            pytest.fail("cold API cleanup was incomplete: " + "; ".join(cleanup_errors))
