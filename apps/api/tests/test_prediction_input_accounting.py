"""The normal producer emits physical evidence without changing consumption."""

from unittest.mock import AsyncMock

from sqlalchemy import select

from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import (
    storage_usage_accounting as accounting,
)
from tests.test_prediction_inputs import (
    input_world as input_world,
    open_inputs,
    snapshot_image,
)


async def mutations(world):
    async with world.factory() as db:
        return list(await db.scalars(select(Mutation).order_by(Mutation.started_at)))


async def test_successful_create_and_exact_delete_emit_real_opposite_bytes(input_world):
    world = input_world
    inputs = await open_inputs(world)
    snapshot = await snapshot_image()
    size = snapshot.size
    await inputs.upload_snapshot(snapshot)
    created = await mutations(world)
    assert len(created) == 1 and created[0].current_delta_bytes == size
    assert created[0].physical_outcome == "written"
    await inputs.close()
    rows = await mutations(world)
    assert len(rows) == 2
    assert rows[1].physical_outcome == "deleted"
    assert rows[1].current_delta_bytes == -size


async def test_current_replacement_keeps_version_fact_but_never_counts_positive(
    input_world,
):
    world = input_world
    inputs = await open_inputs(world)

    def replace():
        from tests.test_prediction_inputs import MemoryStorage

        key = next(iter(world.objects))
        MemoryStorage.seed(world, *key, b"replacement", {})

    world.after_put = replace
    await inputs.upload_snapshot(await snapshot_image())
    row = (await mutations(world))[0]
    assert row.physical_outcome == "written" and row.current_delta_bytes is None
    assert row.state == "unknown"
    world.after_put = None
    await inputs.close()


async def test_publication_failure_keeps_business_success_and_durable_pending(
    input_world, monkeypatch
):
    world = input_world
    inputs = await open_inputs(world)
    publish = accounting.publish
    monkeypatch.setattr(accounting, "publish", AsyncMock())
    url = await inputs.upload_snapshot(await snapshot_image())
    assert "versionId=" in url
    row = (await mutations(world))[0]
    assert row.state == "pending" and row.current_delta_bytes is None
    monkeypatch.setattr(accounting, "publish", publish)
    await inputs.close()


async def test_ambiguous_put_then_412_reuse_never_reconstructs_a_positive_delta(
    input_world,
):
    from types import SimpleNamespace
    import pytest

    world = input_world
    inputs = await open_inputs(world)
    world.fail_put = True
    with pytest.raises(Exception):
        await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"crop")
    world.fail_put = False
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"crop")
    rows = await mutations(world)
    assert len(rows) == 2 and rows[0].state == "unknown"
    assert rows[0].current_delta_bytes is None
    assert (
        rows[1].current_delta_bytes == 0 and rows[1].physical_outcome == "not_written"
    )
    await inputs.close()


async def test_partial_delete_keeps_first_confirmed_negative(input_world, monkeypatch):
    from types import SimpleNamespace
    from tests.test_prediction_inputs import MemoryStorage

    world = input_world
    inputs = await open_inputs(world)
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"first")
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 1, b"second")
    original = MemoryStorage.delete_object

    def partial(self, **params):
        if self.world.deletions:
            raise OSError("second object unavailable")
        return original(self, **params)

    monkeypatch.setattr(MemoryStorage, "delete_object", partial)
    await inputs.close()
    deleted = [row for row in await mutations(world) if row.kind == "delete"]
    assert len(deleted) == 2
    assert deleted[0].physical_outcome == "deleted" and deleted[
        0
    ].current_delta_bytes in {-5, -6}
    assert deleted[1].current_delta_bytes is None and deleted[1].state == "unknown"
    assert len(world.objects) == 1


