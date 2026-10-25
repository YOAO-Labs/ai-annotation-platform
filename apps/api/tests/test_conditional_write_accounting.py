"""Small-object producers retain business behavior and exact physical evidence."""

import asyncio
import gzip
import hashlib
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import (
    avatar,
    avatar_storage,
    comment_attachment_storage,
    conditional_write_accounting as accounting,
    guide_asset_storage,
    media_cache_accounting,
    raster_object_storage,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_s3_target import Target
from tests.test_avatar_storage import record as avatar_record
from tests.test_bug_attachment_accounting import Provider, s3_error
from tests.test_comment_attachment_storage import (
    record as comment_record,
    RAW as COMMENT,
)
from tests.test_guide_asset_storage import record as guide_record, RAW as GUIDE
from tests.test_raster_object_storage import record as raster_record, RAW as RLE
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory


CASES = ("avatar", "comment", "guide", "raster")
MODULES = {
    "avatar": avatar_storage,
    "comment": comment_attachment_storage,
    "guide": guide_asset_storage,
    "raster": raster_object_storage,
}
PRODUCERS = {
    "avatar": "avatar_uploads",
    "comment": "comment_attachment_uploads",
    "guide": "guide_asset_uploads",
    "raster": "raster_mask_objects",
}


def candidate(kind):
    if kind == "avatar":
        data = b"normalized avatar bytes"
        row = avatar_record(
            sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
        )
    elif kind == "comment":
        data, row = COMMENT, comment_record()
    elif kind == "guide":
        data, row = GUIDE, guide_record()
    else:
        data = gzip.compress(RLE, mtime=0)
        row = raster_record(data, "gzip")
    return row, data


def transport(monkeypatch, kind, provider):
    from app.services import storage

    shared = SimpleNamespace(
        meta=SimpleNamespace(
            endpoint_url=provider.meta.endpoint_url,
            config=provider.meta.config,
        )
    )
    shared.head_object = provider.head_object
    shared.get_object = provider.get_object
    service = SimpleNamespace(client=shared)
    monkeypatch.setattr(MODULES[kind], "storage_service", service)
    if kind == "avatar":
        monkeypatch.setattr(avatar, "storage_service", service)
    configs = []

    def create(*, client_config):
        assert client_config is provider.meta.config
        configs.append(client_config)
        return SimpleNamespace(client=provider)

    monkeypatch.setattr(storage, "StorageService", create)
    return shared, configs


async def records(factory):
    async with factory() as db:
        return list(
            await db.scalars(
                select(Mutation).order_by(Mutation.started_at, Mutation.id)
            )
        )


async def physical_write(factory, kind, row, data, client):
    async with factory() as db:
        rec = accounting.recorder(
            db,
            row,
            producer=PRODUCERS[kind],
            purpose="other_platform",
            owner_id=uuid.UUID(hex=row.token) if kind == "avatar" else None,
        )
    failure = None
    try:
        if kind == "avatar":
            await asyncio.to_thread(
                avatar.write_avatar_object, row, data, accounting=rec
            )
        else:
            module = MODULES[kind]
            ref = row.storage_ref
            target = Target(
                LEGACY_LOCATION_ID,
                1,
                None,
                ref["endpoint_url"],
                ref["bucket"],
                "",
                purpose="annotations",
                _legacy_client=client,
            )
            await asyncio.to_thread(
                module.run_io, target, client, "write", row, data, accounting=rec
            )
    except BaseException as exc:
        failure = exc
    return await accounting.finish(rec, failure)


@pytest.mark.parametrize("kind", CASES)
async def test_intent_frozen_owner_and_exact_stored_bytes_before_send(
    accounting_factory, monkeypatch, kind
):
    provider = Provider()
    client, configs = transport(monkeypatch, kind, provider)
    row, data = candidate(kind)
    loop = asyncio.get_running_loop()

    async def pending():
        (fact,) = await records(accounting_factory)
        assert fact.state == "pending" and fact.physical_outcome == "unknown"
        assert fact.owner_id == (
            uuid.UUID(hex=row.token) if kind == "avatar" else row.id
        )
        assert fact.producer == PRODUCERS[kind] and fact.purpose == "other_platform"
        assert fact.evidence["expected_size_bytes"] == len(data)
        assert fact.evidence["expected_sha256"] == hashlib.sha256(data).hexdigest()

    provider.before_put = lambda: asyncio.run_coroutine_threadsafe(
        pending(), loop
    ).result(3)
    assert await physical_write(accounting_factory, kind, row, data, client) is None
    (fact,) = await records(accounting_factory)
    assert fact.state == "confirmed" and fact.current_delta_bytes == len(data)
    assert fact.evidence["final_send_count"] == 1 and fact.evidence["full_readback"]
    assert (
        provider.closed
        and all(body.closed for body in provider.bodies)
        and len(configs) == 1
    )


@pytest.mark.parametrize("kind", CASES)
@pytest.mark.parametrize(
    "uncertain",
    [
        "retry",
        "no_hooks",
        "null",
        "missing",
        "lost_ack",
        "condition",
        "409",
        "bare412",
        "read_version",
    ],
)
async def test_uncertain_submission_never_invents_confirmed_bytes(
    accounting_factory, monkeypatch, kind, uncertain
):
    provider = Provider()
    client, _ = transport(monkeypatch, kind, provider)
    row, data = candidate(kind)
    if uncertain == "retry":
        provider.sends = 2
    elif uncertain == "no_hooks":
        provider.hooks = False
    elif uncertain in {"null", "missing"}:
        provider.version = "null" if uncertain == "null" else None
    elif uncertain == "lost_ack":
        provider.after_put = lambda: (_ for _ in ()).throw(OSError("lost ACK"))
    elif uncertain == "condition":
        provider.params_change = {"IfNoneMatch": None}
    elif uncertain == "409":
        provider.failure = s3_error("ConditionalRequestConflict", 409)
    elif uncertain == "bare412":
        provider.failure = s3_error("412")
    else:

        def replace():
            key = next(iter(provider.current))
            current = provider.current[key]
            provider.objects[key, "foreign-current"] = provider.objects[key, current]
            provider.current[key] = "foreign-current"

        provider.after_put = replace
    failure = await physical_write(accounting_factory, kind, row, data, client)
    if uncertain in {"lost_ack", "409", "bare412"}:
        assert isinstance(failure, Exception)
    else:
        assert failure is None
    (fact,) = await records(accounting_factory)
    assert fact.state == "unknown" and fact.current_delta_bytes is None
    assert provider.closed and all(body.closed for body in provider.bodies)


@pytest.mark.parametrize("kind", CASES)
async def test_only_single_http_412_proves_window_zero(
    accounting_factory, monkeypatch, kind
):
    provider = Provider()
    client, _ = transport(monkeypatch, kind, provider)
    row, data = candidate(kind)
    assert await physical_write(accounting_factory, kind, row, data, client) is None
    provider.closed = False
    failure = await physical_write(accounting_factory, kind, row, data, client)
    assert (failure is None) is (kind == "raster")
    first, second = await records(accounting_factory)
    assert first.id != second.id
    assert second.state == "confirmed" and second.current_delta_bytes == 0
    assert (
        second.physical_outcome == "not_written"
        and second.evidence["final_send_count"] == 1
    )


@pytest.mark.parametrize("kind", CASES)
async def test_intent_failure_prevents_put_and_ordinary_publish_failure_keeps_success(
    accounting_factory, monkeypatch, kind
):
    provider = Provider()
    client, _ = transport(monkeypatch, kind, provider)
    row, data = candidate(kind)
    original = media_cache_accounting.Recorder._begin

    async def broken(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(media_cache_accounting.Recorder, "_begin", broken)
    assert isinstance(
        await physical_write(accounting_factory, kind, row, data, client), Exception
    )
    assert not provider.puts and not await records(accounting_factory)
    monkeypatch.setattr(media_cache_accounting.Recorder, "_begin", original)
    monkeypatch.setattr(media_cache_accounting.accounting, "publish", broken)
    assert await physical_write(accounting_factory, kind, row, data, client) is None
    (fact,) = await records(accounting_factory)
    assert fact.state == "pending" and fact.current_delta_bytes is None
