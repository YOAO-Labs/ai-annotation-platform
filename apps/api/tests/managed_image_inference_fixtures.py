"""Committed synchronous image fixtures with real source and credential fences.

Every business row, immutable creation and source owner lives in the input
fixture's disposable namespace. Copy the migrated constraints and triggers,
including their FK ancestor closure; never delete immutable history at teardown.
Only the native original reader and temporary object provider are substituted.
"""

from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import re
from types import SimpleNamespace
import uuid

from PIL import Image, ImageDraw
import pytest
from sqlalchemy import select, text

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import StorageBinding
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.deps import get_prediction_input_session_factory
from app.services import managed_original_creation as creation
from app.services import prediction_inputs, prediction_originals as originals
from app.services import task_originals
from app.services.dataset_assets import register_item_asset
from tests.factory import create_task
from tests.test_managed_original_creation import Client
from tests.test_prediction_inputs import input_world as input_world


# Descendants used by HTTP writes must be explicit; FK ancestors are discovered
# from the already migrated database, rather than reconstructed from ORM models.
_TABLES = (
    "users",
    "projects",
    "project_members",
    "api_keys",
    "tasks",
    "task_batches",
    "task_locks",
    "datasets",
    "project_datasets",
    "storage_location_probes",
    "dataset_items",
    "dataset_assets",
    "dataset_asset_locations",
    "task_dataset_item_links",
    "task_originals",
    "managed_original_creations",
    "managed_original_retirements",
    "annotations",
    "predictions",
    "prediction_metas",
    "failed_predictions",
    "ai_mask_accept_decisions",
    "ml_backend_registry",
    "ml_backend_service_pools",
    "ml_backend_pool_members",
    "project_ml_backend_pool",
    "audit_logs",
    "notifications",
    "raster_mask_objects",
    "raster_mask_publications",
    "mask_annotation_revisions",
    "dataset_sources",
    "dataset_scan_runs",
    "dataset_scan_entries",
    "dataset_references",
    "dataset_registration_runs",
    "dataset_registration_entries",
    "dataset_source_revisions",
    "dataset_source_items",
    "dataset_source_previews",
    "dataset_source_changes",
    "dataset_source_sync_schedules",
)


async def _clone_business_schema(db, *, extra_tables=()):
    """Preserve production DDL while rebinding all selected FK targets locally."""
    schema = await db.scalar(text("SELECT current_schema()"))
    assert re.fullmatch(r"prediction_inputs_[0-9a-f]{32}", schema)
    required = (*_TABLES, *extra_tables)
    tables = list(
        await db.scalars(
            text("""
        WITH RECURSIVE owned(oid) AS (
          SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND c.relname=ANY(CAST(:names AS text[]))
          UNION
          SELECT fk.confrelid FROM pg_constraint fk JOIN owned o ON o.oid=fk.conrelid
          WHERE fk.contype='f' AND fk.conparentid=0
        )
        SELECT c.relname FROM owned o JOIN pg_class c ON c.oid=o.oid ORDER BY c.relname
    """),
            {"names": list(required)},
        )
    )
    assert set(required) <= set(tables), "A required migrated fixture table is absent"
    assert len(tables) <= 200, "Unexpected fixture FK closure"
    existing = set(
        await db.scalars(
            text("""
        SELECT tablename FROM pg_tables WHERE schemaname=:schema
    """),
            {"schema": schema},
        )
    )
    created = set(tables) - existing
    for table in sorted(created):
        assert re.fullmatch(r"[a-z_][a-z_0-9]*", table)
        await db.execute(
            text(
                f'CREATE TABLE "{schema}"."{table}" (LIKE public."{table}" INCLUDING ALL)'
            )
        )
    # The private tables already exist, so pg_get_constraintdef explicitly
    # qualifies public targets. Rebind those targets without weakening the FK.
    constraints = (
        await db.execute(
            text("""
        SELECT c.relname, k.conname, pg_get_constraintdef(k.oid, true)
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND k.contype='f' AND k.conparentid=0
          AND c.relname=ANY(CAST(:tables AS text[])) ORDER BY c.relname,k.conname
    """),
            {"tables": sorted(created)},
        )
    ).all()
    for table, name, definition in constraints:
        for target in tables:
            definition = re.sub(
                rf'REFERENCES public\."?{re.escape(target)}"?(?=\s|\()',
                f'REFERENCES "{schema}"."{target}"',
                definition,
            )
        assert "REFERENCES public." not in definition
        await db.execute(
            text(
                f'ALTER TABLE "{schema}"."{table}" ADD CONSTRAINT "{name}" {definition}'
            )
        )
    triggers = (
        await db.execute(
            text("""
        SELECT c.relname, pg_get_triggerdef(t.oid, true)
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal
          AND c.relname=ANY(CAST(:tables AS text[])) ORDER BY c.relname,t.tgname
    """),
            {"tables": sorted(created)},
        )
    ).all()
    for table, definition in triggers:
        definition = re.sub(
            rf' ON (?:public\.)?"?{re.escape(table)}"?(?=\s)',
            f' ON "{schema}"."{table}"',
            definition,
            count=1,
        )
        assert f' ON "{schema}".' in definition
        await db.execute(text(definition))
    await db.commit()


