"""Public API -> restricted prebuilt observer -> real controlled backend."""

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
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.user import User
from app.services.ml_backend import MLBackendService
from tests.factory import create_user
from tests.inference_health_observation_helpers import observation_dsn
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
)
from tests.test_inference_health_observation_bindings import backend_server
from tests.test_workspace_configuration_live import _auth, _suppress_request_audit

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_HEALTH_OBSERVATION_CONTAINER_LIVE") != "1",
        reason="requires explicitly selected owned prebuilt observation container",
    ),
]


async def _ready(origin, container_id):
    deadline = time.monotonic() + 30
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        while True:
            try:
                if (await client.get(origin + "/health")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            assert _inspect_docker(container_id)["State"]["Running"]
            assert time.monotonic() < deadline, "Owned observation container not ready"
            await asyncio.sleep(0.1)


def _origin(container_id):
    published = _inspect_docker(container_id)["NetworkSettings"]["Ports"]["8768/tcp"]
    assert len(published) == 1 and published[0]["HostIp"] == "127.0.0.1"
    return "http://127.0.0.1:" + published[0]["HostPort"]


@asynccontextmanager
async def _observer_container(dsn, token):
    prefix = os.environ.get(
        "AAP_HEALTH_OBSERVATION_QUALIFICATION_PREFIX", "inference-health-observation"
    )
    assert prefix in {
        "inference-health-observation",
        "inference-health-data",
        "inference-health-data-transaction",
    }
    artifact = json.loads(
        (ROOT / f".worktree/{prefix}-inference-image-artifact.json").read_text()
    )
    image = _inspect_docker(artifact["image_id"])
    assert image["Config"]["Labels"]["org.aap.acceptance-id"] == artifact["identity"]
    assert (
        image["Config"]["Labels"]["org.aap.source-revision"]
        == artifact["source_commit"]
    )
    identity = secrets.token_hex(12)
    connection = (
        make_url(dsn)
        .set(host="host.docker.internal")
        .render_as_string(hide_password=False)
    )
    container_id = None
    result = {
        "identity": identity,
        "source_commit": artifact["source_commit"],
        "image_id": artifact["image_id"],
    }
    with tempfile.TemporaryDirectory(
        prefix="health-observation-container-", dir=ROOT / ".worktree"
    ) as temporary:
        mounts = []
        for name, value in (("database", connection), ("token", token)):
            path = Path(temporary) / name
            path.write_text(value)
            path.chmod(0o444)
            mounts += [
                "--mount",
                f"type=bind,src={path},dst=/run/secrets/{name},readonly",
            ]
        try:
            container_id = _docker(
                "run",
                "--detach",
                "--pull=never",
                "--name",
                "aap-health-observation-" + identity,
                "--label",
                "org.aap.acceptance=module-data-boundary",
                "--label",
                "org.aap.acceptance-id=" + identity,
                "--user=10008:10008",
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
                "127.0.0.1::8768",
                "--env",
                "INFERENCE_HOST=0.0.0.0",
                "--env",
                "INFERENCE_PORT=8768",
                "--env",
                "INFERENCE_HEALTH_OBSERVATION_DATABASE_URL_FILE=/run/secrets/database",
                "--env",
                "INFERENCE_HEALTH_OBSERVATION_TOKEN_FILE=/run/secrets/token",
                *mounts,
                artifact["image_id"],
            ).strip()
            state = _inspect_docker(container_id)
            host = state["HostConfig"]
            assert state["Config"]["User"] == "10008:10008"
            assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
            assert "no-new-privileges:true" in host["SecurityOpt"]
            assert (host["Memory"], host["NanoCpus"], host["PidsLimit"]) == (
                256 * 1024 * 1024,
                1_000_000_000,
                64,
            )
            assert len(state["Mounts"]) == 2 and all(
                not mount["RW"] for mount in state["Mounts"]
            )
            published = state["NetworkSettings"]["Ports"]["8768/tcp"]
            assert len(published) == 1 and published[0]["HostIp"] == "127.0.0.1"
            origin = _origin(container_id)
            await _ready(origin, container_id)
            result.update(container_id=container_id, restrictions_verified=True)
            yield origin, container_id, result
            assert not _inspect_docker(container_id)["State"]["OOMKilled"]
        finally:
            if container_id is not None:
                state = _inspect_docker(container_id)
                result["oom_killed"] = state["State"]["OOMKilled"]
                captured = subprocess.run(
                    ["rtk", "proxy", "docker", "logs", "--tail", "120", container_id],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True,
                )
                log = captured.stdout + captured.stderr
                for secret in (connection, token, make_url(connection).password):
                    if secret:
                        log = log.replace(secret, "<redacted>")
                log_path = ROOT / f".worktree/{prefix}-runtime-{identity}.log"
                log_path.touch(mode=0o600)
                log_path.write_text(log)
                result["log_file"] = log_path.name
                _stop_remove_container(container_id, identity)
                result["container_removed"] = True
                (ROOT / f".worktree/{prefix}-runtime-{identity}.json").write_text(
                    json.dumps(result, indent=2) + "\n"
                )


async def test_public_health_uses_observer_image_survives_restart_and_fails_without_fallback(
    test_engine, httpx_client, app_module, monkeypatch
):
    from app.deps import get_db

    await assert_owned_test_database(test_engine)
    _suppress_request_audit(monkeypatch)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def request_db():
        async with maker() as db:
            try:
                yield db
            finally:
                await db.rollback()

    monkeypatch.setitem(app_module.dependency_overrides, get_db, request_db)
    for name in (
        "inference_health_observation_database_url",
        "inference_health_observation_database_url_file",
        "inference_health_observation_token_file",
    ):
        monkeypatch.setattr(settings, name, "")
    actor_id = registry_id = pool_id = None
    try:
        async with _observer_container(
            observation_dsn(test_engine.url), "isolated-health-observation-control"
        ) as (origin, container_id, record):
            monkeypatch.setattr(settings, "inference_health_observation_url", origin)
            monkeypatch.setattr(
                settings,
                "inference_health_observation_token",
                "isolated-health-observation-control",
            )
            with backend_server() as (url, calls):
                backend_url = (
                    make_url(url)
                    .set(host="host.docker.internal")
                    .render_as_string(hide_password=False)
                )
                async with maker.begin() as seed:
                    actor = await create_user(
                        seed,
                        "super_admin",
                        f"health-image-{uuid.uuid4().hex}@example.test",
                        "Health image operator",
                    )
                    actor_id = actor.id
                    backend = MLBackendRegistry(
                        name="Image health",
                        url=backend_url,
                        auth_method="token",
                        auth_token="image-health-token",
                        extra_params={},
                    )
                    seed.add(backend)
                    await seed.flush()
                    registry_id = backend.id
                    pool = await MLBackendService(seed)._create_singleton_pool(backend)
                    pool_id = pool.id
                    authorization = _auth(actor)
                route = f"/api/v1/admin/ml-integrations/registry/{registry_id}/health"

                async def health(*, allow_superseded=False):
                    response = await httpx_client.post(route, headers=authorization)
                    assert response.status_code == 200, response.text
                    body = response.json()
                    assert body == {
                        "status": body["status"],
                        "backend_id": str(registry_id),
                        "backend_name": "Image health",
                    }
                    assert body["status"] in {"ok", "error"}
                    if not allow_superseded:
                        assert body["status"] == "ok"
                    return body["status"]

                await health()
                async with maker() as outside:
                    first_checked = (
                        await outside.get(MLBackendRegistry, registry_id)
                    ).last_checked_at
                    assert first_checked is not None
                before = _inspect_docker(container_id)["State"]["StartedAt"]
                record["origin_before_restart"] = origin
                _docker("restart", "--time", "5", container_id)
                # Docker may reassign an ephemeral host port on restart.
                origin = _origin(container_id)
                record["origin_after_restart"] = origin
                monkeypatch.setattr(
                    settings, "inference_health_observation_url", origin
                )
                await _ready(origin, container_id)
                assert _inspect_docker(container_id)["State"]["StartedAt"] != before
                await health()
                record["restart_verified"] = True
                limiter = asyncio.Semaphore(4)

                async def measured():
                    async with limiter:
                        started = time.monotonic()
                        status = await health(allow_superseded=True)
                        return time.monotonic() - started, status

                outcomes = await asyncio.gather(*(measured() for _ in range(16)))
                samples = sorted(seconds for seconds, _ in outcomes)
                record["local_sample"] = {
                    "requests": 16,
                    "concurrency": 4,
                    "p50_seconds": samples[7],
                    "p95_seconds": samples[15],
                    "superseded_observations": sum(
                        status == "error" for _, status in outcomes
                    ),
                    "api_database_backend_outside_container_budget": True,
                }
                assert len(calls) == 36
                assert all(
                    authorization == "Bearer image-health-token"
                    for _, authorization in calls
                )
                _docker("stop", "--time", "5", container_id)
                count = len(calls)
                response = await httpx_client.post(route, headers=authorization)
                assert response.status_code == 503
                assert (
                    response.json()["detail"]["error_code"]
                    == "inference_health_observation_unavailable"
                )
                assert len(calls) == count
                record["no_fallback_verified"] = True
    finally:
        async with maker.begin() as cleanup:
            if registry_id is not None:
                await cleanup.execute(
                    delete(MLBackendPoolMember).where(
                        MLBackendPoolMember.registry_id == registry_id
                    )
                )
            if pool_id is not None:
                await cleanup.execute(
                    delete(MLBackendServicePool).where(
                        MLBackendServicePool.id == pool_id
                    )
                )
            if registry_id is not None:
                await cleanup.execute(
                    delete(MLBackendRegistry).where(MLBackendRegistry.id == registry_id)
                )
            if actor_id is not None:
                await cleanup.execute(delete(User).where(User.id == actor_id))
