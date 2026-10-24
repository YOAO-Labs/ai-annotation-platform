"""Database constraints for fixture-owned RLE schemas; run only on test targets."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import gzip
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.raster_mask_object import (
    RasterMaskObject as Object,
    RasterMaskPublication as Publication,
)
from app.services import raster_object_storage as storage
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
RAW = b'{"encoding":"coco_rle","size":[4,4],"counts":[0,5,11]}'
SHA = hashlib.sha256(RAW).hexdigest()
PORTABLE = {
    "encoding": "coco_rle_ref",
    "size": [4, 4],
    "object_key": f"raster-masks/sha256/{SHA[:2]}/{SHA[2:4]}/{SHA}.json",
    "sha256": SHA,
    "runs": 3,
    "bytes": len(RAW),
}


def migration(connection):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0229_raster_mask_object_storage.py"
    )
    spec = importlib.util.spec_from_file_location(
        "raster_storage_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def owned_db(test_engine):
    schema = "raster_storage_test_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: migration(sync).upgrade())
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.info["owned_engine"] = engine
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def locator(identifier, bucket="rle-original", encoding="identity"):
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return {
        "schema": 1,
        "purpose": "annotations",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "object_key": storage.key_for(identifier, encoding),
        "storage_identity": managed_storage_identity(endpoint, bucket),
    }


async def create(db, **changes):
    values = {
        "id": uuid.uuid4(),
        **storage.content(PORTABLE, RAW),
        "created_at": CREATED,
        "state": "pending",
        **changes,
    }
    if values["state"] == "ready":
        values.setdefault("published_at", CREATED + timedelta(seconds=1))
        values.setdefault("last_used_at", values["published_at"])
    if values["state"] == "retired":
        values.setdefault("retired_at", CREATED + timedelta(seconds=3))
    values.setdefault(
        "storage_ref", locator(values["id"], encoding=values["storage_encoding"])
    )
    row = Object(**values)
    db.add(row)
    await db.flush()
    return row


async def guard(db, identifier, **changes):
    row = Publication(
        id=uuid.uuid4(),
        object_id=identifier,
        gate_revision=1,
        created_at=CREATED + timedelta(seconds=1),
        **changes,
    )
    db.add(row)
    await db.flush()
    return row


async def test_empty_roundtrip_has_no_business_fk_and_preserves_shared_restore_guard(
    owned_db,
):
    connection = await owned_db.connection()

    def roundtrip(sync):
        module = migration(sync)
        assert module.down_revision == "0228"
        schema = sync.scalar(text("SELECT current_schema()"))
        assert (
            inspect(sync).get_foreign_keys("raster_mask_objects", schema=schema) == []
        )
        foreign = inspect(sync).get_foreign_keys(
            "raster_mask_publications", schema=schema
        )
        assert (
            len(foreign) == 1 and foreign[0]["referred_table"] == "raster_mask_objects"
        )
        assert foreign[0]["options"]["ondelete"] == "RESTRICT"
        before = sync.scalar(
            text(
                "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
            )
        )
        module.downgrade()
        assert not inspect(sync).has_table("raster_mask_objects", schema=schema)
        assert (
            sync.scalar(
                text(
                    "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
                )
            )
            == before
        )
        module.upgrade()

    await connection.run_sync(roundtrip)
    row = await create(owned_db)
    assert row.state == "pending" and row.published_at is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", uuid.uuid4()),
        ("logical_key", "raster-masks/sha256/forged.json"),
        ("canonical_sha256", "b" * 64),
        ("canonical_size_bytes", 100),
        ("stored_sha256", "b" * 64),
        ("stored_size_bytes", 100),
        ("storage_encoding", "gzip"),
        ("height", 8),
        ("width", 8),
        ("runs", 5),
        ("created_at", CREATED + timedelta(seconds=1)),
    ],
)
async def test_content_and_identity_are_immutable(owned_db, field, value):
    identifier = (await create(owned_db)).id
    with pytest.raises(IntegrityError, match="identity and content are immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Object).where(Object.id == identifier).values(**{field: value})
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", "1"),
        ("schema", 1.0),
        ("purpose", "training-run"),
        ("location_id", str(uuid.uuid4())),
        ("config_revision", 2),
        ("config_revision", True),
        ("binding_generation", 1.0),
        ("bucket", "bad/path"),
        ("bucket", "bad\\path"),
        ("bucket", "bad bucket"),
        ("endpoint_url", "http://user:secret@storage.invalid"),
        ("endpoint_url", "http://storage.invalid/path"),
        ("object_key", PORTABLE["object_key"]),
        ("storage_identity", "A" * 64),
        ("credential", "forbidden"),
    ],
)
async def test_insert_requires_strict_physical_envelope(owned_db, field, value):
    identifier = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(
                owned_db,
                id=identifier,
                storage_ref={**locator(identifier), field: value},
            )


@pytest.mark.parametrize("bad", [None, {}, [], "text", True])
async def test_insert_rejects_json_null_and_non_object_locator(owned_db, bad):
    identifier = uuid.uuid4()
    row = Object.__table__
    values = {
        "id": identifier,
        **storage.content(PORTABLE, RAW),
        "created_at": CREATED,
        "storage_ref": bad,
    }
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(row.insert().values(**values))


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical_sha256": "A" * 64},
        {"canonical_size_bytes": 0},
        {"stored_size_bytes": 8388609},
        {"height": 8193},
        {"width": 0},
        {"runs": 1000001},
        {"storage_encoding": "other"},
        {"logical_key": "wrong"},
        {"stored_sha256": "b" * 64},
        {"stored_size_bytes": 1},
        {"published_at": CREATED},
        {"last_used_at": CREATED},
        {"state": "ready", "published_at": None},
        {"state": "ready", "last_used_at": None},
        {"state": "ready", "published_at": CREATED - timedelta(seconds=1)},
        {"state": "retired", "retired_at": CREATED - timedelta(seconds=1)},
    ],
)
async def test_content_bounds_and_temporal_state_are_checked(owned_db, changes):
    with pytest.raises((IntegrityError, DBAPIError)):
        async with owned_db.begin_nested():
            await create(owned_db, **changes)


async def test_gzip_has_separate_stored_receipt_and_exact_encoding_keys(owned_db):
    compressed = gzip.compress(RAW, mtime=1)
    portable = {
        **PORTABLE,
        "object_key": PORTABLE["object_key"] + ".gz",
        "storage_encoding": "gzip",
    }
    facts = storage.content(portable, compressed)
    row = await create(owned_db, **facts)
    assert row.canonical_sha256 != row.stored_sha256
    assert row.storage_ref["object_key"].endswith(".json.gz")
    for changes in (
        {"canonical_size_bytes": 1000, "stored_size_bytes": 1},
        {"logical_key": PORTABLE["object_key"]},
        {"storage_ref": locator(uuid.uuid4(), encoding="identity")},
    ):
        with pytest.raises(IntegrityError):
            async with owned_db.begin_nested():
                await create(owned_db, **{**facts, **changes})


async def test_ready_is_unique_globally_but_pending_and_retired_candidates_survive(
    owned_db,
):
    pending = await create(owned_db)
    await create(owned_db, state="retired")
    ready = await create(owned_db, state="ready")
    pending_id, ready_id = pending.id, ready.id
    identifier = uuid.uuid4()
    with pytest.raises(IntegrityError, match="uq_raster_object_ready_logical_key"):
        async with owned_db.begin_nested():
            await create(
                owned_db,
                id=identifier,
                state="ready",
                storage_ref=locator(identifier, bucket="rle-other"),
            )
    await owned_db.execute(
        update(Object)
        .where(Object.id == ready_id)
        .values(state="retired", retired_at=CREATED + timedelta(seconds=2))
    )
    await owned_db.execute(
        update(Object)
        .where(Object.id == pending_id)
        .values(
            state="ready",
            published_at=CREATED + timedelta(seconds=3),
            last_used_at=CREATED + timedelta(seconds=3),
        )
    )
    await owned_db.flush()
    assert (
        await owned_db.scalar(select(Object.id).where(Object.state == "ready"))
        == pending_id
    )


async def test_publication_and_successful_use_clocks_only_move_forward(owned_db):
    identifier = (await create(owned_db)).id
    published = CREATED + timedelta(seconds=1)
    await owned_db.execute(
        update(Object)
        .where(Object.id == identifier)
        .values(state="ready", published_at=published, last_used_at=published)
    )
    used = published + timedelta(seconds=2)
    await owned_db.execute(
        update(Object).where(Object.id == identifier).values(last_used_at=used)
    )
    for changes in (
        {"published_at": used},
        {"published_at": None},
        {"last_used_at": published},
        {"last_used_at": None},
        {"state": "pending", "published_at": None, "last_used_at": None},
        {"state": "retired", "retired_at": published},
    ):
        with pytest.raises(IntegrityError):
            async with owned_db.begin_nested():
                await owned_db.execute(
                    update(Object).where(Object.id == identifier).values(**changes)
                )
    retired = used + timedelta(seconds=1)
    await owned_db.execute(
        update(Object)
        .where(Object.id == identifier)
        .values(state="retired", retired_at=retired)
    )
    for changes in (
        {"state": "ready", "retired_at": None},
        {"retired_at": retired + timedelta(seconds=1)},
        {"last_used_at": retired},
    ):
        with pytest.raises(IntegrityError):
            async with owned_db.begin_nested():
                await owned_db.execute(
                    update(Object).where(Object.id == identifier).values(**changes)
                )


@pytest.mark.parametrize("state", ["pending", "ready"])
async def test_durable_guard_blocks_delete_and_retirement_until_explicitly_removed(
    owned_db, state
):
    identifier = (await create(owned_db, state=state)).id
    publication_id = (await guard(owned_db, identifier)).id
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(delete(Object).where(Object.id == identifier))
    with pytest.raises(IntegrityError, match="Guarded RLE objects cannot retire"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Object)
                .where(Object.id == identifier)
                .values(state="retired", retired_at=CREATED + timedelta(days=400))
            )
    assert await owned_db.get(Publication, publication_id) is not None
    await owned_db.execute(delete(Publication).where(Publication.id == publication_id))
    await owned_db.execute(
        update(Object)
        .where(Object.id == identifier)
        .values(state="retired", retired_at=CREATED + timedelta(days=400))
    )
    await owned_db.execute(delete(Object).where(Object.id == identifier))
    assert await owned_db.get(Object, identifier) is None


async def test_guard_cannot_attach_to_retired_or_missing_candidate(owned_db):
    retired_id = (await create(owned_db, state="retired")).id
    for identifier in (retired_id, uuid.uuid4()):
        with pytest.raises(IntegrityError, match="pending or ready owner"):
            async with owned_db.begin_nested():
                await guard(owned_db, identifier)


@pytest.mark.parametrize("field", ["id", "object_id", "gate_revision", "created_at"])
async def test_publication_facts_are_immutable(owned_db, field):
    identifier = (await create(owned_db)).id
    publication_id = (await guard(owned_db, identifier)).id
    value = {
        "id": uuid.uuid4(),
        "object_id": uuid.uuid4(),
        "gate_revision": 2,
        "created_at": CREATED,
    }[field]
    with pytest.raises(IntegrityError, match="publication facts are immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Publication)
                .where(Publication.id == publication_id)
                .values(**{field: value})
            )


@pytest.mark.parametrize("revision", [0, -1])
async def test_publication_requires_positive_gate_revision(owned_db, revision):
    identifier = (await create(owned_db)).id
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            owned_db.add(
                Publication(
                    id=uuid.uuid4(), object_id=identifier, gate_revision=revision
                )
            )
            await owned_db.flush()


@pytest.mark.parametrize("state", ["pending", "ready", "retired"])
async def test_controlled_restore_preserves_content_times_guards_and_keys(
    owned_db, state
):
    row = await create(owned_db, state=state)
    identifier, original = row.id, deepcopy(row.storage_ref)
    publication_id = (
        (await guard(owned_db, identifier)).id if state != "retired" else None
    )
    before = {
        name: getattr(row, name)
        for name in (
            "logical_key",
            "canonical_sha256",
            "canonical_size_bytes",
            "stored_sha256",
            "stored_size_bytes",
            "created_at",
            "published_at",
            "last_used_at",
            "retired_at",
            "state",
        )
    }
    mapped = {
        **original,
        "bucket": "rle-restored",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "rle-restored"
        ),
    }
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await owned_db.execute(
        update(Object).where(Object.id == identifier).values(storage_ref=mapped)
    )
    await owned_db.refresh(row)
    assert row.storage_ref == mapped
    assert all(getattr(row, name) == value for name, value in before.items())
    if publication_id:
        assert (await owned_db.get(Publication, publication_id)).object_id == identifier
    for field, value in (
        ("object_key", storage.key_for(uuid.uuid4())),
        ("endpoint_url", "https://trap.invalid"),
        ("config_revision", 2),
    ):
        with pytest.raises(IntegrityError, match="ownership is immutable"):
            async with owned_db.begin_nested():
                await owned_db.execute(
                    update(Object)
                    .where(Object.id == identifier)
                    .values(storage_ref={**mapped, field: value})
                )
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='off'"))
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Object)
                .where(Object.id == identifier)
                .values(storage_ref=original)
            )


async def test_runtime_role_cannot_forge_restore_marker(owned_db):
    row = await create(owned_db)
    identifier, original = row.id, deepcopy(row.storage_ref)
    schema = await owned_db.scalar(text("SELECT current_schema()"))
    current = await owned_db.scalar(text("SELECT current_user"))
    role = make_url(settings.database_url).username
    if role == current or not await owned_db.scalar(
        text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"),
        {"role": role},
    ):
        pytest.skip("This configured test target has no distinct runtime role")
    quoted = '"' + role.replace('"', '""') + '"'
    await owned_db.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {quoted}'))
    await owned_db.execute(
        text(f'GRANT SELECT,UPDATE ON "{schema}".raster_mask_objects TO {quoted}')
    )
    mapped = {
        **original,
        "bucket": "runtime-trap",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "runtime-trap"
        ),
    }
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(text(f"SET LOCAL ROLE {quoted}"))
            await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
            await owned_db.execute(
                update(Object).where(Object.id == identifier).values(storage_ref=mapped)
            )
    assert await owned_db.scalar(text("SELECT current_user")) == current


@pytest.mark.parametrize("state", ["pending", "ready", "retired"])
async def test_any_owner_prevents_destructive_downgrade(owned_db, state):
    await create(owned_db, state=state)
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())
