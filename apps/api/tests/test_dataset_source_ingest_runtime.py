"""Real Source HTTP/SQL/S3 qualification for synchronous ZIP and bucket scan."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
from io import BytesIO
import os
from pathlib import Path
import time
from unittest.mock import Mock
from uuid import UUID, uuid4
import zipfile

import httpx
import pytest
from sqlalchemy import text

from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _wait_healthy,
)
from tests.test_assets_catalog_runtime import _lost_response_proxy, _wait_public_ready
from tests.test_assets_source_runtime import _picture, _source_origin, _source_stack

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable module-data verifier",
    ),
]


def _zip(entries):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return output.getvalue()


async def _upload(stack, body, operation, actor="employee"):
    return await stack.api.post(
        f"/api/v1/datasets/{stack.world.dataset_id}/items/upload-zip",
        headers={**stack.headers[actor], "Idempotency-Key": str(operation)},
        files={"file": ("samples.zip", body, "application/zip")},
    )


async def _scan(stack, operation, actor="manager"):
    return await stack.api.post(
        f"/api/v1/datasets/{stack.world.dataset_id}/items/scan",
        headers={**stack.headers[actor], "Idempotency-Key": str(operation)},
    )


async def _facts(stack):
    async with stack.world.maker() as db:
        items = (
            (
                await db.execute(
                    text(
                        "SELECT id,file_name,file_path,file_type,file_size,content_hash,"
                        "width,height,scene_id,frame_index,metadata FROM public.dataset_items "
                        "WHERE dataset_id=:id ORDER BY file_path"
                    ),
                    {"id": stack.world.dataset_id},
                )
            )
            .mappings()
            .all()
        )
        counts = []
        for query, identity in (
            (
                "SELECT count(*) FROM public.tasks WHERE project_id=:id",
                stack.world.project_id,
            ),
            (
                "SELECT total_tasks FROM public.projects WHERE id=:id",
                stack.world.project_id,
            ),
            (
                "SELECT file_count FROM public.datasets WHERE id=:id",
                stack.world.dataset_id,
            ),
            (
                "SELECT count(*) FROM public.scenes WHERE dataset_id=:id",
                stack.world.dataset_id,
            ),
            (
                "SELECT count(*) FROM public.dataset_source_ingest_operations WHERE dataset_id=:id",
                stack.world.dataset_id,
            ),
        ):
            counts.append(await db.scalar(text(query), {"id": identity}))
    return [dict(row) for row in items], tuple(counts)


def _objects(stack):
    return {
        value["Key"]
        for page in stack.storage.client.get_paginator("list_objects_v2").paginate(
            Bucket=stack.storage.datasets_bucket,
            Prefix=f"dataset-sources/{stack.world.dataset_id}/",
        )
        for value in page.get("Contents", [])
    }


def _read(stack, key):
    body = stack.storage.client.get_object(
        Bucket=stack.storage.datasets_bucket, Key=key
    )["Body"]
    try:
        return body.read()
    finally:
        body.close()


async def test_zip_remote_replay_preserves_sources_and_plans_all_dataset_items(
    test_engine, app_module, monkeypatch
):
    from app.config import settings
    from app.db.models.dataset import DatasetItem
    from app.services.dataset_source_ingest_delivery import (
        claim_dataset_source_ingest_delivery,
    )
    from app.workers.dataset_import_delivery import _deliver_source_ingest_one

    async with _source_stack(test_engine, app_module, monkeypatch) as stack:
        operation, legacy_id = uuid4(), uuid4()
        async with stack.world.maker.begin() as db:
            name = await db.scalar(
                text("SELECT name FROM datasets WHERE id=:id"),
                {"id": stack.world.dataset_id},
            )
            legacy_key = f"{name}/scene-a/0000.jpg"
            db.add(
                DatasetItem(
                    id=legacy_id,
                    dataset_id=stack.world.dataset_id,
                    file_name="0000.jpg",
                    file_path=legacy_key,
                    file_type="image",
                    content_hash=hashlib.md5(_picture("green")).hexdigest(),
                )
            )
            await db.execute(
                text("UPDATE datasets SET file_count=1 WHERE id=:id"),
                {"id": stack.world.dataset_id},
            )
        await asyncio.to_thread(
            stack.storage.client.put_object,
            Bucket=stack.storage.datasets_bucket,
            Key=legacy_key,
            Body=_picture("green"),
        )
        entries = {
            "scene-a/0001.jpg": _picture("red"),
            "scene-b/0002.jpg": _picture("blue"),
            ".hidden/ignored.jpg": _picture(),
            "../escape.jpg": b"unsafe",
        }
        body = _zip(entries)
        async with httpx.AsyncClient(trust_env=False) as direct:
            denied = await direct.post(
                stack.origin + "/v1/source/upload-zip", content=body
            )
            assert denied.status_code == 401
        async with _lost_response_proxy(stack.origin) as proxy:
            monkeypatch.setattr(settings, "assets_source_url", proxy)
            uncertain = await _upload(stack, body, operation)
            assert uncertain.status_code == 503, uncertain.text
        monkeypatch.setattr(settings, "assets_source_url", stack.origin)
        accepted = await _upload(stack, body, operation)
        assert accepted.status_code == 200, accepted.text
        assert accepted.headers["Idempotency-Key"] == str(operation)
        result = accepted.json()
        assert {
            key: result[key]
            for key in ("added", "deduped", "skipped", "total_in_zip", "linked_tasks")
        } == {
            "added": 2,
            "deduped": 0,
            "skipped": 2,
            "total_in_zip": 3,
            "linked_tasks": 2,
        }
        assert result["errors"] == []
        items, counts = await _facts(stack)
        assert counts == (2, 2, 3, 2, 1)
        assert all(
            item["scene_id"] is not None and item["frame_index"] is not None
            for item in items
        )
        published = [item for item in items if item["id"] != legacy_id]
        for item in published:
            logical = item["metadata"]["source_ingest"]["relpath"]
            assert item["metadata"]["source_ingest"]["operation_id"] == str(operation)
            prefix, dataset, identifier, publication, relative = item[
                "file_path"
            ].split("/", 4)
            assert (prefix, dataset, identifier, relative) == (
                "dataset-sources",
                str(stack.world.dataset_id),
                str(item["id"]),
                logical,
            )
            assert UUID(publication)
            assert item["file_name"] == Path(logical).name
            assert (item["width"], item["height"]) == (19, 11)
            assert (
                await asyncio.to_thread(_read, stack, item["file_path"])
                == entries[logical]
            )
        assert await asyncio.to_thread(_read, stack, legacy_key) == _picture("green")
        before_objects = await asyncio.to_thread(_objects, stack)
        assert len(before_objects) == 2
        conflict = await _upload(stack, _zip({"changed.jpg": _picture()}), operation)
        assert conflict.status_code == 409, conflict.text
        assert await asyncio.to_thread(_objects, stack) == before_objects
        duplicate = await _upload(stack, body, uuid4())
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["added"] == 0 and duplicate.json()["deduped"] == 2
        assert (await _facts(stack))[1] == (2, 2, 3, 2, 2)
        stack.dispatcher.dispatch_thumbnail.assert_not_called()

        # A broker failure retains the durable handoff for its next lease.
        async with stack.world.maker() as db:
            lease = await claim_dataset_source_ingest_delivery(
                db, lease_token=uuid4(), lease_seconds=120
            )
            await db.commit()
            assert lease is not None and lease.operation_id == operation
            failed_dispatcher = Mock()
            failed_dispatcher.dispatch_thumbnail.side_effect = RuntimeError(
                "broker unavailable"
            )
            with pytest.raises(RuntimeError, match="broker unavailable"):
                await _deliver_source_ingest_one(db, lease, failed_dispatcher)
            await db.rollback()
            assert (
                await db.scalar(
                    text(
                        "SELECT delivered_at FROM dataset_source_ingest_deliveries WHERE id=:id"
                    ),
                    {"id": lease.id},
                )
                is None
            )
            await db.execute(
                text(
                    "UPDATE dataset_source_ingest_deliveries SET lease_expires_at=now()-interval '1 second' WHERE id=:id"
                ),
                {"id": lease.id},
            )
            await db.commit()
            again = await claim_dataset_source_ingest_delivery(
                db, lease_token=uuid4(), lease_seconds=120
            )
            await db.commit()
            assert (
                again is not None
                and again.id == lease.id
                and again.lease_token != lease.lease_token
            )
            dispatcher = Mock()
            assert (
                await _deliver_source_ingest_one(db, again, dispatcher) == "delivered"
            )
            dispatcher.dispatch_thumbnail.assert_called_once_with(again.target_id)


async def test_scan_remote_pages_only_the_legacy_prefix_without_scene_inference(
    test_engine, app_module, monkeypatch
):
    async with _source_stack(test_engine, app_module, monkeypatch) as stack:
        async with stack.world.maker.begin() as db:
            name = await db.scalar(
                text("SELECT name FROM datasets WHERE id=:id"),
                {"id": stack.world.dataset_id},
            )
            await db.execute(
                text("UPDATE datasets SET is_temporal=true WHERE id=:id"),
                {"id": stack.world.dataset_id},
            )
        prefix = name + "/"
        objects = {
            f"{prefix}frames/{index:04d}.bin": f"point-{index}".encode()
            for index in range(1001)
        }
        objects[prefix + "nested/picture.jpg"] = _picture()
        objects[prefix + "folder/"] = b""
        foreign_key = f"{name}-foreign/other.bin"
        orphan_key = (
            f"dataset-sources/{stack.world.dataset_id}/{uuid4()}/{uuid4()}/orphan.bin"
        )
        objects[foreign_key] = objects[orphan_key] = b"not part of the scan"

        def seed():
            def put(pair):
                key, value = pair
                stack.storage.client.put_object(
                    Bucket=stack.storage.datasets_bucket, Key=key, Body=value
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(put, objects.items()))
            key = prefix + "opaque/multipart.dat"
            client, bucket = stack.storage.client, stack.storage.datasets_bucket
            upload = client.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
            parts = []
            for number, value in enumerate((b"m" * (5 * 1024 * 1024), b"tail"), 1):
                reply = client.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload,
                    PartNumber=number,
                    Body=value,
                )
                parts.append({"PartNumber": number, "ETag": reply["ETag"]})
            client.complete_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload,
                MultipartUpload={"Parts": parts},
            )
            return key

        multipart_key = await asyncio.to_thread(seed)
        assert (await _scan(stack, uuid4(), "employee")).status_code == 403
        operation = uuid4()
        result = await _scan(stack, operation)
        assert result.status_code == 200, result.text
        assert result.json() == {
            "status": "ok",
            "new_items": 1003,
            "linked_tasks": 1003,
        }
        assert result.headers["Idempotency-Key"] == str(operation)
        items, counts = await _facts(stack)
        assert counts == (1003, 1003, 1003, 0, 1)
        assert all(
            item["file_path"].startswith(prefix)
            and item["scene_id"] is None
            and item["frame_index"] is None
            for item in items
        )
        picture = next(
            item for item in items if item["file_path"].endswith("picture.jpg")
        )
        assert (picture["width"], picture["height"]) == (19, 11)
        multipart = next(item for item in items if item["file_path"] == multipart_key)
        assert (
            multipart["content_hash"] is None
            and multipart["file_size"] == 5 * 1024 * 1024 + 4
        )
        assert not any(item["file_path"] in {foreign_key, orphan_key} for item in items)
        assert await asyncio.to_thread(_read, stack, orphan_key) == objects[orphan_key]
        extra = prefix + "later.bin"
        await asyncio.to_thread(
            stack.storage.client.put_object,
            Bucket=stack.storage.datasets_bucket,
            Key=extra,
            Body=b"added after the receipt",
        )
        assert (await _scan(stack, operation)).json() == result.json()
        assert (await _facts(stack))[1] == counts
        fresh = await _scan(stack, uuid4())
        assert fresh.status_code == 200 and fresh.json()["new_items"] == 1, fresh.text
        assert (await _facts(stack))[1] == (1004, 1004, 1004, 0, 2)


@asynccontextmanager
async def _finish_gate(test_engine):
    function = "aap_assets_v1.finish_dataset_ingest"
    original = "aap_assets_v1._ingest_acceptance_original_finish"
    signature = "uuid,uuid,uuid,jsonb"
    lock_key = int(uuid4().hex[:15], 16)
    gate = await test_engine.connect()
    installed = False
    try:
        async with test_engine.begin() as db:
            await db.exec_driver_sql(
                f"ALTER FUNCTION {function}({signature}) RENAME TO _ingest_acceptance_original_finish"
            )
            await db.exec_driver_sql(
                f"REVOKE EXECUTE ON FUNCTION {original}({signature}) FROM asset_source_client"
            )
            await db.exec_driver_sql(
                f"CREATE FUNCTION {function}(p_operation uuid,p_actor uuid,p_dataset uuid,p_finish jsonb) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ BEGIN PERFORM pg_advisory_xact_lock({lock_key}); RETURN {original}(p_operation,p_actor,p_dataset,p_finish); END $$"
            )
            await db.exec_driver_sql(
                f"REVOKE ALL ON FUNCTION {function}({signature}) FROM PUBLIC"
            )
            await db.exec_driver_sql(
                f"ALTER FUNCTION {function}({signature}) OWNER TO assets_code"
            )
            await db.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION {function}({signature}) TO asset_source_client"
            )
        installed = True
        await gate.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        yield gate, lock_key
    finally:
        await gate.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
        await gate.close()
        if installed:
            async with test_engine.begin() as db:
                await db.exec_driver_sql(f"DROP FUNCTION {function}({signature})")
                await db.exec_driver_sql(
                    f"ALTER FUNCTION {original}({signature}) RENAME TO finish_dataset_ingest"
                )
                await db.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION {function}({signature}) TO asset_source_client"
                )


async def test_zip_process_loss_rolls_back_the_entire_ingest_and_keeps_orphan_bytes(
    test_engine, app_module, monkeypatch
):
    from app.config import settings

    async with _source_stack(test_engine, app_module, monkeypatch) as stack:
        operation, content = uuid4(), _zip({"nested/frame.jpg": _picture()})
        request = None
        async with _finish_gate(test_engine) as (gate, lock_key):
            try:
                request = asyncio.create_task(_upload(stack, content, operation))
                async with asyncio.timeout(25):
                    while True:
                        if request.done():
                            pytest.fail(
                                f"ingest did not reach the SQL gate: {(await request).text}"
                            )
                        async with test_engine.connect() as observer:
                            if await observer.scalar(
                                text(
                                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='asset_source_client' AND wait_event='advisory')"
                                )
                            ):
                                break
                        await asyncio.sleep(0.03)
                orphans = await asyncio.to_thread(_objects, stack)
                assert len(orphans) == 1
                await asyncio.to_thread(
                    _docker, "kill", "--signal", "KILL", stack.container
                )
                await gate.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                )
                interrupted = await asyncio.wait_for(request, 15)
                assert interrupted.status_code == 503
                async with asyncio.timeout(15):
                    while (await _facts(stack))[1] != (0, 0, 0, 0, 0):
                        await asyncio.sleep(0.05)
                assert (await _facts(stack))[0] == []
                async with stack.world.maker() as db:
                    for table in (
                        "dataset_source_ingest_entries",
                        "dataset_source_ingest_deliveries",
                    ):
                        assert (
                            await db.scalar(
                                text(
                                    "SELECT count(*) FROM "
                                    + table
                                    + " WHERE operation_id=:id"
                                ),
                                {"id": operation},
                            )
                            == 0
                        )
                assert (
                    await asyncio.to_thread(_read, stack, next(iter(orphans)))
                    == _picture()
                )
                await asyncio.to_thread(_docker, "start", stack.container)
                await _wait_healthy(stack.container)
                origin = _source_origin(stack.container, stack.image, stack.identity)
                await _wait_public_ready(origin)
                monkeypatch.setattr(settings, "assets_source_url", origin)
                accepted = await _upload(stack, content, operation)
                assert accepted.status_code == 200, accepted.text
                assert accepted.json()["added"] == accepted.json()["linked_tasks"] == 1
                final_objects = await asyncio.to_thread(_objects, stack)
                assert len(final_objects) == 2 and orphans < final_objects
                assert (await _facts(stack))[1] == (1, 1, 1, 1, 1)
            finally:
                if request is not None and not request.done():
                    await gate.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                    )
                    await asyncio.wait_for(request, 15)


def _near_limit_zip(path):
    expected = {}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, size in (
            ("first.dat", 100 * 1024 * 1024),
            ("second.dat", 100 * 1024 * 1024 - 2048),
        ):
            chunk = os.urandom(1024 * 1024)
            digest = hashlib.md5(usedforsecurity=False)
            with archive.open(name, "w") as member:
                remaining = size
                while remaining:
                    body = chunk[: min(len(chunk), remaining)]
                    member.write(body)
                    digest.update(body)
                    remaining -= len(body)
            expected[name] = (size, digest.hexdigest())
    return expected


async def test_zip_near_200mib_within_the_declared_container_memory_and_spool_budget(
    test_engine, app_module, monkeypatch, record_property
):
    async with _source_stack(test_engine, app_module, monkeypatch) as stack:
        limits = await asyncio.to_thread(_inspect_docker, stack.container)
        assert limits["HostConfig"]["Memory"] == 512 * 1024 * 1024
        assert limits["HostConfig"]["NanoCpus"] == 500_000_000
        assert "size=256m" in limits["HostConfig"]["Tmpfs"]["/tmp"]
        path = stack.directory / "near-limit.zip"
        expected = await asyncio.to_thread(_near_limit_zip, path)
        size = path.stat().st_size
        assert 199 * 1024 * 1024 < size < 200 * 1024 * 1024
        started = time.monotonic()
        with path.open("rb") as body:
            result = await _upload(stack, body, uuid4())
        elapsed = time.monotonic() - started
        assert result.status_code == 200, result.text
        assert result.json()["added"] == result.json()["linked_tasks"] == 2
        items, counts = await _facts(stack)
        assert counts == (2, 2, 2, 1, 1)
        for item in items:
            assert (item["file_size"], item["content_hash"]) == expected[
                item["file_name"]
            ]

            def stored_facts():
                body = stack.storage.client.get_object(
                    Bucket=stack.storage.datasets_bucket, Key=item["file_path"]
                )["Body"]
                size, digest = 0, hashlib.md5(usedforsecurity=False)
                try:
                    while part := body.read(1024 * 1024):
                        size += len(part)
                        digest.update(part)
                finally:
                    body.close()
                return size, digest.hexdigest()

            assert await asyncio.to_thread(stored_facts) == expected[item["file_name"]]
        memory_peak = int(
            await asyncio.to_thread(
                _docker, "exec", stack.container, "cat", "/sys/fs/cgroup/memory.peak"
            )
        )
        state = await asyncio.to_thread(_inspect_docker, stack.container)
        assert state["State"]["Running"] and not state["State"]["OOMKilled"]
        assert memory_peak <= 512 * 1024 * 1024
        record_property("zip_bytes", size)
        record_property("elapsed_seconds", round(elapsed, 3))
        record_property("memory_peak_bytes", memory_peak)
