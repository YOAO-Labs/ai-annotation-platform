"""Run locators retain immutable ownership without adopting historical rows."""

from copy import deepcopy
import importlib.util
import json
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
from app.db.models.training_run import TrainingRun as Run
from app.db.models.training_storage_deletion import TrainingStorageDeletion as Deletion
from app.services import training_dataset_storage as datasets
from app.services import training_run_storage as service
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity


def migration(connection, revision="0221"):
    filename = {
        "0220": "0220_training_dataset_storage.py",
        "0221": "0221_training_run_storage.py",
    }[revision]
    path = Path(__file__).parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location(
        "run_storage_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def owned_db(test_engine):
    schema = "training_run_storage_test_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        source = await connection.scalar(text("SELECT current_schema()"))
        for table in (
            "training_dataset_versions",
            "training_runs",
            "training_storage_deletions",
        ):
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

            def upgrade(sync):
                migration(sync, "0220").upgrade()
                migration(sync).upgrade()

            await connection.run_sync(upgrade)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def reference(row):
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return {
        "schema": 1,
        "purpose": "training-run",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "training-run-original",
        "prefix": service.prefix_for(row.project_id, row.id),
        "storage_identity": managed_storage_identity(endpoint, "training-run-original"),
        "origin": "verified-retained",
        "evidence": {"attempts": [{"sha256": "c" * 64}]},
    }


async def create(db, *, pinned=False):
    row = Run(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="historical run",
        status="failed",
        config={"fixed": "config"},
        config_digest="b" * 64,
        metrics={"fixed": "metrics"},
    )
    db.add(row)
    await db.flush()
    if pinned:
        row.storage_ref = reference(row)
        await db.flush()
    return row


async def test_nullable_roundtrip_preserves_legacy_run_and_dataset_tombstone(owned_db):
    db = owned_db
    row = await create(db)
    original = (row.id, deepcopy(row.config), row.config_digest, deepcopy(row.metrics))
    dataset_ref = reference(row)
    dataset_ref.update(
        purpose="training-dataset",
        prefix=datasets.prefix_for(row.project_id, uuid.uuid4()),
    )
    tombstone = Deletion(
        bucket=dataset_ref["bucket"],
        prefix=dataset_ref["prefix"],
        storage_ref=dataset_ref,
    )
    db.add(tombstone)
    await db.commit()
    async with db.bind.begin() as connection:

        def check(sync):
            module = migration(sync)
            assert module.down_revision == "0220"
            schema = sync.scalar(text("SELECT current_schema()"))
            module.downgrade()
            assert "storage_ref" not in {
                c["name"]
                for c in inspect(sync).get_columns("training_runs", schema=schema)
            }
            assert "storage_ref" in {
                c["name"]
                for c in inspect(sync).get_columns(
                    "training_dataset_versions", schema=schema
                )
            }
            assert (
                sync.scalar(
                    text(
                        "SELECT storage_ref FROM training_storage_deletions WHERE id=:id"
                    ),
                    {"id": tombstone.id},
                )
                == dataset_ref
            )
            module.upgrade()

        await connection.run_sync(check)
    await db.refresh(row)
    assert (row.id, row.config, row.config_digest, row.metrics) == original
    assert row.storage_ref is None


@pytest.mark.parametrize(
    "change", ["bucket", "identity", "evidence", "endpoint", "origin", "clear"]
)
async def test_saved_run_pin_refuses_ordinary_rewrite(owned_db, change):
    row = await create(owned_db, pinned=True)
    changed = deepcopy(row.storage_ref)
    if change == "bucket":
        changed["bucket"] = "other"
    elif change == "identity":
        changed["storage_identity"] = "f" * 64
    elif change == "evidence":
        changed["evidence"]["attempts"][0]["sha256"] = "rewritten"
    elif change == "endpoint":
        changed["endpoint_url"] = "http://other.invalid"
    elif change == "origin":
        changed.update(origin="new", evidence=None)
    else:
        changed = None
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Run).where(Run.id == row.id).values(storage_ref=changed)
            )


@pytest.mark.parametrize("bad", [{}, [], None, "text"])
async def test_first_pin_rejects_incomplete_or_json_null(owned_db, bad):
    row = await create(owned_db)
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text(
                    "UPDATE training_runs SET storage_ref=CAST(:ref AS jsonb) WHERE id=:id"
                ),
                {"ref": json.dumps(bad), "id": row.id},
            )


