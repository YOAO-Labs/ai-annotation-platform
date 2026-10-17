from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, text, update

from app.application.raster_mask_retention import (
    is_raster_mask_key_referenced,
    referenced_raster_mask_keys,
)
from app.db.models.annotation import Annotation
from app.db.models.mask_annotation_revision import MaskAnnotationRevision
from app.services.annotation import AnnotationService
from app.services.mask_slice_restore import expire_mask_annotation_revisions
from app.services.raster_mask_storage import build_rle_reference
from tests.test_raster_mask_write_gate import _seed_image_task


RLE_A = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 3]}
RLE_B = {"encoding": "coco_rle", "size": [2, 3], "counts": [0, 1, 5]}
RLE_C = {"encoding": "coco_rle", "size": [2, 3], "counts": [2, 2, 2]}


def _geometry(kind: str, rle: dict) -> dict:
    reference = build_rle_reference(rle)
    if kind == "raster_mask":
        return {"type": kind, "mask": reference}
    if kind == "video_mask":
        return {"type": kind, "frame_index": 0, "mask": reference}
    return {
        "type": kind,
        "track_id": "trk_revision_test",
        "keyframes": [{"frame_index": 0, "mask": reference}],
        "outside": [],
    }


async def _seed_mask(db, user_id, *, kind: str = "raster_mask"):
    project, task = await _seed_image_task(db, user_id)
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user_id,
        source="manual",
        annotation_type=kind,
        tool_unit_id="region",
        class_name="object",
        geometry=_geometry(kind, RLE_A),
        version=1,
    )
    db.add(annotation)
    await db.flush()
    return project, task, annotation


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["raster_mask", "video_mask", "video_track_mask"])
async def test_database_trigger_captures_orm_core_and_hard_delete(
    db_session, super_admin, kind
):
    user, _ = super_admin
    _project, _task, annotation = await _seed_mask(db_session, user.id, kind=kind)
    annotation_id = annotation.id

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MaskAnnotationRevision)
            .where(MaskAnnotationRevision.annotation_id == annotation_id)
        )
        == 0
    )

    first_geometry = annotation.geometry
    second_geometry = _geometry(kind, RLE_B)
    annotation.geometry = second_geometry
    annotation.version += 1
    await db_session.flush()

    first = await db_session.scalar(
        select(MaskAnnotationRevision).where(
            MaskAnnotationRevision.annotation_id == annotation_id,
            MaskAnnotationRevision.annotation_version == 1,
        )
    )
    assert first is not None
    assert first.geometry == first_geometry
    assert first.source_kind == "manual"
    assert len(first.geometry_digest) == 64

    third_geometry = _geometry(kind, RLE_C)
    await db_session.execute(
        update(Annotation)
        .where(Annotation.id == annotation_id)
        .values(geometry=third_geometry)
        .execution_options(synchronize_session=False)
    )
    db_session.expire_all()
    current = await db_session.get(Annotation, annotation_id)
    assert current is not None
    assert current.version == 3
    second = await db_session.scalar(
        select(MaskAnnotationRevision).where(
            MaskAnnotationRevision.annotation_id == annotation_id,
            MaskAnnotationRevision.annotation_version == 2,
        )
    )
    assert second is not None and second.geometry == second_geometry

    await db_session.execute(
        delete(Annotation)
        .where(Annotation.id == annotation_id)
        .execution_options(synchronize_session=False)
    )
    db_session.expire_all()
    assert await db_session.get(Annotation, annotation_id) is None
    final = await db_session.scalar(
        select(MaskAnnotationRevision).where(
            MaskAnnotationRevision.annotation_id == annotation_id,
            MaskAnnotationRevision.annotation_version == 3,
        )
    )
    assert final is not None
    assert final.geometry == third_geometry
    assert final.source_kind == "manual"


