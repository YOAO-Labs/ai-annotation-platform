"""Physical creation accounting uses durable SQL and the existing native writer."""

import asyncio
import io
import threading
import uuid

import pytest
from sqlalchemy import select, text

from app.db.models.dataset import DatasetItem
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import managed_original_creation as service
from app.services import storage_usage_accounting as accounting
from app.services.content_snapshot import ContentError
from tests.test_managed_original_creation import (
    allocation,
    client as _client,
    create,
    creation_db as _creation_db,
)

creation_db = _creation_db
client = _client


async def mutations(db):
    async with service.storage_access.engine(db).connect() as connection:
        return (await connection.execute(select(Mutation.__table__))).mappings().all()


@pytest.mark.parametrize("data", [b"", b"original"])
async def test_atomic_writing_intent_precedes_final_io_and_publication_follows_close(
    creation_db, client, data
):
    db = creation_db
    row = await allocation(db)
    client.versioned = True
    loop = asyncio.get_running_loop()
    seen = []

    def check_pending():
        facts = asyncio.run_coroutine_threadsafe(mutations(db), loop).result(3)
        current = asyncio.run_coroutine_threadsafe(
            service._read(db, row["id"]), loop
        ).result(3)
        assert len(facts) == 1 and facts[0].state == "pending"
        seen.append(current["status"])

    def hook(operation, params):
        if operation in {"complete", "put"}:
            check_pending()
            assert seen[-1] == "writing"

    close = client.close

    def close_checked():
        check_pending()
        assert seen[-1] == "written"
        close()

    client.hook, client.close = hook, close_checked
    await create(db, row, data)
    fact = (await mutations(db))[0]
    assert fact.state == "confirmed" and fact.current_delta_bytes == len(data)
    assert fact.physical_outcome == "written" and fact.physical_size_bytes == len(data)
    assert fact.evidence["full_readback"] and fact.evidence["current_head_matches"]
    assert fact.evidence["returned_version"]
    assert client.closed == 1 and all(body.closed for body in client.bodies)
    calls = len(client.calls)
    await create(db, row, data)
    assert len(await mutations(db)) == 1 and len(client.calls) == calls


async def test_failed_intent_rolls_back_writing_and_never_attempts_final_io(
    creation_db, client, monkeypatch
):
    db = creation_db
    row = await allocation(db)
    begin = accounting.begin_in_transaction

    async def fail(connection, **values):
        await begin(connection, **values)
        raise OSError("private database failure")

    monkeypatch.setattr(accounting, "begin_in_transaction", fail)
    with pytest.raises(ContentError):
        await create(db, row)
    assert not await mutations(db)
    assert not [call for call in client.calls if call[0] in {"complete", "put"}]
    assert not client.uploads
    assert (await service._read(db, row["id"]))["write_started_at"] is None


@pytest.mark.parametrize("data", [b"", b"original"])
async def test_412_confirms_only_this_attempt_zero(creation_db, client, data):
    row = await allocation(creation_db)
    client.store(row["bucket"], row["object_key"], b"external")
    with pytest.raises(ContentError, match="creation_write_conflict"):
        await create(creation_db, row, data)
    fact = (await mutations(creation_db))[0]
    assert fact.state == "confirmed" and fact.current_delta_bytes == 0
    assert fact.physical_outcome == "not_written"
    assert client.objects[row["bucket"], row["object_key"]]["data"] == b"external"


@pytest.mark.parametrize("data", [b"", b"original"])
async def test_lost_ack_is_unknown_even_if_final_exists(creation_db, client, data):
    row = await allocation(creation_db)
    client.versioned = True
    name = "complete_multipart_upload" if data else "put_object"
    complete = getattr(client, name)

    def lost(**params):
        complete(**params)
        raise TimeoutError("private acknowledgement")

    setattr(client, name, lost)
    with pytest.raises(ContentError):
        await create(creation_db, row, data)
    fact = (await mutations(creation_db))[0]
    assert fact.state == "unknown" and fact.current_delta_bytes is None
    assert fact.physical_outcome == "unknown"
    assert (row["bucket"], row["object_key"]) in client.objects


