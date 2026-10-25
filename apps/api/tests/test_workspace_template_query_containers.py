"""Owned query image, public readback, restart and bounded local concurrency."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import statistics
import time
import uuid

import httpx
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.template_query_contract import template_query_request
from app.config import settings
from app.core.security import create_access_token
from app.deps import get_db
from tests.factory import create_user
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
)
from tests.test_workspace_template_query_bindings import _remove_actor
from tests.workspace_template_query_helpers import (
    api_template_query_binding,
    template_query_dsn,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_TEMPLATE_QUERY_CONTAINER_LIVE") != "1",
        reason="requires an explicitly qualified owned Workspace query image",
    ),
]


@asynccontextmanager
async def _committed_api_client(app, maker):
    async def request_db():
        async with maker() as db:
            yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = request_db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


@asynccontextmanager
async def _query_container(test_engine, directory, token):
    image = _inspect_docker(os.environ["AAP_WORKSPACE_TEMPLATE_QUERY_IMAGE"])
    labels = image["Config"]["Labels"]
    assert labels["org.aap.acceptance"] == "workspace-template-query-owner"
    assert (
        labels["org.aap.acceptance-id"]
        == os.environ["AAP_WORKSPACE_TEMPLATE_QUERY_IDENTITY"]
    )
    assert image["Config"]["User"] == "10004:10004"
    assert image["Config"]["Cmd"] == ["python", "-m", "aap_workspace"]
    parsed = make_url(template_query_dsn(test_engine))
    container_dsn = parsed.set(host="host.docker.internal").render_as_string(
        hide_password=False
    )
    identity = secrets.token_hex(12)
    directory.mkdir(mode=0o700)
    dsn_file, token_file = directory / "query-database", directory / "control"
    container = None
    try:
        for path, value in ((dsn_file, container_dsn), (token_file, token)):
            path.write_text(value)
            path.chmod(0o444)
        container = _docker(
            "run",
            "--detach",
            "--pull=never",
            "--name",
            f"aap-query-{identity}",
            "--label",
            "org.aap.acceptance=module-data-boundary",
            "--label",
            f"org.aap.acceptance-id={identity}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=1",
            "--no-healthcheck",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--publish",
            "127.0.0.1::8175",
            "--mount",
            f"type=bind,src={dsn_file},dst=/run/secrets/query,readonly",
            "--mount",
            f"type=bind,src={token_file},dst=/run/secrets/control,readonly",
            "--env",
            "WORKSPACE_HOST=0.0.0.0",
            "--env",
            "WORKSPACE_PORT=8175",
            "--env",
            "WORKSPACE_TEMPLATE_QUERY_DATABASE_URL_FILE=/run/secrets/query",
            "--env",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE=/run/secrets/control",
            image["Id"],
            timeout=120,
        ).strip()
        metadata = _inspect_docker(container)
        assert metadata["Config"]["User"] == "10004:10004"
        host = metadata["HostConfig"]
        assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in host["SecurityOpt"]
        assert (host["PidsLimit"], host["Memory"], host["NanoCpus"]) == (
            64,
            256 * 1024 * 1024,
            1_000_000_000,
        )
        assert any(
            value in host["Tmpfs"]["/tmp"] for value in ("size=32m", "size=33554432")
        )
        mounts = [entry for entry in metadata["Mounts"] if entry["Type"] == "bind"]
        assert len(mounts) == 2 and all(not entry["RW"] for entry in mounts)
        assert {entry["Destination"]: Path(entry["Source"]) for entry in mounts} == {
            "/run/secrets/query": dsn_file.resolve(),
            "/run/secrets/control": token_file.resolve(),
        }
        published = metadata["NetworkSettings"]["Ports"]["8175/tcp"]
        assert len(published) == 1 and published[0]["HostIp"] == "127.0.0.1"
        origin = f"http://127.0.0.1:{published[0]['HostPort']}"
        deadline = time.monotonic() + 40
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while True:
                try:
                    if (await client.get(origin + "/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert _inspect_docker(container)["State"]["Running"]
                assert time.monotonic() < deadline, "Owned query image not ready"
                await asyncio.sleep(0.1)
        yield origin, container, image["Id"]
        assert not _inspect_docker(container)["State"]["OOMKilled"]
    finally:
        try:
            if container is not None:
                _stop_remove_container(container, identity)
        finally:
            dsn_file.unlink(missing_ok=True)
            token_file.unlink(missing_ok=True)
            directory.rmdir()


async def test_query_image_public_projection_restart_and_local_concurrency(
    test_engine, app_module, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    token = secrets.token_urlsafe(32)
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"query-image-{uuid.uuid4().hex}@example.test",
                "Query image",
            )
            actor_id = actor.id
        headers = {
            "Authorization": f"Bearer {create_access_token(str(actor_id), actor.role)}"
        }
        async with (
            _query_container(test_engine, tmp_path / "query-first", token) as first,
            _committed_api_client(app_module, maker) as httpx_client,
        ):
            origin, container, image_id = first
            async with api_template_query_binding(url=origin, token=token):
                created = await httpx_client.post(
                    "/api/v1/project-templates",
                    json={
                        "name": "Image response projection",
                        "type_label": "Image",
                        "type_key": "image-det",
                    },
                    headers=headers,
                )
                assert created.status_code == 201, created.text
                template = created.json()
                target = f"/api/v1/project-templates/{template['id']}"
                response = await httpx_client.get(target, headers=headers)
                assert response.status_code == 200 and response.json() == template
            request = template_query_request(
                "get_template",
                actor_id,
                "project_admin",
                target_id=uuid.UUID(template["id"]),
            ).model_dump(mode="json", by_alias=True)
            gate = asyncio.Semaphore(4)
            latencies = []
            async with httpx.AsyncClient(trust_env=False, timeout=15) as client:

                async def read_once():
                    async with gate:
                        started = time.perf_counter()
                        response = await client.post(
                            origin + "/v1/workspace/template-queries/execute",
                            headers={"Authorization": f"Bearer {token}"},
                            json=request,
                        )
                        latencies.append((time.perf_counter() - started) * 1000)
                        assert response.status_code == 200 and response.json()["ok"]
                        assert (
                            json.loads(response.json()["data_json"])["id"]
                            == template["id"]
                        )

                started = time.perf_counter()
                await asyncio.gather(*(read_once() for _ in range(16)))
                elapsed = (time.perf_counter() - started) * 1000
            assert not _inspect_docker(container)["State"]["OOMKilled"]
        async with (
            _query_container(
                test_engine, tmp_path / "query-restart", token
            ) as restarted,
            _committed_api_client(app_module, maker) as httpx_client,
        ):
            assert restarted[2] == image_id
            async with api_template_query_binding(url=restarted[0], token=token):
                response = await httpx_client.get(target, headers=headers)
                assert response.status_code == 200 and response.json() == template
        profile = {
            "image_id": image_id,
            "requests": 16,
            "concurrency": 4,
            "successes": len(latencies),
            "failures": 0,
            "oom_killed": False,
            "elapsed_ms": elapsed,
            "p50_ms": statistics.median(latencies),
            "p95_ms": max(latencies),
            "restart_response_matches": True,
            "container_and_secrets_removed": True,
            "scope": "Local fixture only; database and caller outside owner budget",
        }
        report = (
            Path(__file__).resolve().parents[3]
            / ".worktree"
            / (f"workspace-template-query-container-profile-{uuid.uuid4().hex}.json")
        )
        with report.open("x") as output:
            json.dump(profile, output, indent=2)
            output.write("\n")
    finally:
        if actor_id is not None:
            await _remove_actor(maker, actor_id)
