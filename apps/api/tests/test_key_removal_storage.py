"""Actual cache/ZIP cleanup preserves its algorithm and durable per-call windows."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from botocore.config import Config
import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import export_storage as zip_storage
from app.services import image_pyramid_storage as pyramid
from app.services import key_removal_accounting as removal
from app.services import storage as storage_module
from app.services import video_derivative_storage as video
from app.services.managed_s3_target import Target
from tests import live_provider_guard
from tests.test_export_storage import record as zip_record, reference as zip_ref
from tests.test_image_pyramid_storage import reference as pyramid_ref
from tests.test_key_removal_accounting import MarkerProvider
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory
from tests.test_video_derivative_storage import reference as video_ref

KINDS = ("pyramid", "video", "zip")
PRODUCERS = {
    "pyramid": "image_pyramid_removals",
    "video": "video_derivative_removals",
    "zip": "export_zip_removals",
}


class Objects(MarkerProvider):
    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.meta.config = Config(
            connect_timeout=7,
            read_timeout=19,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self.closed, self.before_delete = 0, None
        self.close_failure = None
        self.retain_visible = False

    def delete_object(self, **params):
        if self.before_delete:
            self.before_delete(params)
        self.marker = "marker-" + str(len(self.calls))
        result = super().delete_object(**params)
        if self.retain_visible:
            self.seed("unrelated-new-current", b"still visible", key=params["Key"])
        return result

    def close(self):
        self.closed += 1
        if self.close_failure:
            raise self.close_failure


def record(kind, *, bucket=None, schema=1):
    identifier = uuid.uuid4()
    if kind == "zip":
        ref = zip_ref(bucket=bucket or "saved-zip", output_id=identifier)
        row = zip_record(id=identifier, storage_ref=ref)
    else:
        factory = pyramid_ref if kind == "pyramid" else video_ref
        ref = factory(identifier, bucket=bucket or "saved-cache")
        row = SimpleNamespace(id=identifier, storage_ref=ref, inventory=None)
    if schema == 2:
        ref.update(schema=2, binding_generation=2)
        if kind != "zip":
            ref["key_prefix"] = ""
    return row


def keys(kind, row):
    ref = row.storage_ref
    if kind == "zip":
        return [ref["object_key"]]
    return (
        [ref["prefix"] + name for name in ("overview.webp", "manifest.json")]
        if kind == "pyramid"
        else [ref["prefix"] + name for name in ("poster.webp", "metadata.json")]
    )


async def recorder(factory, kind, row):
    async with factory() as db:
        return removal.recorder(
            db,
            row,
            producer=PRODUCERS[kind],
            purpose="temporary" if kind == "zip" else "media_cache",
        )


async def facts(factory):
    async with factory() as db:
        return list(
            (await db.scalars(select(Mutation).order_by(Mutation.started_at))).all()
        )


def install_zip(monkeypatch, client):
    service = SimpleNamespace(client=client, delete_object=Mock())
    options = []

    def owned(**kwargs):
        options.append(kwargs)
        return SimpleNamespace(client=client)

    monkeypatch.setattr(storage_module, "StorageService", owned)
    monkeypatch.setattr(zip_storage, "storage_service", service)
    return service, options


def remove(kind, row, client, rec, *, target=None):
    if kind == "pyramid":
        return pyramid.delete(SimpleNamespace(client=client), row, accounting=rec)
    if kind == "video":
        return video.delete_output(SimpleNamespace(client=client), row, accounting=rec)
    return zip_storage.delete(row, target=target, accounting=rec)


@pytest.mark.parametrize("kind", KINDS)
async def test_actual_physical_calls_have_durable_intents_before_send(
    accounting_factory, monkeypatch, kind
):
    row = record(kind)
    client = Objects(row.storage_ref["endpoint_url"])
    selected = keys(kind, row)
    for key in selected:
        client.seed("old-version", b"old data retained", key=key)
        client.seed("current-version", b"new", key=key)
    service, options = install_zip(monkeypatch, client)
    rec = await recorder(accounting_factory, kind, row)
    loop = asyncio.get_running_loop()

    def pending_before(params):
        rows = asyncio.run_coroutine_threadsafe(
            facts(accounting_factory), loop
        ).result()
        matches = [fact for fact in rows if fact.object_key == params["Key"]]
        assert len(matches) == 1 and matches[0].state == "pending"
        assert matches[0].expected_version is None
        assert matches[0].owner_id == row.id
        assert params == {"Bucket": row.storage_ref["bucket"], "Key": params["Key"]}

    client.before_delete = pending_before
    assert await asyncio.to_thread(remove, kind, row, client, rec) == (
        None if kind == "zip" else 2
    )
    assert all(fact.state == "pending" for fact in await facts(accounting_factory))
    assert await removal.finish(rec, None) is None
    rows = await facts(accounting_factory)
    assert len(rows) == len(selected)
    assert {fact.object_key for fact in rows} == set(selected)
    assert all(
        fact.state == "confirmed" and fact.current_delta_bytes == -3 for fact in rows
    )
    assert all(
        fact.physical_outcome == "unknown"
        and fact.evidence["delete_marker_created"] is True
        for fact in rows
    )
    assert all(
        fact.producer == PRODUCERS[kind] and fact.owner_id == row.id for fact in rows
    )
    assert all(
        fact.purpose == ("temporary" if kind == "zip" else "media_cache")
        for fact in rows
    )
    assert all(len(client.rows[key]) == 3 for key in selected)
    if kind == "zip":
        assert options == [{"client_config": client.meta.config}]
        service.delete_object.assert_not_called()
        assert client.closed == 1
    else:
        empty = await recorder(accounting_factory, kind, row)
        assert await asyncio.to_thread(remove, kind, row, client, empty) == 0
        assert empty.attempts == []


@pytest.mark.parametrize("kind", ["pyramid", "video"])
@pytest.mark.parametrize("problem", ["next_delete", "next_intent", "final_list"])
async def test_partial_or_final_failure_retains_prior_independent_facts(
    accounting_factory, kind, problem
):
    row = record(kind)
    client = Objects(row.storage_ref["endpoint_url"])
    for key in keys(kind, row):
        client.seed("current-version", b"new", key=key)
    rec = await recorder(accounting_factory, kind, row)
    if problem == "next_delete":

        def second_failure(params):
            if client.calls:
                raise OSError("second delete failed")

        client.before_delete = second_failure
    elif problem == "next_intent":
        begin = rec.begin

        def second_intent(*args):
            if rec.attempts:
                raise OSError("next intent failed")
            return begin(*args)

        rec.begin = second_intent
    else:
        listing = client.list_objects_v2

        def final_list(**params):
            if len(client.calls) == 2:
                raise OSError("final list failed")
            return listing(**params)

        client.list_objects_v2 = final_list
    error = (
        pyramid.ImagePyramidStorageError
        if kind == "pyramid"
        else video.VideoDerivativeStorageError
    )
    with pytest.raises(error) as caught:
        await asyncio.to_thread(remove, kind, row, client, rec)
    assert await removal.finish(rec, caught.value) is caught.value
    rows = await facts(accounting_factory)
    assert rows[0].state == "confirmed" and rows[0].current_delta_bytes == -3
    assert len(client.calls) == (2 if problem == "final_list" else 1)
    assert len(rows) == (1 if problem == "next_intent" else 2)
    if problem == "next_delete":
        assert rows[1].state == "unknown" and rows[1].current_delta_bytes is None


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("problem", ["intent", "unsettled", "observation"])
async def test_uncertainty_preserves_original_business_results(
    accounting_factory, monkeypatch, kind, problem
):
    row = record(kind)
    client = Objects(row.storage_ref["endpoint_url"])
    for key in keys(kind, row):
        client.seed("current-version", b"new", key=key)
    install_zip(monkeypatch, client)
    rec = await recorder(accounting_factory, kind, row)
    if problem == "intent":

        def failed(*args):
            raise OSError("intent failed")

        rec.begin = failed
        with pytest.raises(ValueError):
            await asyncio.to_thread(remove, kind, row, client, rec)
        assert client.calls == [] and await facts(accounting_factory) == []
        return
    if problem == "observation":
        client.fail_list = True
    assert await asyncio.to_thread(remove, kind, row, client, rec) == (
        None if kind == "zip" else 2
    )
    assert await removal.finish(rec, None, settled=problem != "unsettled") is None
    assert all(
        fact.state == "unknown" and fact.current_delta_bytes is None
        for fact in await facts(accounting_factory)
    )


@pytest.mark.parametrize("kind", ["pyramid", "video"])
def test_final_visible_objects_still_fail_original_absence_check(kind):
    row = record(kind)
    client = Objects(row.storage_ref["endpoint_url"])
    client.seed("current", key=keys(kind, row)[0])
    client.retain_visible = True
    from tests.test_export_media_removal_accounting import MemoryRecorder

    rec = MemoryRecorder()
    rec.bucket = row.storage_ref["bucket"]
    with pytest.raises(ValueError) as caught:
        remove(kind, row, client, rec)
    assert caught.value.code.endswith("integrity_mismatch")
    assert "current_delta_bytes" not in rec.attempts[0].evidence


@pytest.mark.parametrize("kind", ["pyramid", "video"])
@pytest.mark.parametrize("single", [False, True])
def test_owned_transport_option_preserves_independent_single_attempt(kind, single):
    calls, client = [], SimpleNamespace()

    @contextmanager
    def owned(**kwargs):
        calls.append(kwargs)
        yield client

    module = pyramid if kind == "pyramid" else video
    target = SimpleNamespace(client=owned, bucket="saved")
    with module.open_storage(
        target=target, single_attempt=single, owned=True
    ) as actual:
        assert actual.client is client
    assert calls == [
        {
            "read_timeout": 30,
            "owned": True,
            **({"single_attempt": True} if single else {}),
        }
    ]


def test_unaccounted_legacy_zip_keeps_existing_helper(monkeypatch):
    row = record("zip")
    client = Objects(row.storage_ref["endpoint_url"])
    service, options = install_zip(monkeypatch, client)
    assert zip_storage.delete(row) is None
    service.delete_object.assert_called_once_with(
        row.storage_ref["object_key"], bucket=row.storage_ref["bucket"]
    )
    assert options == [] and client.calls == [] and client.closed == 0


async def test_owned_zip_client_close_failure_keeps_unknown(
    accounting_factory, monkeypatch
):
    row = record("zip")
    client = Objects(row.storage_ref["endpoint_url"])
    client.seed("current", key=keys("zip", row)[0])
    client.close_failure = OSError("close uncertain")
    install_zip(monkeypatch, client)
    rec = await recorder(accounting_factory, "zip", row)
    with pytest.raises(zip_storage.ExportStorageError) as caught:
        await asyncio.to_thread(remove, "zip", row, client, rec)
    assert caught.value.storage_operation_unsettled is True
    assert await removal.finish(rec, caught.value) is caught.value
    (fact,) = await facts(accounting_factory)
    assert fact.state == "unknown" and fact.current_delta_bytes is None


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("schema", [1, 2])
async def test_real_minio_actual_cleanup_preserves_versions_and_records_marker_delta(
    accounting_factory, kind, schema
):
    purpose = "export" if kind == "zip" else "media-cache"
    bucket = live_provider_guard.owned_bucket(
        settings.minio_export_bucket
        if kind == "zip"
        else settings.minio_media_cache_bucket,
        purpose=purpose,
    )
    row = record(kind, bucket=bucket, schema=schema)
    selected = keys(kind, row)
    client = storage_module.StorageService().client
    rec = await recorder(accounting_factory, kind, row)
    target = (
        Target(
            uuid.UUID(row.storage_ref["location_id"]),
            1,
            None,
            row.storage_ref["endpoint_url"],
            bucket,
            "",
            purpose="export" if kind == "zip" else "media-cache",
            _legacy_client=client,
        )
        if schema == 2
        else None
    )
    try:
        await asyncio.to_thread(
            client.put_bucket_versioning,
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        for key in selected:
            await asyncio.to_thread(
                client.put_object, Bucket=bucket, Key=key, Body=b"older-longer"
            )
            await asyncio.to_thread(
                client.put_object, Bucket=bucket, Key=key, Body=b"new"
            )

        def remove_real():
            if kind == "zip":
                return zip_storage.delete(row, target=target, accounting=rec)
            module = pyramid if kind == "pyramid" else video
            operation = module.delete if kind == "pyramid" else module.delete_output
            if target is not None:
                with module.open_storage(target=target, owned=True) as actual:
                    return operation(actual, row, accounting=rec)
            with video.open_storage() as actual:
                return operation(actual, row, accounting=rec)

        assert await asyncio.to_thread(remove_real) == (None if kind == "zip" else 2)
        await removal.finish(rec, None)
        rows = await facts(accounting_factory)
        assert len(rows) == len(selected)
        assert all(
            fact.state == "confirmed"
            and fact.current_delta_bytes == -3
            and fact.physical_outcome == "unknown"
            for fact in rows
        )
        assert all(
            fact.evidence["final_send_count"] == 1
            and fact.evidence["delete_marker_created"] is True
            and fact.expected_version is None
            for fact in rows
        )
        for key in selected:
            listing = await asyncio.to_thread(
                client.list_object_versions, Bucket=bucket, Prefix=key
            )
            assert len(listing["Versions"]) == 2 and len(listing["DeleteMarkers"]) == 1
            assert listing["DeleteMarkers"][0]["VersionId"] in {
                fact.evidence["returned_version"] for fact in rows
            }
    finally:
        for key in selected:
            listing = await asyncio.to_thread(
                client.list_object_versions, Bucket=bucket, Prefix=key
            )
            for item in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
                if item["Key"] == key:
                    await asyncio.to_thread(
                        client.delete_object,
                        Bucket=bucket,
                        Key=key,
                        VersionId=item["VersionId"],
                    )
        client.close()