@pytest.mark.asyncio
async def test_trigger_revision_and_version_changes_roll_back_together(
    db_session, super_admin
):
    user, _ = super_admin
    _project, _task, annotation = await _seed_mask(db_session, user.id)
    annotation_id = annotation.id

    savepoint = await db_session.begin_nested()
    await db_session.execute(
        update(Annotation)
        .where(Annotation.id == annotation_id)
        .values(geometry=_geometry("raster_mask", RLE_B))
        .execution_options(synchronize_session=False)
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MaskAnnotationRevision)
            .where(MaskAnnotationRevision.annotation_id == annotation_id)
        )
        == 1
    )
    await savepoint.rollback()

    db_session.expire_all()
    current = await db_session.get(Annotation, annotation_id)
    assert current is not None and current.version == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MaskAnnotationRevision)
            .where(MaskAnnotationRevision.annotation_id == annotation_id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_annotation_service_delete_increments_target_and_child_versions(
    db_session, super_admin
):
    user, _ = super_admin
    project, task = await _seed_image_task(db_session, user.id)
    parent = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="object",
        geometry=_geometry("raster_mask", RLE_A),
        version=1,
    )
    db_session.add(parent)
    await db_session.flush()
    child = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="object",
        geometry=_geometry("raster_mask", RLE_B),
        parent_annotation_id=parent.id,
        version=1,
    )
    db_session.add(child)
    await db_session.flush()

    assert await AnnotationService(db_session).delete(parent.id) is True
    assert parent.is_active is False and parent.version == 2
    assert child.is_active is False and child.version == 2
    revisions = list(
        (
            await db_session.execute(
                select(MaskAnnotationRevision).where(
                    MaskAnnotationRevision.annotation_id.in_([parent.id, child.id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert {
        (revision.annotation_id, revision.annotation_version) for revision in revisions
    } == {
        (parent.id, 1),
        (child.id, 1),
    }
    assert {revision.source_kind for revision in revisions} == {"manual"}


@pytest.mark.asyncio
async def test_unexpired_revision_is_present_in_both_gc_reference_checks(
    db_session, super_admin
):
    user, _ = super_admin
    _project, _task, annotation = await _seed_mask(db_session, user.id)
    old_key = annotation.geometry["mask"]["object_key"]
    annotation.geometry = _geometry("raster_mask", RLE_B)
    annotation.version += 1
    await db_session.flush()

    assert old_key in await referenced_raster_mask_keys(db_session)
    assert await is_raster_mask_key_referenced(db_session, old_key) is True

    revision = await db_session.scalar(
        select(MaskAnnotationRevision).where(
            MaskAnnotationRevision.annotation_id == annotation.id,
            MaskAnnotationRevision.annotation_version == 1,
        )
    )
    assert revision is not None
    revision.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()

    assert old_key not in await referenced_raster_mask_keys(db_session)
    assert await is_raster_mask_key_referenced(db_session, old_key) is False
    assert await expire_mask_annotation_revisions(db_session) == 1


@pytest.mark.asyncio
async def test_revision_retention_keeps_latest_twenty_and_thirty_day_floor(
    db_session, super_admin
):
    user, _ = super_admin
    _project, _task, annotation = await _seed_mask(db_session, user.id)

    for version in range(2, 23):
        geometry = _geometry(
            "raster_mask",
            {
                "encoding": "coco_rle",
                "size": [2, 3],
                "counts": [version % 6, 6 - (version % 6)],
            },
        )
        await db_session.execute(
            update(Annotation)
            .where(Annotation.id == annotation.id)
            .values(geometry=geometry, version=version)
            .execution_options(synchronize_session=False)
        )

    retention = (
        await db_session.execute(
            text(
                """
                SELECT annotation_version,
                       expires_at = 'infinity'::timestamptz AS retained,
                       expires_at >= created_at + interval '30 days' AS has_floor
                FROM mask_annotation_revisions
                WHERE annotation_id = :annotation_id
                ORDER BY annotation_version
                """
            ),
            {"annotation_id": annotation.id},
        )
    ).all()
    assert len(retention) == 21
    assert [row.retained for row in retention].count(True) == 20
    assert all(row.has_floor for row in retention)
