"""Exercise public class usage through the owned Labeling and Workspace HTTP owners."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
import warnings

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from tests.labeling_class_query_helpers import (
    add_frozen_interleave_row,
    aliased_uuid,
    api_db_binding,
    assert_owned_test_database,
    cleanup_world,
    clear_local_identity,
    fixture_data,
    installed_workspace_query_provider,
    labeling_class_query_dsn,
    make_request_runtime,
    request_sample,
    seed_legacy_world,
)
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
    _verify_postgres,
)
from tests.test_labeling_class_query_restore import _assert_labeling_client
from tests.workspace_project_query_helpers import (
    api_project_query_binding,
    project_query_dsn,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_LABELING_CLASS_QUERY_CONTAINER_LIVE") != "1",
        reason="requires the explicitly qualified owned Labeling class-query image",
    ),
]

_OWNER_ACCEPTANCE = "labeling-class-query-owner"
_CONTAINER_ACCEPTANCE = "module-data-boundary"
_NETWORK_ACCEPTANCE = "module-data-boundary"
_OWNER_PORT = 8178
_PHASE_SAMPLE = "class_then_attributes_second_session_commit"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"the owned Labeling verifier did not provide {name}")
    return value


def _inspect_network(target: str) -> dict:
    try:
        values = json.loads(_docker("network", "inspect", target, timeout=15))
    except json.JSONDecodeError:
        pytest.fail("Docker returned an invalid owned network record")
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("owned Labeling network identity was not unique")
    return values[0]


def _verify_image(image_ref: str, expected_identity: str) -> dict:
    image = _inspect_docker(image_ref)
    config = image.get("Config", {})
    labels = config.get("Labels") or {}
    assert labels.get("org.aap.acceptance") == _OWNER_ACCEPTANCE
    assert labels.get("org.aap.acceptance-id") == expected_identity
    assert config.get("User") == "10006:10006"
    assert config.get("Cmd") == ["python", "-m", "aap_labeling"]
    assert config.get("Entrypoint") in (None, [])
    image_id = image.get("Id")
    assert isinstance(image_id, str) and image_id.startswith("sha256:")
    return image


def _published_origin(metadata: dict) -> str:
    published = (
        metadata.get("NetworkSettings", {}).get("Ports", {}).get(f"{_OWNER_PORT}/tcp")
        or []
    )
    assert len(published) == 1
    assert published[0].get("HostIp") == "127.0.0.1"
    host_port = int(published[0].get("HostPort", 0))
    assert 1 <= host_port <= 65535
    return f"http://127.0.0.1:{host_port}"


def _verify_container(
    metadata: dict,
    *,
    container_id: str,
    container_identity: str,
    image_id: str,
    network_name: str,
    dsn_file: Path,
    token_file: Path,
) -> dict[str, int | bool]:
    config = metadata.get("Config", {})
    labels = config.get("Labels") or {}
    host = metadata.get("HostConfig", {})
    assert metadata.get("Id") == container_id
    assert metadata.get("Image") == image_id
    assert labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
    assert labels.get("org.aap.acceptance-id") == container_identity
    assert config.get("User") == "10006:10006"
    assert config.get("Cmd") == ["python", "-m", "aap_labeling"]
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
    secret_mounts = [
        mount
        for mount in mounts
        if str(mount.get("Destination", "")).startswith("/run/secrets/")
    ]
    binds = [mount for mount in secret_mounts if mount.get("Type") == "bind"]
    expected_mounts = {
        "/run/secrets/class-query-database": dsn_file.resolve(),
        "/run/secrets/class-query-control": token_file.resolve(),
    }
    assert len(binds) == 2
    assert {
        mount["Destination"]: Path(mount["Source"]).resolve() for mount in binds
    } == expected_mounts
    assert all(mount.get("RW") is False for mount in binds)
    assert len(secret_mounts) == 2
    assert len(binds) == len(secret_mounts)

    networks = metadata.get("NetworkSettings", {}).get("Networks") or {}
    assert set(networks) == {network_name}
    assert networks[network_name].get("NetworkID")
    environment = dict(
        entry.split("=", 1) for entry in (config.get("Env") or []) if "=" in entry
    )
    owner_environment = {
        key: value
        for key, value in environment.items()
        if key.startswith("LABELING_CLASS_QUERY_")
    }
    assert owner_environment == {
        "LABELING_CLASS_QUERY_DATABASE_URL_FILE": "/run/secrets/class-query-database",
        "LABELING_CLASS_QUERY_CONTROL_TOKEN_FILE": "/run/secrets/class-query-control",
        "LABELING_CLASS_QUERY_HOST": "0.0.0.0",
        "LABELING_CLASS_QUERY_PORT": str(_OWNER_PORT),
    }
    assert "LABELING_CLASS_QUERY_DATABASE_URL" not in environment
    assert "LABELING_CLASS_QUERY_CONTROL_TOKEN" not in environment
    assert not any(
        key.startswith(("AAP_", "WORKSPACE_", "IDENTITY_", "PROFILE_"))
        for key in environment
    )
    return {
        "read_only_rootfs": True,
        "cap_drop_all": True,
        "no_new_privileges": True,
        "pids_limit": 64,
        "cpu_nanos": 1_000_000_000,
        "memory_bytes": 256 * 1024 * 1024,
        "readonly_secret_mount_count": len(binds),
        "container_port": _OWNER_PORT,
    }


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
            assert _inspect_docker(container_id).get("State", {}).get("Running") is True
            assert time.monotonic() < deadline, (
                "Labeling class-query image did not become ready"
            )
            await asyncio.sleep(0.1)


async def _verify_labeling_network(
    network_id: str, network_name: str, network_identity: str
) -> dict:
    network = _inspect_network(network_id)
    assert network.get("Id") == network_id
    assert network.get("Name") == network_name
    assert network.get("Driver") == "bridge"
    assert network.get("Internal") is False
    labels = network.get("Labels") or {}
    assert labels.get("org.aap.acceptance") == _NETWORK_ACCEPTANCE
    assert labels.get("org.aap.acceptance-id") == network_identity
    return network


@asynccontextmanager
async def _labeling_owner_container(test_engine, directory: Path, token: str):
    image_ref = _required("AAP_LABELING_CLASS_QUERY_IMAGE")
    image_identity = _required("AAP_LABELING_CLASS_QUERY_IDENTITY")
    assert len(image_identity) == 24 and all(
        character in "0123456789abcdef" for character in image_identity
    )
    image = _verify_image(image_ref, image_identity)

    original_dsn = labeling_class_query_dsn(test_engine)
    parsed = make_url(original_dsn)
    assert parsed.username == "labeling_class_query_client"
    postgres_id = _required("AAP_MODULE_DATA_PG_CONTAINER_ID")
    postgres_name = _required("AAP_MODULE_DATA_PG_CONTAINER_NAME")
    postgres_identity = _required("AAP_MODULE_DATA_PG_CONTAINER_IDENTITY")
    assert len(postgres_id) == 64 and all(
        character in "0123456789abcdef" for character in postgres_id
    )
    assert len(postgres_identity) == 24 and all(
        character in "0123456789abcdef" for character in postgres_identity
    )
    _verify_postgres(test_engine.url)
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
    network_name = f"aap-labeling-class-query-{network_identity}"
    postgres_alias = f"aap-pg-{network_identity}"
    container_identity = secrets.token_hex(12)
    container_name = f"aap-labeling-class-query-{container_identity}"
    directory_created = False
    network_id = None
    postgres_attached = False
    container_id = None
    dsn_file = directory / "class-query-database"
    token_file = directory / "class-query-control"
    limits = None
    origin = None
    try:
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        directory_created = True
        container_dsn = parsed.set(
            host=postgres_alias, port=5432, drivername="postgresql"
        ).render_as_string(hide_password=False)
        for path, value in ((dsn_file, container_dsn), (token_file, token)):
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
        assert len(network_id) == 64 and all(
            character in "0123456789abcdef" for character in network_id
        )
        network = await _verify_labeling_network(
            network_id, network_name, network_identity
        )
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
        network = await _verify_labeling_network(
            network_id, network_name, network_identity
        )
        attached_postgres = (network.get("Containers") or {}).get(postgres_id)
        assert attached_postgres is not None
        assert attached_postgres.get("Name") == postgres_name
        assert (
            postgres_alias
            in _inspect_docker(postgres_id)["NetworkSettings"]["Networks"][
                network_name
            ]["Aliases"]
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
            "--user=10006:10006",
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
            f"127.0.0.1::{_OWNER_PORT}",
            "--mount",
            f"type=bind,src={dsn_file},dst=/run/secrets/class-query-database,readonly",
            "--mount",
            f"type=bind,src={token_file},dst=/run/secrets/class-query-control,readonly",
            "--env",
            "LABELING_CLASS_QUERY_DATABASE_URL_FILE=/run/secrets/class-query-database",
            "--env",
            "LABELING_CLASS_QUERY_CONTROL_TOKEN_FILE=/run/secrets/class-query-control",
            "--env",
            "LABELING_CLASS_QUERY_HOST=0.0.0.0",
            "--env",
            f"LABELING_CLASS_QUERY_PORT={_OWNER_PORT}",
            image["Id"],
            timeout=120,
        ).strip()
        metadata = _inspect_docker(container_id)
        limits = _verify_container(
            metadata,
            container_id=container_id,
            container_identity=container_identity,
            image_id=image["Id"],
            network_name=network_name,
            dsn_file=dsn_file,
            token_file=token_file,
        )
        origin = _published_origin(metadata)
        await _wait_ready(origin, container_id)
        network = await _verify_labeling_network(
            network_id, network_name, network_identity
        )
        network_members = network.get("Containers") or {}
        assert set(network_members) == {postgres_id, container_id}
        assert network_members[postgres_id].get("Name") == postgres_name
        assert (
            postgres_alias
            in _inspect_docker(postgres_id)["NetworkSettings"]["Networks"][
                network_name
            ]["Aliases"]
        )
        assert network_members[container_id].get("Name") == container_name
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            unauthorized = await client.post(
                origin + "/v1/labeling/class-queries/execute", json={}
            )
            assert unauthorized.status_code == 401

        yield {
            "origin": origin,
            "container_id": container_id,
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
            "limits": limits,
        }
        state = _inspect_docker(container_id).get("State", {})
        assert state.get("OOMKilled") is False
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
                network = await _verify_labeling_network(
                    network_id, network_name, network_identity
                )
                attachments = network.get("Containers") or {}
                if set(attachments) - {postgres_id}:
                    raise AssertionError(
                        "owned Labeling container remains attached to its network"
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
                empty_network = await _verify_labeling_network(
                    network_id, network_name, network_identity
                )
                assert not (empty_network.get("Containers") or {})
                _docker("network", "rm", network_id)
            except Exception as exc:
                cleanup_errors.append(exc)
        elif network_id is not None:
            cleanup_errors.append(
                RuntimeError("retaining the owned PG network because its owner remains")
            )
        if directory_created:
            for path in (dsn_file, token_file):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_errors.append(exc)
                if path.exists():
                    cleanup_errors.append(
                        RuntimeError(f"Labeling secret remains: {path.name}")
                    )
            try:
                directory.rmdir()
            except OSError as exc:
                cleanup_errors.append(exc)
            if directory.exists():
                cleanup_errors.append(RuntimeError("Labeling secret directory remains"))
        if active_error is not None:
            for exc in cleanup_errors:
                warnings.warn(
                    f"Labeling owner cleanup failed: {exc!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        elif cleanup_errors:
            raise cleanup_errors[0]


def _expected_public_sample(sample: dict) -> dict:
    request = sample["request"]
    response = sample["response"]
    return {
        "label": sample["label"],
        "request": {
            "method": request["method"],
            "path": request["path"],
            "actor_alias": request["actor"]["alias"],
        },
        "response": {
            "status": response["status"],
            "headers": response["headers"],
            "body": response["body"],
        },
    }


def _observation_path() -> Path:
    target = Path(_required("AAP_LABELING_CLASS_QUERY_OBSERVATION"))
    expected_parent = Path(__file__).resolve().parents[3] / ".worktree"
    assert target.is_absolute()
    assert target.resolve().parent == expected_parent
    assert target.suffix == ".json"
    assert not target.exists()
    return target


async def test_labeling_owner_container_public_api_restart_and_bounded_load(
    test_engine, app_module, monkeypatch, tmp_path
):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    observation_path = _observation_path()
    await assert_owned_test_database(test_engine)
    reference = fixture_data()
    target_sample = next(
        sample
        for sample in reference["samples"]
        if sample["label"] == "target_project_owner_aggregation"
    )
    decode_failure_sample = next(
        sample
        for sample in reference["samples"]
        if sample["label"] == "large_json_number_attribute_read"
    )
    world = None
    observation = None
    workspace_token = secrets.token_urlsafe(32)
    labeling_token = secrets.token_urlsafe(32)
    target_project_id = None

    try:
        world = await seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path)
        target_project_id = aliased_uuid(world, "project_target")
        labeling_dsn = labeling_class_query_dsn(test_engine)
        await _assert_labeling_client(labeling_dsn, target_project_id)
        clear_local_identity(monkeypatch)
        runtime_engine, runtime_maker = await make_request_runtime(
            test_engine, application_name="labeling-container-api"
        )
        try:
            async with installed_workspace_query_provider(
                project_query_dsn(test_engine), workspace_token
            ) as workspace_origin:
                async with _labeling_owner_container(
                    test_engine, tmp_path / "labeling-owner", labeling_token
                ) as owner:
                    async with api_db_binding(app_module, runtime_maker):
                        async with api_project_query_binding(
                            url=workspace_origin, token=workspace_token
                        ):
                            for name, value in (
                                ("labeling_class_query_url", owner["origin"]),
                                ("labeling_class_query_database_url", ""),
                                ("labeling_class_query_database_url_file", ""),
                                ("labeling_class_query_control_token", labeling_token),
                                ("labeling_class_query_control_token_file", ""),
                            ):
                                monkeypatch.setattr(settings, name, value)

                            transport = httpx.ASGITransport(
                                app=app_module, raise_app_exceptions=False
                            )
                            async with httpx.AsyncClient(
                                transport=transport,
                                base_url="http://test",
                                timeout=30,
                            ) as client:
                                observed = []
                                interleave_id = None
                                try:
                                    for frozen in reference["samples"]:
                                        if frozen["label"] == _PHASE_SAMPLE:
                                            interleave_id = (
                                                await add_frozen_interleave_row(
                                                    test_engine, world
                                                )
                                            )
                                        replayed = await request_sample(
                                            client, world, frozen
                                        )
                                        assert replayed == _expected_public_sample(
                                            frozen
                                        )
                                        observed.append(replayed)
                                finally:
                                    if interleave_id is not None:
                                        async with test_engine.begin() as connection:
                                            await connection.execute(
                                                text(
                                                    "DELETE FROM public.annotations "
                                                    "WHERE id=CAST(:annotation_id AS uuid)"
                                                ),
                                                {"annotation_id": str(interleave_id)},
                                            )

                                assert len(observed) == 16
                                raw_500 = next(
                                    sample
                                    for sample in observed
                                    if sample["label"]
                                    == "large_json_number_attribute_read"
                                )
                                assert raw_500["response"]["status"] == 500
                                assert (
                                    raw_500["response"]["body"]
                                    == "Internal Server Error"
                                )
                                assert raw_500["response"]["headers"][
                                    "content-type"
                                ].startswith("text/plain")
                                assert raw_500 == _expected_public_sample(
                                    decode_failure_sample
                                )

                                recent_annotation = await add_frozen_interleave_row(
                                    test_engine, world
                                )
                                try:
                                    recent = await request_sample(
                                        client, world, target_sample
                                    )
                                    assert recent["response"]["status"] == 200
                                    expected_attributes = dict(
                                        target_sample["response"]["body"]["attributes"]
                                    )
                                    assert "between_reads" not in expected_attributes
                                    expected_attributes["between_reads"] = 1
                                    expected_body = {
                                        "classes": target_sample["response"]["body"][
                                            "classes"
                                        ],
                                        "attributes": expected_attributes,
                                    }
                                    assert recent["response"]["body"] == expected_body

                                    path = f"/api/v1/projects/{target_project_id}/class-usage"
                                    headers = {
                                        "Authorization": f"Bearer {world['tokens']['owner']}"
                                    }
                                    gate = asyncio.Semaphore(4)

                                    async def read_once():
                                        async with gate:
                                            started = time.perf_counter()
                                            response = await client.get(
                                                path, headers=headers
                                            )
                                            elapsed_ms = (
                                                time.perf_counter() - started
                                            ) * 1000
                                            assert response.status_code == 200, (
                                                response.text
                                            )
                                            assert response.json() == expected_body
                                            return elapsed_ms

                                    durations = await asyncio.gather(
                                        *(read_once() for _ in range(16))
                                    )
                                    assert len(durations) == 16
                                    assert not _inspect_docker(owner["container_id"])[
                                        "State"
                                    ]["OOMKilled"]

                                    _docker(
                                        "restart", owner["container_id"], timeout=60
                                    )
                                    restarted_metadata = _inspect_docker(
                                        owner["container_id"]
                                    )
                                    assert (
                                        restarted_metadata["State"]["Running"] is True
                                    )
                                    restarted_origin = _published_origin(
                                        restarted_metadata
                                    )
                                    await _wait_ready(
                                        restarted_origin, owner["container_id"]
                                    )
                                    monkeypatch.setattr(
                                        settings,
                                        "labeling_class_query_url",
                                        restarted_origin,
                                    )
                                    after_restart = await request_sample(
                                        client, world, target_sample
                                    )
                                    assert after_restart["response"]["status"] == 200
                                    assert (
                                        after_restart["response"]["body"]
                                        == expected_body
                                    )
                                    assert not _inspect_docker(owner["container_id"])[
                                        "State"
                                    ]["OOMKilled"]

                                    observation = {
                                        "status": "passed",
                                        "image_id": owner["image_id"],
                                        "image_identity": owner["image_identity"],
                                        "container_id": owner["container_id"],
                                        "container_identity": owner[
                                            "container_identity"
                                        ],
                                        "network_id": owner["network_id"],
                                        "network_identity": owner["network_identity"],
                                        "image_limits": owner["limits"],
                                        "owner_budget": {"cpu": 1, "memory_mib": 256},
                                        "secret_mounts": 2,
                                        "database_role": "labeling_class_query_client",
                                        "workspace_provider": "installed-http-artifact",
                                        "workspace_identity_profiles_configured": False,
                                        "api_and_database_outside_owner_budget": True,
                                        "public_api_samples": len(observed),
                                        "raw_500_preserved": True,
                                        "recent_annotation_attribute_reflected": True,
                                        "bounded_requests": 16,
                                        "bounded_concurrency": 4,
                                        "milliseconds": durations,
                                        "container_restarted": True,
                                        "initial_origin": owner["origin"],
                                        "restarted_origin": restarted_origin,
                                    }
                                finally:
                                    async with test_engine.begin() as connection:
                                        await connection.execute(
                                            text(
                                                "DELETE FROM public.annotations "
                                                "WHERE id=CAST(:annotation_id AS uuid)"
                                            ),
                                            {"annotation_id": str(recent_annotation)},
                                        )
        finally:
            await runtime_engine.dispose()
    finally:
        if world is not None:
            await cleanup_world(test_engine, world)

    if observation is not None:
        with observation_path.open("x", encoding="utf-8") as stream:
            json.dump(observation, stream, indent=2)
            stream.write("\n")
