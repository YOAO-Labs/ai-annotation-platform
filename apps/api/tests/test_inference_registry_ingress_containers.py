"""Actual startup entry -> restricted Registry image -> controlled backend."""

from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
import httpx
import pytest
from sqlalchemy.engine import make_url
from app.config import settings
from app.services import ml_backend_env_sync as env
from aap_inference_contract.registry_ingress import RegistryEnvSyncUnavailable
from tests.inference_registry_ingress_helpers import ingress_dsn
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
)
from tests.test_inference_registry_ingress_legacy import _server
from tests.test_inference_registry_ingress_native import _connect, _cleanup_registries
from tests.test_inference_registry_ingress_bindings import _receipt_ids

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_REGISTRY_INGRESS_CONTAINER_LIVE") != "1",
        reason="requires explicitly selected owned Registry container",
    ),
]


async def test_startup_uses_restricted_registry_image_survives_restart_and_has_no_fallback(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    before_receipts = await _receipt_ids(owner)
    registry_ids = []
    trace = []
    for name in ("database_url", "database_url_file", "token_file"):
        monkeypatch.setattr(settings, "inference_registry_ingress_" + name, "")

    async def forbidden_borrowed(urls):
        pytest.fail("the selected Registry process must not fall back")

    monkeypatch.setattr(env, "_sync_borrowed", forbidden_borrowed)
    try:
        async with _registry_container(
            ingress_dsn(test_engine.url), "isolated-registry-ingress-control"
        ) as (origin, container_id, record):
            monkeypatch.setattr(settings, "inference_registry_ingress_url", origin)
            monkeypatch.setattr(
                settings,
                "inference_registry_ingress_token",
                "isolated-registry-ingress-control",
            )
            with _server("new_ok", trace) as url:
                backend_url = (
                    make_url(url)
                    .set(host="host.docker.internal")
                    .render_as_string(hide_password=False)
                )
                monkeypatch.setattr(env, "_observe_urls", lambda: [backend_url])
                await env.sync_env_backends()
                row = await owner.fetchrow(
                    "SELECT id,state,last_checked_at FROM ml_backend_registry WHERE url=$1",
                    backend_url,
                )
                registry_ids.append(row["id"])
                assert (
                    row["state"] == "connected" and row["last_checked_at"] is not None
                )
                old_started = _inspect_docker(container_id)["State"]["StartedAt"]
                record["origin_before_restart"] = origin
                _docker("restart", "--time", "5", container_id)
                origin = _origin(container_id)
                record["origin_after_restart"] = origin
                monkeypatch.setattr(settings, "inference_registry_ingress_url", origin)
                await _ready(origin, container_id)
                assert (
                    _inspect_docker(container_id)["State"]["StartedAt"] != old_started
                )
                await env.sync_env_backends()
                record["restart_verified"] = True
                limiter = asyncio.Semaphore(4)

                async def measured():
                    async with limiter:
                        started = time.monotonic()
                        await env.sync_env_backends()
                        return time.monotonic() - started

                samples = sorted(await asyncio.gather(*(measured() for _ in range(16))))
                record["local_sample"] = {
                    "requests": 16,
                    "concurrency": 4,
                    "p50_seconds": samples[7],
                    "p95_seconds": samples[15],
                    "api_database_backend_outside_container_budget": True,
                }
                assert len(trace) == 18
                receipts = await _receipt_ids(owner) - before_receipts
                assert len(receipts) == 18
                assert (
                    await owner.fetchval(
                        "SELECT count(*) FROM ml_backend_registry WHERE url=$1",
                        backend_url,
                    )
                    == 1
                )
                assert (
                    await owner.fetchval(
                        "SELECT count(*) FROM ml_backend_pool_members WHERE registry_id=$1",
                        row["id"],
                    )
                    == 1
                )
                _docker("stop", "--time", "5", container_id)
                with pytest.raises(RegistryEnvSyncUnavailable):
                    await env.sync_env_backends()
                assert (
                    len(trace) == 18
                    and await _receipt_ids(owner) - before_receipts == receipts
                )
                record["no_fallback_verified"] = True
                record["committed_receipts"] = len(receipts)
    finally:
        try:
            await _cleanup_registries(
                owner, registry_ids, await _receipt_ids(owner) - before_receipts
            )
        finally:
            await owner.close()


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
async def _registry_container(dsn, token):
    prefix = "inference-registry-ingress"
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
        prefix="registry-ingress-container-", dir=ROOT / ".worktree"
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
                "aap-registry-ingress-" + identity,
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
                "INFERENCE_REGISTRY_INGRESS_DATABASE_URL_FILE=/run/secrets/database",
                "--env",
                "INFERENCE_REGISTRY_INGRESS_TOKEN_FILE=/run/secrets/token",
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
