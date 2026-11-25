"""Creation facts survive rollback and have no business ownership cascades."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import MetaData, Table, inspect, null, select, text
from sqlalchemy.exc import IntegrityError

from tests.migration_helpers import bind, load


CREATION_COLUMNS_0219 = frozenset(
    """
    id dataset_id location_id storage_identity bucket object_key logical_path origin
    max_bytes timeout_seconds content_type requested_by authority configuration
    input_fingerprint maintenance_revision staging_bucket staging_key status operation_id
    source prepared sha256 md5 size_bytes receipt etag version_id request_id verification
    error_code allocated_at claimed_at frozen_at write_started_at acknowledged_at
    readback_at settled_at completed_at
    """.split()
)


def creation_table(connection):
    return Table(
        "managed_original_creations",
        MetaData(),
        schema="public",
        autoload_with=connection,
    )


def allocation_values():
    identifier = uuid.uuid4()
    return {
        "id": identifier,
        "dataset_id": uuid.uuid4(),
        "location_id": uuid.uuid4(),
        "storage_identity": "a" * 64,
        "bucket": "historical",
        "object_key": f"originals/{identifier}",
        "logical_path": "images/original.bin",
        "origin": "zip",
        "max_bytes": 10,
        "timeout_seconds": 60,
        "content_type": "application/octet-stream",
        "requested_by": uuid.uuid4(),
        "authority": {"source": "zip"},
        "configuration": {"bucket": "historical"},
        "input_fingerprint": "c" * 64,
        "maintenance_revision": 1,
    }


def verified_evidence():
    now = datetime.now(timezone.utc)
    proof = {"sha256": "d" * 64, "md5": "e" * 32, "size_bytes": 8}
    return {
        **proof,
        "status": "verified",
        "operation_id": uuid.uuid4(),
        "prepared": proof,
        "receipt": {"etag": '"original"'},
        "etag": '"original"',
        "verification": proof,
        "claimed_at": now,
        "frozen_at": now,
        "write_started_at": now,
        "acknowledged_at": now,
        "readback_at": now,
        "settled_at": now,
        "completed_at": now,
    }


@asynccontextmanager
async def historical_creation_database(test_db_url, revision):
    from tests.managed_image_inference_fixtures import _migrated_database

    async with _migrated_database(test_db_url, revision, template="template0") as (
        _,
        engine,
        _,
    ):
        yield engine


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def creation_history(test_db_url):
    async with historical_creation_database(test_db_url, "0219") as engine:
        async with engine.connect() as db:
            creations = await db.run_sync(creation_table)
        yield engine, creations


@pytest.fixture
async def creation_db(creation_history):
    engine, creations = creation_history
    async with engine.connect() as db:
        yield db, creations


async def test_empty_creation_schema_roundtrip_and_exact_columns(creation_db):
    db, _ = creation_db

    def check(sync):
        module = bind(load("0219_managed_original_creations.py"), sync)
        assert module.down_revision == "0218"
        module.downgrade()
        assert not inspect(sync).has_table(
            "managed_original_creations", schema="public"
        )
        module.upgrade()
        assert set(creation_table(sync).c.keys()) == CREATION_COLUMNS_0219
        assert not inspect(sync).get_foreign_keys(
            "managed_original_creations", schema="public"
        )
        for table in ("dataset_asset_locations", "managed_original_retirements"):
            foreign_keys = inspect(sync).get_foreign_keys(table, schema="public")
            assert any(
                fk["constrained_columns"] == ["creation_id"]
                and fk["referred_table"] == "managed_original_creations"
                and fk["options"]["ondelete"] == "RESTRICT"
                for fk in foreign_keys
            )

    await db.run_sync(check)


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
    db, creations = creation_db
    row = allocation_values()
    await db.execute(creations.insert(), row)
    target = creations.c.id == row["id"]
    with pytest.raises(IntegrityError, match="history is immutable"):
        async with db.begin_nested():
            await db.execute(creations.update().where(target).values({field: value}))
    original = (await db.execute(select(creations.c[field]).where(target))).scalar_one()
    assert original == row.get(field)


async def test_terminal_receipt_proof_and_history_cannot_change_or_disappear(
    creation_db,
):
    db, creations = creation_db
    result = await db.execute(
        creations.insert().returning(creations),
        allocation_values() | verified_evidence(),
    )
    row = result.mappings().one()
    target = creations.c.id == row["id"]
    for values in (
        {"status": "allocated"},
        {"receipt": null()},
        {"sha256": None},
        {"version_id": "invented"},
        {"operation_id": None},
        {"verification": {}},
        {"error_code": "rewritten"},
    ):
        with pytest.raises(IntegrityError, match="history is immutable"):
            async with db.begin_nested():
                await db.execute(creations.update().where(target).values(**values))
    with pytest.raises(IntegrityError, match="history is immutable"):
        async with db.begin_nested():
            await db.execute(creations.delete().where(target))
    with pytest.raises(
        RuntimeError, match="creation facts prevent destructive downgrade"
    ):
        async with db.begin_nested():
            module = load("0219_managed_original_creations.py")
            await db.run_sync(lambda sync: bind(module, sync).downgrade())
    assert (await db.execute(select(creations).where(target))).mappings().one() == row


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
async def test_verified_cannot_be_inserted_with_missing_evidence(creation_db, field):
    db, creations = creation_db
    await db.execute(creations.insert(), allocation_values() | verified_evidence())
    candidate = allocation_values() | verified_evidence()
    del candidate[field]
    with pytest.raises(IntegrityError, match="ck_managed_creation_"):
        async with db.begin_nested():
            await db.execute(creations.insert(), candidate)


async def test_retirement_null_link_cannot_be_backfilled(creation_db):
    db, creations = creation_db
    row = allocation_values() | verified_evidence()
    await db.execute(creations.insert(), row)
    retired = await db.scalar(
        text("""
            INSERT INTO managed_original_retirements
                (id, assertion_digest, dataset_id, dataset_item_id, asset_id, location_id,
                 bucket, object_key, role, identity_state, storage_identity,
                 asset_verification_state, sha256, size_bytes, reference_state,
                 dataset_lifecycle_revision)
            VALUES (gen_random_uuid(), :sha256, :dataset_id, gen_random_uuid(),
                    gen_random_uuid(), :location_id, :bucket, :object_key,
                    'managed_original', 'verified_binding', :storage_identity,
                    'verified', :sha256, :size_bytes, 'present', 1)
            RETURNING id
        """),
        row,
    )
    with pytest.raises(IntegrityError, match="link is immutable"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "UPDATE managed_original_retirements SET creation_id=:creation WHERE id=:id"
                ),
                {"creation": row["id"], "id": retired},
            )
    assert (
        await db.execute(
            text("SELECT creation_id FROM managed_original_retirements WHERE id=:id"),
            {"id": retired},
        )
    ).scalar_one() is None


async def test_unverified_receipt_cannot_attach_even_through_sql(creation_db):
    db, creations = creation_db
    allocated = allocation_values()
    row = allocated | verified_evidence()
    terminal = {
        field: row.pop(field)
        for field in (
            "status",
            "verification",
            "readback_at",
            "settled_at",
            "completed_at",
        )
    }
    asset_id = await db.scalar(
        text("""
            WITH actor AS (
                INSERT INTO users (id, email, name, password_hash)
                VALUES (:requested_by, 'creation@example.invalid', 'Creator', 'unused')
                RETURNING id
            ), dataset AS (
                INSERT INTO datasets (id, display_id, name, created_by)
                SELECT :dataset_id, 'historical', 'Historical', id FROM actor RETURNING id
            ), item AS (
                INSERT INTO dataset_items (id, dataset_id, file_name, file_path, file_size)
                SELECT gen_random_uuid(), id, :logical_path, :object_key, :size_bytes
                FROM dataset RETURNING id
            ), asset AS (
                INSERT INTO dataset_assets (id, dataset_item_id, media_type, checksum_algorithm,
                                            checksum_value, size_bytes, verification_state)
                SELECT gen_random_uuid(), id, 'image', 'sha256', :sha256, :size_bytes, 'verified'
                FROM item RETURNING id
            ), location AS (
                INSERT INTO storage_locations (id, name, kind, config)
                VALUES (:location_id, 'originals', 's3', '{}'::jsonb) RETURNING id
            )
            INSERT INTO dataset_asset_locations
                (asset_id, location_id, bucket, object_key, role, etag)
            SELECT asset.id, location.id, :bucket, :object_key, 'managed_original', :etag
            FROM asset, location RETURNING asset_id
        """),
        row,
    )
    attach = text(
        "UPDATE dataset_asset_locations SET creation_id=:id "
        "WHERE asset_id=:asset RETURNING creation_id"
    ).bindparams(id=row["id"], asset=asset_id)
    with pytest.raises(IntegrityError, match="attachment does not match"):
        async with db.begin_nested():
            await db.execute(creations.insert(), allocated)
            await db.execute(attach)
    await db.execute(creations.insert(), row | {"status": "written"})
    with pytest.raises(IntegrityError, match="attachment does not match"):
        async with db.begin_nested():
            await db.execute(attach)
    await db.execute(
        creations.update().where(creations.c.id == row["id"]).values(**terminal)
    )
    assert (await db.execute(attach)).scalar_one() == row["id"]
