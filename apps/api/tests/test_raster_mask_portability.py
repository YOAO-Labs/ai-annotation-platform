from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.annotations_import import import_aap_json_annotations
from app.services.exporting.service import ExportService
from app.services.mask_formats.image_codecs import normalize_coco_segmentation_rle
from app.services.predictions_import import import_aap_json
from app.services.raster_mask_storage import build_rle_reference
from app.utils.raster_mask_rle import encode_coco_rle

pytestmark = pytest.mark.asyncio


async def _image_project_task(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    file_path: str,
    width: int = 3,
    height: int = 2,
) -> tuple[Project, Task]:
    suffix = uuid.uuid4().hex[:6]
    project = Project(
        display_id=f"P-RMP-{suffix}",
        name=f"Raster portability {suffix}",
        type_key="image-det",
        type_label="测试",
        owner_id=owner_id,
        status="in_progress",
        raster_mask_native_editing_enabled=True,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
            }
        },
    )
    dataset = Dataset(
        display_id=f"DS-RMP-{suffix}",
        name=f"Raster portability {suffix}",
        data_type="image",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=file_path.rsplit("/", 1)[-1],
        file_path=file_path,
        file_type="image",
        width=width,
        height=height,
    )
    db.add(item)
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-RMP-{suffix}",
        file_name=item.file_name,
        file_path=file_path,
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return project, task