@pytest.fixture
async def sync_world(input_world, app_module, monkeypatch):
    """Keep real committed rows visible to every independent guard session."""
    async with input_world.factory() as db:
        await _clone_business_schema(db)
    world = SimpleNamespace(
        factory=input_world.factory,
        inputs=input_world,
        source_bytes={},
        captured=[],
        source_files=[],
        capture_hook=None,
        source_close_hook=None,
        monkeypatch=monkeypatch,
    )
    world.factory.configure(info={"managed_image_world": world})

    class SourceFile(io.BytesIO):
        def close(self):
            if world.source_close_hook:
                world.source_close_hook(self)
            super().close()

    def capture(spec, maximum, seconds, deadline, stop, release, **kwargs):
        raw = world.source_bytes[spec.bucket, spec.object_key]
        assert len(raw) == spec.size_bytes <= maximum
        assert hashlib.sha256(raw).hexdigest() == spec.sha256
        assert not stop.is_set()
        if world.capture_hook:
            world.capture_hook(spec)
        stream = SourceFile(raw)
        world.source_files.append(stream)
        world.captured.append(spec)
        return originals.reader.BinaryStage(
            stream,
            spec.sha256,
            len(raw),
            {"size": len(raw)},
            _release=release,
            _retain_on_close_error=True,
        )

    monkeypatch.setattr(originals.reader, "_capture", capture)
    old = app_module.dependency_overrides.get(get_prediction_input_session_factory)
    app_module.dependency_overrides[get_prediction_input_session_factory] = lambda: (
        world.factory
    )
    try:
        yield world
    finally:
        if old is None:
            app_module.dependency_overrides.pop(
                get_prediction_input_session_factory, None
            )
        else:
            app_module.dependency_overrides[get_prediction_input_session_factory] = old
        world.source_close_hook = None
        for stream in world.source_files:
            stream.close()
        world.source_bytes.clear()


@pytest.fixture
async def db_session(sync_world):
    """Override the SAVEPOINT fixture with real transactions in the owned schema."""
    async with sync_world.factory() as db:
        try:
            yield db
        finally:
            await db.rollback()
    from app.services.system_settings_service import SystemSettingsService

    SystemSettingsService.invalidate()