async def test_cancelled_upload_drains_and_records_actual_success(input_world):
    import asyncio
    import threading
    from types import SimpleNamespace
    import pytest

    world = input_world
    inputs = await open_inputs(world)
    world.put_started, world.put_release = threading.Event(), threading.Event()
    pending = asyncio.create_task(
        inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"actual")
    )
    try:
        assert await asyncio.to_thread(world.put_started.wait, 3)
        assert (await mutations(world))[0].state == "pending"
        pending.cancel()
        world.put_release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        row = (await mutations(world))[0]
        assert row.physical_outcome == "written" and row.current_delta_bytes == 6
    finally:
        world.put_release.set()
        await inputs.close()


async def test_explicit_storage_is_shared_by_prepare_delete_and_true_close(
    input_world, monkeypatch
):
    from types import SimpleNamespace
    from tests.test_prediction_inputs import MemoryStorage
    from app.services import prediction_inputs as producer

    world = input_world
    inputs = await open_inputs(world)
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"exact")
    await inputs._seal()
    provided = MemoryStorage(world)

    def forbidden():
        raise AssertionError("explicit adapter must serve both phases")

    monkeypatch.setattr(producer, "make_storage", forbidden)
    assert (
        await producer.purge_input(world.factory, inputs.record.id, storage=provided)
        == "removed"
    )
    assert provided.closed and len(world.deletions) == 1
    assert (await mutations(world))[-1].current_delta_bytes == -5
    await inputs.close()


async def test_retry_412_preserves_the_already_known_physical_write(
    input_world, monkeypatch
):
    from types import SimpleNamespace
    import pytest
    from tests.test_prediction_inputs import MemoryStorage

    world = input_world
    inputs = await open_inputs(world)
    original = MemoryStorage.get_object
    failed = False

    def fail_once(self, **params):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("readback network failure")
        return original(self, **params)

    monkeypatch.setattr(MemoryStorage, "get_object", fail_once)
    with pytest.raises(Exception):
        await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
    first = (await mutations(world))[0]
    assert first.physical_outcome == "written" and first.evidence["returned_version"]
    version = first.evidence["returned_version"]
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
    latest = (await mutations(world))[0]
    assert (
        latest.physical_outcome == "written"
        and latest.evidence["returned_version"] == version
    )
    assert latest.current_delta_bytes is None
    await inputs.close()


async def test_delete_close_failure_cannot_publish_deleted_owner(
    input_world, monkeypatch
):
    from types import SimpleNamespace
    import pytest
    from tests.test_prediction_inputs import MemoryStorage, get_record

    world = input_world
    inputs = await open_inputs(world)
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
    original = MemoryStorage.close
    failed = False

    def close_once(self):
        nonlocal failed
        if world.deletions and not failed:
            failed = True
            raise OSError("true final close failure")
        return original(self)

    monkeypatch.setattr(MemoryStorage, "close", close_once)
    with pytest.raises(Exception):
        await inputs.close()
    row = await get_record(world, inputs.record.id)
    assert row.state == "cleanup_pending" and row.uncertain_write
    negative = [item for item in await mutations(world) if item.kind == "delete"]
    assert len(negative) == 1 and negative[0].current_delta_bytes is None


