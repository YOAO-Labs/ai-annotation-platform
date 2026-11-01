"""Cleanup respects committed ownership across transaction boundaries."""

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    UniqueConstraint,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from app.db.models.raster_mask_object import RasterMaskObject, RasterMaskPublication
from app.db.models.storage_maintenance import StorageOperation
from app.services import (
    raster_mask_objects as owners,
    raster_object_storage as physical,
)
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID
from tests.raster_mask_isolation import remove_test_raster_owners, track_raster_owners
from tests.raster_mask_support import harness as harness


async def add_owner(db, identifier, guard_id, reference, data):
    row = RasterMaskObject(
        id=identifier,
        **physical.content(reference, data),
        state="pending",
        created_at=datetime.now(timezone.utc),
    )
    row.storage_ref = await physical.allocate(db, row)
    db.add(row)
    await db.flush()
    db.add(RasterMaskPublication(id=guard_id, object_id=identifier, gate_revision=1))
    await db.flush()


@pytest.mark.parametrize("boundary", ["root", "savepoint", "released_savepoint"])
async def test_rolled_back_orm_identities_belong_to_neighbor(
    harness, test_engine, test_db_url, boundary
):
    _, reference, data = harness.content()
    _, own_reference, own_data = harness.content()
    rolled_id, rolled_guard = uuid.uuid4(), uuid.uuid4()
    own_id, own_guard = uuid.uuid4(), uuid.uuid4()
    neighbor_engine = create_async_engine(test_db_url)
    try:
        with track_raster_owners(test_engine) as created:
            async with harness.factory() as db:
                if boundary == "savepoint":
                    with pytest.raises(ValueError, match="abort savepoint"):
                        async with db.begin_nested():
                            await add_owner(
                                db, rolled_id, rolled_guard, reference, data
                            )
                            raise ValueError("abort savepoint")
                    await add_owner(db, own_id, own_guard, own_reference, own_data)
                    await db.commit()
                else:
                    if boundary == "released_savepoint":
                        nested = await db.begin_nested()
                    await add_owner(db, rolled_id, rolled_guard, reference, data)
                    if boundary == "released_savepoint":
                        await nested.commit()
                    await db.rollback()
            if boundary != "savepoint":
                async with harness.factory() as db:
                    async with db.begin():
                        async with db.begin_nested():
                            await add_owner(
                                db, own_id, own_guard, own_reference, own_data
                            )
            async with async_sessionmaker(neighbor_engine)() as neighbor:
                await add_owner(neighbor, rolled_id, rolled_guard, reference, data)
                await neighbor.commit()
            async with harness.factory() as db:
                neighbor = await db.get(RasterMaskObject, rolled_id)
                neighbor.last_attempted_at = datetime.now(timezone.utc)
                await db.commit()

        await remove_test_raster_owners(test_engine, created)
        assert {row.id for row in await harness.rows()} == {rolled_id}
        assert {guard.id for guard in await harness.guards()} == {rolled_guard}
    finally:
        await neighbor_engine.dispose()


def operation(identifier, purpose):
    return dict(
        id=identifier,
        location_id=LEGACY_LOCATION_ID,
        revision=1,
        purpose=purpose,
        node_id="raster-isolation-test",
    )


