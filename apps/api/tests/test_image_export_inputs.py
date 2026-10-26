"""Pure input guards and disposable-DB image freezing regressions."""

from dataclasses import replace
from datetime import datetime, timezone
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.exporting import image_inputs as inputs
from app.services.raster_mask_storage import COCO_RLE_REF_ENCODING
from tests.factory import build_tool_bindings
from tests.test_dataset_content import content_world as content_world
from tests.test_dataset_references import reference_world as reference_world
from tests.test_lidar_export_inputs import input_db as input_db
from tests.test_original_sources import add_link, dataset_rows


async def registered_dataset_image(
    db,
    *,
    project,
    dataset,
    data,
    task=None,
    filename="image.png",
    primary=False,
    direct=True,
):
    """Create a real verified receipt using the caller's isolated storage.

    Parent Project/Dataset/User facts must already be committed because Creation
    uses independent owned sessions. This helper never supplies a fake client:
    use the normal test fixture or the real disposable MinIO stack explicitly.
    """
    from PIL import Image
    from app.services import managed_original_creation as creation

    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    allocation = await creation.allocate(
        db,
        dataset_id=dataset.id,
        logical_path=filename,
        origin="zip",
        max_bytes=len(data),
        requested_by=project.owner_id,
        authority={"policy": "image-export-owned-fixture"},
        content_type="image/png",
    )
    written = await creation.create_stream(
        db,
        allocation,
        io.BytesIO(data),
        content_type="image/png",
    )
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name=filename,
        file_path=allocation["object_key"],
        file_type="image",
        file_size=written.size_bytes,
        width=width,
        height=height,
        content_hash=written.sha256,
        metadata_={"storage_relative_path": filename},
    )
    db.add(item)
    await db.flush()
    asset = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type="image",
        size_bytes=written.size_bytes,
        checksum_algorithm="sha256",
        checksum_value=written.sha256,
        verification_state="verified",
    )
    db.add(asset)
    await db.flush()
    replica = DatasetAssetLocation(
        asset_id=asset.id,
        location_id=LEGACY_LOCATION_ID,
        bucket=allocation["bucket"],
        object_key=allocation["object_key"],
        role="managed_original",
        reference_state="present",
        etag=written.etag,
        verified_at=datetime.now(timezone.utc),
        storage_ref=creation.original_ref(allocation),
    )
    db.add(replica)
    await db.flush()
    await creation.attach(db, item, allocation["id"])
    if task is None:
        task = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            display_id="owned-image-" + uuid.uuid4().hex[:12],
            file_name=filename,
            file_path=item.file_path,
            file_type="image",
        )
        db.add(task)
    task.file_name, task.file_path, task.file_type = filename, item.file_path, "image"
    task.dataset_item_id = item.id if direct else None
    task.original_id = None
    await db.flush()
    if primary:
        await add_link(db, task.id, item.id)
    await db.commit()
    return SimpleNamespace(
        task=task,
        item=item,
        asset=asset,
        replica=replica,
        creation=allocation,
        data=data,
        sha256=written.sha256,
    )


def test_inputs_expose_independent_json_copies_and_immutable_source_mapping():
    original = {"schema": 3, "tasks": [{"id": "original"}]}
    value = inputs.Inputs(original, {"task:image": "source"})
    expected = value.digest
    original["tasks"][0]["id"] = "caller mutation"
    value.snapshot["tasks"][0]["id"] = "consumer mutation"
    assert value.digest == expected
    with pytest.raises(TypeError):
        value.sources["another"] = "source"


def test_portable_rle_expansion_is_counted_once_and_conflicts_fail():
    reference = {
        "encoding": COCO_RLE_REF_ENCODING,
        "object_key": "raster-masks/sha256/owned.json",
        "sha256": "a" * 64,
        "bytes": 100,
    }
    geometry = {"type": "raster_mask", "mask": reference}
    annotation = SimpleNamespace(geometry=geometry)
    prediction = SimpleNamespace(result=[geometry])
    assert (
        inputs._portable_masks([annotation, annotation], [prediction], ["aap_json"])
        == 100
    )
    changed = SimpleNamespace(
        geometry={"type": "raster_mask", "mask": {**reference, "bytes": 101}}
    )
    with pytest.raises(ContentError, match="mask_invalid"):
        inputs._portable_masks([annotation, changed], [], ["coco"])


