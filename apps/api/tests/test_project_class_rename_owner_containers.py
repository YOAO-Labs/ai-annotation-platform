"""Exercise Project class rename through the owned Workspace container."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import warnings

import httpx
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from tests import test_project_class_rename_legacy as legacy
from tests.factory import create_project
from tests.labeling_class_query_helpers import (
    api_db_binding,
    assert_owned_test_database,
    clear_local_identity,
    make_request_runtime,
)
from tests.project_class_rename_owner_helpers import (
    aliased_uuid,
    class_rename_binding,
    cleanup_lock_world,
    cleanup_world,
    locks_fixture,
    samples_fixture,
    seed_legacy_world,
    seed_lock_world,
)
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
    _verify_postgres,
)
from tests.test_project_class_rename_owner import (
    _observe_api_commits,
    _replay_http_samples,
    _replay_lock_cases,
    _tracked_request,
)
from tests.workspace_configuration_helpers import configuration_dsn
from tests.workspace_project_query_helpers import (
    project_query_briefs_dsn,
    project_query_dsn,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_PROJECT_CLASS_RENAME_CONTAINER_LIVE") != "1",
        reason="requires the explicitly qualified owned Workspace class-rename image",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_IMAGE_ACCEPTANCE = "project-class-rename-owner"
_CONTAINER_ACCEPTANCE = "module-data-boundary"
_NETWORK_ACCEPTANCE = "module-data-boundary"
_SOURCE_REVISION = "54cd17288acdbcb4e6e3983fcdf3199fcf48c67d"
_CONTAINER_PORT = 8176
_PROFILE_REQUESTS = 16
_PROFILE_CONCURRENCY = 4
_CONFIGURATION_PATH = "/v1/workspace/configuration/execute"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"the owned class-rename verifier did not provide {name}")
    return value


def _observation_path() -> Path | None:
    raw = os.environ.get("AAP_PROJECT_CLASS_RENAME_CONTAINER_OBSERVATION")
    if not raw:
        return None
    target = Path(raw)
    expected_parent = _ROOT / ".worktree"
    assert target.is_absolute()
    assert target.resolve().parent == expected_parent.resolve()
    assert target.suffix == ".json"
    assert expected_parent.is_dir()
    assert not target.exists()
    return target


def _inspect_network(target: str) -> dict:
    try:
        values = json.loads(_docker("network", "inspect", target, timeout=15))
    except json.JSONDecodeError:
        pytest.fail("Docker returned an invalid owned Workspace network record")
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("owned Workspace network identity was not unique")
    return values[0]


def _verify_image(image_ref: str, expected_identity: str) -> dict:
    assert re.fullmatch(r"[0-9a-f]{24}", expected_identity)
    image = _inspect_docker(image_ref)
    config = image.get("Config", {})
    labels = config.get("Labels") or {}
    assert labels.get("org.aap.acceptance") == _IMAGE_ACCEPTANCE
    assert labels.get("org.aap.acceptance-id") == expected_identity
    assert labels.get("org.aap.source-revision") == _SOURCE_REVISION
    assert config.get("User") == "10004:10004"
    assert config.get("Cmd") == ["python", "-m", "aap_workspace"]
    assert config.get("Entrypoint") in (None, [])
    image_id = image.get("Id")
    assert isinstance(image_id, str) and image_id.startswith("sha256:")
    assert image.get("Architecture") in ("arm64", "amd64")
    return image


def _published_origin(metadata: dict) -> tuple[str, int]:
    published = (
        metadata.get("NetworkSettings", {})
        .get("Ports", {})
        .get(f"{_CONTAINER_PORT}/tcp")
        or []
    )
    assert len(published) == 1
    assert published[0].get("HostIp") == "127.0.0.1"
    host_port = int(published[0].get("HostPort", 0))
    assert 1 <= host_port <= 65535
    return f"http://127.0.0.1:{host_port}", host_port


def _container_limits(metadata: dict) -> dict[str, int | bool]:
    host = metadata.get("HostConfig", {})
    tmpfs_options = (host.get("Tmpfs") or {}).get("/tmp", "")
    return {
        "read_only_rootfs": host.get("ReadonlyRootfs") is True,
        "cap_drop_all": host.get("CapDrop") == ["ALL"],
        "no_new_privileges": "no-new-privileges:true"
        in (host.get("SecurityOpt") or []),
        "pids_limit": host.get("PidsLimit"),
        "cpu_nanos": host.get("NanoCpus"),
        "memory_bytes": host.get("Memory"),
        "tmpfs_32m_noexec_nosuid_nodev": all(
            marker in tmpfs_options
            for marker in ("noexec", "nosuid", "nodev", "size=32m")
        )
        or all(
            marker in tmpfs_options
            for marker in ("noexec", "nosuid", "nodev", "size=33554432")
        ),
    }


def _verify_container(
    metadata: dict,
    *,
    container_id: str,
    container_identity: str,
    image_id: str,
    network_name: str,
    secret_files: dict[str, Path],
) -> dict[str, int | bool]:
    config = metadata.get("Config", {})
    labels = config.get("Labels") or {}
    host = metadata.get("HostConfig", {})
    assert metadata.get("Id") == container_id
    assert metadata.get("Image") == image_id
    assert labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
    assert labels.get("org.aap.acceptance-id") == container_identity
    assert config.get("User") == "10004:10004"
    assert config.get("Cmd") == ["python", "-m", "aap_workspace"]
    assert config.get("Entrypoint") in (None, [])
    assert host.get("NetworkMode") == network_name
    assert host.get("ReadonlyRootfs") is True
    assert host.get("CapDrop") == ["ALL"]
    assert host.get("CapAdd") in (None, [])
    assert "no-new-privileges:true" in (host.get("SecurityOpt") or [])
    assert host.get("PidsLimit") == 64
    assert host.get("NanoCpus") == 1_000_000_000
    assert host.get("Memory") == 256 * 1024 * 1024
    tmpfs = host.get("Tmpfs") or {}
    assert "/tmp" in tmpfs
    tmpfs_options = tmpfs["/tmp"]
    assert all(marker in tmpfs_options for marker in ("noexec", "nosuid", "nodev"))
    assert "size=32m" in tmpfs_options or "size=33554432" in tmpfs_options

    mounts = metadata.get("Mounts") or []
    binds = [mount for mount in mounts if mount.get("Type") == "bind"]
    expected_mounts = {
        destination: path.resolve() for destination, path in secret_files.items()
    }
    assert len(binds) == 4
    assert {
        mount["Destination"]: Path(mount["Source"]).resolve() for mount in binds
    } == expected_mounts
    assert all(mount.get("RW") is False for mount in binds)
    assert {mount.get("Destination") for mount in mounts} <= set(expected_mounts) | {
        "/tmp"
    }

    published = (
        metadata.get("NetworkSettings", {})
        .get("Ports", {})
        .get(f"{_CONTAINER_PORT}/tcp")
        or []
    )
    assert len(published) == 1
    assert published[0].get("HostIp") == "127.0.0.1"

    networks = metadata.get("NetworkSettings", {}).get("Networks") or {}
    assert set(networks) == {network_name}
    assert networks[network_name].get("NetworkID")
    environment = dict(
        entry.split("=", 1) for entry in (config.get("Env") or []) if "=" in entry
    )
    assert {
        key: value for key, value in environment.items() if key.startswith("WORKSPACE_")
    } == {
        "WORKSPACE_CONFIGURATION_DATABASE_URL_FILE": (
            "/run/secrets/configuration-database"
        ),
        "WORKSPACE_PROJECT_QUERY_DATABASE_URL_FILE": (
            "/run/secrets/project-query-database"
        ),
        "WORKSPACE_PROJECT_QUERY_IDENTITY_BRIEFS_DATABASE_URL_FILE": (
            "/run/secrets/identity-briefs-database"
        ),
        "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": "/run/secrets/control-token",
        "WORKSPACE_HOST": "0.0.0.0",
        "WORKSPACE_PORT": str(_CONTAINER_PORT),
        "WORKSPACE_TIMEOUT_SECONDS": "15",
        "WORKSPACE_CONFIGURATION_SETUP_TIMEOUT_SECONDS": "10",
        "WORKSPACE_PROJECT_QUERY_IDENTITY_TIMEOUT_SECONDS": "5",
        "WORKSPACE_MAX_CONCURRENCY": str(_PROFILE_CONCURRENCY),
    }
    assert not any(
        key in environment
        for key in (
            "WORKSPACE_CONFIGURATION_DATABASE_URL",
            "WORKSPACE_PROJECT_QUERY_DATABASE_URL",
            "WORKSPACE_PROJECT_QUERY_IDENTITY_BRIEFS_DATABASE_URL",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN",
        )
    )
    assert not any(
        key.startswith(("API_", "DATABASE_URL", "SQLALCHEMY_")) for key in environment
    )

    limits = _container_limits(metadata)
    assert limits == {
        "read_only_rootfs": True,
        "cap_drop_all": True,
        "no_new_privileges": True,
        "pids_limit": 64,
        "cpu_nanos": 1_000_000_000,
        "memory_bytes": 256 * 1024 * 1024,
        "tmpfs_32m_noexec_nosuid_nodev": True,
    }
    return limits


async def _wait_ready(origin: str, container_id: str, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        while True:
            try:
                response = await client.get(origin + "/ready")
                if response.status_code == 200:
                    assert response.json() == {"status": "ready"}
                    return
            except httpx.HTTPError:
                pass
            state = await asyncio.to_thread(_inspect_docker, container_id)
            assert state.get("State", {}).get("Running") is True
            assert time.monotonic() < deadline, "Workspace owner did not become ready"
            await asyncio.sleep(0.1)


async def _verify_network(network_id: str, network_name: str, identity: str) -> dict:
    network = _inspect_network(network_id)
    assert network.get("Id") == network_id
    assert network.get("Name") == network_name
    assert network.get("Driver") == "bridge"
    assert network.get("Internal") is False
    labels = network.get("Labels") or {}
    assert labels.get("org.aap.acceptance") == _NETWORK_ACCEPTANCE
    assert labels.get("org.aap.acceptance-id") == identity
    return network


@asynccontextmanager
async def _workspace_owner_container(test_engine, directory: Path, token: str):
    image_ref = _required("AAP_PROJECT_CLASS_RENAME_IMAGE")
    image_identity = _required("AAP_PROJECT_CLASS_RENAME_IMAGE_IDENTITY")
    image = _verify_image(image_ref, image_identity)

    config_dsn = configuration_dsn(test_engine)
    query_dsn = project_query_dsn(test_engine)
    briefs_dsn = project_query_briefs_dsn(test_engine)
    parsed = make_url(config_dsn)
    assert parsed.username == "workspace_configuration_client"
    for value, username in (
        (make_url(query_dsn), "workspace_project_query_client"),
        (make_url(briefs_dsn), "identity_briefs_client"),
    ):
        assert value.username == username
        assert (value.host, value.port, value.database) == (
            parsed.host,
            parsed.port,
            parsed.database,
        )
    assert parsed.database and parsed.database.endswith("_test")
    _verify_postgres(parsed)

    postgres_id = _required("AAP_MODULE_DATA_PG_CONTAINER_ID")
    postgres_name = _required("AAP_MODULE_DATA_PG_CONTAINER_NAME")
    postgres_identity = _required("AAP_MODULE_DATA_PG_CONTAINER_IDENTITY")
    assert re.fullmatch(r"[0-9a-f]{64}", postgres_id)
    assert re.fullmatch(r"[0-9a-f]{24}", postgres_identity)
    postgres_metadata = _inspect_docker(postgres_id)
    assert postgres_metadata.get("Id") == postgres_id
    assert postgres_metadata.get("Name") == f"/{postgres_name}"
    postgres_labels = postgres_metadata.get("Config", {}).get("Labels") or {}
    assert postgres_labels.get("org.aap.acceptance") == _NETWORK_ACCEPTANCE
    assert postgres_labels.get("org.aap.acceptance-id") == postgres_identity
    original_postgres_networks = set(
        (postgres_metadata.get("NetworkSettings", {}).get("Networks") or {}).keys()
    )
    assert original_postgres_networks

    network_identity = secrets.token_hex(12)
    network_name = f"aap-workspace-class-rename-{network_identity}"
    postgres_alias = f"aap-pg-{network_identity}"
    container_identity = secrets.token_hex(12)
    container_name = f"aap-workspace-class-rename-{container_identity}"
    secret_files = {
        "/run/secrets/configuration-database": directory / "configuration-database",
        "/run/secrets/project-query-database": directory / "project-query-database",
        "/run/secrets/identity-briefs-database": directory / "identity-briefs-database",
        "/run/secrets/control-token": directory / "control-token",
    }
    directory_created = False
    network_id = None
    postgres_attached = False
    container_id = None
    origin = None
    limits = None
    try:
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        directory_created = True
        container_urls = {
            secret_files["/run/secrets/configuration-database"]: parsed.set(
                host=postgres_alias, port=5432, drivername="postgresql"
            ).render_as_string(hide_password=False),
            secret_files["/run/secrets/project-query-database"]: make_url(query_dsn)
            .set(host=postgres_alias, port=5432, drivername="postgresql")
            .render_as_string(hide_password=False),
            secret_files["/run/secrets/identity-briefs-database"]: make_url(briefs_dsn)
            .set(host=postgres_alias, port=5432, drivername="postgresql")
            .render_as_string(hide_password=False),
            secret_files["/run/secrets/control-token"]: token,
        }
        for path, value in container_urls.items():
            path.write_text(value, encoding="utf-8")
            path.chmod(0o444)
            assert stat.S_IMODE(path.stat().st_mode) == 0o444
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

        network_id = _docker(
            "network",
            "create",
            "--driver",
            "bridge",
            "--label",
            f"org.aap.acceptance={_NETWORK_ACCEPTANCE}",
            "--label",
            f"org.aap.acceptance-id={network_identity}",
            network_name,
        ).strip()
        assert re.fullmatch(r"[0-9a-f]{64}", network_id)
        network = await _verify_network(network_id, network_name, network_identity)
        assert not (network.get("Containers") or {})

        _docker(
            "network",
            "connect",
            "--alias",
            postgres_alias,
            network_id,
            postgres_id,
        )
        postgres_attached = True
        network = await _verify_network(network_id, network_name, network_identity)
        attached_postgres = (network.get("Containers") or {}).get(postgres_id)
        assert attached_postgres is not None
        assert attached_postgres.get("Name") == postgres_name
        assert (
            postgres_alias
            in _inspect_docker(postgres_id)["NetworkSettings"]["Networks"][
                network_name
            ]["Aliases"]
        )

        mounts = []
        for destination, path in secret_files.items():
            mounts.extend(
                [
                    "--mount",
                    f"type=bind,src={path},dst={destination},readonly",
                ]
            )
        container_id = _docker(
            "run",
            "--detach",
            "--pull=never",
            "--name",
            container_name,
            "--label",
            f"org.aap.acceptance={_CONTAINER_ACCEPTANCE}",
            "--label",
            f"org.aap.acceptance-id={container_identity}",
            "--network",
            network_name,
            "--user=10004:10004",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=1",
            "--no-healthcheck",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--stop-timeout=45",
            "--publish",
            f"127.0.0.1::{_CONTAINER_PORT}",
            *mounts,
            "--env",
            "WORKSPACE_CONFIGURATION_DATABASE_URL_FILE=/run/secrets/configuration-database",
            "--env",
            "WORKSPACE_PROJECT_QUERY_DATABASE_URL_FILE=/run/secrets/project-query-database",
            "--env",
            "WORKSPACE_PROJECT_QUERY_IDENTITY_BRIEFS_DATABASE_URL_FILE=/run/secrets/identity-briefs-database",
            "--env",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE=/run/secrets/control-token",
            "--env",
            "WORKSPACE_HOST=0.0.0.0",
            "--env",
            f"WORKSPACE_PORT={_CONTAINER_PORT}",
            "--env",
            "WORKSPACE_TIMEOUT_SECONDS=15",
            "--env",
            "WORKSPACE_CONFIGURATION_SETUP_TIMEOUT_SECONDS=10",
            "--env",
            "WORKSPACE_PROJECT_QUERY_IDENTITY_TIMEOUT_SECONDS=5",
            "--env",
            f"WORKSPACE_MAX_CONCURRENCY={_PROFILE_CONCURRENCY}",
            image["Id"],
            timeout=120,
        ).strip()
        metadata = await asyncio.to_thread(_inspect_docker, container_id)
        limits = _verify_container(
            metadata,
            container_id=container_id,
            container_identity=container_identity,
            image_id=image["Id"],
            network_name=network_name,
            secret_files=secret_files,
        )
        origin, host_port = _published_origin(metadata)
        await _wait_ready(origin, container_id)
        network = await _verify_network(network_id, network_name, network_identity)
        network_members = network.get("Containers") or {}
        assert set(network_members) == {postgres_id, container_id}
        assert network_members[postgres_id].get("Name") == postgres_name
        assert network_members[container_id].get("Name") == container_name
        assert (
            postgres_alias
            in _inspect_docker(postgres_id)["NetworkSettings"]["Networks"][
                network_name
            ]["Aliases"]
        )
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            unauthorized = await client.post(origin + _CONFIGURATION_PATH, json={})
            assert unauthorized.status_code == 401

        yield {
            "origin": origin,
            "host_port": host_port,
            "container_id": container_id,
            "container_name": container_name,
            "container_identity": container_identity,
            "network_id": network_id,
            "network_name": network_name,
            "network_identity": network_identity,
            "postgres_id": postgres_id,
            "postgres_name": postgres_name,
            "postgres_alias": postgres_alias,
            "original_postgres_networks": original_postgres_networks,
            "image_id": image["Id"],
            "image_identity": image_identity,
            "image_architecture": image.get("Architecture"),
            "source_revision": _SOURCE_REVISION,
            "limits": limits,
            "secret_mount_destinations": sorted(secret_files),
        }
        state = await asyncio.to_thread(_inspect_docker, container_id)
        assert state.get("State", {}).get("OOMKilled") is False
    finally:
        active_error = sys.exc_info()[1]
        cleanup_errors = []
        container_cleanup_ok = container_id is None
        if container_id is not None:
            try:
                _stop_remove_container(container_id, container_identity)
                container_cleanup_ok = True
            except Exception as exc:
                cleanup_errors.append(exc)
        if network_id is not None and container_cleanup_ok:
            try:
                network = await _verify_network(
                    network_id, network_name, network_identity
                )
                attachments = network.get("Containers") or {}
                if set(attachments) - {postgres_id}:
                    raise AssertionError(
                        "owned Workspace container remains attached to its network"
                    )
                if postgres_id in attachments:
                    entry = attachments[postgres_id]
                    _verify_postgres(test_engine.url)
                    aliases = _inspect_docker(postgres_id)["NetworkSettings"][
                        "Networks"
                    ][network_name]["Aliases"]
                    if not (
                        entry.get("Name") == postgres_name and postgres_alias in aliases
                    ):
                        raise AssertionError(
                            "PostgreSQL network attachment identity changed"
                        )
                    _docker("network", "disconnect", network_id, postgres_id)
                    postgres_attached = False
                elif postgres_attached:
                    raise AssertionError(
                        "owned PostgreSQL network attachment disappeared"
                    )
                postgres_after = _inspect_docker(postgres_id)
                assert postgres_after.get("Id") == postgres_id
                labels = postgres_after.get("Config", {}).get("Labels") or {}
                assert labels.get("org.aap.acceptance") == _NETWORK_ACCEPTANCE
                assert labels.get("org.aap.acceptance-id") == postgres_identity
                assert (
                    set(
                        (
                            postgres_after.get("NetworkSettings", {}).get("Networks")
                            or {}
                        ).keys()
                    )
                    == original_postgres_networks
                )
                empty_network = await _verify_network(
                    network_id, network_name, network_identity
                )
                assert not (empty_network.get("Containers") or {})
                _docker("network", "rm", network_id)
            except Exception as exc:
                cleanup_errors.append(exc)
        elif network_id is not None:
            cleanup_errors.append(
                RuntimeError(
                    "retaining the owned PostgreSQL network because its owner remains"
                )
            )
        if directory_created:
            for path in secret_files.values():
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_errors.append(exc)
                if path.exists():
                    cleanup_errors.append(
                        RuntimeError(f"Workspace secret remains: {path.name}")
                    )
            try:
                directory.rmdir()
            except OSError as exc:
                cleanup_errors.append(exc)
            if directory.exists():
                cleanup_errors.append(
                    RuntimeError("Workspace secret directory remains")
                )
        if active_error is not None:
            for exc in cleanup_errors:
                warnings.warn(
                    f"Workspace class-rename container cleanup failed: {exc!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        elif cleanup_errors:
            raise cleanup_errors[0]


async def _seed_load_projects(test_engine, world: dict) -> list:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    owner_id = world["actors"]["owner"].id
    project_ids = []
    async with maker.begin() as db:
        for index in range(_PROFILE_REQUESTS):
            project = await create_project(
                db,
                owner_id=owner_id,
                name=f"Class rename container profile {index}",
            )
            project.tool_bindings = {"bbox": legacy._binding(["Car"])}
            project_ids.append(project.id)
            world["aliases"][str(project.id)] = f"$project_profile_{index}"
    return project_ids


def _assert_car_projection(response: httpx.Response) -> None:
    assert response.status_code == 200, response.text
    project = response.json()
    classes = project["tool_bindings"]["bbox"]["classes"]
    assert len(classes) == 1
    assert classes[0]["name"] == "Car"
    assert classes[0]["color"] == "#6699cc"


async def test_project_class_rename_owner_container_replay_restart_and_bounded_load(
    test_engine, app_module, monkeypatch, tmp_path
):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    observation_path = _observation_path()
    await assert_owned_test_database(test_engine)
    sample_fixture = samples_fixture()
    lock_fixture = locks_fixture()
    world = None
    lock_world = None
    runtime_engine = None
    observation = None
    owner_cleanup_completed = False
    database_cleanup_completed = False
    workspace_token = secrets.token_urlsafe(32)

    try:
        # Preserve the fixture's original public JWTs before selecting the owner.
        world = await seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path)
        lock_world = await seed_lock_world(test_engine)
        profile_project_ids = await _seed_load_projects(test_engine, world)
        assert len(profile_project_ids) == _PROFILE_REQUESTS
        clear_local_identity(monkeypatch)
        runtime_engine, runtime_maker = await make_request_runtime(
            test_engine, application_name="workspace-class-rename-container-api"
        )
        maker = async_sessionmaker(test_engine, expire_on_commit=False)

        async with class_rename_binding(
            monkeypatch, test_engine, mode="borrowed", tmp_path=tmp_path
        ):
            async with _workspace_owner_container(
                test_engine, tmp_path / "workspace-class-rename-owner", workspace_token
            ) as owner:
                monkeypatch.setattr(
                    settings, "workspace_configuration_url", owner["origin"]
                )
                monkeypatch.setattr(
                    settings, "workspace_membership_control_token", workspace_token
                )
                transport = httpx.ASGITransport(
                    app=app_module, raise_app_exceptions=False
                )
                async with api_db_binding(app_module, runtime_maker):
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://project-class-rename-owner-container",
                        timeout=45,
                    ) as client:
                        with _observe_api_commits(runtime_engine) as commit_state:
                            await _replay_lock_cases(
                                client,
                                test_engine,
                                maker,
                                lock_world,
                                lock_fixture,
                                commit_state,
                            )
                            await _replay_http_samples(
                                client,
                                maker,
                                world,
                                sample_fixture,
                                commit_state,
                            )

                            gate = asyncio.Semaphore(_PROFILE_CONCURRENCY)
                            owner_token = world["tokens"]["owner"]

                            async def no_op_request(project_id):
                                async with gate:
                                    started = time.perf_counter()
                                    response = await client.post(
                                        f"/api/v1/projects/{project_id}/classes/rename",
                                        headers={
                                            "Authorization": f"Bearer {owner_token}"
                                        },
                                        json={
                                            "old_name": "Car",
                                            "new_name": "Car",
                                            "tool_unit_id": "__unknown_unit_ignored_for_noop__",
                                        },
                                    )
                                    elapsed = time.perf_counter() - started
                                    _assert_car_projection(response)
                                    return elapsed

                            commit_state["count"] = 0
                            commit_state["active"] = True
                            try:
                                results = await asyncio.gather(
                                    *(
                                        no_op_request(project_id)
                                        for project_id in profile_project_ids
                                    ),
                                    return_exceptions=True,
                                )
                                # Settle every request while the container
                                # selector and clients still belong to this test.
                                errors = [
                                    result
                                    for result in results
                                    if isinstance(result, BaseException)
                                ]
                                if errors:
                                    raise errors[0]
                                durations = results
                            finally:
                                commit_state["active"] = False
                            assert len(durations) == _PROFILE_REQUESTS
                            assert commit_state["count"] == 0

                            changed_project_id = aliased_uuid(
                                world, "project_all_units"
                            )
                            changed_before_restart = await legacy._snapshot(
                                maker, changed_project_id
                            )
                            assert {
                                item["name"]
                                for binding in changed_before_restart[
                                    "tool_bindings"
                                ].values()
                                for item in binding["classes"]
                            } == {"Vehicle"}
                            assert {
                                row["class_name"]
                                for row in changed_before_restart["annotations"]
                            } == {"Vehicle"}
                            changed_sample = next(
                                sample
                                for sample in sample_fixture["samples"]
                                if sample["label"] == "all_units_including_disabled"
                            )
                            assert (
                                changed_sample["post_response_state"]["tool_bindings"]
                                == changed_before_restart["tool_bindings"]
                            )

                            _docker("restart", owner["container_id"], timeout=60)
                            restarted_metadata = await asyncio.to_thread(
                                _inspect_docker, owner["container_id"]
                            )
                            assert (
                                restarted_metadata.get("State", {}).get("Running")
                                is True
                            )
                            assert restarted_metadata.get("Image") == owner["image_id"]
                            assert (
                                restarted_metadata.get("Config", {})
                                .get("Labels", {})
                                .get("org.aap.acceptance-id")
                                == owner["container_identity"]
                            )
                            restarted_origin, restarted_host_port = _published_origin(
                                restarted_metadata
                            )
                            await _wait_ready(restarted_origin, owner["container_id"])
                            await _verify_network(
                                owner["network_id"],
                                owner["network_name"],
                                owner["network_identity"],
                            )
                            monkeypatch.setattr(
                                settings,
                                "workspace_configuration_url",
                                restarted_origin,
                            )

                            commit_state["count"] = 0
                            response, no_op_commits = await _tracked_request(
                                commit_state,
                                client.post(
                                    f"/api/v1/projects/{changed_project_id}/classes/rename",
                                    headers={"Authorization": f"Bearer {owner_token}"},
                                    json={
                                        "old_name": "Vehicle",
                                        "new_name": "Vehicle",
                                        "tool_unit_id": "__unknown_unit_ignored_for_noop__",
                                    },
                                ),
                            )
                            assert no_op_commits == 0
                            assert response.status_code == 200, response.text
                            persisted_projection = response.json()
                            assert {
                                item["name"]
                                for binding in persisted_projection[
                                    "tool_bindings"
                                ].values()
                                for item in binding["classes"]
                            } == {"Vehicle"}
                            changed_after_restart = await legacy._snapshot(
                                maker, changed_project_id
                            )
                            assert {
                                row["class_name"]
                                for row in changed_after_restart["annotations"]
                            } == {"Vehicle"}
                            assert (
                                changed_after_restart["tool_bindings"]
                                == (changed_before_restart["tool_bindings"])
                            )
                            assert [
                                row["class_name"]
                                for row in changed_after_restart["annotations"]
                            ] == [
                                row["class_name"]
                                for row in changed_before_restart["annotations"]
                            ]
                            container_after_load = await asyncio.to_thread(
                                _inspect_docker, owner["container_id"]
                            )
                            assert (
                                container_after_load.get("State", {}).get("OOMKilled")
                                is False
                            )

                            observation = {
                                "status": "passed",
                                "image": {
                                    "id": owner["image_id"],
                                    "identity": owner["image_identity"],
                                    "architecture": owner["image_architecture"],
                                    "source_revision": owner["source_revision"],
                                    "acceptance_label": _IMAGE_ACCEPTANCE,
                                },
                                "owner": {
                                    "container_id": owner["container_id"],
                                    "container_identity": owner["container_identity"],
                                    "network_id": owner["network_id"],
                                    "network_identity": owner["network_identity"],
                                    "database_roles": [
                                        "workspace_configuration_client",
                                        "workspace_project_query_client",
                                        "identity_briefs_client",
                                    ],
                                    "secret_mount_destinations": owner[
                                        "secret_mount_destinations"
                                    ],
                                    "api_or_orm_mounts": False,
                                    "constraints": owner["limits"],
                                    "private_network_members": [
                                        owner["postgres_name"],
                                        owner["container_name"],
                                    ],
                                    "initial_host_port": owner["host_port"],
                                    "restart_host_port": restarted_host_port,
                                    "ready_after_restart": True,
                                    "oom_killed_after_load": False,
                                },
                                "replay": {
                                    "frozen_http_samples": len(
                                        sample_fixture["samples"]
                                    ),
                                    "http_sample_labels": [
                                        sample["label"]
                                        for sample in sample_fixture["samples"]
                                    ],
                                    "frozen_lock_cases": len(lock_fixture["samples"]),
                                    "lock_case_labels": [
                                        sample["label"]
                                        for sample in lock_fixture["samples"]
                                    ],
                                    "no_op_callback": "real Project Query and Identity Briefs capabilities",
                                    "changed_state_persisted_after_restart": True,
                                    "restart_no_op_commits": no_op_commits,
                                },
                                "bounded_load": {
                                    "requests": _PROFILE_REQUESTS,
                                    "concurrency": _PROFILE_CONCURRENCY,
                                    "duration_seconds": durations,
                                    "all_no_op_projections_succeeded": True,
                                    "api_commits": 0,
                                },
                                "cleanup": {
                                    "test_database_and_rows": "pending fixture teardown",
                                    "owner_container_network_and_secrets": "cleaned by owner context",
                                },
                            }
                owner_cleanup_completed = True
    finally:
        try:
            if runtime_engine is not None:
                await runtime_engine.dispose()
        finally:
            try:
                if lock_world is not None:
                    await cleanup_lock_world(test_engine, lock_world)
            finally:
                if world is not None:
                    await cleanup_world(test_engine, world)
                    database_cleanup_completed = True

    if observation is not None:
        observation["cleanup"]["database_and_rows"] = (
            "cleaned" if database_cleanup_completed else "incomplete"
        )
        observation["cleanup"]["owner_container_network_and_secrets"] = (
            "cleaned" if owner_cleanup_completed else "incomplete"
        )
        if observation_path is not None:
            with observation_path.open("x", encoding="utf-8") as stream:
                json.dump(observation, stream, indent=2, sort_keys=True)
                stream.write("\n")
            observation_path.chmod(0o600)