async def test_owned_live_minio_current_versions_and_exact_physical_deletion(
    input_world, monkeypatch
):
    import os
    from pathlib import Path
    import json
    from types import SimpleNamespace
    import pytest
    from app.config import settings
    from app.services import prediction_inputs as producer
    from app.services.storage import StorageService

    if os.environ.get("AAP_WORKTREE_MODE") != "test":
        pytest.skip("live object mutation requires launcher-owned test mode")
    world = input_world
    manifest = json.loads(
        (Path(__file__).parents[3] / ".worktree/test/resources.json").read_text()
    )["resources"]
    client = StorageService().client
    bucket = settings.minio_import_bucket
    tags = {
        item["Key"]: item["Value"]
        for item in client.get_bucket_tagging(Bucket=bucket)["TagSet"]
    }
    assert tags.get("aap-worktree-owner") == manifest["owner"]
    assert bucket == manifest["buckets"]["MINIO_IMPORT_BUCKET"]
    client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    monkeypatch.setattr(producer, "make_storage", StorageService)
    inputs = await open_inputs(world)
    try:
        await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"first-live")
        created = (await mutations(world))[0]
        assert (
            created.current_delta_bytes == 10 and created.physical_outcome == "written"
        )
        versions = client.list_object_versions(Bucket=bucket, Prefix=inputs.prefix)
        assert len(versions.get("Versions", [])) == 1
        saved = versions["Versions"][0]
        replacement = client.put_object(
            Bucket=bucket, Key=saved["Key"], Body=b"external-replacement"
        )
        assert not accounting.current_matches(
            client, bucket, saved["Key"], saved["VersionId"], 10
        )
        client.delete_object(
            Bucket=bucket, Key=saved["Key"], VersionId=replacement["VersionId"]
        )
        # Removing the newer version really frees that version, but the older
        # current object is exposed: it is not a current-objects negative.
        assert not accounting.deleted_current(
            client, bucket, saved["Key"], replacement["VersionId"]
        )
        assert accounting.current_matches(
            client, bucket, saved["Key"], saved["VersionId"], 10
        )
        await inputs.close()
        deleted = [row for row in await mutations(world) if row.kind == "delete"]
        assert len(deleted) == 1 and deleted[0].current_delta_bytes == -10
        assert not client.list_objects_v2(Bucket=bucket, Prefix=inputs.prefix).get(
            "Contents"
        )
        assert not client.list_object_versions(Bucket=bucket, Prefix=inputs.prefix).get(
            "Versions"
        )
        retry = await open_inputs(world)
        publish = retry._publish
        monkeypatch.setattr(
            retry,
            "_publish",
            AsyncMock(side_effect=OSError("business publication unavailable")),
        )
        try:
            with pytest.raises(Exception):
                await retry.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
            monkeypatch.setattr(retry, "_publish", publish)
            first = [
                row for row in await mutations(world) if row.owner_id == retry.record.id
            ][0]
            client.delete_object(
                Bucket=bucket,
                Key=first.object_key,
                VersionId=first.evidence["returned_version"],
            )
            await retry.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
            attempts = [
                row for row in await mutations(world) if row.owner_id == retry.record.id
            ]
            assert [row.current_delta_bytes for row in attempts] == [4, 4]
            assert (
                attempts[0].evidence["returned_version"]
                != attempts[1].evidence["returned_version"]
            )
        finally:
            await retry.close()
            page = client.list_object_versions(Bucket=bucket, Prefix=retry.prefix)
            for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
                client.delete_object(
                    Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"]
                )
    finally:
        await inputs.close()
        # Exact test-owner prefix only, including any retained failure versions.
        page = client.list_object_versions(Bucket=bucket, Prefix=inputs.prefix)
        for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
            client.delete_object(
                Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"]
            )
        client.close()


async def test_confirmed_write_business_failure_then_412_adds_no_bytes(
    input_world, monkeypatch
):
    from types import SimpleNamespace
    import pytest

    world = input_world
    inputs = await open_inputs(world)
    publish = inputs._publish
    monkeypatch.setattr(
        inputs,
        "_publish",
        AsyncMock(side_effect=OSError("business acknowledgement failure")),
    )
    with pytest.raises(Exception):
        await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
    monkeypatch.setattr(inputs, "_publish", publish)
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
    rows = await mutations(world)
    assert [row.current_delta_bytes for row in rows] == [4, 0]
    assert rows[1].evidence["logical_intent_id"] == str(rows[0].id)
    assert len(next(iter(world.versions.values()))) == 1
    await inputs.close()