@pytest.mark.parametrize(
    "change", ["dataset", "other-run", "other-project", "purpose-prefix", "extra"]
)
async def test_run_pin_requires_exact_role_and_row_namespace(owned_db, change):
    row = await create(owned_db)
    ref = reference(row)
    if change == "dataset":
        ref.update(
            purpose="training-dataset",
            prefix=datasets.prefix_for(row.project_id, row.id),
        )
    elif change == "other-run":
        ref["prefix"] = service.prefix_for(row.project_id, uuid.uuid4())
    elif change == "other-project":
        ref["prefix"] = service.prefix_for(uuid.uuid4(), row.id)
    elif change == "purpose-prefix":
        ref["prefix"] = datasets.prefix_for(row.project_id, row.id)
    else:
        ref["credential"] = "private"
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Run).where(Run.id == row.id).values(storage_ref=ref)
            )


async def test_tombstones_allow_both_roles_but_match_purpose_and_locator(owned_db):
    row = await create(owned_db)
    run_ref = reference(row)
    dataset_ref = {
        **run_ref,
        "purpose": "training-dataset",
        "prefix": datasets.prefix_for(row.project_id, uuid.uuid4()),
    }
    for ref in (run_ref, dataset_ref):
        owned_db.add(
            Deletion(bucket=ref["bucket"], prefix=ref["prefix"], storage_ref=ref)
        )
        await owned_db.flush()
    for ref, bucket, prefix in [
        (
            {**run_ref, "purpose": "training-dataset"},
            run_ref["bucket"],
            run_ref["prefix"],
        ),
        (
            {**dataset_ref, "purpose": "training-run"},
            dataset_ref["bucket"],
            dataset_ref["prefix"],
        ),
        (run_ref, "other-bucket", run_ref["prefix"]),
        (run_ref, run_ref["bucket"], service.prefix_for(row.project_id, uuid.uuid4())),
    ]:
        with pytest.raises(IntegrityError):
            async with owned_db.begin_nested():
                owned_db.add(Deletion(bucket=bucket, prefix=prefix, storage_ref=ref))
                await owned_db.flush()


async def test_explicit_owner_restore_preserves_nested_proof_for_run_and_tombstone(
    owned_db,
):
    db = owned_db
    row = await create(db, pinned=True)
    original = deepcopy(row.storage_ref)
    intent = Deletion(
        bucket=original["bucket"], prefix=original["prefix"], storage_ref=original
    )
    db.add(intent)
    await db.flush()
    mapped = {
        **original,
        "bucket": "training-restored",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "training-restored"
        ),
    }
    await db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await db.execute(update(Run).where(Run.id == row.id).values(storage_ref=mapped))
    await db.execute(
        update(Deletion)
        .where(Deletion.id == intent.id)
        .values(bucket=mapped["bucket"], storage_ref=mapped)
    )
    await db.refresh(row)
    await db.refresh(intent)
    assert row.storage_ref == intent.storage_ref == mapped
    changed = {
        **mapped,
        "bucket": "another",
        "storage_identity": "e" * 64,
        "evidence": {"rewritten": True},
    }
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with db.begin_nested():
            await db.execute(
                update(Run).where(Run.id == row.id).values(storage_ref=changed)
            )
    await db.execute(text("SET LOCAL aap.restore_training_storage='off'"))
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with db.begin_nested():
            await db.execute(
                update(Run).where(Run.id == row.id).values(storage_ref=original)
            )


async def test_runtime_role_cannot_forge_owner_restore_marker(owned_db):
    db = owned_db
    row = await create(db, pinned=True)
    identifier, original = row.id, deepcopy(row.storage_ref)
    schema = await db.scalar(text("SELECT current_schema()"))
    current = await db.scalar(text("SELECT current_user"))
    role = make_url(settings.database_url).username
    if role == current:
        pytest.skip("This configured test target has no distinct runtime role")
    if not await db.scalar(
        text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"),
        {"role": role},
    ):
        pytest.skip("Configured runtime role does not exist in this test server")
    quoted = '"' + role.replace('"', '""') + '"'
    await db.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {quoted}'))
    await db.execute(
        text(f'GRANT SELECT,UPDATE ON "{schema}".training_runs TO {quoted}')
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
                update(Run).where(Run.id == identifier).values(storage_ref=changed)
            )
    assert await db.scalar(text("SELECT current_user")) == current


@pytest.mark.parametrize("kind", ["run", "intent"])
async def test_saved_run_pin_or_tombstone_refuses_downgrade(owned_db, kind):
    row = await create(owned_db, pinned=kind == "run")
    if kind == "intent":
        ref = reference(row)
        owned_db.add(
            Deletion(bucket=ref["bucket"], prefix=ref["prefix"], storage_ref=ref)
        )
        await owned_db.flush()
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())