@pytest.mark.parametrize(
    "phase,delta", [("receipt", None), ("readback", 8), ("business", 8)]
)
async def test_creation_persistence_failures_preserve_actual_physical_evidence(
    creation_db, client, monkeypatch, phase, delta
):
    row = await allocation(creation_db)
    client.versioned = True
    change = service._change

    async def fail(db, identifier, expected, **values):
        should_fail = (
            (phase == "receipt" and values.get("status") == "written")
            or (phase == "readback" and "verification" in values)
            or (phase == "business" and values.get("status") == "verified")
        )
        if should_fail:
            raise OSError("private persistence failure")
        return await change(db, identifier, expected, **values)

    monkeypatch.setattr(service, "_change", fail)
    with pytest.raises(ContentError):
        await create(creation_db, row)
    fact = (await mutations(creation_db))[0]
    assert fact.physical_outcome == "written" and fact.physical_size_bytes == 8
    assert fact.evidence["returned_version"]
    assert fact.current_delta_bytes == delta
    assert fact.state == ("confirmed" if delta is not None else "unknown")


async def test_caller_business_rollback_does_not_erase_confirmed_fact(
    creation_db, client
):
    row = await allocation(creation_db)
    client.versioned = True
    pending = DatasetItem(
        dataset_id=row["dataset_id"],
        file_name="pending",
        file_path="pending",
        file_type="image",
    )
    creation_db.add(pending)
    await creation_db.flush()
    identifier = pending.id
    await create(creation_db, row)
    await creation_db.rollback()
    assert await creation_db.get(DatasetItem, identifier) is None
    assert (await mutations(creation_db))[0].current_delta_bytes == 8


@pytest.mark.parametrize("timing", ["before_readback", "after_readback"])
async def test_old_returned_version_remains_unknown_when_current_replaced(
    creation_db, client, monkeypatch, timing
):
    from app.services import managed_original_writer as writer

    row = await allocation(creation_db)
    client.versioned = True
    verify = writer.verify

    def replace_current(*args, **kwargs):
        if timing == "before_readback":
            client.store(row["bucket"], row["object_key"], b"replacement")
        verified = verify(*args, **kwargs)
        if timing == "after_readback":
            client.store(row["bucket"], row["object_key"], b"replacement")
        return verified

    monkeypatch.setattr(writer, "verify", replace_current)
    if timing == "before_readback":
        with pytest.raises(ContentError, match="creation_source_changed"):
            await create(creation_db, row)
    else:
        result = await create(creation_db, row)
    fact = (await mutations(creation_db))[0]
    if timing == "after_readback":
        assert result.version_id == fact.evidence["returned_version"]
        assert (
            fact.evidence["full_readback"] and not fact.evidence["current_head_matches"]
        )
    assert fact.physical_outcome == "written"
    assert fact.current_delta_bytes is None and fact.state == "unknown"
    assert client.objects[row["bucket"], row["object_key"]]["data"] == b"replacement"


@pytest.mark.parametrize("version", [None, "null"])
async def test_no_immutable_version_stays_unknown(creation_db, client, version):
    row = await allocation(creation_db)
    complete = client.complete_multipart_upload

    def unversioned(**params):
        response = complete(**params)
        response["VersionId"] = version
        return response

    client.complete_multipart_upload = unversioned
    await create(creation_db, row)
    fact = (await mutations(creation_db))[0]
    assert fact.evidence["returned_version"] == version
    assert fact.evidence["full_readback"]
    assert fact.physical_outcome == "written" and fact.current_delta_bytes is None


