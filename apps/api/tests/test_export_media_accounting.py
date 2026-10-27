"""Per-object export facts survive business failure and actual native closing."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import export_media_storage as physical
from app.services import media_cache_accounting as media
from app.services import managed_original_reader as reader
from app.services.content_snapshot import ContentError
from app.services.storage import StorageService
from app.services.managed_s3_target import Target
from tests.test_export_media_storage import RAW
from tests.test_export_media_transfer import runtime as runtime
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory


async def facts(factory):
    async with factory() as db:
        return list(await db.scalars(select(Mutation).order_by(Mutation.started_at)))


async def test_producer_pins_exact_address_and_confirms_after_all_native_close(
    runtime, accounting_factory, monkeypatch
):
    original = media.accounting.publish
    actual_put = runtime.client.put_object
    loop = asyncio.get_running_loop()

    async def before_put():
        (pending,) = await facts(accounting_factory)
        assert pending.state == "pending"
        assert pending.object_key.endswith(runtime.snapshot.relative)

    def put(**params):
        asyncio.run_coroutine_threadsafe(before_put(), loop).result(3)
        return actual_put(**params)

    monkeypatch.setattr(runtime.client, "put_object", put)

    async def publish(*args, **kwargs):
        assert all(stage.file.closed for stage in runtime.stages)
        assert runtime.client.close_count == 1
        assert all(body.closed for body in runtime.client.bodies)
        assert all(permit.settled for permit in runtime.permits)
        return await original(*args, **kwargs)

    monkeypatch.setattr(media.accounting, "publish", publish)
    result = await runtime.copy()
    (row,) = await facts(accounting_factory)
    assert row.producer == "export_media_outputs" and row.purpose == "temporary"
    assert row.owner_id == runtime.snapshot.id
    assert (
        row.object_key == physical.prefix_for(row.owner_id) + runtime.snapshot.relative
    )
    assert row.scope_key == media.accounting.scope_key(
        runtime.source.endpoint_url, "saved-export-media"
    )
    assert row.state == "confirmed" and row.current_delta_bytes == len(RAW)
    assert row.evidence["returned_version"] == "1"
    assert row.evidence["full_readback"] and row.evidence["current_head_matches"]
    assert result.size == len(RAW)


@pytest.mark.parametrize("version", [None, "null"])
async def test_missing_or_null_write_version_keeps_observed_write_unknown(
    runtime, accounting_factory, monkeypatch, version
):
    runtime.client.versioned = False
    for name in ("put_object", "head_object", "get_object"):
        original = getattr(runtime.client, name)

        def no_version(_original=original, **params):
            result = _original(**params)
            if version is None:
                result.pop("VersionId", None)
            return result

        monkeypatch.setattr(runtime.client, name, no_version)
    await runtime.copy()
    (row,) = await facts(accounting_factory)
    assert row.state == "unknown" and row.current_delta_bytes is None
    assert row.physical_outcome == "written" and row.physical_size_bytes == len(RAW)


async def test_current_version_replaced_only_in_accounting_observation_stays_unknown(
    runtime, accounting_factory
):
    count = 0

    def changed(value):
        nonlocal count
        count += 1
        return {**value, "VersionId": "replaced"} if count == 3 else value

    runtime.client.mutate_head = changed
    await runtime.copy()
    (row,) = await facts(accounting_factory)
    assert count == 3 and row.state == "unknown"
    assert row.evidence["full_readback"] and not row.evidence["current_head_matches"]
    assert row.current_delta_bytes is None


async def test_returned_old_version_is_not_explained_by_later_readback(
    runtime, accounting_factory, monkeypatch
):
    original = runtime.client.put_object

    def old(**params):
        return {**original(**params), "VersionId": "old"}

    monkeypatch.setattr(runtime.client, "put_object", old)
    with pytest.raises(physical.ExportMediaStorageError):
        await runtime.copy()
    (row,) = await facts(accounting_factory)
    assert row.state == "unknown" and row.evidence["returned_version"] == "old"
    assert row.current_delta_bytes is None


async def test_lost_response_then_412_has_two_independent_physical_windows(
    runtime, accounting_factory, monkeypatch
):
    original = runtime.client.put_object
    lost = True

    def put(**params):
        nonlocal lost
        result = original(**params)
        if lost:
            lost = False
            raise OSError("lost provider response")
        return result

    monkeypatch.setattr(runtime.client, "put_object", put)
    for _ in range(2):
        with pytest.raises(physical.ExportMediaStorageError):
            await runtime.copy()
    first, second = await facts(accounting_factory)
    assert first.state == "unknown" and first.current_delta_bytes is None
    assert second.state == "confirmed" and second.current_delta_bytes == 0
    assert second.physical_outcome == "not_written"
    assert first.evidence["logical_intent_id"] == second.evidence["logical_intent_id"]
    assert first.evidence["attempt_nonce"] != second.evidence["attempt_nonce"]


async def test_intent_refusal_prevents_put_and_keeps_existing_cleanup_known(
    runtime, accounting_factory, monkeypatch
):
    async def refused(*_args, **_kwargs):
        raise OSError("intent unavailable")

    monkeypatch.setattr(media.Recorder, "_begin", refused)
    with pytest.raises(physical.ExportMediaStorageError) as failure:
        await runtime.copy()
    assert not getattr(failure.value, "storage_operation_unsettled", False)
    assert not any(name == "put" for name, _ in runtime.client.calls)
    assert all(permit.settled for permit in runtime.permits)
    assert not await facts(accounting_factory)


@pytest.mark.parametrize("where", ["output_close", "stage_close"])
async def test_native_close_uncertainty_keeps_written_facts_without_delta(
    runtime, accounting_factory, where
):
    if where == "output_close":
        runtime.client.close_failure = OSError("client close unknown")
    else:
        runtime.stage_close_failure = OSError("stage close unknown")
    with pytest.raises(BaseException):
        await runtime.copy()
    (row,) = await facts(accounting_factory)
    assert row.state == "unknown" and row.physical_outcome == "written"
    assert row.current_delta_bytes is None


@pytest.mark.parametrize("boundary", [6, 7])
async def test_later_business_guard_failure_does_not_rollback_physical_fact(
    runtime, accounting_factory, boundary
):
    runtime.revoke_at = boundary
    with pytest.raises(ContentError):
        await runtime.copy()
    (row,) = await facts(accounting_factory)
    assert row.state == "confirmed" and row.current_delta_bytes == len(RAW)


async def test_partial_bundle_failure_retains_each_object_attempt(
    runtime, accounting_factory, monkeypatch
):
    await runtime.copy()
    runtime.snapshot.relative = str(uuid.uuid4()) + ".bin"
    key = physical.prefix_for(runtime.snapshot.id) + runtime.snapshot.relative
    runtime.snapshot._inventory["objects"][key] = {
        "sha256": hashlib.sha256(RAW).hexdigest(),
        "size_bytes": len(RAW),
    }

    def lost(**_params):
        raise OSError("second object response unknown")

    monkeypatch.setattr(runtime.client, "put_object", lost)
    with pytest.raises(physical.ExportMediaStorageError):
        await runtime.copy()
    first, second = await facts(accounting_factory)
    assert first.state == "confirmed" and first.current_delta_bytes == len(RAW)
    assert second.state == "unknown" and second.current_delta_bytes is None
    assert first.owner_id == second.owner_id and first.object_key != second.object_key


async def test_publication_failure_keeps_pending_without_changing_business_result(
    runtime, accounting_factory, monkeypatch
):
    async def failed(*_args, **_kwargs):
        raise OSError("publication unavailable")

    monkeypatch.setattr(media.accounting, "publish", failed)
    assert (await runtime.copy()).size == len(RAW)
    (row,) = await facts(accounting_factory)
    assert row.state == "pending" and row.current_delta_bytes is None


async def test_cancellation_during_publication_drains_then_propagates(
    runtime, accounting_factory, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    original = media.accounting.publish

    async def slow(*args, **kwargs):
        assert runtime.stages[-1].file.closed and runtime.client.close_count == 1
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(media.accounting, "publish", slow)
    pending = asyncio.create_task(runtime.copy())
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    (row,) = await facts(accounting_factory)
    assert row.state == "confirmed" and row.current_delta_bytes == len(RAW)


async def test_default_media_purpose_remains_compatible(accounting_factory):
    recorder = media.Recorder(
        accounting_factory,
        "thumbnail_outputs",
        uuid.uuid4(),
        "http://localhost:9000",
        "saved",
    )
    await asyncio.to_thread(
        recorder.begin,
        "owned/key",
        {"sha256": hashlib.sha256(RAW).hexdigest(), "size_bytes": len(RAW)},
    )
    await recorder.finish(settled=True)
    (row,) = await facts(accounting_factory)
    assert row.purpose == "media_cache"


async def test_schema2_default_target_owns_single_attempt_client(runtime, monkeypatch):
    reference = deepcopy(runtime.snapshot._ref)
    reference.update(schema=2, binding_generation=2)
    runtime.snapshot._ref = reference
    calls = []

    @contextmanager
    def client(**options):
        calls.append(options)
        try:
            yield runtime.client
        finally:
            runtime.client.close()

    owner = physical.resolve_ref(reference)
    target = SimpleNamespace(
        location_id=physical.LEGACY_LOCATION_ID,
        endpoint_url=owner.ref["endpoint_url"],
        bucket=owner.bucket,
        key_prefix="",
        client=client,
    )

    async def saved(*_args):
        return target

    from app.services import export_media_transfer

    monkeypatch.setattr(export_media_transfer, "_io_target", saved)
    await runtime.copy()
    assert calls == [{"single_attempt": True}]


@pytest.mark.parametrize("schema", [1, 2])
async def test_actual_minio_copy_source_records_current_immutable_bytes(
    runtime, accounting_factory, monkeypatch, schema
):
    service = StorageService()
    client = service.client
    bucket = settings.minio_export_bucket
    source_bucket = settings.minio_datasets_bucket
    source_key = "export-accounting-source/" + uuid.uuid4().hex
    output_key = physical.prefix_for(runtime.snapshot.id) + runtime.snapshot.relative
    client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    written_source = client.put_object(Bucket=source_bucket, Key=source_key, Body=RAW)
    runtime.snapshot._ref = physical.make_ref(runtime.snapshot.id, bucket=bucket)
    if schema == 2:
        from app.services import export_media_transfer

        runtime.snapshot._ref.update(schema=2, binding_generation=2)
        target = Target(
            physical.LEGACY_LOCATION_ID,
            1,
            None,
            physical._runtime_endpoint(),
            bucket,
            "",
            purpose="export",
        )

        async def saved(*_args):
            return target

        monkeypatch.setattr(export_media_transfer, "_io_target", saved)
    runtime.source = replace(
        runtime.source, bucket=source_bucket, object_key=source_key
    )
    monkeypatch.setattr(
        reader, "create_client", lambda **_options: StorageService().client
    )
    monkeypatch.setattr(physical, "create_client", lambda: StorageService().client)
    try:
        result = await runtime.copy(inspect=False)
        (row,) = await facts(accounting_factory)
        head = client.head_object(Bucket=bucket, Key=output_key)
        response = client.get_object(
            Bucket=bucket, Key=output_key, VersionId=head["VersionId"]
        )
        with response["Body"] as body:
            assert body.read() == RAW
        assert result.size == head["ContentLength"] == len(RAW)
        assert row.object_key == output_key and row.owner_id == runtime.snapshot.id
        assert row.state == "confirmed" and row.current_delta_bytes == len(RAW)
        assert (
            row.evidence["returned_version"] == head["VersionId"] not in {None, "null"}
        )
    finally:
        for version in client.list_object_versions(
            Bucket=bucket, Prefix=output_key
        ).get("Versions", []):
            client.delete_object(
                Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"]
            )
        client.delete_object(
            Bucket=source_bucket,
            Key=source_key,
            **(
                {"VersionId": written_source["VersionId"]}
                if written_source.get("VersionId")
                else {}
            ),
        )
        client.close()
