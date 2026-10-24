from __future__ import annotations

import random
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update

from app.application.raster_mask_retention import is_raster_mask_key_referenced
from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.mask_annotation_revision import MaskAnnotationRevision
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.schemas.annotation_slice import AnnotationSliceRestoreRequest
from app.schemas.mask_mutation import MaskMutationCommitRequest
from app.services.annotation_slice import AnnotationSliceService
from app.services.annotation_slice_contracts import AnnotationSliceError
from app.services.audit import AuditService
from app.services.mask_mutation_contract import MaskMutationError
from app.services.mask_mutation import (
    MaskMutationService,
    _AlgebraBudget,
)
from app.services.mask_slice import slice_mask_rle
from app.services.mask_slice_restore import (
    expire_mask_annotation_revisions,
    protect_mask_slice_versions,
)
from app.services.raster_mask_storage import build_rle_reference
from tests.test_mask_mutations import _payload, _scope, _seed_image_task


FULL = {"encoding": "coco_rle", "size": [2, 3], "counts": [0, 6]}
CUT = [(1 / 3, 0), (1 / 3, 1)]


def _encode(pixels, width, height):
    counts, last = [0], False
    for x in range(width):
        for y in range(height):
            value = bool(pixels[y * width + x])
            if value != last:
                counts.append(0)
                last = value
            counts[-1] += 1
    return {"encoding": "coco_rle", "size": [height, width], "counts": counts}


def _decode(rle):
    height, width = rle["size"]
    result, cursor = [False] * (width * height), 0
    for i, count in enumerate(rle["counts"]):
        for position in range(cursor, cursor + count):
            x, y = divmod(position, height)
            result[y * width + x] = bool(i % 2)
        cursor += count
    return result


@pytest.mark.parametrize(
    "cut",
    [
        CUT,
        list(reversed(CUT)),
        [(0, 0.5), (1, 0.5)],
        [(1, 0.5), (0, 0.5)],
        [(0, 0), (1, 1)],
        [(0, 0.25), (1, 0.25)],
    ],
)
def test_partition_pixel_center_oracle_on_non_square_masks(cut):
    rng = random.Random(9481)
    for width, height in [(3, 2), (11, 7), (37, 19)]:
        for iteration in range(20):
            # Dense, disconnected and holed sources; compare individual pixels.
            pixels = [
                True if iteration == 0 else rng.random() < 0.6
                for _ in range(width * height)
            ]
            (ax, ay), (bx, by) = cut
            left = [
                bool(
                    value
                    and (bx - ax) * ((y + 0.5) / height - ay)
                    - (by - ay) * ((x + 0.5) / width - ax)
                    >= 0
                )
                for y in range(height)
                for x, value in enumerate(pixels[y * width : (y + 1) * width])
            ]
            right = [value and not left[i] for i, value in enumerate(pixels)]
            rle = _encode(pixels, width, height)
            before = deepcopy(rle)
            if not any(left) or not any(right):
                with pytest.raises(ValueError, match="非空"):
                    slice_mask_rle(rle, cut, _AlgebraBudget())
                continue
            kept, new = slice_mask_rle(rle, cut, _AlgebraBudget())
            assert rle == before
            expected = (left, right) if sum(left) >= sum(right) else (right, left)
            assert (_decode(kept), _decode(new)) == expected
            assert all(
                not (a and b) and (a or b) == original
                for a, b, original in zip(
                    _decode(kept), _decode(new), pixels, strict=True
                )
            )


@pytest.mark.parametrize(
    "cut",
    [
        [(0, 0), (0, 0)],
        [(0, 0), (0, 1)],
        [(float("nan"), 0), (1, 1)],
        [(0, 0), (2, 1)],
        [(0, 0)],
        [(0, 0), (1, 1), (0.5, 0.5)],
    ],
)
def test_invalid_cut_is_rejected(cut):
    with pytest.raises(ValueError):
        slice_mask_rle(FULL, cut, _AlgebraBudget())


