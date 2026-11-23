"""Prebuilt restricted Inference container on the embedded public config path."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from ipaddress import ip_address
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from tests.conftest import create_registry_with_pool
from tests.factory import create_user
from tests.inference_backend_probe_helpers import probe_dsn
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _verify_postgres,
)
from tests.test_inference_backend_probe_bindings import setup_backend
from tests.test_workspace_configuration_live import _auth, _cleanup_world
from tests.workspace_configuration_helpers import public_workspace_client

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_INFERENCE_PROBE_CONTAINER_LIVE") != "1",
        reason="requires the explicitly selected owned prebuilt Inference probe image",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


@asynccontextmanager
async def _probe_container(test_engine, token, monkeypatch):
    artifact_path = (
        ROOT / ".worktree/workspace-inference-probe-inference-image-artifact.json"
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    image = _inspect_docker(artifact["image_id"])
    assert image["Id"] == artifact["image_id"]
    labels = image["Config"]["Labels"]
    assert labels["org.aap.acceptance-id"] == artifact["identity"]
    assert labels["org.aap.source-revision"] == artifact["source_commit"]

    identity = secrets.token_hex(12)
    project = f"aap-workspace-probe-{identity}"
    postgres_id = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
    postgres_name = os.environ["AAP_MODULE_DATA_PG_CONTAINER_NAME"]
    dsn = make_url(probe_dsn(test_engine.url))
    connection = dsn.set(host=postgres_name, port=5432).render_as_string(
        hide_password=False
    )
    network_id = None
    connected = False
    container_id = None
    with (
        tempfile.TemporaryDirectory(
            prefix=f"{project}-", dir=ROOT / ".worktree"
        ) as temporary,
        monkeypatch.context() as environment,
    ):
        directory = Path(temporary)
        values = {
            "INFERENCE_RUNTIME_BACKEND_PROBE_DATABASE_URL_FILE": (
                "inference_backend_probe_database_url",
                connection,
            ),
            "INFERENCE_RUNTIME_BACKEND_PROBE_CONTROL_TOKEN_FILE": (
                "inference_backend_probe_control_token",
                token,
            ),
        }
        for variable, (name, value) in values.items():
            path = directory / name
            path.write_text(value, encoding="utf-8")
            path.chmod(0o444)
            environment.setenv(variable, str(path))
        override = directory / "compose.test.yml"
        override.write_text(
            f"""services:
  inference-probe:
    image: {image["Id"]}
    build: !reset null
    ports: !override ["127.0.0.1::8768"]
    labels:
      org.aap.acceptance: module-data-boundary
      org.aap.acceptance-id: {identity}
networks:
  default:
    external: true
    name: {project}
""",
            encoding="utf-8",
        )
        compose = (
            "compose",
            "--project-name",
            project,
            "--project-directory",
            str(directory),
            "-f",
            str(ROOT / "docker-compose.inference-probe.yml"),
            "-f",
            str(override),
        )
        try:
            network_id = _docker(
                "network",
                "create",
                "--label",
                "org.aap.acceptance=module-data-boundary",
                "--label",
                f"org.aap.acceptance-id={identity}",
                project,
            )
            _verify_postgres(dsn)
            _docker(
                "network", "connect", "--gw-priority", "-1", network_id, postgres_id
            )
            connected = True
            _docker(*compose, "up", "--detach", "--no-build", "--pull", "never")
            container_id = _docker(*compose, "ps", "--quiet", "inference-probe").strip()
            state = _inspect_docker(container_id)
            config = state["Config"]
            host = state["HostConfig"]
            assert state["Image"] == image["Id"]
            assert config["Labels"]["org.aap.acceptance-id"] == identity
            assert config["User"] == "10008:10008"
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
            assert {
                mount["Destination"]: Path(mount["Source"]) for mount in mounts
            } == {
                f"/run/secrets/{name}": directory / name for name, _ in values.values()
            }
            networks = state["NetworkSettings"]["Networks"]
            assert set(networks) == {project}
            assert networks[project]["NetworkID"] == network_id
            gateway = ip_address(networks[project]["Gateway"])
            assert gateway.version == 4 and not gateway.is_loopback
            published = state["NetworkSettings"]["Ports"]["8768/tcp"]
            assert len(published) == 1 and published[0]["HostIp"] == "127.0.0.1"
            origin = f"http://127.0.0.1:{int(published[0]['HostPort'])}"
            deadline = time.monotonic() + 30
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                while True:
                    try:
                        if (await client.get(origin + "/health")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert _inspect_docker(container_id)["State"]["Running"]
                    assert time.monotonic() < deadline, "Inference probe not ready"
                    await asyncio.sleep(0.1)
            yield origin, container_id, str(gateway)
            assert not _inspect_docker(container_id)["State"]["OOMKilled"]
        finally:
            try:
                if container_id is not None:
                    log = _docker("logs", "--tail", "160", container_id, timeout=15)
                    for secret in (connection, token):
                        log = log.replace(secret, "<redacted>")
                    log_path = ROOT / f".worktree/{project}.log"
                    log_path.touch(mode=0o600)
                    log_path.write_text(log, encoding="utf-8")
            finally:
                if network_id is not None:
                    try:
                        _docker(*compose, "down")
                    finally:
                        if connected:
                            _verify_postgres(dsn)
                            _docker("network", "disconnect", network_id, postgres_id)
                        _docker("network", "rm", network_id)


async def test_public_configuration_probes_through_restricted_inference_container(
    test_engine, app_module, monkeypatch
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = registry_id = None
    try:
        probe_token = secrets.token_urlsafe(32)
        async with _probe_container(test_engine, probe_token, monkeypatch) as (
            probe_url,
            probe_id,
            gateway,
        ):
            monkeypatch.setattr(settings, "inference_backend_probe_url", probe_url)
            monkeypatch.setattr(settings, "inference_backend_probe_token", probe_token)
            monkeypatch.setattr(settings, "inference_backend_probe_token_file", "")
            with setup_backend(gateway) as (backend_url, calls):
                async with maker.begin() as seed:
                    actor = await create_user(
                        seed,
                        "project_admin",
                        f"probe-container-{uuid.uuid4().hex}@example.test",
                        "Probe container actor",
                    )
                    actor_id = actor.id
                    registry, _pool = await create_registry_with_pool(
                        seed,
                        name="Container probe",
                        url=backend_url + "/container",
                        state="disconnected",
                        is_interactive=False,
                        auth_method="token",
                        auth_token="snapshot-secret",
                        extra_params={},
                    )
                    registry_id = registry.id
                request = {
                    "name": "Container probe project",
                    "type_label": "Image",
                    "ml_backend_source_id": str(registry_id),
                }
                async with public_workspace_client(
                    test_engine, app_module, monkeypatch
                ) as client:
                    response = await client.post(
                        "/api/v1/projects", json=request, headers=_auth(actor)
                    )
                    assert response.status_code == 200, response.text
                    assert calls == [
                        ("/container/setup", "Bearer snapshot-secret", "no-cache")
                    ]
                    _docker("stop", "--time", "5", probe_id)
                    failed = await client.post(
                        "/api/v1/projects",
                        json={**request, "name": "Provider unavailable"},
                        headers=_auth(actor),
                    )
                    assert failed.status_code == 503, failed.text
                    assert len(calls) == 1
            async with maker() as observer:
                assert (
                    await observer.scalar(
                        text("SELECT count(*) FROM public.projects WHERE owner_id=:id"),
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
