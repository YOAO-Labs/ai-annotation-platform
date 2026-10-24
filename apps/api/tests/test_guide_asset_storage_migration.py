"""Guide image ownership migration checks in a disposable, fixture-owned schema."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.guide_asset_upload import GuideAssetUpload
from app.db.models.project_guide_asset import ProjectGuideAsset
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def migration(connection):
    path = Path(__file__).parents[1] / "alembic/versions/0228_guide_asset_storage.py"
    spec = importlib.util.spec_from_file_location(
        "guide_asset_storage_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def owned_db(test_engine):
    schema = "guide_asset_storage_test_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE projects (id uuid PRIMARY KEY, guide_assets jsonb NOT NULL DEFAULT '[]'::jsonb, annotation_guide text)"
                )
            )
            await connection.run_sync(lambda sync: migration(sync).upgrade())
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.info["owned_engine"] = engine
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def reference(upload_id, project_id):
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
        "bucket": "attachments-original",
        "object_key": f"projects/{project_id}/guide/{upload_id}",
        "storage_identity": managed_storage_identity(endpoint, "attachments-original"),
    }


async def create(db, **changes):
    values = {
        "id": uuid.uuid4(),
        "owner_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "mime_type": "image/png",
        "file_name": "screen.png",
        "sha256": "a" * 64,
        "size_bytes": 101,
        "created_at": CREATED,
        **changes,
    }
    if "storage_ref" not in values:
        values["storage_ref"] = reference(values["id"], values["project_id"])
    row = GuideAssetUpload(**values)
    db.add(row)
    await db.flush()
    return row


async def test_empty_roundtrip_preserves_shared_guard_and_snapshot_ownership(owned_db):
    connection = await owned_db.connection()

    def roundtrip(sync):
        module = migration(sync)
        assert module.down_revision == "0227"
        schema = sync.scalar(text("SELECT current_schema()"))
        assert (
            inspect(sync).get_foreign_keys("guide_asset_uploads", schema=schema) == []
        )
        shared_before = sync.scalar(
            text(
                "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
            )
        )
        module.downgrade()
        assert not inspect(sync).has_table("guide_asset_uploads", schema=schema)
        assert (
            sync.scalar(
                text(
                    "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
                )
            )
            == shared_before
        )
        module.upgrade()
        assert inspect(sync).has_table("guide_asset_uploads", schema=schema)

    await connection.run_sync(roundtrip)
    row = await create(owned_db)
    assert row.state == "pending"
    assert row.retired_at is None


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "owner_id",
        "project_id",
        "mime_type",
        "file_name",
        "sha256",
        "size_bytes",
        "created_at",
    ],
)
async def test_all_identity_content_and_deadline_facts_are_immutable(owned_db, field):
    row = await create(owned_db)
    changed = {
        "id": uuid.uuid4(),
        "owner_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "mime_type": "image/webp",
        "file_name": "other.png",
        "sha256": "b" * 64,
        "size_bytes": 102,
        "created_at": row.created_at + timedelta(seconds=1),
    }[field]
    upload_id = row.id
    with pytest.raises(IntegrityError, match="identity and content are immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(GuideAssetUpload)
                .where(GuideAssetUpload.id == upload_id)
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
    upload_id = row.id
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(GuideAssetUpload)
                .where(GuideAssetUpload.id == upload_id)
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
        ("object_key", "../attachment.png"),
        ("storage_identity", "A" * 64),
        ("storage_identity", None),
        ("credential", "forbidden"),
    ],
)
async def test_insert_requires_strict_envelope_and_exact_upload_key(
    owned_db, field, value
):
    upload_id, project_id = uuid.uuid4(), uuid.uuid4()
    ref = {**reference(upload_id, project_id), field: value}
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(
                owned_db,
                id=upload_id,
                project_id=project_id,
                storage_ref=ref,
            )


@pytest.mark.parametrize("bad", [None, {}, [], "text", True])
async def test_insert_rejects_json_null_or_missing_envelope(owned_db, bad):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text(
                    "INSERT INTO guide_asset_uploads (id,owner_id,project_id,mime_type,file_name,storage_ref,sha256,size_bytes,created_at) "
                    "VALUES (:id,:owner,:project,'image/png','screen.png',CAST(:ref AS jsonb),:sha,101,:created)"
                ),
                {
                    "id": uuid.uuid4(),
                    "owner": uuid.uuid4(),
                    "project": uuid.uuid4(),
                    "ref": json.dumps(bad),
                    "sha": "a" * 64,
                    "created": CREATED,
                },
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"mime_type": "Image/PNG"},
        {"mime_type": "text/plain; charset=utf-8"},
        {"mime_type": "text/plain\n"},
        {"mime_type": "x/" + "a" * 127},
        {"file_name": ""},
        {"file_name": "../screen.png"},
        {"file_name": "screen\n.png"},
        {"file_name": "screen\x85.png"},
        {"file_name": "x" * 256},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"size_bytes": -1},
        {"size_bytes": 0},
        {"mime_type": "application/octet-stream"},
        {"size_bytes": 5 * 1024**2 + 1},
        {"state": "unknown"},
        {"state": "retired"},
        {"retired_at": CREATED},
    ],
)
async def test_insert_requires_receipt_retention_and_consistent_lifecycle(
    owned_db, changes
):
    column_overflow = (
        changes.get("file_name") == "x" * 256
        or changes.get("mime_type") == "x/" + "a" * 127
    )
    expected = DBAPIError if column_overflow else (IntegrityError, DataError)
    with pytest.raises(expected) as rejected:
        async with owned_db.begin_nested():
            await create(owned_db, **changes)
    if column_overflow:
        assert rejected.value.orig.sqlstate == "22001"


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
    upload_id = row.id
    with pytest.raises(IntegrityError, match="state cannot move backwards"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(GuideAssetUpload)
                .where(GuideAssetUpload.id == upload_id)
                .values(state=target, retired_at=None)
            )


async def test_retirement_fact_cannot_be_rewritten(owned_db):
    row = await create(owned_db, state="retired", retired_at=CREATED)
    upload_id = row.id
    with pytest.raises(IntegrityError, match="retirement is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(GuideAssetUpload)
                .where(GuideAssetUpload.id == upload_id)
                .values(retired_at=CREATED + timedelta(seconds=1))
            )


async def test_controlled_restore_changes_only_bucket_and_identity(owned_db):
    row = await create(owned_db)
    upload_id, original = row.id, deepcopy(row.storage_ref)
    mapped = {
        **original,
        "bucket": "attachments-restored",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "attachments-restored"
        ),
    }
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await owned_db.execute(
        update(GuideAssetUpload)
        .where(GuideAssetUpload.id == upload_id)
        .values(storage_ref=mapped)
    )
    await owned_db.refresh(row)
    assert row.storage_ref == mapped
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(GuideAssetUpload)
                .where(GuideAssetUpload.id == upload_id)
                .values(storage_ref={**mapped, "endpoint_url": "https://trap.invalid"})
            )
    for changes in ({"sha256": "b" * 64},):
        with pytest.raises(IntegrityError, match="identity and content are immutable"):
            async with owned_db.begin_nested():
                await owned_db.execute(
                    update(GuideAssetUpload)
                    .where(GuideAssetUpload.id == upload_id)
                    .values(**changes)
                )
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='off'"))
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(GuideAssetUpload)
                .where(GuideAssetUpload.id == upload_id)
                .values(storage_ref=original)
            )


async def test_runtime_role_cannot_forge_restore_marker(owned_db):
    row = await create(owned_db)
    upload_id, original = row.id, deepcopy(row.storage_ref)
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
        text(f'GRANT SELECT,UPDATE ON "{schema}".guide_asset_uploads TO {quoted}')
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
                update(GuideAssetUpload)
                .where(GuideAssetUpload.id == upload_id)
                .values(storage_ref=mapped)
            )
    assert await owned_db.scalar(text("SELECT current_user")) == current


@pytest.mark.parametrize("state", ["pending", "ready", "retired"])
async def test_any_saved_attachment_blocks_destructive_downgrade(owned_db, state):
    await create(
        owned_db, state=state, retired_at=CREATED if state == "retired" else None
    )
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())


@pytest.mark.parametrize(
    "mime_type",
    [
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "image/svg+xml",
    ],
)
@pytest.mark.parametrize("size", [1, 5 * 1024**2])
async def test_supported_mime_and_nonempty_boundaries_have_no_extension_contract(
    owned_db, mime_type, size
):
    row = await create(
        owned_db, mime_type=mime_type, size_bytes=size, file_name="截图 原件.bin"
    )
    assert row.storage_ref == reference(row.id, row.project_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"file_name": "."},
        {"file_name": ".."},
        {"file_name": "folder\\screen.png"},
        {"file_name": "screen\x7f.png"},
        {"file_name": "screen\x9f.png"},
    ],
)
async def test_initial_pending_and_file_name_are_safe(owned_db, changes):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(owned_db, **changes)


async def project(db, *, identifier=None, **values):
    identifier = identifier or uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO projects(id, guide_assets) VALUES (:id, CAST(:assets AS jsonb))"
        ),
        {"id": identifier, "assets": json.dumps(values.get("guide_assets", []))},
    )
    return identifier


async def add_reference(db, project_id, upload_id):
    row = ProjectGuideAsset(project_id=project_id, upload_id=upload_id)
    db.add(row)
    await db.flush()
    return row


@pytest.mark.parametrize("state", ["pending", "retired"])
async def test_reference_insert_requires_ready_owner(owned_db, state):
    row = await create(
        owned_db, state=state, retired_at=CREATED if state == "retired" else None
    )
    target = await project(owned_db)
    upload_id = row.id
    with pytest.raises(IntegrityError, match="require a ready owner"):
        async with owned_db.begin_nested():
            await add_reference(owned_db, target, upload_id)


async def test_shared_reference_survives_original_project_deletion(owned_db):
    original, copied = await project(owned_db), await project(owned_db)
    row = await create(owned_db, project_id=original, state="ready")
    await add_reference(owned_db, original, row.id)
    await add_reference(owned_db, copied, row.id)
    await owned_db.execute(text("DELETE FROM projects WHERE id=:id"), {"id": original})
    await owned_db.flush()
    refs = list(
        await owned_db.scalars(
            select(ProjectGuideAsset).where(ProjectGuideAsset.upload_id == row.id)
        )
    )
    assert [ref.project_id for ref in refs] == [copied]
    await owned_db.refresh(row)
    assert row.project_id == original and row.state == "ready"
    upload_id = row.id
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text("DELETE FROM guide_asset_uploads WHERE id=:id"), {"id": upload_id}
            )


@pytest.mark.parametrize("field", ["project_id", "upload_id", "created_at"])
async def test_reference_facts_cannot_be_rewritten(owned_db, field):
    row = await create(owned_db, state="ready")
    target = await project(owned_db)
    await add_reference(owned_db, target, row.id)
    upload_id = row.id
    changed = CREATED if field == "created_at" else uuid.uuid4()
    with pytest.raises(IntegrityError, match="reference facts are immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(ProjectGuideAsset)
                .where(
                    ProjectGuideAsset.project_id == target,
                    ProjectGuideAsset.upload_id == upload_id,
                )
                .values({field: changed})
            )


async def test_live_reference_blocks_retirement_until_last_reference_removed(owned_db):
    row = await create(owned_db, state="ready")
    target = await project(owned_db)
    await add_reference(owned_db, target, row.id)
    upload_id = row.id
    with pytest.raises(IntegrityError, match="Referenced guide images cannot retire"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(GuideAssetUpload)
                .where(GuideAssetUpload.id == upload_id)
                .values(state="retired", retired_at=CREATED)
            )
    await owned_db.execute(
        delete(ProjectGuideAsset).where(ProjectGuideAsset.upload_id == upload_id)
    )
    await owned_db.execute(
        update(GuideAssetUpload)
        .where(GuideAssetUpload.id == upload_id)
        .values(state="retired", retired_at=CREATED)
    )
    with pytest.raises(IntegrityError, match="require a ready owner"):
        async with owned_db.begin_nested():
            await add_reference(owned_db, target, upload_id)


async def test_revision_advances_only_for_actual_json_change(owned_db):
    target = await project(owned_db)

    async def revision():
        return await owned_db.scalar(
            text("SELECT guide_assets_revision FROM projects WHERE id=:id"),
            {"id": target},
        )

    assert await revision() == 1
    await owned_db.execute(
        text(
            "UPDATE projects SET annotation_guide='body-only', guide_assets=guide_assets WHERE id=:id"
        ),
        {"id": target},
    )
    assert await revision() == 1
    await owned_db.execute(
        text("UPDATE projects SET guide_assets=CAST(:assets AS jsonb) WHERE id=:id"),
        {"id": target, "assets": json.dumps([{"key": "legacy", "size": 1}])},
    )
    assert await revision() == 2
    await owned_db.execute(
        text("UPDATE projects SET guide_assets=CAST(:assets AS jsonb) WHERE id=:id"),
        {"id": target, "assets": json.dumps([{"size": 1, "key": "legacy"}])},
    )
    assert await revision() == 2
    await owned_db.execute(
        text("UPDATE projects SET guide_assets='[]'::jsonb WHERE id=:id"),
        {"id": target},
    )
    assert await revision() == 3


@pytest.mark.parametrize("revision", [None, 0, -1, 2, 99])
async def test_revision_cannot_be_assigned_even_together_with_json_change(
    owned_db, revision
):
    target = await project(owned_db)
    with pytest.raises(IntegrityError, match="cannot be assigned directly"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text(
                    "UPDATE projects SET guide_assets='[{}]'::jsonb, guide_assets_revision=:revision WHERE id=:id"
                ),
                {"id": target, "revision": revision},
            )


@pytest.mark.parametrize("revision", [None, 0, -1, 2])
async def test_new_projects_start_at_revision_one(owned_db, revision):
    with pytest.raises(IntegrityError, match="must start at one"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text(
                    "INSERT INTO projects(id, guide_assets_revision) VALUES (:id, :revision)"
                ),
                {"id": uuid.uuid4(), "revision": revision},
            )


async def test_noninitial_project_revision_blocks_downgrade_without_owner(owned_db):
    target = await project(owned_db)
    await owned_db.execute(
        text("UPDATE projects SET guide_assets='[{}]'::jsonb WHERE id=:id"),
        {"id": target},
    )
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())


async def test_reference_insert_serializes_with_owner_state_and_project_lock(owned_db):
    row = await create(owned_db, state="ready")
    target = await project(owned_db)
    upload_id = row.id
    await owned_db.commit()
    engine = owned_db.info["owned_engine"]
    async with async_sessionmaker(engine, expire_on_commit=False)() as other:
        await owned_db.execute(
            select(GuideAssetUpload.id)
            .where(GuideAssetUpload.id == upload_id)
            .with_for_update()
        )
        with pytest.raises(DBAPIError) as caught:
            await add_reference(other, target, upload_id)
        assert caught.value.orig.sqlstate == "55P03"
        await other.rollback()
        await owned_db.rollback()
        await owned_db.execute(
            text("SELECT id FROM projects WHERE id=:id FOR UPDATE"), {"id": target}
        )
        with pytest.raises(DBAPIError) as caught:
            await add_reference(other, target, upload_id)
        assert caught.value.orig.sqlstate == "55P03"
        await other.rollback()
        await owned_db.rollback()
        await add_reference(other, target, upload_id)
        await other.commit()
    with pytest.raises(IntegrityError, match="Referenced guide images cannot retire"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(GuideAssetUpload)
                .where(GuideAssetUpload.id == upload_id)
                .values(state="retired", retired_at=CREATED)
            )