async def test_close_failure_cannot_confirm_even_with_complete_verification(
    creation_db, client
):
    row = await allocation(creation_db)
    client.versioned, client.close_failure = True, True
    with pytest.raises(ContentError):
        await create(creation_db, row)
    fact = (await mutations(creation_db))[0]
    assert fact.physical_outcome == "written" and fact.evidence["full_readback"]
    assert fact.current_delta_bytes is None and fact.state == "unknown"


async def test_repeated_caller_cancellation_waits_for_native_close_and_fact(
    creation_db, client
):
    row = await allocation(creation_db)
    client.versioned = True
    entered, release = threading.Event(), threading.Event()
    close = client.close

    def blocked_close():
        entered.set()
        assert release.wait(8)
        close()

    client.close = blocked_close
    task = asyncio.create_task(create(creation_db, row))
    try:
        assert await asyncio.to_thread(entered.wait, 4)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert (await mutations(creation_db))[0].state == "pending"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed == 1
    assert (await mutations(creation_db))[0].current_delta_bytes == 8


async def test_accounting_publication_failure_preserves_business_success_and_pending(
    creation_db, client, monkeypatch
):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from sqlalchemy.ext.asyncio import async_sessionmaker

    row = await allocation(creation_db)
    client.versioned = True
    factory = async_sessionmaker(
        service.storage_access.engine(creation_db), expire_on_commit=False
    )

    @asynccontextmanager
    async def failed_factory(*args, **kwargs):
        async with factory() as db:
            db.commit = AsyncMock(side_effect=OSError("private commit failure"))
            yield db

    monkeypatch.setattr(
        service, "async_sessionmaker", lambda *args, **kwargs: failed_factory
    )
    result = await create(creation_db, row)
    assert result.size_bytes == 8
    assert (await service._read(creation_db, row["id"]))["status"] == "verified"
    assert (await mutations(creation_db))[0].state == "pending"


@pytest.mark.parametrize(
    "origin", ["direct", "zip", "connector", "directory_copy", "task", "auxiliary"]
)
async def test_central_owner_uses_frozen_exact_address_for_every_original_kind(
    creation_db, client, origin
):
    db = creation_db
    if origin == "auxiliary":
        # Reuse the current schema's actual scope constraint without changing
        # the old migration fixture's historical guard contract.
        async with service.storage_access.engine(db).begin() as connection:
            scope = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_managed_creation_scope' AND conrelid='public.managed_original_creations'::regclass"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE managed_original_creations DROP CONSTRAINT ck_managed_creation_scope"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE managed_original_creations ADD CONSTRAINT ck_managed_creation_scope "
                    + scope
                )
            )
        row = await service.allocate_auxiliary(
            db,
            dataset_id=uuid.uuid4(),
            kind="map",
            source_contract={
                "token": "map",
                "filename": "maps/map.bin",
                "log_tokens": ["log"],
            },
            logical_path="maps/map.bin",
            max_bytes=10,
            authority={"source": "test"},
        )
    elif origin == "task":
        row = await service.allocate_task(
            db,
            project_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            logical_path="exact/%2F/中文.bin",
            max_bytes=10,
        )
    else:
        row = await allocation(
            db,
            origin=origin,
            staging=origin == "direct",
            logical_path="exact/%2F/中文.bin",
        )
    client.versioned = True
    if row["origin"] == "direct":
        client.store(row["staging_bucket"], row["staging_key"], b"original")
        await service.complete_direct(db, row)
    else:
        await service.create_stream(
            db, row, io.BytesIO(b"original"), content_type=row["content_type"]
        )
    fact = (await mutations(db))[0]
    assert fact.producer == "managed_original_creation" and fact.owner_id == row["id"]
    assert fact.purpose == "managed_original"
    assert fact.scope_key == accounting.scope_key(
        row["configuration"]["endpoint"], row["bucket"]
    )
    assert (
        fact.object_key == row["object_key"] and fact.object_key != row["staging_key"]
    )
    assert (
        fact.evidence["owner_kind"] == row["owner_kind"]
        and fact.evidence["origin"] == row["origin"]
    )
    assert fact.current_delta_bytes == 8