async def test_aap_raster_mask_round_trip_deduplicates_objects(
    super_admin,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    user, _ = super_admin
    source_project, source_task = await _image_project_task(
        db_session,
        owner_id=user.id,
        file_path="portable/shared.png",
    )
    target_project, target_task = await _image_project_task(
        db_session,
        owner_id=user.id,
        file_path="portable/shared.png",
    )
    rle = encode_coco_rle([0, 1, 0, 0, 1, 0], 3, 2)
    reference = build_rle_reference(rle)
    for _ in range(2):
        db_session.add(
            Annotation(
                task_id=source_task.id,
                project_id=source_project.id,
                user_id=user.id,
                source="manual",
                annotation_type="raster_mask",
                tool_unit_id="region",
                class_name="car",
                geometry={"type": "raster_mask", "mask": reference},
            )
        )
    await db_session.flush()

    load = AsyncMock(return_value=rle)
    monkeypatch.setattr("app.services.exporting.service.load_coco_rle", load)
    exported = json.loads(
        await ExportService(db_session).export_aap_json(source_project.id)
    )
    assert exported["mask_objects"] == {reference["sha256"]: rle}
    assert load.await_count == 1

    store = AsyncMock()
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    import app.services.annotations_import as annotations_import

    monkeypatch.setattr(annotations_import, "store_mask_reference_objects", store)
    result = await import_aap_json_annotations(
        db_session,
        target_project.id,
        json.dumps(exported).encode(),
        operator_user_id=user.id,
    )

    assert result.imported == 2
    assert result.errors == []
    assert store.await_count == 2
    assert len(store.await_args_list[0].args[2]) == 1
    assert store.await_args_list[1].args[2] == []
    imported = list(
        (
            await db_session.execute(
                select(Annotation).where(Annotation.task_id == target_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(imported) == 2
    assert {ann.tool_unit_id for ann in imported} == {"region"}
    assert {ann.geometry["mask"]["sha256"] for ann in imported} == {reference["sha256"]}


async def test_aap_raster_mask_dry_run_validates_objects_and_create_flag(
    super_admin,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    user, _ = super_admin
    project, task = await _image_project_task(
        db_session,
        owner_id=user.id,
        file_path="portable/dry-run.png",
    )
    rle = encode_coco_rle([0, 1, 0, 0, 0, 0], 3, 2)
    reference = build_rle_reference(rle)
    envelope = {
        "schema_version": "1.3",
        "mask_objects": {},
        "tasks": [
            {
                "task_match": {"display_id": task.display_id},
                "annotations": [
                    {
                        "geometry": {"type": "raster_mask", "mask": reference},
                        "class_name": "car",
                    }
                ],
            }
        ],
    }

    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    missing = await import_aap_json_annotations(
        db_session,
        project.id,
        json.dumps(envelope).encode(),
        operator_user_id=user.id,
        dry_run=True,
    )
    assert missing.imported == 0
    assert missing.skipped == 1
    assert "mask_objects missing" in missing.errors[0].reason

    envelope["mask_objects"] = {reference["sha256"]: rle}
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    disabled = await import_aap_json_annotations(
        db_session,
        project.id,
        json.dumps(envelope).encode(),
        operator_user_id=user.id,
        dry_run=True,
    )
    assert disabled.imported == 0
    assert disabled.skipped == 1
    assert disabled.errors[0].reason.endswith("raster mask creation is disabled")

    empty = encode_coco_rle([0] * 6, 3, 2)
    empty_reference = build_rle_reference(empty)
    envelope["mask_objects"] = {empty_reference["sha256"]: empty}
    envelope["tasks"][0]["annotations"][0]["geometry"]["mask"] = empty_reference
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    empty_result = await import_aap_json_annotations(
        db_session,
        project.id,
        json.dumps(envelope).encode(),
        operator_user_id=user.id,
        dry_run=True,
    )
    assert empty_result.imported == 0
    assert empty_result.skipped == 1
    assert "foreground pixels" in empty_result.errors[0].reason


async def test_aap_raster_mask_prediction_rejects_empty_foreground(
    super_admin,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    user, _ = super_admin
    project, task = await _image_project_task(
        db_session,
        owner_id=user.id,
        file_path="portable/prediction-empty.png",
    )
    empty = encode_coco_rle([0] * 6, 3, 2)
    reference = build_rle_reference(empty)
    envelope = {
        "schema_version": "1.3",
        "mask_objects": {reference["sha256"]: empty},
        "tasks": [
            {
                "task_match": {"display_id": task.display_id},
                "predictions": [
                    {
                        "geometry": {"type": "raster_mask", "mask": reference},
                        "class_name": "car",
                    }
                ],
            }
        ],
    }

    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    result = await import_aap_json(
        db_session,
        project.id,
        json.dumps(envelope).encode(),
        actor_id=user.id,
        job_id=None,
        dry_run=True,
    )
    assert result.imported == 0
    assert result.skipped == 1
    assert "foreground pixels" in result.errors[0].reason


async def test_export_coco_raster_mask_uses_pixel_bbox_area_and_rle(
    super_admin,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    user, _ = super_admin
    project, task = await _image_project_task(
        db_session,
        owner_id=user.id,
        file_path="coco/non-square.png",
    )
    single = encode_coco_rle([0, 0, 0, 0, 0, 1], 3, 2)
    empty = encode_coco_rle([0] * 6, 3, 2)
    references = [build_rle_reference(single), build_rle_reference(empty)]
    for reference in references:
        db_session.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=user.id,
                source="manual",
                annotation_type="raster_mask",
                tool_unit_id="region",
                class_name="car",
                geometry={"type": "raster_mask", "mask": reference},
            )
        )
    await db_session.flush()
    by_digest = {references[0]["sha256"]: single, references[1]["sha256"]: empty}

    async def _load(reference):
        return by_digest[reference["sha256"]]

    monkeypatch.setattr("app.services.exporting.service.load_coco_rle", _load)
    body = json.loads(await ExportService(db_session).export_coco(project.id))

    assert body["images"][0]["width"] == 3
    assert body["images"][0]["height"] == 2
    rows = body["annotations"]
    assert len(rows) == 2
    single_row = next(row for row in rows if row["area"] == 1)
    empty_row = next(row for row in rows if row["area"] == 0)
    assert single_row["bbox"] == [2.0, 1.0, 1.0, 1.0]
    assert single_row["iscrowd"] == 1
    assert isinstance(single_row["segmentation"]["counts"], str)
    assert (
        normalize_coco_segmentation_rle(
            single_row["segmentation"], expected_width=3, expected_height=2
        )
        == single
    )
    assert empty_row["bbox"] == [0.0, 0.0, 0.0, 0.0]
    assert (
        normalize_coco_segmentation_rle(
            empty_row["segmentation"], expected_width=3, expected_height=2
        )
        == empty
    )
