"""Installed Mask roles under bounded local load, only in verifier-owned resources."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import secrets
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.engine import make_url

from aap_assets_mask_content import codec
from aap_assets_mask_content.contract import MAX_MASK_RUNS, RleContent
from app.config import settings
from tests.test_assets_media_containers import _memory_peak
from tests.test_asset_storage_cleanup_runtime import (
    _container_s3_endpoint,
    _docker,
    _inspect_docker,
    _local_s3_client,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_bucket_owner,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MASK_CONTENT_ACCEPTANCE") != "1"
        or os.environ.get("AAP_MASK_CONTENT_CONTAINER_ACCEPTANCE") != "1"
        or os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKTREE_MODE") != "test",
        reason="requires explicit verifier-owned DB/S3 resources and installed image",
    ),
]


def _owned_image() -> tuple[str, str]:
    image = _required_env("AAP_MASK_CONTENT_IMAGE")
    rows = json.loads(_docker("image", "inspect", image, timeout=15))
    assert len(rows) == 1
    labels = rows[0]["Config"]["Labels"]
    identity = labels.get("org.aap.acceptance-id", "")
    assert labels.get("org.aap.acceptance") == "module-data-boundary"
    assert re.fullmatch(r"[0-9a-f]{24}", identity)
    return rows[0]["Id"], identity


async def _start_role(role, image, identity, resources, directory):
    token = secrets.token_urlsafe(32)
    environment = {
        "MASK_CONTENT_ROLE": role,
        "MASK_CONTENT_TOKEN": token,
        "MASK_CONTENT_S3_ENDPOINT_URL": _container_s3_endpoint(settings),
        "MASK_CONTENT_S3_ACCESS_KEY": _required_env(
            f"AAP_MODULE_DATA_MASK_{role.upper()}_S3_ACCESS_KEY"
        ),
        "MASK_CONTENT_S3_SECRET_KEY": _required_env(
            f"AAP_MODULE_DATA_MASK_{role.upper()}_S3_SECRET_KEY"
        ),
        "MASK_CONTENT_BUCKET": settings.minio_bucket,
    }
    environment_file = directory / f"{role}.env"
    environment_file.touch(mode=0o600)
    environment_file.write_text(
        "".join(f"{key}={value}\n" for key, value in environment.items())
    )
    container = None
    try:
        container = _docker(
            "run",
            "--detach",
            "--name",
            f"aap-mask-{role}-{uuid4().hex[:12]}",
            "--label",
            "org.aap.acceptance=module-data-boundary",
            "--label",
            f"org.aap.acceptance-id={identity}",
            "--label",
            f"org.aap.worktree-owner={resources['owner']}",
            "--add-host",
            "host.docker.internal:host-gateway",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--publish",
            "127.0.0.1::8770",
            "--env-file",
            str(environment_file),
            image,
        ).strip()
        observed = _inspect_docker(container)
        assert observed["Image"] == image
        assert observed["HostConfig"]["ReadonlyRootfs"] is True
        assert observed["Config"]["User"] == "10005:10005"
        assert all(
            mount["Type"] == "tmpfs" and mount["Destination"] == "/tmp"
            for mount in observed["Mounts"]
        )
        assert not any(
            re.match(r"(?:DATABASE|REDIS|CELERY)_", value)
            for value in observed["Config"]["Env"]
        )
        ports = observed["NetworkSettings"]["Ports"]["8770/tcp"]
        assert len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1"
        origin = f"http://127.0.0.1:{ports[0]['HostPort']}"
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            for _ in range(100):
                assert _inspect_docker(container)["State"]["Running"] is True
                try:
                    if (await client.get(origin + "/health/ready")).status_code == 200:
                        return container, origin, token
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
        pytest.fail("installed Mask role did not become ready", pytrace=False)
    except BaseException:
        if container:
            _stop_remove_container(container, identity)
        raise
    finally:
        environment_file.unlink(missing_ok=True)


async def test_installed_mask_roles_bound_large_run_bursts(
    test_engine,
    tmp_path: Path,
    record_property,
):
    url = make_url(_required_env("AAP_MODULE_DATA_LEGACY_RUNTIME_URL"))
    assert url.host == "127.0.0.1" and url.database.endswith("_test")
    resources = _verified_resource_manifest(test_engine, url)
    bucket = resources["buckets"]["MINIO_BUCKET"]
    assert settings.minio_bucket == bucket
    owner = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    _verify_bucket_owner(owner, bucket, resources["owner"])
    image, identity = _owned_image()
    containers = []
    try:
        content, content_origin, content_token = await _start_role(
            "content",
            image,
            identity,
            resources,
            tmp_path,
        )
        containers.append(content)
        cleanup, cleanup_origin, cleanup_token = await _start_role(
            "cleanup",
            image,
            identity,
            resources,
            tmp_path,
        )
        containers.append(cleanup)
        rle = RleContent(
            encoding="coco_rle",
            size=(8192, 8192),
            counts=tuple([67] * (MAX_MASK_RUNS - 1) + [108931]),
        )
        expected = codec.build_rle_reference(rle)
        raw = b'{"content":' + codec.canonical_rle_bytes(rle) + b"}"
        headers = {
            "Authorization": "Bearer " + content_token,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        content_origin + "/v1/mask-content/put",
                        content=raw,
                        headers=headers,
                    )
                    for _ in range(4)
                )
            )
            statuses = [response.status_code for response in responses]
            assert set(statuses) <= {200, 503} and 200 in statuses and 503 in statuses
            reference = next(
                response.json()["data"]["reference"]
                for response in responses
                if response.status_code == 200
            )
            assert reference["sha256"] == expected.sha256
            loaded = await client.post(
                content_origin + "/v1/mask-content/get",
                json={"reference": reference},
                headers=headers,
            )
            assert loaded.status_code == 200
            actual = RleContent.model_validate(loaded.json()["data"]["content"])
            assert codec.build_rle_reference(actual) == expected
            forbidden = await client.post(
                content_origin + "/v1/mask-content/list",
                json={"limit": 1},
                headers=headers,
            )
            assert forbidden.status_code == 403
            cleanup_headers = {"Authorization": "Bearer " + cleanup_token}
            page = await client.post(
                cleanup_origin + "/v1/mask-content/list",
                json={"limit": 1000},
                headers=cleanup_headers,
            )
            assert page.status_code == 200
            candidate = next(
                item
                for item in page.json()["data"]["items"]
                if item["key"] == expected.object_key
            )
            removed = await client.post(
                cleanup_origin + "/v1/mask-content/delete",
                headers=cleanup_headers,
                json={
                    "object_key": candidate["key"],
                    "etag": candidate["etag"],
                    "last_modified": candidate["last_modified"],
                },
            )
            assert (
                removed.status_code == 200 and removed.json()["data"]["deleted"] is True
            )
        peaks = [_memory_peak(container) for container in containers]
        assert all(peak is not None and 0 < peak < 256 * 1024 * 1024 for peak in peaks)
        assert all(
            _inspect_docker(container)["State"]["OOMKilled"] is False
            for container in containers
        )
        record_property("max_runs", MAX_MASK_RUNS)
        record_property("burst_statuses", json.dumps(statuses))
        record_property("cgroup_memory_peak_bytes", json.dumps(peaks))
        record_property("installed_image", image)
        record_property("no_database_or_redis_credentials", True)
    finally:
        for container in reversed(containers):
            _stop_remove_container(container, identity)
