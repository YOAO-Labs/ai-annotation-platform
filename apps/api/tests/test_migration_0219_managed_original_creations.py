"""Creation facts survive rollback and have no business ownership cascades."""

import uuid

import pytest
from sqlalchemy import delete, inspect, insert, text, update
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset_asset import DatasetAssetLocation
from app.db.models.managed_original_creation import ManagedOriginalCreation as Creation
from app.db.models.managed_original_retirement import (
    ManagedOriginalRetirement as Retirement,
)
from app.services import managed_original_creation as service
from tests.test_managed_original_creation import (  # noqa: F401
    allocation,
    client as _client,
    create,
    creation_db as _creation_db,
    migration,
)
from tests.test_managed_original_reclamation import assertion


client = _client
creation_db = _creation_db


async def test_empty_creation_schema_roundtrip_and_exact_columns(creation_db):
    engine = service.storage_access.engine(creation_db)
    async with engine.begin() as connection:

        def check(sync):
            scope = migration(sync, "0230_managed_original_task_scope.py")
            scope.downgrade()
            module = migration(sync)
            owned_schema = sync.scalar(text("SELECT current_schema()"))
            assert module.down_revision == "0218"
            module.downgrade()
            assert "managed_original_creations" not in inspect(sync).get_table_names(
                schema=owned_schema
            )
            module.upgrade()
            schema = inspect(sync)
            assert {
                column["name"]
                for column in schema.get_columns(
                    "managed_original_creations", schema=owned_schema
                )
            } == set(Creation.__table__.columns.keys()) - {
                "owner_kind",
                "project_id",
                "task_id",
            }
            assert not schema.get_foreign_keys(
                "managed_original_creations", schema=owned_schema
            )
            for table in ("dataset_asset_locations", "managed_original_retirements"):
                assert any(
                    fk["referred_table"] == "managed_original_creations"
                    and fk["options"]["ondelete"] == "RESTRICT"
                    for fk in schema.get_foreign_keys(table, schema=owned_schema)
                )
            scope.upgrade()
            assert {
                column["name"]
                for column in inspect(sync).get_columns(
                    "managed_original_creations", schema=owned_schema
                )
            } == set(Creation.__table__.columns.keys())

        await connection.run_sync(check)


@pytest.mark.parametrize(
    "field,value",
    [
        ("dataset_id", uuid.uuid4()),
        ("authority", {"rewritten": True}),
        ("configuration", {}),
        ("requested_by", uuid.uuid4()),
        ("staging_key", "invented"),
        ("content_type", "image/png"),
        ("input_fingerprint", "b" * 64),
    ],
)
async def test_initial_allocation_is_immutable(creation_db, field, value):
    row = await allocation(creation_db)
    with pytest.raises(IntegrityError):
        async with creation_db.begin_nested():
            await creation_db.execute(
                update(Creation).where(Creation.id == row["id"]).values({field: value})
            )


async def test_terminal_receipt_proof_and_history_cannot_change_or_disappear(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    await create(db, row)
    for values in (
        {"status": "allocated"},
        {"receipt": None},
        {"sha256": None},
        {"version_id": "invented"},
        {"operation_id": None},
        {"verification": {}},
        {"error_code": "rewritten"},
    ):
        with pytest.raises(IntegrityError, match="history is immutable"):
            async with db.begin_nested():
                await db.execute(
                    update(Creation).where(Creation.id == row["id"]).values(**values)
                )
    with pytest.raises(IntegrityError, match="history is immutable"):
        async with db.begin_nested():
            await db.execute(delete(Creation).where(Creation.id == row["id"]))


@pytest.mark.parametrize(
    "field",
    [
        "sha256",
        "size_bytes",
        "md5",
        "prepared",
        "receipt",
        "etag",
        "verification",
        "readback_at",
        "settled_at",
        "claimed_at",
        "operation_id",
        "completed_at",
    ],
)
async def test_verified_cannot_be_inserted_with_missing_evidence(
    creation_db, client, field
):
    db = creation_db
    row = await allocation(db)
    await create(db, row)
    values = await service._read(db, row["id"])
    values.update(
        id=uuid.uuid4(),
        object_key="other/" + uuid.uuid4().hex,
        operation_id=uuid.uuid4(),
    )
    values[field] = None
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(insert(Creation).values(**values))


async def test_retirement_null_link_cannot_be_backfilled(creation_db, client):
    db = creation_db
    row = await allocation(db)
    await create(db, row)
    retired = await assertion(db)
    # The baseline ORM intentionally has no new retirement column yet; its
    # owning integration change supplies it. SQL still exercises 0219's guard.
    from sqlalchemy import text

    with pytest.raises(IntegrityError, match="link is immutable"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "UPDATE managed_original_retirements SET creation_id=:creation WHERE id=:id"
                ),
                {"creation": row["id"], "id": retired.id},
            )
    assert retired.id and Retirement.__tablename__


async def test_unverified_receipt_cannot_attach_even_through_sql(creation_db):
    from tests.test_managed_original_creation import publication
    from types import SimpleNamespace

    db = creation_db
    row = await allocation(db)
    item, replica = await publication(
        db, row, SimpleNamespace(size_bytes=1, sha256="a" * 64, etag='"etag"')
    )
    with pytest.raises(IntegrityError, match="attachment does not match"):
        async with db.begin_nested():
            await db.execute(
                update(DatasetAssetLocation)
                .where(DatasetAssetLocation.asset_id == replica.asset_id)
                .values(creation_id=row["id"])
            )
    assert item.id
