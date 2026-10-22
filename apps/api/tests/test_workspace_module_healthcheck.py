"""Real image health probes on every existing Workspace service port."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import time

import httpx
import pytest
from sqlalchemy.engine import make_url

from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
    _verify_postgres,
)

_SERVICES = (
    ("membership", 8172, "WORKSPACE_MEMBERSHIP"),
    ("owner-transfer", 8173, "WORKSPACE_OWNER_TRANSFER"),
    ("configuration", 8174, "WORKSPACE_CONFIGURATION"),
    ("template-query", 8175, "WORKSPACE_TEMPLATE_QUERY"),
    ("project-query", 8176, "WORKSPACE_PROJECT_QUERY"),
    ("directory-query", 8177, "WORKSPACE_DIRECTORY_QUERY"),
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or not os.getenv("AAP_WORKSPACE_MODULE_IMAGE"),
        reason="requires the owned verifier and explicit Workspace module image",
    ),
]


@pytest.mark.parametrize(
    "service,port,capability", _SERVICES, ids=[s[0] for s in _SERVICES]
)
async def test_image_healthcheck_follows_the_selected_service_port(
    test_engine, tmp_path, service, port, capability
):
    _verify_postgres(test_engine.url)
    image = _inspect_docker(os.environ["AAP_WORKSPACE_MODULE_IMAGE"])
    assert image["Config"]["Labels"]["org.aap.acceptance"] == "workspace-module-owner"
    assert (
        image["Config"]["Labels"]["org.aap.acceptance-id"]
        == os.environ["AAP_WORKSPACE_MODULE_IDENTITY"]
    )
    assert image["Config"]["User"] == "10004:10004"
    selected = make_url(os.environ[f"AAP_MODULE_DATA_{capability}_URL"])
    assert (selected.host, selected.port, selected.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    identity = secrets.token_hex(12)
    network = "aap-workspace-health-" + identity
    pg = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
    pg_name = os.environ["AAP_MODULE_DATA_PG_CONTAINER_NAME"]
    dsn_file, control_file = tmp_path / "database", tmp_path / "control"
    dsn_file.write_text(
        selected.set(host=pg_name, port=5432).render_as_string(hide_password=False)
    )
    control_file.write_text(secrets.token_urlsafe(32))
    for path in (dsn_file, control_file):
        path.chmod(0o444)
    container = None
    connected = created = False
    record = {"service": service, "port": port, "image_id": image["Id"]}
    try:
        _docker(
            "network",
            "create",
            "--label",
            "org.aap.acceptance=module-data-boundary",
            "--label",
            f"org.aap.acceptance-id={identity}",
            network,
        )
        created = True
        _docker("network", "connect", network, pg)
        connected = True
        container = _docker(
            "run",
            "--detach",
            "--pull=never",
            "--network",
            network,
            "--name",
            "aap-workspace-health-" + identity,
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
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--health-interval=1s",
            "--health-timeout=2s",
            "--health-start-period=0s",
            "--health-retries=1",
            "--publish",
            f"127.0.0.1::{port}",
            "--mount",
            f"type=bind,src={dsn_file},dst=/run/secrets/database,readonly",
            "--mount",
            f"type=bind,src={control_file},dst=/run/secrets/control,readonly",
            "--env",
            f"WORKSPACE_PORT={port}",
            "--env",
            f"{capability}_DATABASE_URL_FILE=/run/secrets/database",
            "--env",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE=/run/secrets/control",
            image["Id"],
            timeout=90,
        )
        metadata = _inspect_docker(container)
        assert (
            metadata["Config"]["Healthcheck"]["Test"]
            == image["Config"]["Healthcheck"]["Test"]
        )
        binding = metadata["NetworkSettings"]["Ports"][f"{port}/tcp"]
        assert len(binding) == 1 and binding[0]["HostIp"] == "127.0.0.1"
        origin = "http://127.0.0.1:" + binding[0]["HostPort"]
        deadline = time.monotonic() + 30
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while True:
                try:
                    if (await client.get(origin + "/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert _inspect_docker(container)["State"]["Running"]
                assert time.monotonic() < deadline, (
                    "Workspace policy endpoint not ready"
                )
                await asyncio.sleep(0.1)
        record["ready_http_status"] = 200
        deadline = time.monotonic() + 8
        while True:
            state = _inspect_docker(container)["State"]
            if state["Health"]["Status"] == "healthy" or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.2)
        record["health"] = state["Health"]
        assert state["Health"]["Status"] == "healthy", record
        assert not state["OOMKilled"]
    finally:
        try:
            if container is not None:
                _stop_remove_container(container, identity)
        finally:
            if created:
                details = json.loads(_docker("network", "inspect", network))[0]
                assert details["Labels"]["org.aap.acceptance-id"] == identity
                if connected:
                    _verify_postgres(test_engine.url)
                    _docker("network", "disconnect", network, pg)
                _docker("network", "rm", network)
            for path in (dsn_file, control_file):
                path.unlink(missing_ok=True)
            record["owned_container_network_removed"] = True
            output = (
                Path(__file__).resolve().parents[3]
                / ".worktree"
                / f"workspace-module-health-{service}-{identity}.json"
            )
            output.write_text(json.dumps(record, indent=2) + "\n")