async def test_external_removal_then_actual_retry_creation_is_a_new_fact(
    input_world, monkeypatch
):
    from types import SimpleNamespace
    import pytest
    from tests.test_prediction_inputs import MemoryStorage

    world = input_world
    inputs = await open_inputs(world)
    publish = inputs._publish
    monkeypatch.setattr(
        inputs,
        "_publish",
        AsyncMock(side_effect=OSError("business acknowledgement failure")),
    )
    with pytest.raises(Exception):
        await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
    bucket, key = next(iter(world.objects))
    external = MemoryStorage(world)
    external.delete_object(
        Bucket=bucket,
        Key=key,
        VersionId=world.current_versions[bucket, key],
        IfMatch=external._head(bucket, key)["ETag"],
    )
    external.close()
    monkeypatch.setattr(inputs, "_publish", publish)
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
    rows = await mutations(world)
    assert [row.current_delta_bytes for row in rows] == [4, 4]
    assert rows[0].evidence["returned_version"] != rows[1].evidence["returned_version"]
    assert rows[1].evidence["logical_intent_id"] == str(rows[0].id)
    await inputs.close()


async def test_unknown_written_version_external_removal_does_not_hide_new_creation(
    input_world, monkeypatch
):
    from types import SimpleNamespace
    import pytest
    from tests.test_prediction_inputs import MemoryStorage

    world = input_world
    inputs = await open_inputs(world)
    read = MemoryStorage.get_object
    failed = False

    def fail_once(self, **params):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("unknown first readback")
        return read(self, **params)

    monkeypatch.setattr(MemoryStorage, "get_object", fail_once)
    with pytest.raises(Exception):
        await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
    first = (await mutations(world))[0]
    assert first.physical_outcome == "written" and first.state == "unknown"
    bucket, key = next(iter(world.objects))
    external = MemoryStorage(world)
    external.delete_object(
        Bucket=bucket,
        Key=key,
        VersionId=world.current_versions[bucket, key],
        IfMatch=external._head(bucket, key)["ETag"],
    )
    external.close()
    await inputs.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
    rows = await mutations(world)
    assert [row.current_delta_bytes for row in rows] == [None, 4]
    assert (
        rows[0].physical_outcome == "written"
        and rows[0].evidence["returned_version"] == first.evidence["returned_version"]
    )
    assert rows[0].evidence["returned_version"] != rows[1].evidence["returned_version"]
    assert rows[0].evidence["logical_group"] == rows[1].evidence["logical_group"]
    await inputs.close()


async def test_concurrent_conditional_requests_have_separate_durable_windows(
    input_world,
):
    import asyncio
    import threading
    from types import SimpleNamespace
    from app.services import prediction_inputs as producer

    world = input_world
    first = await open_inputs(world)
    second = producer.PredictionInputs(world.factory, first.record)
    world.put_started, world.put_release = threading.Event(), threading.Event()
    calls = [
        asyncio.create_task(
            first.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
        )
    ]
    try:
        # Let the first reservation commit before issuing the concurrent retry;
        # allocation's existing NOWAIT guard is a separate business contract.
        assert await asyncio.to_thread(world.put_started.wait, 3)
        calls.append(
            asyncio.create_task(
                second.upload_crop(SimpleNamespace(id=world.task_id), 0, b"same")
            )
        )
        for _ in range(100):
            if len(world.puts) == 2:
                break
            await asyncio.sleep(0.02)
        assert len(world.puts) == 2
        pending = await mutations(world)
        assert len(pending) == 2 and all(row.state == "pending" for row in pending)
        assert len({row.evidence["attempt_nonce"] for row in pending}) == 2
        world.put_release.set()
        await asyncio.gather(*calls, return_exceptions=True)
        rows = await mutations(world)
        assert sorted(row.current_delta_bytes for row in rows) == [0, 4]
        assert all(row.finished_at is not None for row in rows)
        assert len(next(iter(world.versions.values()))) == 1
    finally:
        world.put_release.set()
        await asyncio.gather(*calls, return_exceptions=True)
        await first.close()
        await second.close()
