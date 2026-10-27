"""Real SQL facts and paged observations do not infer unobserved mutations."""

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.storage_usage import (
    StorageUsageMutation as Mutation,
    StorageUsageInventoryObject as Observation,
    StorageUsageReconciliation as Reconciliation,
    StorageUsageSchedule as Schedule,
    StorageUsageSnapshot as Snapshot,
)
from app.services import storage_usage_accounting as svc


@pytest.fixture
async def accounting_factory(test_engine):
    schema = "usage_accounting_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            for model in (Mutation, Observation, Reconciliation, Schedule, Snapshot):
                await connection.run_sync(
                    lambda conn, table=model.__table__: table.create(conn)
                )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def record():
    return SimpleNamespace(
        id=uuid.uuid4(),
        storage_ref={
            "endpoint_url": f"http://{settings.minio_endpoint}",
            "bucket": settings.minio_import_bucket,
            "key_prefix": "exact/%2F/中文/",
        },
    )


async def test_intent_precedes_io_and_repeated_publication_counts_once(
    accounting_factory,
):
    owner = record()
    handle = await svc.begin(accounting_factory, owner, "a", "create", nonce="n")
    async with accounting_factory() as db:
        assert (await db.get(Mutation, handle.id)).state == "pending"
    again = await svc.begin(accounting_factory, owner, "a", "create", nonce="n")
    assert again.id == handle.id and not again.fresh
    await svc.publish(
        accounting_factory,
        handle,
        {
            "physical_outcome": "written",
            "physical_size_bytes": 7,
            "current_delta_bytes": 7,
        },
    )
    await svc.publish(accounting_factory, again, {"current_delta_bytes": 999})
    async with accounting_factory() as db:
        row = await db.get(Mutation, handle.id)
        assert row.object_key == "exact/%2F/中文/a" and row.current_delta_bytes == 7
        summary, _, _ = await svc.summaries(db, [{"key": row.scope_key}])
        assert summary.net_change_bytes == 7 and summary.projected_total_bytes is None


async def test_publication_failure_preserves_pending_and_does_not_raise(
    accounting_factory,
):
    handle = await svc.begin(accounting_factory, record(), "a", "create", nonce="n")

    @asynccontextmanager
    async def broken():
        async with accounting_factory() as db:
            db.commit = AsyncMock(side_effect=OSError("private commit acknowledgement"))
            yield db

    await svc.publish(broken, handle, {"current_delta_bytes": 8})
    async with accounting_factory() as db:
        row = await db.get(Mutation, handle.id)
        assert row.state == "pending" and row.current_delta_bytes is None


async def test_repeated_unresolved_intent_and_unsettled_close_stay_unknown(
    accounting_factory,
):
    owner = record()
    handle = await svc.begin(accounting_factory, owner, "a", "create", nonce="n")
    repeated = await svc.begin(accounting_factory, owner, "a", "create", nonce="n")
    await svc.publish(
        accounting_factory,
        repeated,
        {"physical_outcome": "not_written", "current_delta_bytes": 0},
    )
    second = await svc.begin(accounting_factory, owner, "b", "create", nonce="n")
    await svc.publish(
        accounting_factory,
        second,
        {
            "physical_outcome": "written",
            "physical_size_bytes": 5,
            "current_delta_bytes": 5,
        },
        settled=False,
    )
    async with accounting_factory() as db:
        rows = list(await db.scalars(select(Mutation)))
        assert all(
            row.state == "unknown" and row.current_delta_bytes is None for row in rows
        )
        assert (await db.get(Mutation, handle.id)).physical_outcome == "not_written"


async def observe(db, scope, generation, objects, start, end):
    snapshot = await db.get(Snapshot, scope)
    if snapshot is None:
        snapshot = Snapshot(scope_key=scope, attempt_status="never")
        db.add(snapshot)
        await db.flush()
    await svc.stage_page(db, generation, scope, objects, start, end)
    await svc.complete_bucket(db, snapshot, generation, start, end)
    await db.flush()
    return await db.get(Reconciliation, (generation, scope))


async def test_real_delta_reconciles_and_external_bytes_remain_unexplained(
    accounting_factory,
):
    owner = record()
    scope = svc.scope_key(
        owner.storage_ref["endpoint_url"], owner.storage_ref["bucket"]
    )
    start = svc.now() - timedelta(hours=3)
    async with accounting_factory() as db:
        first = await observe(
            db, scope, uuid.uuid4(), [], start, start + timedelta(seconds=1)
        )
        assert first.summary["status"] == "no_baseline"
        await db.commit()
    handle = await svc.begin(accounting_factory, owner, "a", "create", nonce="n")
    await svc.publish(
        accounting_factory,
        handle,
        {
            "current_delta_bytes": 7,
            "physical_outcome": "written",
            "physical_size_bytes": 7,
        },
    )
    async with accounting_factory() as db:
        row = await db.get(Mutation, handle.id)
        end = svc.now() + timedelta(seconds=1)
        result = await observe(
            db,
            scope,
            uuid.uuid4(),
            [{"key": row.object_key, "size": 7}, {"key": "uncovered", "size": 3}],
            end,
            end + timedelta(seconds=1),
        )
        assert result.summary["confirmed_change_bytes"] == 7
        assert result.summary["known_unexplained_change_bytes"] == 3
        assert result.summary["unexplained_object_count"] == 1
        assert result.summary["uncertain_object_count"] == 0
        await db.commit()


