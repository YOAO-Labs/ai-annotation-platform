"""Export ownership migration checks in a disposable, fixture-owned schema."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import MetaData, Table, inspect, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import registry

from app.config import settings
from app.db.models.export_output import ExportOutput as CurrentExportOutput
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity


# Keep this migration test on the historical schema. The current ORM also has
# the 0234 media association, whose separate migration is qualified elsewhere.
class ExportOutput:
    pass


registry().map_imperatively(
    ExportOutput,
    Table(
        "export_outputs",
        MetaData(),
        *(
            column._copy()
            for column in CurrentExportOutput.__table__.columns
            if column.name != "media_bundle_id"
        ),
    ),
)


CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
LEGACY_CACHE = uuid.UUID(int=98)


def migration(connection):
    path = Path(__file__).parents[1] / "alembic/versions/0224_export_output_storage.py"
    spec = importlib.util.spec_from_file_location(
        "export_storage_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def owned_db(test_engine):
    schema = "export_output_storage_test_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            # Only the predecessor column touched by this migration is needed.
            # A legacy cache row must remain an unproven NULL association.
            await connection.execute(
                text("CREATE TABLE export_artifacts (id uuid PRIMARY KEY)")
            )
            await connection.execute(
                text("INSERT INTO export_artifacts (id) VALUES (:id)"),
                {"id": LEGACY_CACHE},
            )
            await connection.run_sync(lambda sync: migration(sync).upgrade())
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def reference(output_id, project_id, job_id, media="image"):
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return {
        "schema": 1,
        "purpose": "export",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "exports-original",
        "object_key": f"{media}/{project_id}/{job_id}/{output_id}.zip",
        "storage_identity": managed_storage_identity(endpoint, "exports-original"),
    }


async def create(db, **changes):
    values = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "media": "image",
        "format": "coco,yolo-det",
        "file_count": 2,
        "sha256": "a" * 64,
        "size_bytes": 101,
        "created_at": CREATED,
        "expires_at": CREATED + timedelta(days=7),
        **changes,
    }
    if "storage_ref" not in values:
        values["storage_ref"] = reference(
            values["id"], values["project_id"], values["job_id"], values["media"]
        )
    row = ExportOutput(**values)
    db.add(row)
    await db.flush()
    return row


async def test_empty_roundtrip_preserves_legacy_cache_and_shared_guard(owned_db):
    connection = await owned_db.connection()

    def roundtrip(sync):
        module = migration(sync)
        assert module.down_revision == "0223"
        schema = sync.scalar(text("SELECT current_schema()"))
        assert inspect(sync).get_foreign_keys("export_outputs", schema=schema) == []
        foreign_keys = inspect(sync).get_foreign_keys("export_artifacts", schema=schema)
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "export_outputs"
        assert foreign_keys[0]["constrained_columns"] == ["output_id"]
        assert foreign_keys[0]["options"]["ondelete"] == "RESTRICT"
        assert "ix_export_artifacts_output_id" in {
            item["name"]
            for item in inspect(sync).get_indexes("export_artifacts", schema=schema)
        }
        assert (
            sync.scalar(
                text("SELECT output_id FROM export_artifacts WHERE id=:id"),
                {"id": LEGACY_CACHE},
            )
            is None
        )
        shared_before = sync.scalar(
            text(
                "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
            )
        )
        module.downgrade()
        assert not inspect(sync).has_table("export_outputs", schema=schema)
        assert (
            sync.scalar(
                text("SELECT count(*) FROM export_artifacts WHERE id=:id"),
                {"id": LEGACY_CACHE},
            )
            == 1
        )
        assert (
            sync.scalar(
                text(
                    "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
                )
            )
            == shared_before
        )
        module.upgrade()
        assert inspect(sync).has_table("export_outputs", schema=schema)

    await connection.run_sync(roundtrip)
    row = await create(owned_db)
    assert row.state == "pending"
    assert row.retired_at is None
    assert row.expires_at - row.created_at == timedelta(days=7)


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "project_id",
        "job_id",
        "media",
        "format",
        "file_count",
        "sha256",
        "size_bytes",
        "created_at",
        "expires_at",
    ],
)
async def test_all_identity_content_and_deadline_facts_are_immutable(owned_db, field):
    row = await create(owned_db)
    changed = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "media": "video",
        "format": "voc",
        "file_count": 3,
        "sha256": "b" * 64,
        "size_bytes": 102,
        "created_at": row.created_at + timedelta(seconds=1),
        "expires_at": row.expires_at - timedelta(seconds=1),
    }[field]
    output_id = row.id
    with pytest.raises(
        IntegrityError, match="identity, content and deadline are immutable"
    ):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(ExportOutput)
                .where(ExportOutput.id == output_id)
                .values({field: changed})
            )


@pytest.mark.parametrize("change", ["bucket", "identity", "key", "endpoint", "clear"])
async def test_saved_owner_refuses_ordinary_rewrite(owned_db, change):
    row = await create(owned_db)
    ref = deepcopy(row.storage_ref)
    if change == "bucket":
        ref["bucket"] = "different"
    elif change == "identity":
        ref["storage_identity"] = "0" * 64
    elif change == "key":
        ref["object_key"] = ref["object_key"].replace(str(row.id), str(uuid.uuid4()))
    elif change == "endpoint":
        ref["endpoint_url"] = "https://other.invalid"
    else:
        ref = None
    output_id = row.id
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(ExportOutput)
                .where(ExportOutput.id == output_id)
                .values(storage_ref=ref)
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", "1"),
        ("schema", 1.0),
        ("purpose", "training-run"),
        ("location_id", str(uuid.UUID(int=1))),
        ("config_revision", 2),
        ("config_revision", True),
        ("config_revision", 1.0),
        ("binding_generation", "1"),
        ("binding_generation", 1.0),
        ("endpoint_url", "http://user:secret@storage.invalid"),
        ("endpoint_url", "http://storage.invalid/path"),
        ("endpoint_url", "http://storage.invalid\\path"),
        ("bucket", ""),
        ("bucket", "bad/path"),
        ("bucket", "bad\\path"),
        ("bucket", "bad bucket"),
        ("object_key", "../export.zip"),
        ("storage_identity", "A" * 64),
        ("storage_identity", None),
        ("credential", "forbidden"),
    ],
)
async def test_insert_requires_strict_envelope_and_exact_candidate_key(
    owned_db, field, value
):
    output_id, project_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ref = {**reference(output_id, project_id, job_id), field: value}
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(
                owned_db,
                id=output_id,
                project_id=project_id,
                job_id=job_id,
                storage_ref=ref,
            )


@pytest.mark.parametrize("bad", [None, {}, [], "text", True])
async def test_insert_rejects_json_null_or_missing_envelope(owned_db, bad):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text(
                    "INSERT INTO export_outputs (id,project_id,job_id,media,format,file_count,storage_ref,sha256,size_bytes,created_at,expires_at) "
                    "VALUES (:id,:project,:job,'image','coco',2,CAST(:ref AS jsonb),:sha,101,:created,:expires)"
                ),
                {
                    "id": uuid.uuid4(),
                    "project": uuid.uuid4(),
                    "job": uuid.uuid4(),
                    "ref": json.dumps(bad),
                    "sha": "a" * 64,
                    "created": CREATED,
                    "expires": CREATED + timedelta(days=7),
                },
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"media": "other"},
        {"format": ""},
        {"format": "coco\n"},
        {"file_count": -1},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"size_bytes": 0},
        {"size_bytes": 64 * 1024**3 + 1},
        {"state": "unknown"},
        {"state": "retired"},
        {"retired_at": CREATED},
        {"expires_at": CREATED},
        {"expires_at": CREATED + timedelta(days=7, seconds=1)},
    ],
)
async def test_insert_requires_receipt_retention_and_consistent_lifecycle(
    owned_db, changes
):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(owned_db, **changes)


@pytest.mark.parametrize("first_ready", [False, True])
async def test_forward_lifecycle_and_cleanup_clock_are_mutable(owned_db, first_ready):
    row = await create(owned_db)
    if first_ready:
        row.state = "ready"
        await owned_db.flush()
    row.state, row.retired_at = "retired", datetime.now(timezone.utc)
    await owned_db.flush()
    retired_at = row.retired_at
    row.last_attempted_at = datetime.now(timezone.utc)
    await owned_db.flush()
    await owned_db.refresh(row)
    assert row.state == "retired"
    assert row.retired_at == retired_at
    assert row.last_attempted_at is not None


@pytest.mark.parametrize(
    ("initial", "target"),
    [("ready", "pending"), ("retired", "pending"), ("retired", "ready")],
)
async def test_lifecycle_cannot_move_backwards(owned_db, initial, target):
    row = await create(
        owned_db, state=initial, retired_at=CREATED if initial == "retired" else None
    )
    output_id = row.id
    with pytest.raises(IntegrityError, match="state cannot move backwards"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(ExportOutput)
                .where(ExportOutput.id == output_id)
                .values(state=target, retired_at=None)
            )


async def test_retirement_fact_cannot_be_rewritten(owned_db):
    row = await create(owned_db, state="retired", retired_at=CREATED)
    output_id = row.id
    with pytest.raises(IntegrityError, match="retirement is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(ExportOutput)
                .where(ExportOutput.id == output_id)
                .values(retired_at=CREATED + timedelta(seconds=1))
            )


async def test_controlled_restore_changes_only_bucket_and_identity(owned_db):
    row = await create(owned_db)
    output_id, original = row.id, deepcopy(row.storage_ref)
    mapped = {
        **original,
        "bucket": "exports-restored",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "exports-restored"
        ),
    }
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await owned_db.execute(
        update(ExportOutput)
        .where(ExportOutput.id == output_id)
        .values(storage_ref=mapped)
    )
    await owned_db.refresh(row)
    assert row.storage_ref == mapped
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(ExportOutput)
                .where(ExportOutput.id == output_id)
                .values(storage_ref={**mapped, "endpoint_url": "https://trap.invalid"})
            )
    for changes in ({"sha256": "b" * 64}, {"expires_at": CREATED + timedelta(days=6)}):
        with pytest.raises(
            IntegrityError, match="identity, content and deadline are immutable"
        ):
            async with owned_db.begin_nested():
                await owned_db.execute(
                    update(ExportOutput)
                    .where(ExportOutput.id == output_id)
                    .values(**changes)
                )
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='off'"))
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(ExportOutput)
                .where(ExportOutput.id == output_id)
                .values(storage_ref=original)
            )


async def test_runtime_role_cannot_forge_restore_marker(owned_db):
    row = await create(owned_db)
    output_id, original = row.id, deepcopy(row.storage_ref)
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
        text(f'GRANT SELECT,UPDATE ON "{schema}".export_outputs TO {quoted}')
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
                update(ExportOutput)
                .where(ExportOutput.id == output_id)
                .values(storage_ref=mapped)
            )
    assert await owned_db.scalar(text("SELECT current_user")) == current


async def test_cache_reference_restricts_output_deletion(owned_db):
    row = await create(owned_db)
    output_id = row.id
    await owned_db.execute(
        text("UPDATE export_artifacts SET output_id=:output WHERE id=:id"),
        {"output": output_id, "id": LEGACY_CACHE},
    )
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text("DELETE FROM export_outputs WHERE id=:id"), {"id": output_id}
            )
    await owned_db.execute(
        text("DELETE FROM export_artifacts WHERE id=:id"), {"id": LEGACY_CACHE}
    )
    await owned_db.execute(
        text("DELETE FROM export_outputs WHERE id=:id"), {"id": output_id}
    )
    assert await owned_db.scalar(text("SELECT count(*) FROM export_outputs")) == 0


async def test_any_saved_output_blocks_destructive_downgrade(owned_db):
    await create(owned_db)
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())
