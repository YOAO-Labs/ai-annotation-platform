"""Replay Guide ownership over two separately restricted real containers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import secrets

from sqlalchemy.engine import make_url

from app.config import settings
from tests import project_guide_asset_owner_helpers as helpers
from tests import test_project_guide_asset_owner as replay
from tests import test_project_guide_object_runtime as source_runtime
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _required_env,
    _stop_remove_container,
    _verify_postgres,
    _wait_healthy,
)
from tests.test_project_guide_object_container import _guide_container
from tests.workspace_configuration_helpers import ROOT, configuration_dsn

pytestmark = replay.pytestmark


def _origin(container):
    binding = _inspect_docker(container)["NetworkSettings"]["Ports"]["8172/tcp"]
    assert len(binding) == 1 and binding[0]["HostIp"] == "127.0.0.1"
    return f"http://127.0.0.1:{int(binding[0]['HostPort'])}"


@asynccontextmanager
async def _workspace_container(
    test_engine, tmp_path, *, token, guide_url, guide_token, records
):
    image = _required_env("AAP_WORKSPACE_GUIDE_IMAGE")
    assert image.startswith("aap-workspace:guide-assets-owner-local-")
    image_id = json.loads(_docker("image", "inspect", image))[0]["Id"]
    dsn = make_url(configuration_dsn(test_engine))
    _verify_postgres(dsn)
    postgres = _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID")
    source = records["source"]["container_id"]
    identity = secrets.token_hex(12)
    network = container = None
    peers = []
    directory = tmp_path / identity
    directory.mkdir(mode=0o700)
    internal_dsn = dsn.set(host="guide-postgres", port=5432).render_as_string(
        hide_password=False
    )
    mounts = []
    for name, value in (
        ("database", internal_dsn),
        ("control", token),
        ("guide", guide_token),
    ):
        path = directory / name
        path.write_text(value)
        path.chmod(0o444)
        mounts.extend(
            ("--mount", f"type=bind,source={path},target=/run/secrets/{name},readonly")
        )
    labels = [
        "--label",
        "org.aap.acceptance=module-data-boundary",
        "--label",
        f"org.aap.acceptance-id={identity}",
    ]
    try:
        network = _docker(
            "network", "create", *labels, f"aap-guide-owner-{identity[:12]}"
        )
        for peer, alias in ((postgres, "guide-postgres"), (source, "guide-source")):
            _docker("network", "connect", "--alias", alias, network, peer)
            peers.append(peer)
        environment = {
            "WORKSPACE_HOST": "0.0.0.0",
            "WORKSPACE_PORT": "8172",
            "WORKSPACE_TIMEOUT_SECONDS": "20",
            "WORKSPACE_CONFIGURATION_DATABASE_URL_FILE": "/run/secrets/database",
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": "/run/secrets/control",
            "WORKSPACE_GUIDE_OBJECTS_URL": "http://guide-source:8767",
            "WORKSPACE_GUIDE_OBJECTS_TOKEN_FILE": "/run/secrets/guide",
        }
        args = [
            "create",
            "--name",
            f"aap-guide-owner-{identity[:12]}",
            *labels,
            "--network",
            network,
            "--publish",
            "127.0.0.1::8172",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--health-interval",
            "1s",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            *mounts,
        ]
        for name, value in environment.items():
            args.extend(("--env", f"{name}={value}"))
        container = _docker(*args, image)
        _docker("start", container)
        await _wait_healthy(container, timeout=45)
        observed = _inspect_docker(container)
        host = observed["HostConfig"]
        assert (
            observed["Image"] == image_id
            and observed["Config"]["User"] == "10004:10004"
        )
        assert host["ReadonlyRootfs"] and host["Memory"] == 268435456
        assert host["NanoCpus"] == 1_000_000_000 and host["PidsLimit"] == 64
        assert (
            host["CapDrop"] == ["ALL"]
            and "no-new-privileges:true" in host["SecurityOpt"]
        )
        assert len(observed["Mounts"]) == 3 and all(
            not m["RW"] for m in observed["Mounts"]
        )
        assert not any(
            value.startswith(("ASSETS_SOURCE_", "MINIO_", "AWS_", "DATABASE_URL="))
            for value in observed["Config"]["Env"]
        )
        assert set(
            json.loads(_docker("network", "inspect", network))[0]["Containers"]
        ) == {postgres, source, container}
        records["owner"] = {
            "container_id": container,
            "network_id": network,
            "identity": identity,
            "image_id": image_id,
            "memory_bytes": host["Memory"],
            "cpu_count": 1,
            "no_s3_credentials": True,
            "api_postgres_s3_outside_owner_budget": True,
        }
        yield _origin(container)
        assert _inspect_docker(container)["State"]["OOMKilled"] is False
    finally:
        try:
            if container is not None:
                log = _docker("logs", container)
                if any(value in log for value in (internal_dsn, token, guide_token)):
                    raise AssertionError("Workspace container log retained credentials")
                path = ROOT / f".worktree/project-guide-owner-container-{identity}.log"
                assert not path.exists()
                path.write_text(log)
                path.chmod(0o600)
        finally:
            try:
                if container is not None:
                    _stop_remove_container(container, identity)
            finally:
                for peer in reversed(peers):
                    _docker("network", "disconnect", network, peer)
                if network is not None:
                    owned = json.loads(_docker("network", "inspect", network))[0]
                    assert (
                        owned["Labels"]["org.aap.acceptance-id"] == identity
                        and not owned["Containers"]
                    )
                    _docker("network", "rm", network)
                for name in ("database", "control", "guide"):
                    (directory / name).unlink()


async def test_guide_owner_restricted_containers_replay_restart_and_bounded_signs(
    test_engine, app_module, monkeypatch, tmp_path
):
    records = {}

    @asynccontextmanager
    async def source_launcher(*args, **kwargs):
        async with _guide_container(*args, **kwargs) as (origin, token, record):
            records["source"] = record
            yield origin, token, record

    @asynccontextmanager
    async def owner_launcher(*args, **kwargs):
        async with _workspace_container(*args, **kwargs, records=records) as origin:
            yield origin

    original = replay._request

    async def request_with_probes(client, **kwargs):
        result = await original(client, **kwargs)
        label = kwargs["sample"]["label"]
        owner = records["owner"]
        if label == "upload_complete_head_then_manager_lock_write":
            prior = _origin(owner["container_id"])
            _docker("restart", "--time", "10", owner["container_id"])
            await _wait_healthy(owner["container_id"], timeout=45)
            current = _origin(owner["container_id"])
            monkeypatch.setattr(settings, "workspace_configuration_url", current)
            owner["restart"] = {
                "prior_origin": prior,
                "current_origin": current,
                "reinspected": True,
            }
        elif label == "visible_member_signs_registered_asset":

            async def sign():
                response = await client.get(
                    kwargs["path"],
                    params=kwargs["params"],
                    headers={"Authorization": f"Bearer {kwargs['token']}"},
                )
                assert response.status_code in {200, 503}
                return response.status_code

            started = asyncio.get_running_loop().time()
            results = await asyncio.gather(
                *(sign() for _ in range(16)), return_exceptions=True
            )
            assert all(type(value) is int for value in results) and 200 in results
            owner["sign_profile"] = {
                "requests": 16,
                "success": results.count(200),
                "unavailable": results.count(503),
                "seconds": round(asyncio.get_running_loop().time() - started, 3),
                "production_capacity_or_slo": False,
            }
        return result

    monkeypatch.setattr(source_runtime, "_installed_guide_process", source_launcher)
    monkeypatch.setattr(helpers, "installed_workspace_guide_owner", owner_launcher)
    monkeypatch.setattr(replay, "_request", request_with_probes)
    await replay._replay_mode(test_engine, app_module, monkeypatch, tmp_path, "http")
    assert records["owner"]["restart"]["reinspected"]
    target = ROOT / ".worktree/project-guide-owner-container-observation.json"
    assert not target.exists()
    target.write_text(
        json.dumps(
            {
                "status": "passed",
                "records": records,
                "owned_containers_and_networks_removed": True,
                "all11_frozen_samples_matched": True,
                "limitations": [
                    "Local linux/arm64",
                    "API, PostgreSQL and S3 outside owner resource budget",
                    "No production capacity or SLO",
                ],
            },
            indent=2,
        )
        + "\n"
    )
