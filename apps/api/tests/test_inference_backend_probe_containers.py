"""Prebuilt restricted Inference/Workspace containers executing the real API probe path."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_backend import MLBackendService
from tests.inference_backend_probe_helpers import probe_dsn
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
)
from tests.test_inference_backend_probe_bindings import _insert, setup_backend
from tests.test_workspace_configuration_live import (
    _auth,
    _cleanup_world,
    _suppress_request_audit,
)
from tests.workspace_configuration_helpers import (
    api_configuration_binding,
    configuration_dsn,
)
from tests.factory import create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_INFERENCE_PROBE_CONTAINER_LIVE") != "1",
        reason="requires explicitly selected owned prebuilt probe containers",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


@asynccontextmanager
async def _container(owner, dsn, token, *, probe_url=None, probe_token=None):
    artifact = json.loads(
        (
            ROOT / f".worktree/workspace-inference-probe-{owner}-image-artifact.json"
        ).read_text()
    )
    image = _inspect_docker(artifact["image_id"])
    assert image["Config"]["Labels"]["org.aap.acceptance-id"] == artifact["identity"]
    assert (
        image["Config"]["Labels"]["org.aap.source-revision"]
        == artifact["source_commit"]
    )
    identity = secrets.token_hex(12)
    uid, port, ready = (
        (10008, 8768, "/health") if owner == "inference" else (10004, 8174, "/ready")
    )
    connection = (
        make_url(dsn)
        .set(host="host.docker.internal")
        .render_as_string(hide_password=False)
    )
    values = {"database": connection, "control": token}
    if probe_token is not None:
        values["probe-control"] = probe_token
    container_id = None
    with tempfile.TemporaryDirectory(
        prefix="workspace-probe-container-", dir=ROOT / ".worktree"
    ) as temporary:
        directory = Path(temporary)
        arguments = []
        for name, value in values.items():
            path = directory / name
            path.write_text(value, encoding="utf-8")
            path.chmod(0o444)
            arguments += [
                "--mount",
                f"type=bind,src={path},dst=/run/secrets/{name},readonly",
            ]
        if owner == "inference":
            environment = {
                "INFERENCE_BACKEND_PROBE_DATABASE_URL_FILE": "/run/secrets/database",
                "INFERENCE_BACKEND_PROBE_CONTROL_TOKEN_FILE": "/run/secrets/control",
            }
        else:
            environment = {
                "WORKSPACE_HOST": "0.0.0.0",
                "WORKSPACE_PORT": str(port),
                "WORKSPACE_CONFIGURATION_DATABASE_URL_FILE": "/run/secrets/database",
                "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": "/run/secrets/control",
                "WORKSPACE_INFERENCE_PROBE_URL": probe_url,
                "WORKSPACE_INFERENCE_PROBE_TOKEN_FILE": "/run/secrets/probe-control",
            }
        for name, value in environment.items():
            arguments += ["--env", f"{name}={value}"]
        try:
            container_id = _docker(
                "run",
                "--detach",
                "--pull=never",
                "--name",
                "aap-workspace-probe-" + identity,
                "--label",
                "org.aap.acceptance=module-data-boundary",
                "--label",
                "org.aap.acceptance-id=" + identity,
                f"--user={uid}:{uid}",
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
                f"127.0.0.1::{port}",
                *arguments,
                artifact["image_id"],
            ).strip()
            state = _inspect_docker(container_id)
            host = state["HostConfig"]
            assert state["Config"]["User"] == f"{uid}:{uid}"
            assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
            assert "no-new-privileges:true" in host["SecurityOpt"]
            assert (host["Memory"], host["NanoCpus"], host["PidsLimit"]) == (
                256 * 1024 * 1024,
                1_000_000_000,
                64,
            )
            mounts = [mount for mount in state["Mounts"] if mount["Type"] == "bind"]
            assert len(mounts) == len(values) and all(
                not mount["RW"] for mount in mounts
            )
            assert {mount["Destination"] for mount in mounts} == {
                "/run/secrets/" + name for name in values
            }
            published = state["NetworkSettings"]["Ports"][f"{port}/tcp"]
            assert len(published) == 1 and published[0]["HostIp"] == "127.0.0.1"
            host_port = int(published[0]["HostPort"])
            origin = f"http://127.0.0.1:{host_port}"
            deadline = time.monotonic() + 30
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                while True:
                    try:
                        if (await client.get(origin + ready)).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert _inspect_docker(container_id)["State"]["Running"]
                    assert time.monotonic() < deadline, (
                        "Owned container readiness failed"
                    )
                    await asyncio.sleep(0.1)
            yield origin, host_port, container_id
            state = _inspect_docker(container_id)
            assert not state["State"]["OOMKilled"]
        finally:
            if container_id is not None:
                captured = subprocess.run(
                    ["rtk", "proxy", "docker", "logs", "--tail", "160", container_id],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True,
                )
                log = captured.stdout + captured.stderr
                for secret in (connection, token, probe_token):
                    if secret:
                        log = log.replace(secret, "<redacted>")
                log_path = (
                    ROOT
                    / f".worktree/workspace-inference-probe-runtime-{owner}-{identity}.log"
                )
                log_path.touch(mode=0o600)
                log_path.write_text(log, encoding="utf-8")
                state = _inspect_docker(container_id)
                receipt = (
                    ROOT
                    / f".worktree/workspace-inference-probe-runtime-{owner}-{identity}.json"
                )
                receipt.write_text(
                    json.dumps(
                        {
                            "identity": identity,
                            "container_id": container_id,
                            "image_id": artifact["image_id"],
                            "oom_killed": state["State"]["OOMKilled"],
                            "log_file": log_path.name,
                        },
                        indent=2,
                    )
                    + "\n"
                )
                if owner == "inference" and state["State"]["Running"]:
                    _docker("stop", "--time", "5", container_id)
                _stop_remove_container(container_id, identity)


async def test_public_api_uses_restricted_workspace_and_inference_containers_without_fallback(
    test_engine, httpx_client, monkeypatch
):
    await assert_owned_test_database(test_engine)
    _suppress_request_audit(monkeypatch)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = registry_id = None
    try:
        async with _container(
            "inference", probe_dsn(test_engine.url), "dedicated-probe-token"
        ) as (_, probe_port, probe_id):
            async with _container(
                "workspace",
                configuration_dsn(test_engine),
                "workspace-control-token",
                probe_url=f"http://host.docker.internal:{probe_port}",
                probe_token="dedicated-probe-token",
            ) as (origin, _, workspace_id):
                with setup_backend() as (backend_url, calls):
                    parsed = make_url(backend_url).set(host="host.docker.internal")
                    async with maker.begin() as seed:
                        actor = await create_user(
                            seed,
                            "project_admin",
                            f"probe-container-{uuid.uuid4().hex}@example.test",
                            "Probe container actor",
                        )
                        actor_id = actor.id
                        registry_id = uuid.uuid4()
                        await _insert(
                            seed,
                            registry_id,
                            parsed.render_as_string(hide_password=False) + "/container",
                        )
                        registry = await seed.get(MLBackendRegistry, registry_id)
                        await MLBackendService(seed)._create_singleton_pool(registry)
                    async with api_configuration_binding(
                        url=origin, token="workspace-control-token"
                    ):
                        request = {
                            "name": "Container probe project",
                            "type_label": "Image",
                            "ml_backend_source_id": str(registry_id),
                        }
                        response = await httpx_client.post(
                            "/api/v1/projects", json=request, headers=_auth(actor)
                        )
                        assert response.status_code == 200, response.text
                        assert calls == [
                            ("/container/setup", "Bearer snapshot-secret", "no-cache")
                        ]
                        # Stop only this owned provider; the Workspace cannot switch to a local credential path.
                        _docker("stop", "--time", "5", probe_id)
                        failed = await httpx_client.post(
                            "/api/v1/projects",
                            json={**request, "name": "Provider unavailable"},
                            headers=_auth(actor),
                        )
                        assert failed.status_code == 503, failed.text
                        assert len(calls) == 1
                    async with maker() as observer:
                        assert (
                            await observer.scalar(
                                text(
                                    "SELECT count(*) FROM public.projects WHERE owner_id=:id"
                                ),
                                {"id": actor_id},
                            )
                            == 1
                        )
                        assert (
                            await observer.scalar(
                                text(
                                    "SELECT count(*) FROM public.workspace_configuration_operation_receipts WHERE actor_id=:id"
                                ),
                                {"id": actor_id},
                            )
                            == 1
                        )
    finally:
        if actor_id is not None:
            await _cleanup_world(maker, actor_id, {})
        if registry_id is not None:
            async with maker.begin() as cleanup:
                await cleanup.execute(
                    text(
                        "DELETE FROM public.ml_backend_service_pools WHERE legacy_instance_id=:id"
                    ),
                    {"id": registry_id},
                )
                await cleanup.execute(
                    text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                    {"id": registry_id},
                )