def test_portable_rle_expansion_rejects_before_any_object_reads(monkeypatch):
    monkeypatch.setattr(inputs, "MAX_JSON_BYTES", 80)
    reference = {
        "encoding": COCO_RLE_REF_ENCODING,
        "object_key": "raster-masks/sha256/owned.json",
        "bytes": 100,
    }
    with pytest.raises(ContentError, match="input_budget_exceeded"):
        inputs._portable_masks(
            [SimpleNamespace(geometry={"type": "raster_mask", "mask": reference})],
            [],
            ["aap_json"],
        )


@pytest.mark.parametrize("invalid", [[], ["bad"], [uuid.uuid4()] * 2])
def test_explicit_selection_never_silently_drops_invalid_or_duplicate_ids(invalid):
    with pytest.raises(ContentError):
        inputs._selection(invalid)


async def test_fresh_source_guard_returns_the_actual_matching_owner(monkeypatch):
    source = SimpleNamespace()
    task_id = uuid.uuid4()
    resolver = SimpleNamespace(task=AsyncMock(return_value=source))
    monkeypatch.setattr(
        inputs.original_sources, "CachedResolver", lambda *args, **kwargs: resolver
    )
    monkeypatch.setattr(
        inputs.lidar_inputs, "source_value", lambda _: {"bucket": "saved"}
    )
    monkeypatch.setattr(inputs.lidar_inputs, "source_key", lambda _: "a" * 64)
    binding = {
        "task_id": str(task_id),
        "role": "image",
        "link_id": None,
        "source": {"bucket": "saved"},
        "source_key": "a" * 64,
    }
    assert await inputs.check_source(None, binding) is source
    binding["source"]["bucket"] = "current-wrong"
    with pytest.raises(ContentError, match="original_changed"):
        await inputs.check_source(None, binding)


async def setup(db, *, direct=True, primary=False):
    project = Project(
        id=uuid.uuid4(),
        display_id="im-" + uuid.uuid4().hex[:15],
        name="Images",
        data_type="image",
        type_key="image-detection",
        type_label="Image",
        owner_id=uuid.uuid4(),
        tool_bindings=build_tool_bindings(["car"]),
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id="ds-" + uuid.uuid4().hex[:15],
        name="Images",
        data_type="image",
        created_by=uuid.uuid4(),
        metadata_={"axis_convention": "iso8855"},
    )
    db.add_all([project, dataset])
    await db.flush()
    item, asset, replica, task = await dataset_rows(db)
    item.dataset_id, task.project_id = dataset.id, project.id
    item.file_name = task.file_name = "same.jpg"
    if not direct:
        task.dataset_item_id = None
    if primary:
        await add_link(db, task.id, item.id)
    annotation = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        class_name="car",
        annotation_type="bbox",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        attributes={},
    )
    db.add(annotation)
    await db.commit()
    return project, dataset, item, asset, replica, task, annotation


@pytest.mark.parametrize("primary", [False, True])
async def test_db_canonical_saved_bucket_and_primary_binding_are_frozen(
    input_db, primary
):
    db = input_db
    project, _, item, _, replica, task, annotation = await setup(
        db, direct=not primary, primary=primary
    )
    frozen = await inputs.collect(
        db, project.id, None, ["coco", "yolo-det"], {}, lock=True
    )
    snapshot = frozen.snapshot
    assert snapshot["schema"] == 3
    assert snapshot["tasks"][0]["version"] == task.version
    assert snapshot["annotations"][0]["id"] == str(annotation.id)
    binding = snapshot["media"][0]
    assert binding["source"]["bucket"] == replica.bucket == "saved-original-bucket"
    assert binding["source"]["item_id"] == str(item.id)
    assert (await inputs.check(db, snapshot)).digest == frozen.digest
    assert (await inputs.check_source(db, binding)).bucket == replica.bucket
    changed = replace(frozen.sources[f"{task.id}:image"], bucket="other")
    assert changed.bucket != frozen.sources[f"{task.id}:image"].bucket


async def test_db_empty_project_is_a_valid_metadata_snapshot(input_db):
    db = input_db
    project, _, _, _, _, task, annotation = await setup(db)
    await db.delete(annotation)
    await db.delete(task)
    await db.commit()
    snapshot = (await inputs.collect(db, project.id, None, ["aap_json"], {})).snapshot
    assert snapshot["tasks"] == snapshot["media"] == snapshot["annotations"] == []
    assert snapshot["references"] == {}
    assert snapshot["counts"]["tasks"] == 0


