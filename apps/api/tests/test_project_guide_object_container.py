"""Run the real guide byte driver in the existing restricted Source image."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import secrets
from urllib.parse import urlsplit

import httpx
import pytest

from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _required_env,
    _stop_remove_container,
    _wait_healthy,
)
from tests.test_project_guide_object_runtime import (
    ROOT,
    _exercise_guide_runtime,
    pytestmark as _guide_runtime_marks,
)

pytestmark = _guide_runtime_marks


@asynccontextmanager
async def _guide_container(tmp_path, *, endpoint, bucket, access_key, secret_key):
    image = _required_env("AAP_ASSETS_GUIDE_IMAGE")
    assert image.startswith("aap-assets-source:guide-object-local-")
    image_id = json.loads(_docker("image", "inspect", image))[0]["Id"]
    identity = secrets.token_hex(12)
    token = secrets.token_urlsafe(32)
    directory = tmp_path / identity
    directory.mkdir(mode=0o700)
    mounts = []
    for name, value in (
        ("token", token),
        ("access", access_key),
        ("secret", secret_key),
    ):
        path = directory / name
        path.write_text(value)
        # Only this file is mounted. The private host parent remains mode0700.
        path.chmod(0o444)
        mounts.extend(
            ("--mount", f"type=bind,source={path},target=/run/secrets/{name},readonly")
        )
    parsed = urlsplit(endpoint)
    assert parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    internal_endpoint = f"{parsed.scheme}://host.docker.internal:{parsed.port}"
    environment = {
        "ASSETS_SOURCE_MODE": "guide-only",
        "ASSETS_SOURCE_TOKEN_FILE": "/run/secrets/token",
        "ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT": "4",
        "ASSETS_SOURCE_GUIDE_S3_ENDPOINT": internal_endpoint,
        "ASSETS_SOURCE_GUIDE_S3_PUBLIC_URL": "/minio",
        "ASSETS_SOURCE_GUIDE_S3_BUCKET": bucket,
        "ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY_FILE": "/run/secrets/access",
        "ASSETS_SOURCE_GUIDE_S3_SECRET_KEY_FILE": "/run/secrets/secret",
    }
    labels = [
        "--label",
        "org.aap.acceptance=module-data-boundary",
        "--label",
        f"org.aap.acceptance-id={identity}",
    ]
    network = None
    container = None
    try:
        network = _docker(
            "network", "create", *labels, f"aap-guide-object-{identity[:12]}"
        )
        arguments = [
            "create",
            "--name",
            f"aap-guide-object-{identity[:12]}",
            *labels,
            "--network",
            network,
            "--add-host",
            "host.docker.internal:host-gateway",
            "--publish",
            "127.0.0.1::8767",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--health-interval",
            "1s",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            *mounts,
        ]
        for name, value in environment.items():
            arguments.extend(("--env", f"{name}={value}"))
        arguments.append(image)
        container = _docker(*arguments)
        _docker("start", container)
        await _wait_healthy(container, timeout=45)
        observed = _inspect_docker(container)
        config, host = observed["Config"], observed["HostConfig"]
        assert observed["Image"] == image_id
        assert config["User"] == "10003:10003"
        assert host["ReadonlyRootfs"] is True and host["Memory"] == 256 * 1024 * 1024
        assert host["NanoCpus"] == 1_000_000_000 and host["PidsLimit"] == 64
        assert (
            host["CapDrop"] == ["ALL"]
            and "no-new-privileges:true" in host["SecurityOpt"]
        )
        assert len(observed["Mounts"]) == 3 and all(
            not mount["RW"] for mount in observed["Mounts"]
        )
        assert not any(
            "DATABASE_URL=" in value
            or value.startswith(("MINIO_", "AWS_", "CELERY_", "REDIS_"))
            for value in config["Env"]
        )
        members = json.loads(_docker("network", "inspect", network))[0]["Containers"]
        assert set(members) == {container}
        bindings = observed["NetworkSettings"]["Ports"]["8767/tcp"]
        assert len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1"
        origin = f"http://127.0.0.1:{int(bindings[0]['HostPort'])}"
        yield (
            origin,
            token,
            {
                "container_id": container,
                "network_id": network,
                "identity": identity,
                "image_id": image_id,
                "memory_bytes": host["Memory"],
                "cpu_count": 1,
                "guide_s3_outside_container_budget": True,
                "no_database_credentials": True,
            },
        )
        assert _inspect_docker(container)["State"]["OOMKilled"] is False
    finally:
        try:
            if container is not None:
                log = _docker("logs", container)
                if any(value in log for value in (token, access_key, secret_key)):
                    pytest.fail("Guide service log contains private credentials")
                target = (
                    ROOT / f".worktree/project-guide-object-container-{identity}.log"
                )
                assert not target.exists()
                target.write_text(log)
                target.chmod(0o600)
        finally:
            try:
                if container is not None:
                    _stop_remove_container(container, identity)
            finally:
                if network is not None:
                    details = json.loads(_docker("network", "inspect", network))[0]
                    assert details["Labels"]["org.aap.acceptance-id"] == identity
                    assert not details["Containers"]
                    _docker("network", "rm", network)
                for name in ("token", "access", "secret"):
                    (directory / name).unlink()


async def _restart_guide_container(origin, token, record):
    container = record["container_id"]
    owned = _inspect_docker(container)
    assert owned["Config"]["Labels"]["org.aap.acceptance-id"] == record["identity"]
    _docker("restart", "--time", "10", container)
    await _wait_healthy(container, timeout=45)
    observed = _inspect_docker(container)
    assert observed["Image"] == record["image_id"]
    binding = observed["NetworkSettings"]["Ports"]["8767/tcp"]
    assert len(binding) == 1 and binding[0]["HostIp"] == "127.0.0.1"
    current = f"http://127.0.0.1:{int(binding[0]['HostPort'])}"
    async with httpx.AsyncClient(trust_env=False) as client:
        assert (await client.get(current + "/ready")).status_code == 200
    record["restart"] = {
        "prior_origin": origin,
        "current_origin": current,
        "ready": True,
    }
    return current


async def test_guide_only_restricted_container_scoped_s3_and_storage_failure(
    test_engine, tmp_path
):
    await _exercise_guide_runtime(
        test_engine,
        tmp_path,
        launch=_guide_container,
        observation_name="project-guide-object-container-restart-observation.json",
        after_upload=_restart_guide_container,
    )
