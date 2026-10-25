"""The async retirement owner publishes only after its real resources settle."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock, Mock
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import export_media_retirement as retirement
from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services import storage_access
from app.services import storage_usage_accounting as usage
from app.services.content_snapshot import ContentError
from tests import test_export_media_retirement as retirement_tests
from tests.test_export_media_removal_accounting import Provider

owned_db = retirement_tests.owned_db
gc_db = retirement_tests.gc_db
owner = retirement_tests.owner


@pytest.fixture
def lifecycle(monkeypatch):
    receipt = retirement.snapshot(
        owner(state="retiring", retired_at=datetime.now(timezone.utc))
    )
    state = SimpleNamespace(
        receipt=receipt,
        events=[],
        facts=[],
        fault=None,
        finish_failure=None,
        finished=[],
        permit=SimpleNamespace(id=uuid.uuid4(), _settled=False),
    )

    class Recorder:
        async def finish(self, *, settled):
            assert state.events[-1] in {"settle", "close", "guard"}
            state.events.append("finish")
            state.finished.append((settled, tuple(state.facts)))
            if state.finish_failure is not None:
                raise state.finish_failure

    state.accounting = Recorder()

    async def acquire(*args):
        state.events.append("acquire")
        return state.permit

    async def guard(*args, **kwargs):
        state.events.append("guard")
        if (
            state.fault in {"postguard", "unsettled_guard"}
            and state.events.count("guard") == 2
        ):
            failure = ContentError("postguard_changed")
            if state.fault == "unsettled_guard":
                storage_access.mark_unsettled(failure)
            raise failure

    async def settle(value):
        assert value is state.permit and "close" in state.events
        state.events.append("settle")
        if state.fault == "permit":
            raise OSError("permit_close_failed")
        if state.fault != "pending_permit":
            value._settled = True

    def close():
        state.events.append("close")
        if state.fault == "close":
            raise OSError("client_close_failed")

    state.client = SimpleNamespace(
        meta=SimpleNamespace(
            endpoint_url=receipt.storage_ref["endpoint_url"], events=Mock()
        ),
        close=close,
    )

    def construct():
        state.events.append("construct")
        return state.client

    def delete(client, record, *, accounting):
        assert client is state.client and record is receipt
        assert accounting is state.accounting and not state.permit._settled
        state.events.append("delete")
        state.facts.append("first_version_deleted")
        if state.fault == "partial":
            raise ContentError("later_delete_failed")
        return 1

    monkeypatch.setattr(transfer, "_acquire", acquire)
    monkeypatch.setattr(transfer, "_settle", settle)
    monkeypatch.setattr(retirement, "_guard", guard)
    monkeypatch.setattr(physical, "create_client", construct)
    monkeypatch.setattr(physical, "delete_inventory", delete)
    monkeypatch.setattr(
        retirement.removal, "recorder", lambda db, record: state.accounting
    )
    return state


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "partial",
        "postguard",
        "unsettled_guard",
        "close",
        "permit",
        "pending_permit",
    ],
)
async def test_accounting_waits_for_client_postguard_and_actual_permit(
    lifecycle, fault
):
    state = lifecycle
    state.fault = fault
    if fault in {None, "pending_permit"}:
        await retirement._physical_delete(None, state.receipt)
    else:
        expected = {
            "partial": "later_delete_failed",
            "postguard": "postguard_changed",
            "unsettled_guard": "postguard_changed",
            "close": "storage_close_failed",
            "permit": "permit_close_failed",
        }[fault]
        with pytest.raises(Exception) as error:
            await retirement._physical_delete(None, state.receipt)
        if fault == "close":
            assert error.value.storage_operation_unsettled
        else:
            assert expected in str(error.value)
    assert state.finished == [
        (
            fault not in {"close", "unsettled_guard", "permit", "pending_permit"},
            ("first_version_deleted",),
        )
    ]
    assert state.events.index("close") < state.events.index("finish")
    if fault in {"close", "unsettled_guard"}:
        assert "settle" not in state.events
        assert state.events.count("guard") == (1 if fault == "close" else 2)
    else:
        assert state.events[-2:] == ["settle", "finish"]
    if fault in {None, "pending_permit", "permit"}:
        assert state.events.index("close") < len(state.events) - 3
        assert state.events[-3] == "guard"


@pytest.mark.parametrize("business_failure", [False, True])
async def test_ordinary_publication_failure_preserves_business_result(
    lifecycle, business_failure
):
    state = lifecycle
    state.finish_failure = RuntimeError("publication_unavailable")
    if business_failure:
        state.fault = "postguard"
        with pytest.raises(ContentError, match="postguard_changed"):
            await retirement._physical_delete(None, state.receipt)
    else:
        await retirement._physical_delete(None, state.receipt)
    assert state.finished == [(True, ("first_version_deleted",))]


@pytest.mark.parametrize("business_failure", [False, True])
async def test_new_publication_cancellation_propagates_after_settlement(
    lifecycle, business_failure
):
    state = lifecycle
    state.finish_failure = asyncio.CancelledError("new_publication_cancel")
    if business_failure:
        state.fault = "postguard"
    with pytest.raises(asyncio.CancelledError, match="new_publication_cancel"):
        await retirement._physical_delete(None, state.receipt)
    assert state.permit._settled is True
    assert state.events[-2:] == ["settle", "finish"]


async def test_cancelled_native_delete_drains_before_close_settle_and_publish(
    lifecycle, monkeypatch
):
    state = lifecycle
    entered, release = threading.Event(), threading.Event()
    original = physical.delete_inventory

    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(physical, "delete_inventory", blocking)
    pending = asyncio.create_task(retirement._physical_delete(None, state.receipt))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and "close" not in state.events and not state.finished
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert state.events[-3:] == ["close", "settle", "finish"]
    assert state.finished == [(True, ("first_version_deleted",))]


async def test_zip_delete_does_not_allocate_media_recorder(monkeypatch):
    receipt = retirement.snapshot(owner())
    output = SimpleNamespace(storage_ref=receipt.storage_ref)
    permit = SimpleNamespace(id=uuid.uuid4(), _settled=False)
    monkeypatch.setattr(transfer, "_acquire", AsyncMock(return_value=permit))
    monkeypatch.setattr(transfer, "_settle", AsyncMock())
    monkeypatch.setattr(retirement, "_guard", AsyncMock())
    monkeypatch.setattr(
        retirement.removal, "recorder", Mock(side_effect=AssertionError("media only"))
    )
    monkeypatch.setattr(
        retirement.export_storage, "io_target", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(retirement.export_storage, "delete", Mock())
    monkeypatch.setattr(retirement.key_removal, "recorder", Mock(return_value=None))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session(db):
        yield db

    monkeypatch.setattr(transfer, "_session", session)
    await retirement._physical_delete(None, receipt, zip_output=output)
    retirement.export_storage.delete.assert_called_once_with(output, accounting=None)


async def mutations(db, row):
    return list(
        await db.scalars(
            select(Mutation)
            .where(Mutation.owner_id == row.id)
            .order_by(Mutation.started_at, Mutation.id)
            .execution_options(populate_existing=True)
        )
    )


async def database_case(db, monkeypatch, *, count=1):
    values = retirement_tests.model_tests._values()
    if count == 2:
        values["inventory"]["objects"][
            physical.prefix_for(values["id"]) + str(uuid.uuid4()) + ".bin"
        ] = {"sha256": "b" * 64, "size_bytes": 11}
        values["object_count"], values["size_bytes"] = 2, 22
    row = await retirement_tests.database_owner(db, **values)
    await db.commit()
    client = Provider(endpoint=row.storage_ref["endpoint_url"])
    client.close_count, client.close_failure = 0, None
    for key in row.inventory["objects"]:
        client.seed("v1", b"x" * 11, key=key)
    loop = asyncio.get_running_loop()
    factory = async_sessionmaker(db.bind, expire_on_commit=False)

    async def durable(params):
        async with factory() as check:
            pending = list(
                await check.scalars(
                    select(Mutation).where(
                        Mutation.owner_id == row.id,
                        Mutation.object_key == params["Key"],
                        Mutation.expected_version == params["VersionId"],
                    )
                )
            )
            assert len(pending) == 1 and pending[0].state == "pending"
            assert pending[0].producer == "export_media_removals"
            assert pending[0].kind == "delete" and pending[0].purpose == "temporary"
            assert (
                await check.scalar(select(func.count()).select_from(StorageOperation))
                == 1
            )

    original = client.delete_object

    def delete(**params):
        assert params["Bucket"] == row.storage_ref["bucket"]
        asyncio.run_coroutine_threadsafe(durable(params), loop).result(timeout=5)
        return original(**params)

    def close():
        client.close_count += 1
        if client.close_failure is not None:
            raise client.close_failure

    client.delete_object, client.close = delete, close
    monkeypatch.setattr(physical, "create_client", lambda: client)
    return row, client


async def test_database_partial_delete_preserves_earlier_confirmed_fact(
    gc_db, monkeypatch
):
    row, client = await database_case(gc_db, monkeypatch, count=2)
    original = client.delete_object

    def lost_second_ack(**params):
        client.lose_ack = len(client.calls) == 1
        return original(**params)

    client.delete_object = lost_second_ack
    assert await retirement.cleanup(gc_db) == {"deleted": 0, "retained": 0, "failed": 1}
    facts = await mutations(gc_db, row)
    assert len(facts) == 2
    assert [
        (fact.state, fact.physical_outcome, fact.current_delta_bytes) for fact in facts
    ] == [("confirmed", "deleted", -11), ("unknown", "unknown", None)]
    await gc_db.refresh(row)
    assert row.state == "retiring" and row.inventory and client.close_count == 1
    assert await gc_db.scalar(select(func.count()).select_from(StorageOperation)) == 0


async def test_database_postguard_failure_keeps_physical_fact(gc_db, monkeypatch):
    row, client = await database_case(gc_db, monkeypatch)
    original = retirement._guard
    guards = 0

    async def changed(*args, **kwargs):
        nonlocal guards
        await original(*args, **kwargs)
        guards += 1
        if guards == 2:
            assert client.close_count == 1
            raise ContentError("changed_after_delete")

    monkeypatch.setattr(retirement, "_guard", changed)
    assert (await retirement.cleanup(gc_db))["failed"] == 1
    (fact,) = await mutations(gc_db, row)
    assert (fact.state, fact.physical_outcome, fact.current_delta_bytes) == (
        "confirmed",
        "deleted",
        -11,
    )
    await gc_db.refresh(row)
    assert row.state == "retiring" and guards == 2
    assert await gc_db.scalar(select(func.count()).select_from(StorageOperation)) == 0


@pytest.mark.parametrize("resource", ["client", "permit"])
async def test_database_unknown_close_keeps_physical_fact_without_delta(
    gc_db, monkeypatch, resource
):
    row, client = await database_case(gc_db, monkeypatch)
    if resource == "client":
        client.close_failure = OSError("client_close_failed")
    else:

        async def fail(permit):
            assert client.close_count == 1
            raise OSError("permit_close_failed")

        monkeypatch.setattr(storage_access.Permit, "_settle", fail)
    assert (await retirement.cleanup(gc_db))["failed"] == 1
    (fact,) = await mutations(gc_db, row)
    assert (fact.state, fact.physical_outcome, fact.current_delta_bytes) == (
        "unknown",
        "deleted",
        None,
    )
    assert client.close_count == 1
    assert await gc_db.scalar(select(func.count()).select_from(StorageOperation)) == 1
    await gc_db.refresh(row)
    assert row.state == "retiring" and row.inventory
    assert await retirement.cleanup(gc_db) == {"deleted": 0, "retained": 0, "failed": 0}


async def test_database_ordinary_publication_failure_keeps_business_success(
    gc_db, monkeypatch
):
    row, client = await database_case(gc_db, monkeypatch)

    async def unavailable(*args, **kwargs):
        assert client.close_count == 1
        async with async_sessionmaker(gc_db.bind)() as check:
            assert (
                await check.scalar(select(func.count()).select_from(StorageOperation))
                == 0
            )
        raise OSError("publication_unavailable")

    monkeypatch.setattr(usage, "publish", unavailable)
    assert (await retirement.cleanup(gc_db))["deleted"] == 1
    (fact,) = await mutations(gc_db, row)
    assert fact.state == "pending" and fact.current_delta_bytes is None
    await gc_db.refresh(row)
    assert row.state == "deleted"


async def test_database_publication_cancellation_drains_real_recorder(
    gc_db, monkeypatch
):
    row, client = await database_case(gc_db, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = usage.publish

    async def blocked(*args, **kwargs):
        assert client.close_count == 1
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(usage, "publish", blocked)
    pending = asyncio.create_task(retirement.cleanup(gc_db))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        assert (
            await gc_db.scalar(select(func.count()).select_from(StorageOperation)) == 0
        )
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    (fact,) = await mutations(gc_db, row)
    assert (fact.state, fact.physical_outcome, fact.current_delta_bytes) == (
        "confirmed",
        "deleted",
        -11,
    )
    await gc_db.refresh(row)
    assert row.state == "retiring" and client.close_count == 1


async def test_real_minio_bundle_retires_versions_through_production_owner(
    gc_db, monkeypatch
):
    values = retirement_tests.model_tests._values()
    # The launcher owns this bucket and all database/Redis resources in test mode.
    ref = physical.make_ref(values["id"], bucket=settings.minio_export_bucket)
    values["storage_ref"] = ref
    row = Bundle(**values)
    gc_db.add(row)
    await gc_db.commit()
    (key,) = row.inventory["objects"]
    seed = physical.create_client()
    factory = async_sessionmaker(gc_db.bind, expire_on_commit=False)
    loop = asyncio.get_running_loop()
    original_create, original_publish = physical.create_client, usage.publish
    closed = []

    async def durable(params):
        async with factory() as check:
            fact = await check.scalar(
                select(Mutation).where(
                    Mutation.owner_id == row.id,
                    Mutation.object_key == params["Key"],
                    Mutation.expected_version == params["VersionId"],
                )
            )
            assert fact is not None and fact.state == "pending"
            assert (
                await check.scalar(select(func.count()).select_from(StorageOperation))
                == 1
            )

    def tracked():
        client = original_create()
        original_close = client.close

        def close():
            original_close()
            closed.append(True)

        def parameters(params, **kwargs):
            asyncio.run_coroutine_threadsafe(durable(params), loop).result(timeout=5)

        client.close = close
        client.meta.events.register(
            "before-parameter-build.s3.DeleteObject", parameters
        )
        return client

    async def publish(*args, **kwargs):
        assert closed == [True]
        async with factory() as check:
            assert (
                await check.scalar(select(func.count()).select_from(StorageOperation))
                == 0
            )
        return await original_publish(*args, **kwargs)

    versions = set()
    try:
        seed.put_bucket_versioning(
            Bucket=ref["bucket"], VersioningConfiguration={"Status": "Enabled"}
        )
        for raw in (b"older!!", b"x" * 11):
            response = seed.put_object(Bucket=ref["bucket"], Key=key, Body=raw)
            versions.add(response["VersionId"])
        assert len(versions) == 2 and "null" not in versions
        monkeypatch.setattr(physical, "create_client", tracked)
        monkeypatch.setattr(usage, "publish", publish)
        assert await retirement.cleanup(gc_db) == {
            "deleted": 1,
            "retained": 0,
            "failed": 0,
        }
        facts = await mutations(gc_db, row)
        assert len(facts) == 2 and {fact.expected_version for fact in facts} == versions
        assert all(
            fact.state == "confirmed" and fact.physical_outcome == "deleted"
            for fact in facts
        )
        assert sum(fact.current_delta_bytes for fact in facts) == -11
        assert all(fact.evidence["final_send_count"] == 1 for fact in facts)
        assert (
            await gc_db.scalar(select(func.count()).select_from(StorageOperation)) == 0
        )
        await gc_db.refresh(row)
        assert row.state == "deleted" and row.inventory
        response = seed.list_object_versions(Bucket=ref["bucket"], Prefix=key)
        assert not response.get("Versions") and not response.get("DeleteMarkers")
    finally:
        try:
            remaining = seed.list_object_versions(Bucket=ref["bucket"], Prefix=key)
            for version in remaining.get("Versions", []) + remaining.get(
                "DeleteMarkers", []
            ):
                assert version["Key"] == key
                seed.delete_object(
                    Bucket=ref["bucket"], Key=key, VersionId=version["VersionId"]
                )
        finally:
            seed.close()