async def test_db_selected_scope_does_not_widen_and_retains_unfiltered_classes(
    input_db,
):
    db = input_db
    project, _, _, _, _, task, annotation = await setup(db)
    annotation.class_name = "outside-current-schema"
    await db.commit()
    snapshot = (
        await inputs.collect(db, project.id, None, ["coco"], {}, task_ids=[task.id])
    ).snapshot
    assert snapshot["task_ids"] == [str(task.id)]
    assert snapshot["annotations"][0]["class_name"] == "outside-current-schema"
    with pytest.raises(ContentError, match="scope_changed"):
        await inputs.collect(
            db, project.id, None, ["coco"], {}, task_ids=[task.id, uuid.uuid4()]
        )


@pytest.mark.parametrize(
    "mutation", ["restore", "category", "filename", "original", "prediction"]
)
async def test_db_final_check_rejects_changed_format_inputs(input_db, mutation):
    db = input_db
    project, _, _, asset, _, task, annotation = await setup(db)
    inactive = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        class_name="car",
        annotation_type="bbox",
        geometry=annotation.geometry,
        attributes={},
        is_active=False,
    )
    prediction = Prediction(
        id=uuid.uuid4(), task_id=task.id, project_id=project.id, result=[]
    )
    db.add_all([inactive, prediction])
    await db.commit()
    snapshot = (await inputs.collect(db, project.id, None, ["aap_json"], {})).snapshot
    if mutation == "restore":
        inactive.is_active = True
    elif mutation == "category":
        project.tool_bindings = build_tool_bindings(["truck"])
    elif mutation == "filename":
        task.file_name = "changed.jpg"
    elif mutation == "original":
        asset.checksum_value = "b" * 64
    else:
        prediction.result = [{"changed": True}]
    await db.commit()
    with pytest.raises(ContentError, match="changed"):
        await inputs.check(db, snapshot, lock=True)


async def test_db_parent_lock_fences_new_annotations_and_inactive_restore(input_db):
    db = input_db
    project, _, _, _, _, task, annotation = await setup(db)
    annotation.is_active = False
    await db.commit()
    snapshot = (await inputs.collect(db, project.id, None, ["coco"], {})).snapshot
    await db.commit()
    await inputs.check(db, snapshot, lock=True)
    from sqlalchemy.exc import DBAPIError

    async with async_sessionmaker(db.bind, expire_on_commit=False)() as writer:
        await writer.execute(text("SET LOCAL lock_timeout='100ms'"))
        with pytest.raises(DBAPIError):
            await writer.execute(
                update(Annotation)
                .where(Annotation.id == annotation.id)
                .values(is_active=True)
            )
        await writer.rollback()
        await writer.execute(text("SET LOCAL lock_timeout='100ms'"))
        with pytest.raises(DBAPIError):
            await writer.execute(
                Annotation.__table__.insert().values(
                    id=uuid.uuid4(),
                    task_id=task.id,
                    project_id=project.id,
                    class_name="car",
                    annotation_type="bbox",
                    geometry={"type": "bbox"},
                    attributes={},
                )
            )
        await writer.rollback()
    await db.rollback()


async def test_db_oversized_task_json_is_rejected_before_hydration(
    input_db, monkeypatch
):
    db = input_db
    project, _, _, _, _, task, _ = await setup(db)
    await db.execute(update(Task).where(Task.id == task.id).values(tags=["x" * 4096]))
    await db.commit()
    # Exercise the shared MVCC guard using a small test limit, without creating
    # hundreds of MiB of data in the disposable test database.
    monkeypatch.setattr(inputs.lidar_inputs, "MAX_JSON_BYTES", 2048)
    with pytest.raises(ContentError, match="input_budget_exceeded"):
        await inputs.collect(db, project.id, None, ["coco"], {})


async def test_db_annotation_budget_includes_inactive_rows(input_db, monkeypatch):
    db = input_db
    project, _, _, _, _, _, annotation = await setup(db)
    annotation.is_active = False
    await db.commit()
    monkeypatch.setattr(inputs, "MAX_ANNOTATIONS", 0)
    with pytest.raises(ContentError, match="input_budget_exceeded"):
        await inputs.collect(db, project.id, None, ["coco"], {}, lock=True)


async def test_db_reference_projection_exactly_matches_existing_binding(
    db_session, reference_world
):
    from app.services.reference_bindings import reference_binding

    _, project, task, _ = reference_world
    item = await db_session.get(DatasetItem, task.dataset_item_id)
    expected = await reference_binding(db_session, task)
    actual = await inputs._reference(db_session, task, item, lock=False)
    assert actual == expected
    snapshot = (
        await inputs.collect(db_session, project.id, None, ["coco", "aap_json"], {})
    ).snapshot
    assert snapshot["references"] == {str(task.id): expected}
    assert snapshot["media"] == []
    assert snapshot["tasks"][0]["version"] == task.version
