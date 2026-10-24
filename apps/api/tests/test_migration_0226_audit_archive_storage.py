"""Permanent archive ownership checks in a disposable fixture-owned schema."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

CREATED = datetime(2026, 3, 1, tzinfo=timezone.utc)


def migration(connection):
    path = Path(__file__).parents[1] / "alembic/versions/0226_audit_archive_storage.py"
    spec = importlib.util.spec_from_file_location(
        "audit_archive_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def owned_db(test_engine):
    schema = "audit_archive_storage_test_" + uuid.uuid4().hex
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
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def reference(archive_id):
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    bucket = "archive-original"
    return {
        "schema": 1,
        "purpose": "audit-archive",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "object_key": f"2026/01/{archive_id}.jsonl.gz",
        "storage_identity": managed_storage_identity(endpoint, bucket),
    }


async def create(db, **changes):
    archive_id = changes.get("id", uuid.uuid4())
    row = AuditArchiveOutput(
        **{
            "id": archive_id,
            "job_id": uuid.uuid4(),
            "year": 2026,
            "month": 1,
            "source_schema": "public",
            "source_table": "audit_logs_y2026m01",
            "parent_oid": 100,
            "source_oid": 101,
            "month_start": date(2026, 1, 1),
            "month_end": date(2026, 2, 1),
            "format_revision": 1,
            "row_count": 0,
            "source_size_bytes": 0,
            "source_sha256": "a" * 64,
            "sha256": "b" * 64,
            "size_bytes": 100,
            "storage_ref": reference(archive_id),
            "state": "pending",
            "created_at": CREATED,
            "published_at": None,
            **changes,
        }
    )
    db.add(row)
    await db.flush()
    return row


async def test_schema_has_no_job_or_partition_lifetime_dependency(owned_db):
    connection = await owned_db.connection()
    assert (
        await connection.run_sync(
            lambda sync: inspect(sync).get_foreign_keys("audit_archive_outputs")
        )
        == []
    )
    indexes = await connection.run_sync(
        lambda sync: inspect(sync).get_indexes("audit_archive_outputs")
    )
    index = next(
        item for item in indexes if item["name"] == "uq_audit_archive_ready_month"
    )
    assert index["unique"]
    assert "ready" in str(index["dialect_options"]["postgresql_where"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", uuid.UUID(int=5)),
        ("job_id", uuid.UUID(int=6)),
        ("year", 2025),
        ("month", 2),
        ("source_schema", "other"),
        ("source_table", "other"),
        ("parent_oid", 102),
        ("source_oid", 103),
        ("month_start", date(2026, 1, 2)),
        ("month_end", date(2026, 2, 2)),
        ("format_revision", 2),
        ("row_count", 1),
        ("source_size_bytes", 1),
        ("source_sha256", "c" * 64),
        ("sha256", "c" * 64),
        ("size_bytes", 101),
        ("created_at", CREATED + timedelta(seconds=1)),
    ],
)
async def test_every_identity_and_content_fact_is_immutable(owned_db, field, value):
    row = await create(owned_db)
    archive_id = row.id
    with pytest.raises(IntegrityError, match="identity and content are immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(AuditArchiveOutput)
                .where(AuditArchiveOutput.id == archive_id)
                .values(**{field: value})
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"year": 1999},
        {"year": 9999, "month": 12},
        {"month": 0},
        {"month": 13},
        {"source_schema": "other"},
        {"source_table": "audit_logs_y2026m1"},
        {"month_start": date(2026, 1, 2)},
        {"month_end": date(2026, 2, 2)},
        {"parent_oid": 0},
        {"source_oid": 2**32},
        {"format_revision": 2},
        {"row_count": -1},
        {"row_count": 10_000_001},
        {"source_size_bytes": -1},
        {"source_size_bytes": 256 * 1024**3 + 1},
        {"size_bytes": 0},
        {"size_bytes": 64 * 1024**3 + 1},
        {"source_sha256": "A" * 64},
        {"sha256": "x" * 64},
        {"state": "retired"},
        {"state": "ready"},
        {"published_at": CREATED},
        {"state": "ready", "published_at": CREATED - timedelta(seconds=1)},
    ],
)
async def test_insert_requires_bounded_receipt_and_exact_month(owned_db, changes):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(owned_db, **changes)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", 1.0),
        ("schema", "1"),
        ("purpose", "exports"),
        ("location_id", str(uuid.UUID(int=2))),
        ("config_revision", 2),
        ("binding_generation", True),
        ("endpoint_url", "http://user:secret@host"),
        ("endpoint_url", "http://host/path"),
        ("bucket", ""),
        ("bucket", "bad/path"),
        ("object_key", "2026/01/archive.jsonl.gz"),
        ("storage_identity", "A" * 64),
        ("unknown", "secret"),
    ],
)
async def test_insert_rejects_ambiguous_or_unowned_reference(owned_db, field, value):
    archive_id = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(
                owned_db,
                id=archive_id,
                storage_ref={**reference(archive_id), field: value},
            )


@pytest.mark.parametrize("bad", [None, {}, [], True, "old-month-key"])
async def test_insert_requires_complete_independent_owner(owned_db, bad):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(owned_db, storage_ref=bad)


async def test_ready_transition_is_one_way_and_publication_is_frozen(owned_db):
    row = await create(owned_db)
    row.state, row.published_at = "ready", CREATED
    await owned_db.flush()
    archive_id = row.id
    for changes in (
        {"state": "pending", "published_at": None},
        {"published_at": CREATED + timedelta(seconds=1)},
    ):
        with pytest.raises(IntegrityError):
            async with owned_db.begin_nested():
                await owned_db.execute(
                    update(AuditArchiveOutput)
                    .where(AuditArchiveOutput.id == archive_id)
                    .values(**changes)
                )


async def test_only_one_ready_owner_per_month_but_candidates_survive(owned_db):
    await create(owned_db, state="ready", published_at=CREATED)
    await create(owned_db)
    await create(owned_db)
    with pytest.raises(IntegrityError, match="uq_audit_archive_ready_month"):
        async with owned_db.begin_nested():
            await create(owned_db, state="ready", published_at=CREATED)


@pytest.mark.parametrize("ready", [False, True])
async def test_permanent_owner_cannot_be_deleted_or_downgraded(owned_db, ready):
    row = await create(
        owned_db,
        state="ready" if ready else "pending",
        published_at=CREATED if ready else None,
    )
    archive_id = row.id
    with pytest.raises(IntegrityError, match="ownership is permanent"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                delete(AuditArchiveOutput).where(AuditArchiveOutput.id == archive_id)
            )
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())


async def test_shared_restore_whitelist_changes_only_bucket_and_identity(owned_db):
    row = await create(owned_db)
    archive_id, original = row.id, deepcopy(row.storage_ref)
    restored = {
        **original,
        "bucket": "archive-restored",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "archive-restored"
        ),
    }
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(AuditArchiveOutput)
                .where(AuditArchiveOutput.id == archive_id)
                .values(storage_ref=restored)
            )
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await owned_db.execute(
        update(AuditArchiveOutput)
        .where(AuditArchiveOutput.id == archive_id)
        .values(storage_ref=restored)
    )
    await owned_db.refresh(row)
    assert row.storage_ref == restored
    for changes in (
        {"storage_ref": {**restored, "endpoint_url": "https://trap.invalid"}},
        {"source_oid": 102},
        {"source_sha256": "c" * 64},
    ):
        with pytest.raises(IntegrityError):
            async with owned_db.begin_nested():
                await owned_db.execute(
                    update(AuditArchiveOutput)
                    .where(AuditArchiveOutput.id == archive_id)
                    .values(**changes)
                )


async def test_runtime_role_cannot_forge_restore_marker(owned_db):
    row = await create(owned_db)
    archive_id, original = row.id, deepcopy(row.storage_ref)
    schema = await owned_db.scalar(text("SELECT current_schema()"))
    current = await owned_db.scalar(text("SELECT current_user"))
    role = make_url(settings.database_url).username
    if role == current:
        pytest.skip("This configured test target has no distinct runtime role")
    if not await owned_db.scalar(
        text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"),
        {"role": role},
    ):
        pytest.skip("Configured runtime role does not exist in this test server")
    quoted = '"' + role.replace('"', '""') + '"'
    await owned_db.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {quoted}'))
    await owned_db.execute(
        text(f'GRANT SELECT,UPDATE ON "{schema}".audit_archive_outputs TO {quoted}')
    )
    restored = {
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
                update(AuditArchiveOutput)
                .where(AuditArchiveOutput.id == archive_id)
                .values(storage_ref=restored)
            )


async def test_empty_downgrade_preserves_shared_guard(owned_db):
    definition = text(
        "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
    )
    before = await owned_db.scalar(definition)
    connection = await owned_db.connection()
    await connection.run_sync(lambda sync: migration(sync).downgrade())
    assert await owned_db.scalar(definition) == before
    assert not await connection.run_sync(
        lambda sync: inspect(sync).has_table(
            "audit_archive_outputs", schema=sync.scalar(text("SELECT current_schema()"))
        )
    )