@pytest.mark.parametrize("boundary", ["root", "savepoint", "released_savepoint"])
async def test_cleanup_preserves_rolled_back_and_conflicting_permits(
    harness, test_engine, test_db_url, boundary
):
    owner_id = uuid.uuid4()
    harness.identifiers.add(owner_id)
    purpose = owners.purpose("write", owner_id)
    existing_id, rolled_id, returning_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    neighbor_engine = create_async_engine(test_db_url)
    try:
        async with neighbor_engine.begin() as connection:
            await connection.execute(
                insert(StorageOperation).values(**operation(existing_id, purpose))
            )
        with track_raster_owners(test_engine) as created:
            async with test_engine.connect() as connection:
                root = await connection.begin()
                if boundary != "root":
                    nested = await connection.begin_nested()
                await connection.execute(
                    insert(StorageOperation).values(**operation(rolled_id, purpose))
                )
                if boundary == "savepoint":
                    await nested.rollback()
                    await root.commit()
                else:
                    if boundary == "released_savepoint":
                        await nested.commit()
                    await root.rollback()
            async with neighbor_engine.begin() as connection:
                await connection.execute(
                    insert(StorageOperation).values(**operation(rolled_id, purpose))
                )
            async with test_engine.begin() as connection:
                conflict = pg_insert(StorageOperation).values(
                    **operation(existing_id, purpose)
                )
                result = await connection.execute(
                    conflict.on_conflict_do_nothing(
                        index_elements=[StorageOperation.id]
                    ).returning(StorageOperation.id)
                )
                assert result.all() == []
                await connection.execute(
                    conflict.on_conflict_do_update(
                        index_elements=[StorageOperation.id],
                        set_={"node_id": "raster-isolation-updated"},
                    )
                )
                result = await connection.execute(
                    insert(StorageOperation)
                    .values(**operation(returning_id, purpose))
                    .returning(StorageOperation.id)
                )
                assert result.scalar_one() == returning_id
            async with harness.factory() as db:
                permit = await storage_access.acquire(db, LEGACY_LOCATION_ID, purpose)

        await remove_test_raster_owners(test_engine, created)
        async with harness.factory() as db:
            remaining = set(
                (
                    await db.scalars(
                        select(StorageOperation.id).where(
                            StorageOperation.id.in_(
                                [existing_id, rolled_id, returning_id, permit.id]
                            )
                        )
                    )
                ).all()
            )
        assert remaining == {existing_id, rolled_id}
    finally:
        await neighbor_engine.dispose()


async def test_failed_commit_preserves_reused_ids_and_other_commits(
    harness, test_engine, test_db_url
):
    _, reference, data = harness.content()
    _, own_reference, own_data = harness.content()
    rolled_id, rolled_guard, rolled_operation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    own_id, own_guard = uuid.uuid4(), uuid.uuid4()
    harness.identifiers.update({rolled_id, own_id})
    purpose = owners.purpose("write", rolled_id)
    neighbor_engine = create_async_engine(test_db_url)
    try:
        with track_raster_owners(test_engine) as created:
            async with harness.factory() as db:
                await add_owner(db, own_id, own_guard, own_reference, own_data)
                await db.commit()
                permit = await storage_access.acquire(
                    db, LEGACY_LOCATION_ID, owners.purpose("write", own_id)
                )
            async with harness.factory() as db:
                await add_owner(db, rolled_id, rolled_guard, reference, data)
                await db.execute(
                    insert(StorageOperation).values(
                        **operation(rolled_operation, purpose)
                    )
                )
                constraint = Table(
                    "raster_commit_guard",
                    MetaData(),
                    Column("id", Integer),
                    UniqueConstraint("id", deferrable=True, initially="DEFERRED"),
                    prefixes=["TEMPORARY"],
                    postgresql_on_commit="DROP",
                )
                await db.execute(CreateTable(constraint))
                await db.execute(insert(constraint), [{"id": 1}, {"id": 1}])
                with pytest.raises(IntegrityError) as failure:
                    await db.commit()
                assert failure.value.orig.sqlstate == "23505"
            async with async_sessionmaker(neighbor_engine)() as neighbor:
                await add_owner(neighbor, rolled_id, rolled_guard, reference, data)
                await neighbor.execute(
                    insert(StorageOperation).values(
                        **operation(rolled_operation, purpose)
                    )
                )
                await neighbor.commit()

        await remove_test_raster_owners(test_engine, created)
        assert {row.id for row in await harness.rows()} == {rolled_id}
        assert {guard.id for guard in await harness.guards()} == {rolled_guard}
        async with harness.factory() as db:
            remaining = set(
                await db.scalars(
                    select(StorageOperation.id).where(
                        StorageOperation.id.in_([rolled_operation, permit.id])
                    )
                )
            )
        assert remaining == {rolled_operation}
    finally:
        await neighbor_engine.dispose()
