"""Auxiliary locators add nullable ownership without weakening older owners."""

from copy import deepcopy
from datetime import datetime, timezone
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
from app.db.models.active_learning import ActiveLearningRound
from app.db.models.sample_evaluation import SampleEvaluationRun
from app.db.models.training_preview import TrainingPreview
from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services import (
    active_learning_storage,
    sample_evaluation_storage,
    training_preview_storage,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

KINDS = [
    (TrainingPreview, training_preview_storage, "training-preview"),
    (SampleEvaluationRun, sample_evaluation_storage, "sample-evaluation"),
    (ActiveLearningRound, active_learning_storage, "active-learning"),
]


def migration(connection, revision="0222"):
    filename = {
        "0220": "0220_training_dataset_storage.py",
        "0221": "0221_training_run_storage.py",
        "0222": "0222_auxiliary_training_storage.py",
    }[revision]
    path = Path(__file__).parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location(
        "auxiliary_storage_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def owned_db(test_engine):
    schema = "auxiliary_training_storage_test_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        source = await connection.scalar(text("SELECT current_schema()"))
        for table in (
            "training_dataset_versions",
            "training_runs",
            "training_storage_deletions",
            "training_previews",
            "sample_evaluation_runs",
            "active_learning_rounds",
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
                for revision in ("0220", "0221", "0222"):
                    migration(sync, revision).upgrade()

            await connection.run_sync(upgrade)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture(params=KINDS, ids=[kind[2] for kind in KINDS])
def kind(request):
    return request.param


def reference(row, kind):
    _, service, purpose = kind
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return {
        "schema": 1,
        "purpose": purpose,
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "auxiliary-training-original",
        "prefix": service.prefix_for(row.project_id, row.id),
        "storage_identity": managed_storage_identity(
            endpoint, "auxiliary-training-original"
        ),
        "origin": "verified-retained",
        "evidence": {"retained": [{"sha256": "c" * 64}]},
    }


async def create(db, kind, *, pinned=False):
    model, _, _ = kind
    values = dict(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        spec={"frozen": ["original"]},
        authorization={"frozen": "authority"},
        status="failed",
    )
    if model is TrainingPreview:
        values.update(
            dataset_id=uuid.uuid4(),
            request={"frozen": "request"},
            input_digest="b" * 64,
            resource_id="preview-cpu",
            runtime={},
            upload_inventory={},
            artifacts={},
            expires_at=datetime.now(timezone.utc),
        )
    else:
        values.update(
            name="historical auxiliary run",
            model_version_id=uuid.uuid4(),
            deployment_id=uuid.uuid4(),
            spec_digest="b" * 64,
        )
        if model is SampleEvaluationRun:
            values["dataset_id"] = uuid.uuid4()
    row = model(**values)
    db.add(row)
    await db.flush()
    if pinned:
        row.storage_ref = reference(row, kind)
        await db.flush()
    return row


async def test_nullable_downgrade_upgrade_roundtrip_keeps_semantic_facts(
    owned_db, kind
):
    row = await create(owned_db, kind)
    model, _, _ = kind
    original = (
        row.id,
        deepcopy(row.spec),
        deepcopy(row.authorization),
        row.request_digest,
    )
    await owned_db.commit()
    async with owned_db.bind.begin() as connection:

        def roundtrip(sync):
            module = migration(sync)
            assert module.down_revision == "0221"
            schema = sync.scalar(text("SELECT current_schema()"))
            module.downgrade()
            for auxiliary, *_ in KINDS:
                assert "storage_ref" not in {
                    column["name"]
                    for column in inspect(sync).get_columns(
                        auxiliary.__tablename__, schema=schema
                    )
                }
            # Existing dataset/run/deletion shape constraints and columns are untouched.
            for older in [
                "training_dataset_versions",
                "training_runs",
                "training_storage_deletions",
            ]:
                assert "storage_ref" in {
                    column["name"]
                    for column in inspect(sync).get_columns(older, schema=schema)
                }
            module.upgrade()

        await connection.run_sync(roundtrip)
    await owned_db.refresh(row)
    assert (row.id, row.spec, row.authorization, row.request_digest) == original
    assert row.storage_ref is None
    assert isinstance(row, model)


@pytest.mark.parametrize(
    "change", ["bucket", "identity", "evidence", "endpoint", "origin", "clear"]
)
async def test_saved_pin_refuses_ordinary_rewrite(owned_db, kind, change):
    row = await create(owned_db, kind, pinned=True)
    model, _, _ = kind
    changed = deepcopy(row.storage_ref)
    if change == "bucket":
        changed["bucket"] = "different"
    elif change == "identity":
        changed["storage_identity"] = "0" * 64
    elif change == "evidence":
        changed["evidence"]["retained"][0]["sha256"] = "rewritten"
    elif change == "endpoint":
        changed["endpoint_url"] = "http://other.invalid"
    elif change == "origin":
        changed.update(origin="new", evidence=None)
    else:
        changed = None
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(model).where(model.id == row.id).values(storage_ref=changed)
            )


@pytest.mark.parametrize("bad", [{}, [], None, "text", {"schema": True}])
async def test_first_pin_rejects_incomplete_or_json_null(owned_db, kind, bad):
    row = await create(owned_db, kind)
    model, _, _ = kind
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text(
                    f"UPDATE {model.__tablename__} SET storage_ref=CAST(:ref AS jsonb) WHERE id=:id"
                ),
                {"ref": json.dumps(bad), "id": row.id},
            )


@pytest.mark.parametrize(
    "change", ["other-kind", "other-row", "other-project", "foreign-prefix", "extra"]
)
async def test_first_pin_requires_exact_kind_and_row_namespace(owned_db, kind, change):
    row = await create(owned_db, kind)
    model, service, _ = kind
    ref = reference(row, kind)
    if change == "other-kind":
        ref["purpose"] = "training-run"
    elif change == "other-row":
        ref["prefix"] = service.prefix_for(row.project_id, uuid.uuid4())
    elif change == "other-project":
        ref["prefix"] = service.prefix_for(uuid.uuid4(), row.id)
    elif change == "foreign-prefix":
        ref["prefix"] = f"projects/{row.project_id}/runs/{row.id}/"
    else:
        ref["credential"] = "private"
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(model).where(model.id == row.id).values(storage_ref=ref)
            )