def test_slice_compressed_spans_respect_budget(monkeypatch):
    monkeypatch.setattr("app.services.mask_mutation.MAX_MASK_MUTATION_ALGEBRA_STEPS", 2)
    with pytest.raises(MaskMutationError) as rejected:
        slice_mask_rle(FULL, CUT, _AlgebraBudget())
    assert rejected.value.detail["reason"] == "operation_too_large"


@pytest.fixture
def slice_content(monkeypatch):
    content = {}

    async def load(reference):
        return deepcopy(content[reference["sha256"]])

    monkeypatch.setattr(
        "app.services.raster_mask_storage.load_coco_rle", AsyncMock(side_effect=load)
    )
    monkeypatch.setattr(
        "app.services.mask_mutation.load_coco_rle", AsyncMock(side_effect=load)
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    return content


async def _seed(db, actor, content, *, parent=False):
    task = await _seed_image_task(db, actor.id)
    parent_row = None
    if parent:
        parent_row = Annotation(
            task_id=task.id,
            project_id=task.project_id,
            user_id=actor.id,
            annotation_type="bbox",
            class_name="parent",
            tool_unit_id="bbox",
            geometry={"type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1},
        )
        db.add(parent_row)
        await db.flush()
    reference = build_rle_reference(FULL)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=actor.id,
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="object",
        geometry={"type": "raster_mask", "mask": reference},
        source="prediction_based",
        confidence=0.75,
        attributes={"nested": {"values": ["kept"]}},
        attributes_meta={"nested": {"origin": "ai"}},
        parent_prediction_id=uuid.uuid4(),
        parent_annotation_id=parent_row.id if parent_row else None,
        z_order=8,
        version=1,
    )
    db.add(source)
    db.add(
        RasterMaskUpload(
            task_id=task.id,
            object_key=reference["object_key"],
            linked_at=datetime.now(timezone.utc),
        )
    )
    kept, new = slice_mask_rle(FULL, CUT, _AlgebraBudget())
    for rle in (FULL, kept, new):
        content[build_rle_reference(rle)["sha256"]] = rle
    references = [build_rle_reference(rle) for rle in (kept, new)]
    for ref in references:
        db.add(RasterMaskUpload(task_id=task.id, object_key=ref["object_key"]))
    await db.flush()
    payload = _payload(source, _scope(), reference=references[1], key=uuid.uuid4().hex)
    payload.update(operation="slice_mask", cut_path=CUT)
    payload["mutations"].insert(
        0,
        {
            "kind": "update",
            "annotation_id": str(source.id),
            "geometry": {"type": "raster_mask", "mask": references[0]},
        },
    )
    return task, source, MaskMutationCommitRequest.model_validate(payload)


def _restore(receipt, target="before", **overrides):
    return AnnotationSliceRestoreRequest.model_validate(
        {
            "target": target,
            "expected_versions": receipt.result_versions,
            "idempotency_key": uuid.uuid4().hex,
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_slice_and_restore_preserve_identity_metadata_and_versions(
    db_session, super_admin, slice_content
):
    actor, _ = super_admin
    task, source, payload = await _seed(db_session, actor, slice_content, parent=True)
    parent = await db_session.get(Annotation, source.parent_annotation_id)
    before = deepcopy(source.geometry)
    response = await MaskMutationService(db_session)._commit(task.id, payload, actor)
    receipt = response.slice_restore
    assert receipt and receipt.source_annotation_id == source.id
    new = await db_session.get(Annotation, receipt.created_annotation_id)
    assert (
        source.geometry != before
        and source.user_id == actor.id
        and source.confidence == 0.75
        and source.source == "prediction_based"
    )
    assert (
        new.attributes == source.attributes
        and new.attributes_meta == source.attributes_meta
    )
    assert new.parent_annotation_id == source.parent_annotation_id == parent.id
    assert (
        new.z_order == 8
        and new.source == "manual"
        and new.confidence is None
        and new.parent_prediction_id is None
    )
    operation = await db_session.get(AnnotationOperation, receipt.operation_id)
    assert operation.report["slice_snapshot_schema"] == 2
    assert "object_key" not in str(operation.report) and "counts" not in str(
        operation.report
    )
    assert len(response.lineage_edges) == 2
    undo = await AnnotationSliceService(db_session)._restore(
        task.id, receipt.operation_id, _restore(receipt), actor
    )
    assert source.geometry == before and source.is_active and not new.is_active
    assert undo.result_versions == {str(source.id): 3, str(new.id): 2}
    redo = await AnnotationSliceService(db_session)._restore(
        task.id, receipt.operation_id, _restore(undo, "after"), actor
    )
    assert new.is_active and source.geometry != before
    assert redo.result_versions == {str(source.id): 4, str(new.id): 3}
    assert redo.restore_expires_at == receipt.restore_expires_at
    replay = await MaskMutationService(db_session)._commit(task.id, payload, actor)
    assert replay.idempotent_replay and replay.operation_id == receipt.operation_id

    task.status = "completed"
    await db_session.flush()
    locked_task_replay = await MaskMutationService(db_session)._commit(
        task.id, payload, actor
    )
    assert locked_task_replay.idempotent_replay


@pytest.mark.asyncio
async def test_undo_gc_redo_and_reference_retention(
    db_session, super_admin, slice_content
):
    actor, _ = super_admin
    task, source, payload = await _seed(db_session, actor, slice_content)
    receipt = (
        await MaskMutationService(db_session)._commit(task.id, payload, actor)
    ).slice_restore
    undo = await AnnotationSliceService(db_session)._restore(
        task.id, receipt.operation_id, _restore(receipt), actor
    )
    operation = await db_session.get(AnnotationOperation, receipt.operation_id)
    new = await db_session.get(Annotation, receipt.created_annotation_id)
    assert not new.is_active
    # Exercise finite retention plus infinity without shortening either kind.
    await db_session.execute(
        update(MaskAnnotationRevision)
        .where(MaskAnnotationRevision.annotation_id == source.id)
        .values(expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    )
    await protect_mask_slice_versions(
        db_session, task.id, operation.report, receipt.restore_expires_at
    )
    retained = (
        await db_session.execute(
            text(
                "SELECT annotation_id, expires_at >= :deadline AS protected, expires_at = 'infinity'::timestamptz AS forever FROM mask_annotation_revisions WHERE annotation_id IN (:source, :created)"
            ),
            {
                "deadline": receipt.restore_expires_at,
                "source": source.id,
                "created": new.id,
            },
        )
    ).all()
    assert retained and all(row.protected for row in retained)
    assert any(row.forever for row in retained if row.annotation_id == new.id)
    assert await expire_mask_annotation_revisions(db_session) == 0
    for snapshot in operation.report["after"].values():
        assert "annotation_version" in snapshot
    assert await is_raster_mask_key_referenced(
        db_session, new.geometry["mask"]["object_key"]
    )
    await AnnotationSliceService(db_session)._restore(
        task.id, receipt.operation_id, _restore(undo, "after"), actor
    )
    assert (
        new.is_active
        and sum(_decode(slice_content[source.geometry["mask"]["sha256"]]))
        + sum(_decode(slice_content[new.geometry["mask"]["sha256"]]))
        == 6
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    ["swapped", "lost_pixel", "locked", "child", "same_key", "source_version"],
)
async def test_commit_guards_are_atomic(
    db_session, super_admin, slice_content, invalid
):
    actor, _ = super_admin
    task, source, payload = await _seed(db_session, actor, slice_content)
    value = payload.model_dump(mode="json")
    if invalid == "swapped":
        value["mutations"][0]["geometry"], value["mutations"][1]["geometry"] = (
            value["mutations"][1]["geometry"],
            value["mutations"][0]["geometry"],
        )
    elif invalid == "lost_pixel":
        value["mutations"][1]["geometry"] = value["mutations"][0]["geometry"]
    elif invalid == "locked":
        source.is_locked = True
    elif invalid == "child":
        db_session.add(
            Annotation(
                task_id=task.id,
                project_id=task.project_id,
                user_id=actor.id,
                annotation_type="bbox",
                class_name="child",
                tool_unit_id="bbox",
                parent_annotation_id=source.id,
                geometry={"type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1},
            )
        )
    elif invalid == "source_version":
        value["expected_versions"][0]["version"] += 1
    elif invalid == "same_key":
        await MaskMutationService(db_session)._commit(task.id, payload, actor)
        value["cut_path"] = list(reversed(value["cut_path"]))
    await db_session.flush()
    before = deepcopy(source.geometry), source.version
    count = await db_session.scalar(
        select(func.count())
        .select_from(AnnotationOperation)
        .where(AnnotationOperation.task_id == task.id)
    )
    with pytest.raises(MaskMutationError):
        await MaskMutationService(db_session)._commit(
            task.id, MaskMutationCommitRequest.model_validate(value), actor
        )
    assert (source.geometry, source.version) == before
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationOperation)
            .where(AnnotationOperation.task_id == task.id)
        )
        == count
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "missing_revision",
        "expired_revision",
        "expired_operation",
        "edited_output",
        "missing_content",
    ],
)
async def test_restore_rejects_missing_expired_and_changed_results_atomically(
    db_session, super_admin, slice_content, invalid
):
    actor, _ = super_admin
    task, source, payload = await _seed(db_session, actor, slice_content)
    receipt = (
        await MaskMutationService(db_session)._commit(task.id, payload, actor)
    ).slice_restore
    revision_where = (MaskAnnotationRevision.annotation_id == source.id) & (
        MaskAnnotationRevision.annotation_version == 1
    )
    if invalid == "missing_revision":
        await db_session.execute(delete(MaskAnnotationRevision).where(revision_where))
    elif invalid == "expired_revision":
        await db_session.execute(
            update(MaskAnnotationRevision)
            .where(revision_where)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        )
    elif invalid == "expired_operation":
        operation = await db_session.get(AnnotationOperation, receipt.operation_id)
        operation.created_at -= timedelta(days=31)
    elif invalid == "edited_output":
        source.version += 1
        source.attributes = {"edited": True}
    elif invalid == "missing_content":
        slice_content.pop(build_rle_reference(FULL)["sha256"])
    await db_session.flush()
    before = deepcopy(source.geometry), source.version
    with pytest.raises(AnnotationSliceError):
        await AnnotationSliceService(db_session)._restore(
            task.id, receipt.operation_id, _restore(receipt), actor
        )
    assert (source.geometry, source.version) == before
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationOperation)
            .where(AnnotationOperation.task_id == task.id)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_slice_and_captured_revisions(
    db_session, super_admin, slice_content, monkeypatch
):
    actor, _ = super_admin
    task, source, payload = await _seed(db_session, actor, slice_content)
    source_id = source.id
    before = deepcopy(source.geometry), source.version
    monkeypatch.setattr(
        AuditService, "log", AsyncMock(side_effect=RuntimeError("audit unavailable"))
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        async with db_session.begin_nested():
            await MaskMutationService(db_session)._commit(task.id, payload, actor)
    await db_session.refresh(source)
    assert (source.geometry, source.version) == before
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MaskAnnotationRevision)
            .where(MaskAnnotationRevision.annotation_id == source_id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_real_routes_slice_restore_and_schema_guards(
    db_session, httpx_client, super_admin, slice_content
):
    actor, token = super_admin
    task, source, payload = await _seed(db_session, actor, slice_content)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit"
    response = await httpx_client.post(
        url, headers=headers, json=payload.model_dump(mode="json")
    )
    assert response.status_code == 200, response.text
    receipt = response.json()["slice_restore"]
    restore_url = f"/api/v1/tasks/{task.id}/annotations/slices/{receipt['slice_operation_id']}:restore"
    restored = await httpx_client.post(
        restore_url,
        headers=headers,
        json={
            "target": "before",
            "expected_versions": receipt["result_versions"],
            "idempotency_key": uuid.uuid4().hex,
        },
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["active_annotation_ids"] == [str(source.id)]
    for edit in (
        {"cut_path": [[0, 0], [0, 0]]},
        {"cut_path": [[0, 0], [1, 1], [0.5, 0.5]]},
        {"cut_path": None},
        {"operation": "split_components"},
    ):
        with pytest.raises(ValidationError):
            MaskMutationCommitRequest.model_validate(
                {**payload.model_dump(mode="json"), **edit}
            )
