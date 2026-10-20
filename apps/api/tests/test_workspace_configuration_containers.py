"""Prebuilt Workspace configuration image and local-load acceptance."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
import re
import secrets
import statistics
import sys
import time
import uuid
import warnings

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.configuration_contract import configuration_request
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from tests.factory import create_project, create_user
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
)
from tests.workspace_configuration_helpers import (
    api_configuration_binding,
    configuration_dsn,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_CONFIGURATION_CONTAINER_LIVE") != "1",
        reason="requires the explicit owned Workspace configuration image verifier",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_CONFIGURATION_PATH = "/v1/workspace/configuration/execute"
_IMAGE_ACCEPTANCE = "workspace-configuration-owner"
_CONTAINER_ACCEPTANCE = "module-data-boundary"
_CONTAINER_PORT = 8174
_PROFILE_REQUESTS = 16
_PROFILE_CONCURRENCY = 4


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"the owned configuration verifier did not provide {name}")
    return value


def _auth(actor: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(actor.id), actor.role)}"}


def _suppress_request_audit(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)


def _fixed_api_operation_ids(monkeypatch):
    from app.services import workspace_configuration

    operation_ids = {
        operation: uuid.uuid4()
        for operation in ("update_template", "create_project", "update_project")
    }
    original = workspace_configuration.configuration_request

    def build(**kwargs):
        operation = kwargs["operation"]
        return original(**kwargs, operation_id=operation_ids[operation])

    monkeypatch.setattr(workspace_configuration, "configuration_request", build)
    return operation_ids


def _verify_image(image_ref: str, expected_identity: str) -> dict:
    image = _inspect_docker(image_ref)
    config = image.get("Config", {})
    labels = config.get("Labels") or {}
    assert labels.get("org.aap.acceptance") == _IMAGE_ACCEPTANCE
    assert labels.get("org.aap.acceptance-id") == expected_identity
    assert config.get("User") == "10004:10004"
    assert config.get("Cmd") == ["python", "-m", "aap_workspace"]
    assert config.get("Entrypoint") in (None, [])
    image_id = image.get("Id")
    assert isinstance(image_id, str) and image_id.startswith("sha256:")
    return image


def _container_limits(metadata: dict) -> dict[str, int | bool]:
    host = metadata["HostConfig"]
    tmpfs_options = (host.get("Tmpfs") or {}).get("/tmp", "")
    mounts = metadata.get("Mounts") or []
    secret_mounts = [
        mount
        for mount in mounts
        if mount.get("Type") == "bind"
        and str(mount.get("Destination", "")).startswith("/run/secrets/")
    ]
    return {
        "read_only_rootfs": host.get("ReadonlyRootfs") is True,
        "cap_drop_all": "ALL" in (host.get("CapDrop") or []),
        "no_new_privileges": any(
            value.startswith("no-new-privileges")
            for value in (host.get("SecurityOpt") or [])
        ),
        "pids_limit": host.get("PidsLimit"),
        "cpu_nanos": host.get("NanoCpus"),
        "memory_bytes": host.get("Memory"),
        "tmpfs_32m": "size=32m" in tmpfs_options or "size=33554432" in tmpfs_options,
        "readonly_secret_mount_count": sum(
            mount.get("RW") is False for mount in secret_mounts
        ),
        "container_port": _CONTAINER_PORT,
    }


def _verify_container(
    metadata: dict,
    *,
    container_id: str,
    container_identity: str,
    image_id: str,
    host_port: int,
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
    assert config.get("User") == "10004:10004"
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

    binds = [
        mount for mount in (metadata.get("Mounts") or []) if mount.get("Type") == "bind"
    ]
    assert len(binds) == 2
    expected_mounts = {
        "/run/secrets/configuration-database": dsn_file.resolve(),
        "/run/secrets/membership-control": token_file.resolve(),
    }
    assert {
        mount["Destination"]: Path(mount["Source"]).resolve() for mount in binds
    } == expected_mounts
    assert all(mount.get("RW") is False for mount in binds)
    assert {
        mount.get("Destination") for mount in (metadata.get("Mounts") or [])
    } <= set(expected_mounts) | {"/tmp"}

    published = (
        metadata.get("NetworkSettings", {})
        .get("Ports", {})
        .get(f"{_CONTAINER_PORT}/tcp")
        or []
    )
    assert len(published) == 1
    assert published[0].get("HostIp") == "127.0.0.1"
    assert int(published[0].get("HostPort", 0)) == host_port

    environment = dict(
        entry.split("=", 1) for entry in (config.get("Env") or []) if "=" in entry
    )
    assert environment.get("WORKSPACE_CONFIGURATION_DATABASE_URL_FILE") == (
        "/run/secrets/configuration-database"
    )
    assert environment.get("WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE") == (
        "/run/secrets/membership-control"
    )
    assert environment.get("WORKSPACE_HOST") == "0.0.0.0"
    assert environment.get("WORKSPACE_PORT") == str(_CONTAINER_PORT)
    assert (
        environment.get("WORKSPACE_CONFIGURATION_VIDEO_SEGMENT_SIZE_FRAMES") == "18000"
    )
    assert (
        environment.get("WORKSPACE_CONFIGURATION_VIDEO_TRACKER_WINDOW_SIZE_FRAMES")
        == "300"
    )
    assert (
        environment.get("WORKSPACE_CONFIGURATION_VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES")
        == "16"
    )
    assert environment.get("WORKSPACE_CONFIGURATION_SETUP_TIMEOUT_SECONDS") == "10"
    assert environment.get("WORKSPACE_TIMEOUT_SECONDS") == "15"
    for forbidden in (
        "WORKSPACE_CONFIGURATION_DATABASE_URL",
        "WORKSPACE_CONFIGURATION_URL",
        "WORKSPACE_MEMBERSHIP_DATABASE_URL",
        "WORKSPACE_MEMBERSHIP_DATABASE_URL_FILE",
        "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN",
        "WORKSPACE_MEMBERSHIP_URL",
        "WORKSPACE_OWNER_TRANSFER_DATABASE_URL",
        "WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE",
        "WORKSPACE_OWNER_TRANSFER_URL",
    ):
        assert forbidden not in environment
    return _container_limits(metadata)


@asynccontextmanager
async def _configuration_image_container(test_engine, directory: Path, token: str):
    image_ref = _required("AAP_WORKSPACE_CONFIGURATION_IMAGE")
    image_identity = _required("AAP_WORKSPACE_CONFIGURATION_IDENTITY")
    assert re.fullmatch(r"[0-9a-f]{24}", image_identity)
    image = _verify_image(image_ref, image_identity)

    dsn = configuration_dsn(test_engine)
    parsed = make_url(dsn)
    assert parsed.username == "workspace_configuration_client"
    container_dsn = parsed.set(host="host.docker.internal").render_as_string(
        hide_password=False
    )
    dsn_file = directory / "configuration-database"
    token_file = directory / "membership-control"
    container_identity = secrets.token_hex(12)
    container_name = f"aap-workspace-configuration-{container_identity}"
    directory_created = False
    container_id = None
    try:
        directory.mkdir(mode=0o700)
        directory_created = True
        directory.chmod(0o700)
        for path, value in ((dsn_file, container_dsn), (token_file, token)):
            path.write_text(value, encoding="utf-8")
            path.chmod(0o444)
            assert path.stat().st_mode & 0o777 == 0o444
        assert directory.stat().st_mode & 0o777 == 0o700

        container_id = _docker(
            "run",
            "--detach",
            "--name",
            container_name,
            "--pull=never",
            "--label",
            f"org.aap.acceptance={_CONTAINER_ACCEPTANCE}",
            "--label",
            f"org.aap.acceptance-id={container_identity}",
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
            "--publish",
            f"127.0.0.1::{_CONTAINER_PORT}",
            "--mount",
            f"type=bind,src={dsn_file},dst=/run/secrets/configuration-database,readonly",
            "--mount",
            f"type=bind,src={token_file},dst=/run/secrets/membership-control,readonly",
            "--env",
            "WORKSPACE_CONFIGURATION_DATABASE_URL_FILE=/run/secrets/configuration-database",
            "--env",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE=/run/secrets/membership-control",
            "--env",
            "WORKSPACE_HOST=0.0.0.0",
            "--env",
            f"WORKSPACE_PORT={_CONTAINER_PORT}",
            "--env",
            "WORKSPACE_CONFIGURATION_VIDEO_SEGMENT_SIZE_FRAMES=18000",
            "--env",
            "WORKSPACE_CONFIGURATION_VIDEO_TRACKER_WINDOW_SIZE_FRAMES=300",
            "--env",
            "WORKSPACE_CONFIGURATION_VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES=16",
            "--env",
            "WORKSPACE_CONFIGURATION_SETUP_TIMEOUT_SECONDS=10",
            "--env",
            "WORKSPACE_TIMEOUT_SECONDS=15",
            image["Id"],
            timeout=120,
        ).strip()
        metadata = await asyncio.to_thread(_inspect_docker, container_id)
        published_ports = (
            metadata.get("NetworkSettings", {})
            .get("Ports", {})
            .get(f"{_CONTAINER_PORT}/tcp")
            or []
        )
        assert len(published_ports) == 1
        host_port = int(published_ports[0]["HostPort"])
        limits = _verify_container(
            metadata,
            container_id=container_id,
            container_identity=container_identity,
            image_id=image["Id"],
            host_port=host_port,
            dsn_file=dsn_file,
            token_file=token_file,
        )
        origin = f"http://127.0.0.1:{host_port}"
        deadline = time.monotonic() + 40
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while True:
                try:
                    ready = await client.get(origin + "/ready")
                    if ready.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                state = (await asyncio.to_thread(_inspect_docker, container_id)).get(
                    "State", {}
                )
                assert state.get("Running") is True, (
                    "Workspace image exited before readiness"
                )
                assert time.monotonic() < deadline, (
                    "Workspace image did not become ready"
                )
                await asyncio.sleep(0.1)
            unauthorized = await client.post(origin + _CONFIGURATION_PATH, json={})
            assert unauthorized.status_code == 401
        yield {
            "origin": origin,
            "container_id": container_id,
            "container_identity": container_identity,
            "image_id": image["Id"],
            "image_identity": image_identity,
            "image_labels": {
                "org.aap.acceptance": _IMAGE_ACCEPTANCE,
                "org.aap.acceptance-id": image_identity,
            },
            "limits": limits,
        }
    finally:
        active_error = sys.exc_info()[1]
        cleanup_errors = []
        if container_id is not None:
            try:
                _stop_remove_container(container_id, container_identity)
            except Exception as exc:
                cleanup_errors.append(exc)
        if directory_created:
            for path in (dsn_file, token_file):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_errors.append(exc)
                if path.exists():
                    cleanup_errors.append(
                        RuntimeError(f"configuration secret remains: {path.name}")
                    )
            try:
                directory.rmdir()
            except OSError as exc:
                cleanup_errors.append(exc)
            if directory.exists():
                cleanup_errors.append(
                    RuntimeError("configuration secret directory remains")
                )
            if active_error is None:
                assert not dsn_file.exists()
                assert not token_file.exists()
                assert not directory.exists()
        if active_error is not None:
            for exc in cleanup_errors:
                warnings.warn(
                    f"configuration container cleanup failed: {exc!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        elif cleanup_errors:
            raise cleanup_errors[0]


async def _private_call(client, origin: str, token: str, request):
    return await client.post(
        origin + _CONFIGURATION_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json=request.model_dump(mode="json", by_alias=True),
    )


async def _counts(db, operation_id: uuid.UUID) -> tuple[int, int, int]:
    params = {"operation_id": str(operation_id)}
    values = []
    for table in (
        "workspace_configuration_operation_receipts",
        "workspace_configuration_operation_states",
        "workspace_configuration_operation_attempts",
    ):
        values.append(
            int(
                await db.scalar(
                    text(
                        f"SELECT count(*) FROM public.{table} "
                        "WHERE operation_id=CAST(:operation_id AS uuid)"
                    ),
                    params,
                )
            )
        )
    return tuple(values)


async def _seed_actor_and_deleted_template(test_engine, suffix: str):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    profile_project_ids = []
    async with maker.begin() as db:
        actor = await create_user(
            db,
            "project_admin",
            f"workspace-configuration-container-{suffix}@example.test",
            "Workspace configuration image actor",
        )
        deleted_template = ProjectTemplate(
            id=uuid.uuid4(),
            display_id="PT-" + uuid.uuid4().hex[:8],
            name=f"Deleted replay template {suffix}",
            type_label="Image",
            type_key="image-det",
            created_by=actor.id,
            scope="private",
        )
        db.add(deleted_template)
        await db.flush()
        for index in range(_PROFILE_REQUESTS):
            profile_project = await create_project(
                db,
                owner_id=actor.id,
                name=f"Workspace configuration profile target {suffix} {index}",
            )
            profile_project_ids.append(profile_project.id)
    return maker, actor, deleted_template.id, profile_project_ids


async def _cleanup_world(maker, actor_id: uuid.UUID, operation_ids: list[uuid.UUID]):
    async with maker.begin() as db:
        for operation_id in operation_ids:
            params = {"operation_id": str(operation_id)}
            for table in (
                "workspace_configuration_operation_states",
                "workspace_configuration_operation_attempts",
                "workspace_configuration_operation_receipts",
            ):
                await db.execute(
                    text(
                        f"DELETE FROM public.{table} "
                        "WHERE operation_id=CAST(:operation_id AS uuid)"
                    ),
                    params,
                )
        await db.execute(
            delete(ProjectTemplate).where(ProjectTemplate.created_by == actor_id)
        )
        await db.execute(delete(Project).where(Project.owner_id == actor_id))
        await db.execute(delete(User).where(User.id == actor_id))


def _write_local_profile(payload: dict, image_identity: str, run_id: str) -> Path:
    directory = _ROOT / ".worktree"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (
        f"workspace-configuration-container-profile-{image_identity}-{run_id}.json"
    )
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    return path


def _update_local_profile(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)


def _nearest_rank(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


async def test_installed_configuration_image_public_flow_restart_and_local_profile(
    test_engine, httpx_client, tmp_path, monkeypatch
):
    token = secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex[:10]
    (
        maker,
        actor,
        deleted_template_id,
        profile_project_ids,
    ) = await _seed_actor_and_deleted_template(test_engine, suffix)
    assert len(profile_project_ids) == _PROFILE_REQUESTS
    assert len(set(profile_project_ids)) == _PROFILE_REQUESTS
    create_operation_id, delete_operation_id = uuid.uuid4(), uuid.uuid4()
    profile_operation_ids = [uuid.uuid4() for _ in range(_PROFILE_REQUESTS)]
    service_operation_ids = _fixed_api_operation_ids(monkeypatch)
    all_operation_ids = [
        create_operation_id,
        delete_operation_id,
        *profile_operation_ids,
        *service_operation_ids.values(),
    ]
    _suppress_request_audit(monkeypatch)

    profile: dict = {
        "profile": "LOCAL Workspace configuration image only",
        "production_capacity_claim": False,
        "profile_run_id": uuid.uuid4().hex[:12],
        "image_id": None,
        "image_labels": None,
        "resource_limits": None,
        "workload": {
            "operation": "update_project",
            "target": "independent_owned_projects",
            "target_count": _PROFILE_REQUESTS,
            "requests": _PROFILE_REQUESTS,
            "concurrency": _PROFILE_CONCURRENCY,
        },
        "completed": False,
    }
    template_id = project_id = None
    try:
        create_request = configuration_request(
            operation="create_template",
            actor_id=actor.id,
            target_id=None,
            payload={
                "name": f"Image private template {suffix}",
                "type_label": "Image",
                "type_key": "image-det",
                "scope": "private",
            },
            operation_id=create_operation_id,
        )
        delete_request = configuration_request(
            operation="delete_template",
            actor_id=actor.id,
            target_id=deleted_template_id,
            payload={},
            operation_id=delete_operation_id,
        )

        async with _configuration_image_container(
            test_engine, tmp_path / "workspace-configuration-first", token
        ) as first:
            profile["image_id"] = first["image_id"]
            profile["image_labels"] = first["image_labels"]
            profile["resource_limits"] = first["limits"]
            profile["first_container_id"] = first["container_id"]
            async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
                created = await _private_call(
                    client, first["origin"], token, create_request
                )
                assert created.status_code == 200, created.text
                created_reply = created.json()
                assert created_reply["ok"] is True
                assert created_reply["operation_id"] == str(create_operation_id)
                assert set(created_reply["result"]) == {"resource_id", "template_id"}
                assert created_reply["result"]["template_id"] is None
                template_id = uuid.UUID(created_reply["result"]["resource_id"])

                deleted = await _private_call(
                    client, first["origin"], token, delete_request
                )
                assert deleted.status_code == 200, deleted.text
                deleted_reply = deleted.json()
                assert deleted_reply["ok"] is True
                assert deleted_reply["result"]["resource_id"] == str(
                    deleted_template_id
                )

            async with api_configuration_binding(url=first["origin"], token=token):
                template_name = f"Image template later edit {suffix}"
                updated_template = await httpx_client.patch(
                    f"/api/v1/project-templates/{template_id}",
                    json={"name": template_name},
                    headers=_auth(actor),
                )
                assert updated_template.status_code == 200, updated_template.text
                assert updated_template.json()["name"] == template_name

                created_project = await httpx_client.post(
                    "/api/v1/projects",
                    json={
                        "name": f"Image project {suffix}",
                        "template_id": str(template_id),
                    },
                    headers=_auth(actor),
                )
                assert created_project.status_code == 200, created_project.text
                project_id = uuid.UUID(created_project.json()["id"])

                updated_project = await httpx_client.patch(
                    f"/api/v1/projects/{project_id}",
                    json={
                        "annotation_guide": f"container image public update {suffix}"
                    },
                    headers=_auth(actor),
                )
                assert updated_project.status_code == 200, updated_project.text

            semaphore = asyncio.Semaphore(_PROFILE_CONCURRENCY)
            latencies_ms: list[float] = []
            outcomes: list[tuple[int | None, bool]] = []

            async with httpx.AsyncClient(trust_env=False, timeout=15) as client:

                async def update_once(index: int):
                    request = configuration_request(
                        operation="update_project",
                        actor_id=actor.id,
                        target_id=profile_project_ids[index],
                        payload={"annotation_guide": f"local profile {suffix} {index}"},
                        operation_id=profile_operation_ids[index],
                    )
                    async with semaphore:
                        started = time.perf_counter()
                        try:
                            response = await _private_call(
                                client, first["origin"], token, request
                            )
                            payload = response.json()
                            succeeded = (
                                response.status_code == 200
                                and payload.get("ok") is True
                            )
                            status_code = response.status_code
                        except httpx.HTTPError:
                            succeeded = False
                            status_code = None
                        latencies_ms.append((time.perf_counter() - started) * 1000)
                        outcomes.append((status_code, succeeded))

                elapsed_started = time.perf_counter()
                await asyncio.gather(
                    *(update_once(index) for index in range(_PROFILE_REQUESTS))
                )
                elapsed_ms = (time.perf_counter() - elapsed_started) * 1000

            image_metadata = await asyncio.to_thread(
                _inspect_docker, first["container_id"]
            )
            oom_killed = bool(image_metadata.get("State", {}).get("OOMKilled"))
            profile["workload"].update(
                {
                    "success_count": sum(success for _status, success in outcomes),
                    "failure_count": sum(not success for _status, success in outcomes),
                    "http_statuses": [status for status, _success in outcomes],
                    "elapsed_ms": elapsed_ms,
                    "latency_ms": {
                        "p50": statistics.median(latencies_ms),
                        "p95": _nearest_rank(latencies_ms, 0.95),
                    },
                    "oom_killed": oom_killed,
                }
            )
            assert oom_killed is False
            profile_path = _write_local_profile(
                profile, first["image_identity"], profile["profile_run_id"]
            )
            assert profile["workload"]["success_count"] == _PROFILE_REQUESTS

        async with maker() as observer:
            template = await observer.get(ProjectTemplate, template_id)
            project = await observer.get(Project, project_id)
            assert template is not None
            assert template.name == template_name
            assert project is not None
            assert project.annotation_guide == f"container image public update {suffix}"
            for index, profile_project_id in enumerate(profile_project_ids):
                profile_project = await observer.get(Project, profile_project_id)
                assert profile_project is not None
                assert (
                    profile_project.annotation_guide
                    == f"local profile {suffix} {index}"
                )
            assert await observer.scalar(
                text(
                    "SELECT count(*) FROM public.workspace_configuration_operation_receipts "
                    "WHERE actor_id=CAST(:actor_id AS uuid)"
                ),
                {"actor_id": str(actor.id)},
            ) == len(all_operation_ids)
            for operation_id in all_operation_ids:
                assert await _counts(observer, operation_id) == (1, 0, 0)
            assert await observer.get(ProjectTemplate, deleted_template_id) is None

        async with _configuration_image_container(
            test_engine, tmp_path / "workspace-configuration-restart", token
        ) as restarted:
            profile["restart_container_id"] = restarted["container_id"]
            assert restarted["image_id"] == profile["image_id"]
            async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
                replay_create = await _private_call(
                    client, restarted["origin"], token, create_request
                )
                assert replay_create.status_code == 200, replay_create.text
                assert replay_create.json()["ok"] is True
                assert replay_create.json()["result"]["resource_id"] == str(template_id)
                assert replay_create.json()["result"]["template_id"] is None
                replay_delete = await _private_call(
                    client, restarted["origin"], token, delete_request
                )
                assert replay_delete.status_code == 200, replay_delete.text
                assert replay_delete.json() == deleted_reply

            restarted_metadata = await asyncio.to_thread(
                _inspect_docker, restarted["container_id"]
            )
            restart_oom_killed = bool(
                restarted_metadata.get("State", {}).get("OOMKilled")
            )
            profile["restart_oom_killed"] = restart_oom_killed
            assert restart_oom_killed is False
            async with maker() as observer:
                template = await observer.get(ProjectTemplate, template_id)
                assert template is not None and template.name == template_name
                assert await observer.get(ProjectTemplate, deleted_template_id) is None
                for operation_id in all_operation_ids:
                    assert await _counts(observer, operation_id) == (1, 0, 0)

            async with maker.begin() as db:
                await db.execute(
                    text(
                        "UPDATE public.users SET is_active=false "
                        "WHERE id=CAST(:actor_id AS uuid)"
                    ),
                    {"actor_id": str(actor.id)},
                )
            async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
                revoked = await _private_call(
                    client, restarted["origin"], token, create_request
                )
            assert revoked.status_code == 200
            assert revoked.json()["ok"] is False
            assert revoked.json()["error"]["status_code"] == 401
            async with maker() as observer:
                assert (
                    await observer.get(ProjectTemplate, template_id)
                ).name == template_name
                assert await observer.get(ProjectTemplate, deleted_template_id) is None
                for operation_id in all_operation_ids:
                    assert await _counts(observer, operation_id) == (1, 0, 0)

        profile["completed"] = True
        if "profile_path" in locals():
            _update_local_profile(profile_path, profile)
    finally:
        await _cleanup_world(maker, actor.id, all_operation_ids)