async def test_controlled_restore_changes_only_bucket_and_identity(owned_db, kind):
    row = await create(owned_db, kind, pinned=True)
    model, _, _ = kind
    original = deepcopy(row.storage_ref)
    mapped = {
        **original,
        "bucket": "auxiliary-training-restored",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "auxiliary-training-restored"
        ),
    }
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await owned_db.execute(
        update(model).where(model.id == row.id).values(storage_ref=mapped)
    )
    await owned_db.refresh(row)
    assert row.storage_ref == mapped
    changed = {
        **mapped,
        "bucket": "other",
        "storage_identity": "f" * 64,
        "evidence": {"rewritten": True},
    }
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(model).where(model.id == row.id).values(storage_ref=changed)
            )
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='off'"))
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(model).where(model.id == row.id).values(storage_ref=original)
            )


async def test_runtime_role_cannot_forge_restore_marker(owned_db, kind):
    row = await create(owned_db, kind, pinned=True)
    model, _, _ = kind
    identifier, original = row.id, deepcopy(row.storage_ref)
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
        text(f'GRANT SELECT,UPDATE ON "{schema}".{model.__tablename__} TO {quoted}')
    )
    changed = {
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
                update(model).where(model.id == identifier).values(storage_ref=changed)
            )
    assert await owned_db.scalar(text("SELECT current_user")) == current


async def test_saved_auxiliary_pin_refuses_destructive_downgrade(owned_db, kind):
    await create(owned_db, kind, pinned=True)
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())


async def test_existing_deletion_owner_does_not_accept_auxiliary_kinds(owned_db, kind):
    row = await create(owned_db, kind)
    ref = reference(row, kind)
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            owned_db.add(
                TrainingStorageDeletion(
                    bucket=ref["bucket"], prefix=ref["prefix"], storage_ref=ref
                )
            )
            await owned_db.flush()