async def test_window_overlap_pending_and_missing_are_not_false_drift(
    accounting_factory,
):
    owner = record()
    handle = await svc.begin(accounting_factory, owner, "a", "create", nonce="n")
    scope = svc.scope_key(
        owner.storage_ref["endpoint_url"], owner.storage_ref["bucket"]
    )
    start = svc.now() - timedelta(hours=2)
    async with accounting_factory() as db:
        await observe(
            db,
            scope,
            uuid.uuid4(),
            [{"key": "stable", "size": 8}],
            start,
            start + timedelta(seconds=1),
        )
        end = svc.now() + timedelta(seconds=1)
        result = await observe(
            db, scope, uuid.uuid4(), [], end, end + timedelta(seconds=1)
        )
        assert result.summary["known_unexplained_change_bytes"] == -8
        assert result.summary["uncertain_object_count"] == 1
        assert result.summary["unexplained_change_bytes"] is None
        assert (await db.get(Mutation, handle.id)).state == "pending"


async def test_duplicate_across_pages_fails_and_two_successful_generations_remain(
    accounting_factory,
):
    async with accounting_factory() as db:
        scope, started = "a" * 64, svc.now()
        generation = uuid.uuid4()
        await svc.stage_page(
            db, generation, scope, [{"key": "exact", "size": 1}], started, started
        )
        with pytest.raises(Exception):
            async with db.begin_nested():
                await svc.stage_page(
                    db,
                    generation,
                    scope,
                    [{"key": "exact", "size": 1}],
                    started,
                    started,
                )
        await svc.discard_staging(db, generation)
        for _ in range(3):
            await observe(
                db, scope, uuid.uuid4(), [{"key": "a", "size": 1}], started, started
            )
        assert len(list(await db.scalars(select(Observation)))) == 2
        assert len(list(await db.scalars(select(Reconciliation)))) == 2


async def test_delete_deduplicates_the_exact_physical_version_across_owner_claims(
    accounting_factory,
):
    first, second = record(), record()
    one = await svc.begin(
        accounting_factory, first, "a", "delete", version="immutable-v1"
    )
    two = await svc.begin(
        accounting_factory, second, "a", "delete", version="immutable-v1"
    )
    assert one.id == two.id and not two.fresh
    await svc.publish(
        accounting_factory,
        one,
        {
            "physical_outcome": "deleted",
            "physical_size_bytes": 7,
            "current_delta_bytes": -7,
        },
    )
    await svc.publish(
        accounting_factory,
        two,
        {"physical_outcome": "deleted", "current_delta_bytes": -7},
    )
    newer = await svc.begin(
        accounting_factory, second, "a", "delete", version="immutable-v2"
    )
    assert newer.id != one.id
    elsewhere = record()
    elsewhere.storage_ref["bucket"] += "-different"
    other = await svc.begin(
        accounting_factory, elsewhere, "a", "delete", version="immutable-v1"
    )
    assert other.id != one.id
    async with accounting_factory() as db:
        scope = svc.scope_key(
            first.storage_ref["endpoint_url"], first.storage_ref["bucket"]
        )
        summary, _, _ = await svc.summaries(db, [{"key": scope}])
        assert summary.confirmed_removed_bytes == 7 and summary.net_change_bytes == -7


async def test_explicit_address_joins_session_and_connection_without_committing(
    accounting_factory,
):
    values = dict(
        endpoint_url="https://frozen.example/s3",
        bucket="frozen-bucket",
        object_key="exact/%2F/中文/key",
        producer="managed_original_creation",
        owner_id=uuid.uuid4(),
        purpose="managed_original",
        kind="create",
        nonce="attempt",
        evidence={"owner_kind": "dataset_auxiliary", "origin": "connector"},
    )
    async with accounting_factory() as db:
        first = await svc.begin_in_transaction(db, **values)
        await db.rollback()
    async with accounting_factory() as db:
        assert await db.get(Mutation, first.id) is None
    async with accounting_factory.kw["bind"].begin() as connection:
        handle = await svc.begin_in_transaction(connection, **values)
    async with accounting_factory() as db:
        row = await db.get(Mutation, handle.id)
        assert (
            row.object_key == values["object_key"]
            and row.owner_id == values["owner_id"]
        )
        assert row.scope_key == svc.scope_key(values["endpoint_url"], values["bucket"])
        assert row.evidence["owner_kind"] == "dataset_auxiliary"
        same = await svc.begin_in_transaction(db, **values)
        assert same.id == handle.id and not same.fresh
        separate = await svc.begin_in_transaction(
            db, **{**values, "producer": "prediction_inputs"}
        )
        assert separate.id != handle.id


async def test_explicit_producers_share_exact_immutable_delete_identity(
    accounting_factory,
):
    values = dict(
        endpoint_url="https://frozen.example/s3",
        bucket="frozen",
        object_key="exact/%2F/中文/key",
        purpose="temporary",
        kind="delete",
        version="v1",
    )
    async with accounting_factory() as db:
        first = await svc.begin_in_transaction(
            db, **values, producer="one", owner_id=uuid.uuid4()
        )
        second = await svc.begin_in_transaction(
            db, **values, producer="two", owner_id=uuid.uuid4()
        )
        assert first.id == second.id and not second.fresh
        third = await svc.begin_in_transaction(
            db, **{**values, "version": "v2"}, producer="two", owner_id=uuid.uuid4()
        )
        assert third.id != first.id
