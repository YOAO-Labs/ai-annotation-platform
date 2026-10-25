"""Cold consumer proof for the immutable API image and installed Labeling package."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.factory import create_project, create_task, create_user
from tests.test_asset_storage_cleanup_runtime import (
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url as _verified_role_url

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
_API_ALIAS = "labeling-cold-api"
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
    value = os.environ.get(name)
    if not value or "\n" in value or "\r" in value:
        pytest.fail(f"the verifier did not provide {name}", pytrace=False)
    return value


def _plain(value: object) -> str:
    getter = getattr(value, "get_secret_value", None)
    return getter() if callable(getter) else str(value)


def _docker(*arguments: str, timeout: int = 90) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"owned Docker operation timed out: {arguments[0]}"
        ) from None
    if result.returncode != 0:
        raise RuntimeError(f"owned Docker operation failed: {arguments[0]}")
    return result.stdout.strip()


def _inspect_container(container_id: str) -> dict:
    try:
        value = json.loads(_docker("inspect", container_id, timeout=20))
    except (json.JSONDecodeError, RuntimeError):
        raise RuntimeError("Docker returned no unique container identity") from None
    if not isinstance(value, list) or len(value) != 1:
        raise RuntimeError("Docker container identity was not unique")
    return value[0]


def _inspect_network(network_name: str) -> dict:
    try:
        value = json.loads(_docker("network", "inspect", network_name, timeout=20))
    except (json.JSONDecodeError, RuntimeError):
        raise RuntimeError("Docker returned no unique network identity") from None
    if not isinstance(value, list) or len(value) != 1:
        raise RuntimeError("Docker network identity was not unique")
    return value[0]


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
        and artifact.get("proof_container_removed") is True
        and artifact.get("image") == image_ref
        and artifact.get("image_id") == expected_image_id
        and isinstance(artifact.get("identity"), str)
        and isinstance(artifact.get("source_commit"), str)
        and isinstance(artifact.get("source_manifest"), dict)
        and isinstance(artifact.get("api_hashes"), dict)
        and isinstance(artifact.get("package_hashes"), dict)
        and isinstance(offline, dict)
        and offline.get("status") == "passed"
        and offline.get("source_hashes_match") is True
        and any(
            name.endswith("aap_labeling/annotation_write_contract.py")
            for name in artifact["package_hashes"]
        )
    ):
        pytest.fail("the current API image artifact did not prove its source bytes")
    return image_ref, expected_image_id, artifact


def _verify_image(image_ref: str, expected_id: str, artifact: dict) -> dict:
    try:
        images = json.loads(_docker("image", "inspect", image_ref, timeout=20))
    except (json.JSONDecodeError, RuntimeError):
        pytest.fail("Docker did not return the qualified API image", pytrace=False)
    if not isinstance(images, list) or len(images) != 1:
        pytest.fail("the qualified API image identity was not unique", pytrace=False)
    image = images[0]
    config = image.get("Config", {})
    labels = config.get("Labels") or {}
    if not (
        image.get("Id") == expected_id
        and image.get("Id", "").startswith("sha256:")
        and labels.get(_ACCEPTANCE_LABEL) == _IMAGE_ACCEPTANCE
        and labels.get(_ACCEPTANCE_ID_LABEL) == artifact["identity"]
        and labels.get(_SOURCE_REVISION_LABEL) == artifact["source_commit"]
        and config.get("Entrypoint") == ["/app/scripts/entrypoint.sh"]
        and config.get("Cmd")
        == ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    ):
        pytest.fail("API image ID or provenance labels did not match the artifact")
    return image


def _role_url(
    variable: str,
    role: str,
    test_engine,
    *,
    internal_network: bool = False,
) -> URL:
    try:
        value = make_url(_verified_role_url(variable, role))
    except Exception:
        pytest.fail(f"verifier role URL {variable} was invalid", pytrace=False)
    if not (
        value.drivername in {"postgresql", "postgresql+asyncpg"}
        and value.username == role
        and value.password
        and value.database == test_engine.url.database
        and value.database
        and value.database.endswith("_test")
        and (value.host, value.port) == (test_engine.url.host, test_engine.url.port)
    ):
        pytest.fail(f"verifier role URL {variable} escaped the test database")
    updates = {"drivername": "postgresql+asyncpg"}
    if internal_network:
        updates.update(host=_POSTGRES_ALIAS, port=5432)
    return value.set(**updates)


async def _verify_role_profile(url: URL, role: str, *, labeling_api: bool) -> None:
    engine = create_async_engine(url.set(host="127.0.0.1"), hide_parameters=True)
    try:
        async with engine.connect() as db:
            identity = await db.execute(
                text(
                    "SELECT current_user,current_database(),role.rolsuper,"
                    "role.rolcreatedb,role.rolcreaterole,role.rolbypassrls,"
                    "pg_catalog.pg_has_role(current_user,'owner','MEMBER') "
                    "FROM pg_catalog.pg_roles AS role "
                    "WHERE role.rolname=current_user"
                )
            )
            current_user, database, *privileges = identity.one()
            assert current_user == role
            assert database == url.database
            assert privileges == [False, False, False, False, False]
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_schema_privilege(current_user,"
                        "'aap_identity_v1','USAGE')"
                    )
                )
                is True
            )
            if labeling_api:
                assert (
                    await db.scalar(
                        text(
                            "SELECT pg_catalog.has_function_privilege(current_user,"
                            "'aap_labeling_v1.annotation_write_command(jsonb)'::"
                            "pg_catalog.regprocedure,'EXECUTE')"
                        )
                    )
                    is True
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT pg_catalog.has_table_privilege(current_user,"
                            "'aap_labeling_v1.annotation_rows','SELECT')"
                        )
                    )
                    is True
                )
    finally:
        await engine.dispose()


def _write_private_file(path: Path, value: str, *, mode: int) -> None:
    if path.is_symlink() or not value or "\0" in value or "\r" in value:
        raise RuntimeError("cold API file input was unsafe")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.write("\n")
    path.chmod(mode)


def _write_environment(path: Path, values: dict[str, str]) -> None:
    if any(
        not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
        or "\0" in value
        or "\n" in value
        or "\r" in value
        for name, value in values.items()
    ):
        raise RuntimeError("cold API environment was not safe to write")
    _write_private_file(
        path,
        "\n".join(f"{name}={value}" for name, value in sorted(values.items())),
        mode=0o600,
    )


def _redis_dependency(resources: dict, test_engine) -> tuple[str, str, URL]:
    progress = _verify_redis()
    broker = make_url(_plain(settings.effective_celery_broker))
    if not (
        progress.drivername == broker.drivername == "redis"
        and progress.host == broker.host == "127.0.0.1"
        and progress.port is not None
        and progress.port == broker.port
        and progress.username is None
        and broker.username is None
        and progress.password is None
        and broker.password is None
        and progress.database == broker.database == 0
    ):
        pytest.fail("verifier Redis is not the expected loopback test service")
    name, owner = resources.get("redis_name"), resources.get("owner")
    if not isinstance(name, str) or not isinstance(owner, str):
        pytest.fail("the test resource manifest omitted its Redis identity")
    ids = _docker(
        "container",
        "ls",
        "--all",
        "--filter",
        f"name=^/{name}$",
        "--format",
        "{{.ID}}",
    ).splitlines()
    if len(ids) != 1:
        pytest.fail("the verifier Redis container identity was not unique")
    container_id = ids[0]
    details = _inspect_container(container_id)
    labels = details.get("Config", {}).get("Labels") or {}
    bindings = (details.get("NetworkSettings", {}).get("Ports") or {}).get(
        "6379/tcp"
    ) or []
    if not (
        details.get("Name") == f"/{name}"
        and details.get("State", {}).get("Running") is True
        and labels.get(_WORKTREE_OWNER_LABEL) == owner
        and len(bindings) == 1
        and bindings[0].get("HostIp") == "127.0.0.1"
        and int(bindings[0].get("HostPort", 0)) == progress.port
    ):
        pytest.fail("the Redis container was not this worktree's test resource")
    return container_id, owner, progress.set(host=_REDIS_ALIAS, port=6379)


def _minio_dependency(resources: dict) -> tuple[str, int]:
    host = _plain(settings.minio_endpoint)
    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = urlsplit(f"{scheme}://{host}")
    if (
        endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
        or endpoint.port is None
    ):
        pytest.fail("verifier MinIO endpoint was not loopback scoped")
    owner = resources.get("owner")
    if not isinstance(owner, str):
        pytest.fail("the test resource manifest omitted its MinIO owner")
    ids = _docker(
        "container",
        "ls",
        "--all",
        "--filter",
        f"publish={endpoint.port}",
        "--format",
        "{{.ID}}",
    ).splitlines()
    matches = []
    for container_id in ids:
        details = _inspect_container(container_id)
        labels = details.get("Config", {}).get("Labels") or {}
        ports = details.get("NetworkSettings", {}).get("Ports") or {}
        for container_port, bindings in ports.items():
            if (
                any(
                    binding.get("HostIp") == "127.0.0.1"
                    and int(binding.get("HostPort", 0)) == endpoint.port
                    for binding in bindings or []
                )
                and labels.get(_WORKTREE_OWNER_LABEL) == owner
            ):
                matches.append((container_id, int(container_port.split("/", 1)[0])))
    if len(matches) != 1:
        pytest.fail("the verifier-owned MinIO endpoint was not unique")
    minio_id, service_port = matches[0]
    details = _inspect_container(minio_id)
    if details.get("State", {}).get("Running") is not True:
        pytest.fail("the verifier-owned MinIO container was not running")
    return minio_id, service_port


def _network_details(network_name: str, expected_id: str, run_id: str) -> dict:
    details = _inspect_network(network_name)
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
    details = _inspect_container(matches[0])
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
    details = _inspect_network(matches[0])
    if details.get("Name") != network_name:
        raise RuntimeError("cold API network name did not match")
    network_id = details.get("Id")
    if not isinstance(network_id, str) or not network_id:
        raise RuntimeError("cold API network identity was missing")
    return network_id


def _connect_dependency(
    network_name: str,
    network_id: str,
    container_id: str,
    alias: str,
    *,
    owner_label: str,
    owner_value: str,
) -> None:
    details = _inspect_container(container_id)
    labels = details.get("Config", {}).get("Labels") or {}
    networks = details.get("NetworkSettings", {}).get("Networks") or {}
    if labels.get(owner_label) != owner_value or network_name in networks:
        raise RuntimeError(
            "refusing to attach an unowned or already attached dependency"
        )
    _docker("network", "connect", "--alias", alias, network_name, container_id)
    attached = (
        _inspect_container(container_id)
        .get("NetworkSettings", {})
        .get("Networks", {})
        .get(network_name)
    )
    if not (
        attached
        and attached.get("NetworkID") == network_id
        and attached.get("EndpointID")
        and alias in (attached.get("Aliases") or [])
    ):
        raise RuntimeError("private dependency network alias was not installed")


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
    details: dict | None = None,
) -> tuple[str, dict[str, str]]:
    details = details or _inspect_container(container_id)
    config = details.get("Config", {})
    host = details.get("HostConfig", {})
    labels = config.get("Labels") or {}
    if not (
        details.get("Id") == container_id
        and details.get("Name") == f"/{container_name}"
        and details.get("Image") == image_id
        and config.get("User") == "10001:10001"
        and config.get("Entrypoint") == ["/app/scripts/entrypoint.sh"]
        and config.get("Cmd")
        == ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
        and labels.get(_ACCEPTANCE_LABEL) == _CONTAINER_ACCEPTANCE
        and labels.get(_ACCEPTANCE_ID_LABEL) == run_id
        and labels.get(_WORKTREE_OWNER_LABEL) == resource_owner
        and host.get("NetworkMode") == network_name
        and host.get("ReadonlyRootfs") is True
        and host.get("CapDrop") == ["ALL"]
        and host.get("CapAdd") in (None, [])
        and host.get("Privileged") is False
        and "no-new-privileges:true" in (host.get("SecurityOpt") or [])
        and host.get("PidsLimit") == 128
        and host.get("NanoCpus") == 1_000_000_000
        and host.get("Memory") == 1024 * 1024 * 1024
        and "size=64m" in (host.get("Tmpfs") or {}).get("/tmp", "")
    ):
        raise RuntimeError("cold API container identity or resource profile changed")
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
        and not Path(binds[0]["Source"])
        .resolve()
        .is_relative_to(Path(__file__).resolve().parents[3] / "apps")
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
        and "legacy_runtime" not in environment.get("DATABASE_URL", "")
        and "owner:" not in environment.get("DATABASE_URL", "")
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
    origin = f"http://127.0.0.1:{int(bindings[0]['HostPort'])}"
    return origin, environment


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
            details = _inspect_container(container_id)
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


async def _wait_for_task_lock_waiter(test_engine) -> bool:
    deadline = time.monotonic() + 8
    statement = text(
        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
        "WHERE datname=current_database() AND usename='identity_api_consumer' "
        "AND state='active' "
        "AND wait_event_type='Lock' AND query LIKE '%lock_task_for_core_read%')"
    )
    while time.monotonic() < deadline:
        async with test_engine.connect() as db:
            if await db.scalar(statement):
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
    identity_auth_host_url = _role_url(
        "AAP_MODULE_DATA_IDENTITY_AUTH_URL", "identity_auth_client", test_engine
    )
    _verify_postgres(api_db_host_url)
    resources = _verified_resource_manifest(test_engine, api_db_host_url)
    await _verify_role_profile(
        api_db_host_url, "identity_api_consumer", labeling_api=True
    )
    await _verify_role_profile(
        identity_auth_host_url, "identity_auth_client", labeling_api=False
    )

    postgres_id = _required("AAP_MODULE_DATA_PG_CONTAINER_ID")
    postgres_name = _required("AAP_MODULE_DATA_PG_CONTAINER_NAME")
    postgres_identity = _required("AAP_MODULE_DATA_PG_CONTAINER_IDENTITY")
    postgres = _inspect_container(postgres_id)
    pg_labels = postgres.get("Config", {}).get("Labels") or {}
    if not (
        postgres.get("Name") == f"/{postgres_name}"
        and pg_labels.get(_ACCEPTANCE_LABEL) == "module-data-boundary"
        and pg_labels.get(_ACCEPTANCE_ID_LABEL) == postgres_identity
        and postgres.get("State", {}).get("Running") is True
    ):
        pytest.fail("the verifier PostgreSQL identity was not stable")

    redis_id, resource_owner, redis_host_url = _redis_dependency(resources, test_engine)
    minio_id, minio_port = _minio_dependency(resources)
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
        "IDENTITY_BRIEFS_DATABASE_URL": "",
        "IDENTITY_BRIEFS_DATABASE_URL_FILE": "",
        "MINIO_ENDPOINT": f"{_MINIO_ALIAS}:{minio_port}",
        "MINIO_ACCESS_KEY": api_s3_access,
        "MINIO_SECRET_KEY": api_s3_secret,
        "MINIO_USE_SSL": "true" if settings.minio_use_ssl else "false",
        "MINIO_PUBLIC_URL": "",
        "SENTRY_DSN": "",
        "TURNSTILE_ENABLED": "false",
        "TRAINING_ENABLED": "false",
        "CORS_ALLOW_ORIGINS": "[]",
    }
    for bucket_name in _API_BUCKET_NAMES:
        bucket = resources.get("buckets", {}).get(bucket_name)
        if not isinstance(bucket, str) or not bucket:
            pytest.fail(f"the verifier omitted its owned {bucket_name}")
        api_environment[bucket_name] = bucket

    network_id: str | None = None
    api_container_id: str | None = None
    connected_dependencies = (postgres_id, redis_id, minio_id)
    actor_id: uuid.UUID | None = None
    outsider_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    secret_values = [api_secret, api_s3_access, api_s3_secret]
    secret_values.extend(
        [
            api_database_url.render_as_string(hide_password=False),
            auth_database_url.render_as_string(hide_password=False),
            identity_auth_host_url.render_as_string(hide_password=False),
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

        _connect_dependency(
            network_name,
            network_id,
            postgres_id,
            _POSTGRES_ALIAS,
            owner_label=_ACCEPTANCE_ID_LABEL,
            owner_value=postgres_identity,
        )
        _connect_dependency(
            network_name,
            network_id,
            redis_id,
            _REDIS_ALIAS,
            owner_label=_WORKTREE_OWNER_LABEL,
            owner_value=resource_owner,
        )
        _connect_dependency(
            network_name,
            network_id,
            minio_id,
            _MINIO_ALIAS,
            owner_label=_WORKTREE_OWNER_LABEL,
            owner_value=resource_owner,
        )

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
            "--network-alias",
            _API_ALIAS,
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
        origin, _ = _verify_api_container(
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
            minio_endpoint=f"{_MINIO_ALIAS}:{minio_port}",
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
            actor_id = None
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

            missing_task = await client.post(
                f"/api/v1/tasks/{uuid.uuid4()}/annotations",
                headers=authorized,
                json=payload,
            )
            assert missing_task.status_code == 404, missing_task.text

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
                lock_wait_observed = await _wait_for_task_lock_waiter(test_engine)
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

            await _verify_api_container(
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
                minio_endpoint=f"{_MINIO_ALIAS}:{minio_port}",
                minio_access_key=api_s3_access,
                minio_secret_key=api_s3_secret,
            )
            await client.aclose()
            _docker("restart", api_container_id, timeout=60)
            restarted_details = _inspect_container(api_container_id)
            restarted_origin, _ = _verify_api_container(
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
                minio_endpoint=f"{_MINIO_ALIAS}:{minio_port}",
                minio_access_key=api_s3_access,
                minio_secret_key=api_s3_secret,
                details=restarted_details,
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
            assert not restarted_details.get("State", {}).get("OOMKilled", True)

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
            except Exception:
                cleanup_errors.append(
                    "partial cold API container could not be inspected"
                )
        if network_id is None:
            try:
                network_id = _find_created_network_id(network_name)
            except Exception:
                cleanup_errors.append("partial private network could not be inspected")
        if api_container_id is not None:
            try:
                details = _inspect_container(api_container_id)
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
            except Exception:
                cleanup_errors.append("cold API container could not be removed")

        if network_id is not None:
            try:
                _network_details(network_name, network_id, run_id)
            except Exception:
                cleanup_errors.append("private cold API network ownership changed")
            else:
                for dependency_id in connected_dependencies:
                    try:
                        details = _inspect_container(dependency_id)
                        if network_name not in (
                            details.get("NetworkSettings", {}).get("Networks") or {}
                        ):
                            continue
                        labels = details.get("Config", {}).get("Labels") or {}
                        if dependency_id == postgres_id:
                            is_owned = (
                                labels.get(_ACCEPTANCE_ID_LABEL) == postgres_identity
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
                        _docker("network", "disconnect", network_name, dependency_id)
                    except Exception:
                        cleanup_errors.append(
                            "owned dependency could not be detached from private network"
                        )
                try:
                    network = _network_details(network_name, network_id, run_id)
                    if network.get("Containers"):
                        cleanup_errors.append(
                            "private cold API network still has endpoints"
                        )
                    else:
                        _docker("network", "rm", network_name, timeout=30)
                except Exception:
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
        except Exception:
            cleanup_errors.append("cold API fixture rows could not be removed")
        for path in (auth_secret_file, environment_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append("cold API temporary secret could not be removed")
        if cleanup_errors:
            pytest.fail("cold API cleanup was incomplete: " + "; ".join(cleanup_errors))
