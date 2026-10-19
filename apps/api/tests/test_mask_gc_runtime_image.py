"""Installed GC owner and cleanup byte owner under bounded local capacity."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from aap_labeling_mask_gc import contract as c
from app.config import settings
from app.services import mask_gc_collection as collection
from tests.test_assets_media_containers import _memory_peak
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _required_env,
    _stop_remove_container,
)
from tests.test_mask_content_containers import _owned_image as _byte_image, _start_role
from tests.test_mask_gc_collection_live import _inventory
from tests.test_mask_gc_native import _native_call
from tests.test_mask_gc_preview_authority import _snapshot


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_MASK_CONTENT_ACCEPTANCE") != "1"
        or not os.getenv("AAP_MASK_GC_IMAGE")
        or not os.getenv("AAP_MASK_CONTENT_IMAGE"),
        reason="requires verifier-owned resources and both installed runtime images",
    ),
]


async def _start_gc(image, identity, resources, byte_origin, byte_token, directory):
    dsn = make_url(_required_env("AAP_MODULE_DATA_LABELING_MASK_GC_URL"))
    assert (
        dsn.username == "labeling_mask_gc_client"
        and dsn.database == resources["database"]
    )
    token = secrets.token_urlsafe(32)
    content = urlsplit(byte_origin)
    environment = {
        "MASK_GC_DATABASE_URL": dsn.set(
            drivername="postgresql", host="host.docker.internal"
        ).render_as_string(hide_password=False),
        "MASK_GC_TOKEN": token,
        "MASK_GC_CONTENT_URL": f"http://host.docker.internal:{content.port}",
        "MASK_GC_CONTENT_TOKEN": byte_token,
        "MASK_GC_POLL_ENABLED": "false",
    }
    envfile = directory / "gc-image.env"
    envfile.touch(mode=0o600)
    envfile.write_text(
        "".join(f"{key}={value}\n" for key, value in environment.items())
    )
    container = None
    try:
        container = _docker(
            "run",
            "--detach",
            "--name",
            f"aap-labeling-mask-gc-{uuid4().hex[:12]}",
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
            "1",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "--publish",
            "127.0.0.1::8771",
            "--env-file",
            str(envfile),
            image,
        ).strip()
        details = _inspect_docker(container)
        assert details["Image"] == image
        assert details["HostConfig"]["ReadonlyRootfs"] is True
        assert details["Config"]["User"] == "10003:10003"
        assert all(
            mount["Type"] == "tmpfs" and mount["Destination"] == "/tmp"
            for mount in details["Mounts"]
        )
        assert not any(
            value.startswith(("REDIS_", "CELERY_", "MINIO_", "MASK_CONTENT_S3_"))
            for value in details["Config"]["Env"]
        )
        ports = details["NetworkSettings"]["Ports"]["8771/tcp"]
        assert len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1"
        origin = f"http://127.0.0.1:{ports[0]['HostPort']}"
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            for _ in range(100):
                assert _inspect_docker(container)["State"]["Running"] is True
                try:
                    if (await client.get(origin + "/health/ready")).status_code == 200:
                        return container, origin, token
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
        pytest.fail(
            "installed Labeling Mask GC image did not become ready", pytrace=False
        )
    except BaseException:
        if container:
            _stop_remove_container(container, identity)
        raise
    finally:
        envfile.unlink(missing_ok=True)


async def test_installed_gc_progresses_past_retained_prefix_with_independent_budgets(
    test_engine,
    tmp_path,
    monkeypatch,
    record_property,
):
    byte_image, identity = _byte_image()
    image_details = json.loads(
        _docker("image", "inspect", _required_env("AAP_MASK_GC_IMAGE"))
    )[0]
    assert image_details["Config"]["Labels"]["org.aap.acceptance-id"] == identity
    image = image_details["Id"]
    containers = []
    async with _inventory(test_engine, retained=1001, orphans=7) as world:
        try:
            cleanup, byte_origin, byte_token = await _start_role(
                "cleanup",
                byte_image,
                identity,
                world["resources"],
                tmp_path,
            )
            containers.append(cleanup)
            gc, origin, token = await _start_gc(
                image,
                identity,
                world["resources"],
                byte_origin,
                byte_token,
                tmp_path,
            )
            containers.append(gc)
            diagnostic = _docker(
                "exec",
                gc,
                "python",
                "-I",
                "-c",
                (
                    "import importlib.util,json,os,sys; from pathlib import Path; "
                    "import aap_labeling_mask_gc,aap_labeling_mask_gc_runtime; "
                    "assert os.getuid()==10003; "
                    "assert Path(aap_labeling_mask_gc.__file__).is_relative_to(sys.prefix); "
                    "assert not Path('/app').exists(); "
                    "assert all(importlib.util.find_spec(m) is None for m in "
                    "('app','sqlalchemy','celery','redis','boto3')); "
                    "print(json.dumps({'installed':True,'application_dependencies':False}))"
                ),
            )
            assert json.loads(diagnostic) == {
                "installed": True,
                "application_dependencies": False,
            }
            monkeypatch.setattr(settings, "labeling_mask_gc_url", origin)
            monkeypatch.setattr(settings, "labeling_mask_gc_token", token)
            monkeypatch.setattr(settings, "labeling_mask_gc_timeout_seconds", 90)
            async with world["maker"]() as db:
                state_before = await _snapshot(db)
            journal_before = (
                world["s3"]
                .list_objects_v2(Bucket=world["bucket"], Prefix="raster-mask-gc/v1/")
                .get("KeyCount", 0)
            )
            started = time.monotonic()
            first = await collection.collect_raster_masks(
                None, c.RunRequest(dry_run=True, scan_pages=1, delete_limit=1)
            )
            assert first.scanned == 1000 and first.scan_complete is False
            assert first.referenced >= 1001 and first.next_after_key is not None
            second = await collection.collect_raster_masks(
                None,
                c.RunRequest(
                    dry_run=True,
                    scan_pages=1,
                    delete_limit=1,
                    after_key=first.next_after_key,
                ),
            )
            assert second.scanned == 8 and second.scan_complete is True
            assert first.eligible == second.eligible == 0
            async with world["maker"]() as db:
                assert await _snapshot(db) == state_before
            assert (
                world["s3"]
                .list_objects_v2(Bucket=world["bucket"], Prefix="raster-mask-gc/v1/")
                .get("KeyCount", 0)
                == journal_before
            )

            # Only this disposable fixture's sweep uses a future cutoff; actual
            # object identity, conditional deletion and journal receipts stay real.
            fixture_worker = uuid4()
            async with world["maker"].begin() as db:
                claim = await _native_call(
                    db, "claim_mask_gc_sweep", {"worker_id": str(fixture_worker)}
                )
                assert claim["ok"] is True and claim["data"]["sweep"] is not None
                changed = await db.execute(
                    text(
                        "UPDATE mask_gc_sweeps SET cutoff=clock_timestamp()+interval '1 day', "
                        "lease_expires_at=clock_timestamp()-interval '1 second' "
                        "WHERE sweep_id=CAST(:sweep AS uuid) AND worker_id=:worker"
                    ),
                    {
                        "sweep": claim["data"]["sweep"]["sweep_id"],
                        "worker": fixture_worker,
                    },
                )
                assert changed.rowcount == 1
            cycles = []
            for pages in (2, 1, 1):
                result = await collection.collect_raster_masks(
                    None, c.RunRequest(scan_pages=pages, delete_limit=3)
                )
                assert (
                    result.data_errors
                    == result.scan_errors
                    == result.finish_errors
                    == result.durable_uncertainties
                    == 0
                )
                assert result.delete_claims <= 3
                cycles.append(result)
            assert cycles[0].scanned == 1008 and cycles[0].eligible == 7
            assert [cycle.deleted for cycle in cycles] == [3, 3, 1]
            from tests.test_asset_storage_cleanup_runtime import _object_exists

            assert all(
                _object_exists(world["s3"], world["bucket"], key)
                for key in world["retained"]
            )
            assert not any(
                _object_exists(world["s3"], world["bucket"], key)
                for key in world["orphans"]
            )
            peaks = [_memory_peak(container) for container in containers]
            assert all(
                peak is not None and 0 < peak < 256 * 1024 * 1024 for peak in peaks
            )
            assert all(
                _inspect_docker(container)["State"]["OOMKilled"] is False
                for container in containers
            )
            record_property("retained_prefix", 1001)
            record_property("eligible_objects", 7)
            record_property("delete_budget", 3)
            record_property("cycle_deleted", "3,3,1")
            record_property("preview_pages", "1000,8")
            record_property("cgroup_memory_peak_bytes", json.dumps(peaks))
            record_property("wall_seconds", time.monotonic() - started)
            record_property("controlled_fixture_sweep_cutoff", True)
            record_property("installed_gc_image", image)
        finally:
            for container in reversed(containers):
                _stop_remove_container(container, identity)
