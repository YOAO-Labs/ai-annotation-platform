"""Pinned storage survives ordinary updates and only explicit owner restore maps it."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.training_dataset import TrainingDatasetVersion as Version
from app.db.models.training_storage_deletion import TrainingStorageDeletion as Deletion
from app.services import training_dataset_storage as service
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity


def migration(connection):
    path = (
        Path(__file__).parents[1] / "alembic/versions/0220_training_dataset_storage.py"
    )
    spec = importlib.util.spec_from_file_location(
        "training_storage_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def owned_db(test_engine):
    schema = "training_storage_test_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        source = await connection.scalar(text("SELECT current_schema()"))
        for table in ("training_dataset_versions", "training_storage_deletions"):
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}"."{table}" (LIKE "{source}"."{table}" INCLUDING ALL)'
                )
            )
            await connection.execute(
                text(
                    f'ALTER TABLE "{schema}"."{table}" DROP COLUMN IF EXISTS storage_ref'
                )
            )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: migration(sync).upgrade())
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def values():
    project, identifier = uuid.uuid4(), uuid.uuid4()
    return {
        "id": identifier,
        "project_id": project,
        "request_id": uuid.uuid4(),
        "request_digest": "a" * 64,
        "name": "historical",
        "status": "failed",
        "snapshot": {"fixed": "snapshot"},
        "snapshot_digest": "b" * 64,
        "manifest": {"fixed": "manifest"},
    }


def reference(row):
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return {
        "schema": 1,
        "purpose": "training-dataset",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "training-original",
        "prefix": service.prefix_for(row.project_id, row.id),
        "storage_identity": managed_storage_identity(endpoint, "training-original"),
        "origin": "new",
        "evidence": None,
    }


async def create(db, *, pinned=False):
    row = Version(**values())
    db.add(row)
    await db.flush()
    if pinned:
        row.storage_ref = reference(row)
        await db.flush()
    return row


async def test_empty_schema_roundtrip_keeps_legacy_data_unpinned(owned_db):
    db = owned_db
    row = await create(db)
    identifier = row.id
    snapshot = deepcopy(row.snapshot)
    await db.commit()
    async with db.bind.begin() as connection:

        def check(sync):
            module = migration(sync)
            assert module.down_revision == "0219"
            schema = sync.scalar(text("SELECT current_schema()"))
            module.downgrade()
            for table in ("training_dataset_versions", "training_storage_deletions"):
                assert "storage_ref" not in {
                    c["name"] for c in inspect(sync).get_columns(table, schema=schema)
                }
            module.upgrade()

        await connection.run_sync(check)
    await db.refresh(row)
    assert row.id == identifier and row.snapshot == snapshot and row.storage_ref is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("bucket", "other"),
        ("prefix", "projects/other/datasets/other/"),
        ("storage_identity", "f" * 64),
        ("evidence", {}),
        ("origin", "verified-retained"),
        ("endpoint_url", "http://other.invalid"),
    ],
)
async def test_existing_pin_rejects_ordinary_json_changes(owned_db, field, value):
    row = await create(owned_db, pinned=True)
    changed = {**row.storage_ref, field: value}
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Version).where(Version.id == row.id).values(storage_ref=changed)
            )
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Version).where(Version.id == row.id).values(storage_ref=None)
            )


@pytest.mark.parametrize("bad", [{}, [], None, "text"])
async def test_first_pin_requires_complete_object(owned_db, bad):
    row = await create(owned_db)
    # SQL NULL is valid historical state; JSON null is not a locator.
    serialized = __import__("json").dumps(bad)
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text(
                    "UPDATE training_dataset_versions SET storage_ref=CAST(:ref AS jsonb) WHERE id=:id"
                ),
                {"ref": serialized, "id": row.id},
            )


async def test_version_and_intent_locator_must_match_owner(owned_db):
    row = await create(owned_db)
    wrong = reference(row)
    wrong["prefix"] = service.prefix_for(uuid.uuid4(), row.id)
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Version).where(Version.id == row.id).values(storage_ref=wrong)
            )
    valid = reference(row)
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            owned_db.add(
                Deletion(bucket="wrong", prefix=valid["prefix"], storage_ref=valid)
            )
            await owned_db.flush()


async def test_owner_restore_can_only_map_bucket_and_identity(owned_db):
    db = owned_db
    row = await create(db, pinned=True)
    original = deepcopy(row.storage_ref)
    mapped = {
        **original,
        "bucket": "training-restored",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "training-restored"
        ),
    }
    await db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await db.execute(
        update(Version).where(Version.id == row.id).values(storage_ref=mapped)
    )
    await db.refresh(row)
    assert row.storage_ref == mapped
    for changed in [
        {**mapped, "evidence": {"rewritten": True}},
        {**mapped, "bucket": "training-another"},
        {**mapped, "storage_identity": "0" * 64},
    ]:
        with pytest.raises(IntegrityError, match="ownership is immutable"):
            async with db.begin_nested():
                await db.execute(
                    update(Version)
                    .where(Version.id == row.id)
                    .values(storage_ref=changed)
                )
    await db.execute(text("SET LOCAL aap.restore_training_storage='off'"))
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with db.begin_nested():
            await db.execute(
                update(Version).where(Version.id == row.id).values(storage_ref=original)
            )


async def test_runtime_role_cannot_forge_restore_marker(owned_db):
    db = owned_db
    row = await create(db, pinned=True)
    identifier = row.id
    original = deepcopy(row.storage_ref)
    schema = await db.scalar(text("SELECT current_schema()"))
    current = await db.scalar(text("SELECT current_user"))
    role = make_url(settings.database_url).username
    if role == current:
        pytest.skip("This configured test target has no distinct runtime role")
    exists = await db.scalar(
        text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"),
        {"role": role},
    )
    if not exists:
        pytest.skip("Configured runtime role does not exist in this test server")
    quoted = '"' + role.replace('"', '""') + '"'
    await db.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {quoted}'))
    await db.execute(
        text(f'GRANT SELECT,UPDATE ON "{schema}".training_dataset_versions TO {quoted}')
    )
    changed = {
        **original,
        "bucket": "runtime-trap",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "runtime-trap"
        ),
    }
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with db.begin_nested():
            await db.execute(text(f"SET LOCAL ROLE {quoted}"))
            await db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
            await db.execute(
                update(Version)
                .where(Version.id == identifier)
                .values(storage_ref=changed)
            )
    assert await db.scalar(text("SELECT current_user")) == current


@pytest.mark.parametrize("kind", ["version", "intent"])
async def test_pinned_facts_prevent_destructive_downgrade(owned_db, kind):
    row = await create(owned_db, pinned=kind == "version")
    if kind == "intent":
        ref = reference(row)
        owned_db.add(
            Deletion(bucket=ref["bucket"], prefix=ref["prefix"], storage_ref=ref)
        )
        await owned_db.flush()
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())
    assert "storage_ref" in Version.__table__.columns