async def attach_managed(
    db,
    project,
    actor,
    *,
    task=None,
    size=(3, 2),
    origin="direct",
    item=None,
    roi_marker=False,
):
    """Register genuine saved owners; only their physical source read is faked."""
    assert origin in {"direct", "primary", "task"}
    world = db.info["managed_image_world"]
    output = io.BytesIO()
    with Image.new("RGB", size, (23, 42, 61)) as image:
        if roi_marker:
            width, height = size
            ImageDraw.Draw(image).rectangle(
                (width // 4, height // 4, width * 3 // 4 - 1, height * 3 // 4 - 1),
                fill=(100, 150, 200),
            )
        image.save(output, "PNG")
    raw = output.getvalue()
    task = task or await create_task(db, project_id=project.id)
    task.file_type = "image"
    if origin == "task":
        assert item is None
        task.dataset_item_id = None
        task.status = "uploading"
        await db.flush()
        await db.commit()
        allocation = await creation.allocate_task(
            db,
            project_id=project.id,
            task_id=task.id,
            logical_path="original.png",
            max_bytes=len(raw),
            content_type="image/png",
            requested_by=actor.id,
            authority={
                "schema": 1,
                "actor_id": str(actor.id),
                "operation": "test-upload",
            },
        )
        client = Client()
        client.store(allocation["staging_bucket"], allocation["staging_key"], raw)
        # This is a provider seam, not a creation/publication receipt shortcut.
        with world.monkeypatch.context() as patch:
            patch.setattr(creation, "create_client", lambda **_: client)
            from tests.test_managed_original_creation import stage_client

            stage_client(patch, client)
            created = await creation.complete_direct(db, allocation)
        task.file_path, task.file_name = allocation["object_key"], "original.png"
        await task_originals.publish(db, task, allocation, created)
        task.status = "pending"
    else:
        if item is None:
            dataset = Dataset(
                id=uuid.uuid4(),
                display_id="sync-" + uuid.uuid4().hex[:15],
                name="Managed synchronous source",
                created_by=actor.id,
            )
            db.add(dataset)
            await db.flush()
            item = DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                file_name="original.png",
                file_path="sync-original/" + uuid.uuid4().hex + ".png",
                file_type="image",
                file_size=len(raw),
                width=size[0],
                height=size[1],
            )
            db.add(item)
        else:
            item.file_size, item.width, item.height = len(raw), size[0], size[1]
        await db.flush()
        binding = await db.get(StorageBinding, "annotations")
        await register_item_asset(
            db,
            item,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            binding=binding,
        )
        task.file_path, task.file_name = item.file_path, item.file_name
        task.dataset_item_id = item.id if origin == "direct" else None
        if origin == "primary":
            db.add(
                TaskDatasetItemLink(
                    id=uuid.uuid4(),
                    task_id=task.id,
                    dataset_item_id=item.id,
                    role="primary_lidar",
                    sensor_name="primary",
                )
            )
    await db.flush()
    source = await originals.resolve(db, task.id)
    world.source_bytes[source.bucket, source.object_key] = raw
    # A current-bucket read would find different bytes at the identical key.
    current = await db.get(StorageBinding, "datasets")
    assert source.bucket != current.bucket
    world.source_bytes[current.bucket, source.object_key] = b"wrong-current-bucket"
    proof = originals.source_receipt(source)
    identity = {
        name: deepcopy(proof[name])
        for name in (
            "schema",
            "owner_kind",
            "content_id",
            "sha256",
            "size_bytes",
            "location_id",
            "location_revision",
            "maintenance_revision",
            "facts_digest",
        )
    }
    identity.update(task_id=str(task.id), width=size[0], height=size[1])
    task._test_original_input = identity
    await db.commit()
    return SimpleNamespace(
        task=task, item=item, raw=raw, source=source, identity=identity
    )


async def assert_inputs_retired(world, task_id, monkeypatch, *, require_records=True):
    """Run the intentional second cleanup round after real source handles close."""
    async with world.factory() as db:
        assert not await db.scalar(select(StorageOperation.id))
        records = list(
            await db.scalars(
                select(PredictionInputLease).where(
                    PredictionInputLease.task_id == task_id
                )
            )
        )
    if require_records:
        assert records
    future = prediction_inputs.now() + timedelta(minutes=2)
    with monkeypatch.context() as patch:
        patch.setattr(prediction_inputs, "now", lambda: future)
        async with world.factory() as db:
            for row in records:
                if row.state != "deleted":
                    await prediction_inputs.purge_input(world.factory, row.id)
    async with world.factory() as db:
        rows = list(
            await db.scalars(
                select(PredictionInputLease).where(
                    PredictionInputLease.task_id == task_id
                )
            )
        )
        assert len(rows) == len(records) and all(row.state == "deleted" for row in rows)
        assert not await db.scalar(select(StorageOperation.id))
    for row in rows:
        for entry in row.inventory["objects"].values():
            version = entry["version_id"]
            assert version not in {None, "null"}
            assert all(
                version not in versions for versions in world.inputs.versions.values()
            )
    assert all(stream.closed for stream in world.source_files)
    assert all(storage.closed for storage in world.inputs.storages)
    assert not world.inputs.objects and not world.inputs.versions
