"""Installed Python and Node owner images use only the two-function credential."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import secrets
import time

import httpx
import pytest
from sqlalchemy.engine import make_url

from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
)
from tests.test_workspace_owner_transfer_live import (
    _cleanup_world,
    _peer,
    _public_transfer,
    _request,
    _seed_world,
    _snapshot,
)
from tests.workspace_owner_transfer_helpers import api_transfer_binding, transfer_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_OWNER_TRANSFER_CONTAINER_LIVE") != "1",
        reason="requires explicitly qualified owner images and owned resources",
    ),
]


@asynccontextmanager
async def _image_owner(test_engine, directory, token, provider):
    prefix = "AAP_WORKSPACE_OWNER_TRANSFER_" + ("NODE_" if provider == "node" else "")
    image = _inspect_docker(os.environ[prefix + "IMAGE"])
    assert (
        image["Config"]["Labels"]["org.aap.acceptance-id"]
        == os.environ[prefix + "IDENTITY"]
    )
    assert image["Config"]["Labels"][
        "org.aap.acceptance"
    ] == "workspace-owner-transfer" + ("-node" if provider == "node" else "")
    assert image["Config"]["User"] == ("node" if provider == "node" else "10004:10004")
    directory.mkdir(parents=True)
    dsn = (
        make_url(transfer_dsn(test_engine))
        .set(host="host.docker.internal")
        .render_as_string(hide_password=False)
    )
    dsn_file, token_file = directory / "database", directory / "control"
    for path, value in ((dsn_file, dsn), (token_file, token)):
        path.write_text(value, encoding="utf-8")
        path.chmod(0o444)
    identity, port = secrets.token_hex(12), 8173 if provider == "node" else 8172
    container = _docker(
        "run",
        "--detach",
        "--name",
        f"aap-owner-transfer-{identity}",
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
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=32m",
        "--publish",
        f"127.0.0.1::{port}",
        "--mount",
        f"type=bind,src={dsn_file},dst=/run/secrets/database,readonly",
        "--mount",
        f"type=bind,src={token_file},dst=/run/secrets/control,readonly",
        "--env",
        "WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE=/run/secrets/database",
        "--env",
        "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE=/run/secrets/control",
        "--env",
        "WORKSPACE_HOST=0.0.0.0",
        "--env",
        f"WORKSPACE_PORT={port}",
        image["Id"],
    ).strip()
    try:
        metadata = _inspect_docker(container)
        mapped = metadata["NetworkSettings"]["Ports"][f"{port}/tcp"][0]["HostPort"]
        origin = f"http://127.0.0.1:{mapped}"
        deadline = time.monotonic() + 35
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while True:
                try:
                    if (await client.get(origin + "/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert _inspect_docker(container)["State"]["Running"]
                assert time.monotonic() < deadline, "Owned image did not become ready"
                await asyncio.sleep(0.1)
            assert (
                await client.post(
                    origin + "/v1/workspace/owner-transfer/execute", json={}
                )
            ).status_code == 401
        assert metadata["HostConfig"]["ReadonlyRootfs"]
        assert metadata["HostConfig"]["Memory"] == 256 * 1024 * 1024
        assert metadata["HostConfig"]["NanoCpus"] == 1_000_000_000
        assert len(metadata["Mounts"]) == 2 and all(
            not mount["RW"] for mount in metadata["Mounts"]
        )
        yield origin
    finally:
        _stop_remove_container(container, identity)


@pytest.mark.parametrize("provider", ["python", "node"])
async def test_owner_image_round_trip_and_restart_replay(
    test_engine, tmp_path, httpx_client, provider
):
    maker, actor, first, second, employee, project = await _seed_world(
        test_engine, "image-" + provider
    )
    token = secrets.token_urlsafe(32)
    request = _request(actor.id, project.id, first.id)
    try:
        async with _image_owner(
            test_engine, tmp_path / "first", token, provider
        ) as origin:
            reply = _peer(origin, token, request)
            assert reply["ok"] is True and reply["reply"]["ok"] is True
            async with api_transfer_binding(url=origin, token=token):
                response = await _public_transfer(
                    httpx_client, project.id, second.id, actor
                )
                assert response.status_code == 200, response.text
            superseded = await _snapshot(maker, project.id, actor.id)
            assert superseded[0] == second.id
        async with _image_owner(
            test_engine, tmp_path / "restart", token, provider
        ) as origin:
            assert _peer(origin, token, request) == reply
            assert await _snapshot(maker, project.id, actor.id) == superseded
    finally:
        await _cleanup_world(
            maker, actor.id, project.id, [actor.id, first.id, second.id, employee.id]
        )
