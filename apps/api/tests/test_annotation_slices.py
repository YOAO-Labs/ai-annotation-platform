from __future__ import annotations

import asyncio
import math
import random
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from shapely.geometry import LineString, Polygon
from shapely.ops import split
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import (
    AnnotationLineageEdge,
    AnnotationOperation,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.schemas.annotation_slice import (
    AnnotationSliceRestoreRequest,
    PolygonSliceCommitRequest,
)
from app.services.annotation import AnnotationService
from app.services.annotation_slice import AnnotationSliceError, AnnotationSliceService
from app.services.audit import AuditService
from app.services.polygon_slice import (
    PolygonSliceGeometryError,
    signed_area,
    slice_polygon,
)

SQUARE = {"type": "polygon", "points": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]}
CONCAVE = {
    "type": "polygon",
    "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.8], [0.6, 0.8], [0.6, 0.4], [0.1, 0.4]],
}
CUT = [[0.7, 0], [0.7, 1]]


def _payload(source, **overrides):
    return PolygonSliceCommitRequest.model_validate(
        {
            "annotation_id": source.id,
            "expected_version": source.version,
            "idempotency_key": uuid.uuid4().hex,
            "cut_path": CUT,
            **overrides,
        }
    )


def _restore(response, target="before", **overrides):
    return AnnotationSliceRestoreRequest.model_validate(
        {
            "target": target,
            "expected_versions": response.result_versions,
            "idempotency_key": uuid.uuid4().hex,
            **overrides,
        }
    )


async def _seed(db, actor, *, parent=False):
    suffix = uuid.uuid4().hex
    project = Project(
        display_id=f"P-SL-{suffix[:8]}",
        name=f"slice-{suffix}",
        type_label="图像分割",
        type_key="image-seg",
        data_type="image",
        owner_id=actor.id,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "object"}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(project)
    await db.flush()
    task = Task(
        project_id=project.id,
        display_id=f"T-SL-{suffix[:8]}",
        file_name="slice.png",
        file_path=f"slices/{suffix}.png",
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    parent_row = None
    if parent:
        parent_row = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=actor.id,
            annotation_type="bbox",
            tool_unit_id="bbox",
            class_name="parent",
            geometry={"type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1},
        )
        db.add(parent_row)
        await db.flush()
    source = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=actor.id,
        source="prediction_based",
        annotation_type="polygon",
        tool_unit_id="region",
        class_name="object",
        geometry=deepcopy(CONCAVE),
        confidence=0.8,
        parent_prediction_id=uuid.uuid4(),
        parent_annotation_id=parent_row.id if parent_row else None,
        attributes={"weather": "sunny"},
        attributes_meta={"weather": {"origin": "ai", "confidence": 0.9}},
        z_order=7,
        version=1,
    )
    db.add(source)
    await db.flush()
    return task, source, parent_row


@pytest.mark.parametrize("reverse_ring", [False, True])
@pytest.mark.parametrize(
    "path", [CUT, list(reversed(CUT)), [[0, 0.3], [0.65, 0.3], [0.7, 0.6], [1, 0.6]]]
)
def test_geometry_matches_independent_geos_partition(reverse_ring, path):
    geometry = deepcopy(CONCAVE)
    if reverse_ring:
        geometry["points"].reverse()
    before = deepcopy(geometry)
    results = slice_polygon(geometry, path)
    source = Polygon(geometry["points"])
    left, right = [Polygon(item["points"]) for item in results]
    oracle = split(source, LineString(path))
    assert len(oracle.geoms) == 2
    assert all(result.is_valid and result.area > 0 for result in (left, right))
    assert left.intersection(right).area < 1e-12
    assert left.union(right).symmetric_difference(source).area < 1e-12
    assert left.area >= right.area - 1e-12
    assert all(
        any(
            result.symmetric_difference(expected).area < 1e-12
            for expected in oracle.geoms
        )
        for result in (left, right)
    )
    assert geometry == before


def test_geometry_equal_area_uses_centroid_and_supports_vertex_crossings_and_closed_ring():
    geometry = {**SQUARE, "points": [*SQUARE["points"], SQUARE["points"][0]]}
    for path in (
        [[0, 0], [1, 1]],
        [[1, 1], [0, 0]],
        [[0.5, 0], [0.5, 1]],
        [[0.5, 1], [0.5, 0]],
    ):
        first, second = [
            Polygon(item["points"]) for item in slice_polygon(geometry, path)
        ]
        assert first.area == pytest.approx(second.area)
        assert (first.centroid.x, first.centroid.y) < (
            second.centroid.x,
            second.centroid.y,
        )


@pytest.mark.parametrize(
    "geometry,path",
    [
        (SQUARE, [[0, 0.4], [0.2, 0.2], [0, 0]]),  # tangent vertex
        (SQUARE, [[0, 0.2], [1, 0.2]]),  # boundary overlap
        (CONCAVE, [[0, 0.3], [1, 0.3], [1, 0.6], [0, 0.6]]),  # four crossings
        (SQUARE, [[0.5, 0.5], [1, 0.5]]),  # begins inside
        (SQUARE, [[0, 0], [1, 1], [0, 1], [1, 0]]),
        (SQUARE, [[0, 0.5], [1, 0.5], [0, 0.5]]),
        (SQUARE, [[0, 0], [0, 0]]),
        (SQUARE, [[float("nan"), 0], [1, 1]]),
        (SQUARE, [[-0.1, 0], [1, 1]]),
        ({**SQUARE, "holes": [[[0.3, 0.3], [0.4, 0.3], [0.3, 0.4]]]}, CUT),
        ({"type": "multi_polygon", "polygons": [SQUARE]}, CUT),
        (
            {
                "type": "polygon",
                "points": [[0.2, 0.2], [0.8, 0.8], [0.2, 0.8], [0.8, 0.2]],
            },
            CUT,
        ),
        ({"type": "polygon", "points": [[0.2, 0.2], [0.4, 0.4], [0.6, 0.6]]}, CUT),
        ({**SQUARE, "points": [*SQUARE["points"], SQUARE["points"][1]]}, CUT),
        (SQUARE, [[0, 0]] * 257),
    ],
)
def test_invalid_geometry_is_rejected(geometry, path):
    with pytest.raises(PolygonSliceGeometryError):
        slice_polygon(geometry, path)


def test_boundary_endpoints_can_slice_a_full_image_polygon():
    geometry = {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]}
    results = slice_polygon(geometry, [[0.5, 0], [0.5, 1]])
    first, second = [Polygon(result["points"]) for result in results]
    assert first.area == second.area == 0.5
    assert first.intersection(second).area == 0
    assert first.union(second).equals(Polygon(geometry["points"]))


def test_randomized_simple_partitions_preserve_shape_against_geos():
    rng = random.Random(8493)
    checked = 0
    for _ in range(100):
        angles = sorted(rng.random() * 2 * math.pi for _ in range(10))
        ring = [
            [0.5 + radius * math.cos(angle), 0.5 + radius * math.sin(angle)]
            for angle in angles
            for radius in [rng.uniform(0.2, 0.4)]
        ]
        if not Polygon(ring).is_valid:
            continue
        path = [[rng.uniform(0.35, 0.65), 0], [rng.uniform(0.35, 0.65), 1]]
        oracle = split(Polygon(ring), LineString(path))
        if len(oracle.geoms) != 2:
            continue
        first, second = [
            Polygon(item["points"])
            for item in slice_polygon({"type": "polygon", "points": ring}, path)
        ]
        assert first.union(second).symmetric_difference(Polygon(ring)).area < 1e-10
        assert first.intersection(second).area < 1e-10
        assert first.area >= second.area - 1e-12
        checked += 1
    assert checked >= 70


async def test_commit_undo_redo_preserve_ids_provenance_parent_attributes_and_deadline(
    db_session, super_admin
):
    actor, _ = super_admin
    task, source, parent = await _seed(db_session, actor, parent=True)
    original = deepcopy(source.geometry)
    prediction_id = source.parent_prediction_id
    service = AnnotationSliceService(db_session)
    payload = _payload(source)
    result = await service.commit(task.id, payload, actor)
    created = await db_session.get(Annotation, result.created_annotation_id)
    assert result.source_annotation_id == source.id
    assert source.version == 2 and created.version == 1
    assert (
        source.source == "prediction_based"
        and source.confidence == 0.8
        and source.parent_prediction_id == prediction_id
    )
    assert (
        created.source == "manual"
        and created.user_id == actor.id
        and created.confidence is None
        and created.parent_prediction_id is None
    )
    assert created.parent_annotation_id == source.parent_annotation_id == parent.id
    assert created.attributes == source.attributes == {"weather": "sunny"}
    assert created.attributes_meta == source.attributes_meta
    assert (
        created.z_order == source.z_order == 7
        and created.tool_unit_id == source.tool_unit_id == "region"
    )
    assert task.total_annotations == 3
    after = {str(row.id): deepcopy(row.geometry) for row in (source, created)}
    assert abs(signed_area(source.geometry["points"])) >= abs(
        signed_area(created.geometry["points"])
    )
    replay = await service.commit(task.id, payload, actor)
    assert replay.idempotent_replay and replay.operation_id == result.operation_id
    undo_payload = _restore(result)
    undone = await service.restore(task.id, result.operation_id, undo_payload, actor)
    assert source.geometry == original and source.version == 3
    assert (
        not created.is_active and created.version == 2 and task.total_annotations == 2
    )
    same = await service.restore(task.id, result.operation_id, undo_payload, actor)
    assert same.idempotent_replay and same.operation_id == undone.operation_id
    no_op = await service.restore(task.id, result.operation_id, _restore(undone), actor)
    assert no_op.no_op and no_op.result_versions == undone.result_versions
    redone = await service.restore(
        task.id, result.operation_id, _restore(undone, "after"), actor
    )
    assert (
        redone.restore_expires_at
        == undone.restore_expires_at
        == result.restore_expires_at
    )
    assert redone.result_versions == {str(source.id): 4, str(created.id): 3}
    assert all(
        row.is_active and row.geometry == after[str(row.id)]
        for row in (source, created)
    )
    assert task.total_annotations == 3
    assert (
        await db_session.scalar(select(func.count()).select_from(AnnotationLineageEdge))
    ) == 6


async def test_slice_commit_and_restore_replay_after_task_becomes_locked(
    db_session, super_admin
):
    actor, _ = super_admin
    task, source, _ = await _seed(db_session, actor)
    service = AnnotationSliceService(db_session)
    payload = _payload(source)
    result = await service.commit(task.id, payload, actor)

    task.status = "completed"
    await db_session.flush()
    commit_replay = await service.commit(task.id, payload, actor)
    assert commit_replay.idempotent_replay

    task.status = "pending"
    await db_session.flush()
    restore_payload = _restore(result)
    restored = await service.restore(
        task.id, result.operation_id, restore_payload, actor
    )

    task.status = "completed"
    await db_session.flush()
    restore_replay = await service.restore(
        task.id, result.operation_id, restore_payload, actor
    )
    assert restore_replay.idempotent_replay
    assert restore_replay.operation_id == restored.operation_id


@pytest.mark.parametrize(
    "failure",
    [
        "source_version",
        "locked",
        "active_child",
        "inactive",
        "holes",
        "multi",
        "bad_cut",
        "task_locked",
        "foreign_task",
        "lock_owner",
    ],
)
async def test_commit_rejects_without_annotation_or_ledger_changes(
    failure, db_session, super_admin, annotator
):
    actor, _ = super_admin
    task, source, _ = await _seed(db_session, actor)
    payload = _payload(source)
    target_task_id = task.id
    if failure == "source_version":
        payload.expected_version = 7
    elif failure == "locked":
        source.is_locked = True
    elif failure == "active_child":
        db_session.add(
            Annotation(
                task_id=task.id,
                project_id=task.project_id,
                user_id=actor.id,
                annotation_type="polygon",
                tool_unit_id="region",
                class_name="object",
                geometry=SQUARE,
                parent_annotation_id=source.id,
            )
        )
    elif failure == "inactive":
        source.is_active = False
    elif failure == "holes":
        source.geometry = {**CONCAVE, "holes": [[[0.3, 0.2], [0.4, 0.2], [0.4, 0.3]]]}
    elif failure == "multi":
        source.annotation_type = "multi_polygon"
        source.geometry = {"type": "multi_polygon", "polygons": [CONCAVE]}
    elif failure == "bad_cut":
        payload.cut_path = [(0, 0.1), (1, 0.1)]
    elif failure == "task_locked":
        task.status = "completed"
    elif failure == "foreign_task":
        other, _, _ = await _seed(db_session, actor)
        target_task_id = other.id
    elif failure == "lock_owner":
        db_session.add(
            TaskLock(
                task_id=task.id,
                user_id=annotator[0].id,
                expire_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
    await db_session.flush()
    before = deepcopy(source.geometry)
    count = await db_session.scalar(select(func.count()).select_from(Annotation))
    with pytest.raises((AnnotationSliceError, HTTPException)):
        await AnnotationSliceService(db_session).commit(target_task_id, payload, actor)
    assert source.geometry == before and source.version == 1
    assert (
        await db_session.scalar(select(func.count()).select_from(Annotation)) == count
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(AnnotationOperation))
        == 0
    )


@pytest.mark.parametrize(
    "failure",
    [
        "changed_output",
        "refreshed_versions",
        "active_child",
        "inactive_child_output_locked",
        "expired",
        "unsupported_operation",
        "different_key_content",
    ],
)
async def test_restore_rejects_atomically_and_does_not_advance_ledger(
    failure, db_session, super_admin
):
    actor, _ = super_admin
    task, source, _ = await _seed(db_session, actor)
    service = AnnotationSliceService(db_session)
    result = await service.commit(task.id, _payload(source), actor)
    created = await db_session.get(Annotation, result.created_annotation_id)
    original = await db_session.get(AnnotationOperation, result.operation_id)
    payload = _restore(result)
    if failure in {"changed_output", "refreshed_versions"}:
        created.attributes = {"weather": "rain"}
        created.version += 1
        if failure == "refreshed_versions":
            payload.expected_versions[created.id] = created.version
    elif failure == "active_child":
        db_session.add(
            Annotation(
                task_id=task.id,
                project_id=task.project_id,
                user_id=actor.id,
                annotation_type="polygon",
                tool_unit_id="region",
                class_name="object",
                geometry=SQUARE,
                parent_annotation_id=created.id,
            )
        )
    elif failure == "inactive_child_output_locked":
        result = await service.restore(task.id, result.operation_id, payload, actor)
        payload = _restore(result, "after")
        created.is_locked = True
    elif failure == "expired":
        original.created_at -= timedelta(days=31)
    elif failure == "unsupported_operation":
        original.kind = "split_components"
    elif failure == "different_key_content":
        first = await service.restore(
            task.id, result.slice_operation_id, payload, actor
        )
        payload = _restore(first, "after", idempotency_key=payload.idempotency_key)
    await db_session.flush()
    before = {
        row.id: (deepcopy(row.geometry), row.is_active, row.version)
        for row in (source, created)
    }
    report = deepcopy(original.report)
    count = await db_session.scalar(
        select(func.count()).select_from(AnnotationOperation)
    )
    with pytest.raises(AnnotationSliceError):
        await service.restore(task.id, original.id, payload, actor)
    assert all(
        (row.geometry, row.is_active, row.version) == before[row.id]
        for row in (source, created)
    )
    assert original.report == report
    assert (
        await db_session.scalar(select(func.count()).select_from(AnnotationOperation))
        == count
    )


async def test_idempotency_content_conflict_and_unexpected_failure_roll_back(
    db_session, super_admin, monkeypatch
):
    actor, _ = super_admin
    task, source, _ = await _seed(db_session, actor)
    service = AnnotationSliceService(db_session)
    payload = _payload(source)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        async with db_session.begin_nested():
            with monkeypatch.context() as patch:
                patch.setattr(
                    AuditService,
                    "log",
                    AsyncMock(side_effect=RuntimeError("audit unavailable")),
                )
                await service.commit(task.id, payload, actor)
    await db_session.refresh(source)
    assert source.geometry == CONCAVE and source.version == 1
    await db_session.refresh(task)
    assert task.annotation_contributor_ids == []
    assert (
        await db_session.scalar(select(func.count()).select_from(AnnotationOperation))
        == 0
    )
    assert await db_session.scalar(select(func.count()).select_from(Annotation)) == 1
    await service.commit(task.id, payload, actor)
    payload.cut_path = [(0, 0.3), (1, 0.3)]
    with pytest.raises(AnnotationSliceError, match="幂等键"):
        await service.commit(task.id, payload, actor)


async def test_routes_validate_shape_authorization_and_return_saved_versions(
    httpx_client, db_session, super_admin, annotator
):
    actor, token = super_admin
    task, source, _ = await _seed(db_session, actor)
    url = f"/api/v1/tasks/{task.id}/annotations/polygon-slices:commit"
    headers = {"Authorization": f"Bearer {token}"}
    payload = _payload(source).model_dump(mode="json")
    assert (await httpx_client.post(url, json=payload)).status_code == 401
    assert (
        await httpx_client.post(
            url, json=payload, headers={"Authorization": f"Bearer {annotator[1]}"}
        )
    ).status_code == 404
    invalid = await httpx_client.post(
        url, json={**payload, "results": [SQUARE, SQUARE]}, headers=headers
    )
    assert invalid.status_code == 422
    response = await httpx_client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["result_versions"][str(source.id)] == 2
    undo = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/slices/{result['operation_id']}:restore",
        headers=headers,
        json={
            "target": "before",
            "expected_versions": result["result_versions"],
            "idempotency_key": uuid.uuid4().hex,
        },
    )
    assert undo.status_code == 200, undo.text
    assert undo.json()["active_annotation_ids"] == [str(source.id)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("cut_path", [[float("inf"), 0], [1, 1]]),
        ("cut_path", [[0, 0]] * 257),
        ("cut_path", [[0, 0]]),
        ("expected_version", True),
        ("expected_version", 0),
        ("idempotency_key", "short"),
    ],
)
def test_request_limits(field, value):
    with pytest.raises(ValidationError):
        PolygonSliceCommitRequest.model_validate(
            {
                "annotation_id": uuid.uuid4(),
                "expected_version": 1,
                "idempotency_key": uuid.uuid4().hex,
                "cut_path": CUT,
                field: value,
            }
        )


async def test_concurrent_replay_and_child_creation_wait_for_restore(
    test_engine, monkeypatch
):
    # This committed-session test checks locks and idempotency. Other transaction
    # tests exercise real audit writes; avoid immutable audit rows in its cleanup.
    monkeypatch.setattr(AuditService, "log", AsyncMock(return_value=None))
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        actor = User(
            id=uuid.uuid4(),
            email=f"slice-{uuid.uuid4().hex}@test.local",
            name="Slice concurrency",
            password_hash="unused",
            role="super_admin",
            is_active=True,
        )
        db.add(actor)
        await db.flush()
        task, source, _ = await _seed(db, actor)
        payload = _payload(source)
        await db.commit()

    async def commit():
        async with maker() as db:
            result = await AnnotationSliceService(db).commit(task.id, payload, actor)
            await db.commit()
            return result

    try:
        first, second = await asyncio.gather(commit(), commit())
        assert first.operation_id == second.operation_id
        assert sorted([first.idempotent_replay, second.idempotent_replay]) == [
            False,
            True,
        ]
        async with maker() as restoring:
            await AnnotationSliceService(restoring).restore(
                task.id, first.operation_id, _restore(first), actor
            )
            started = asyncio.Event()

            async def create_child():
                async with maker() as db:
                    started.set()
                    await AnnotationService(db).create(
                        task.id,
                        actor.id,
                        "polygon",
                        "object",
                        SQUARE,
                        parent_annotation_id=first.created_annotation_id,
                        tool_unit_id="region",
                    )
                    await db.commit()

            child = asyncio.create_task(create_child())
            await started.wait()
            await asyncio.sleep(0.05)
            assert not child.done()
            await restoring.commit()
            with pytest.raises(HTTPException, match="inactive"):
                await asyncio.wait_for(child, 5)
        async with maker() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Annotation)
                    .where(Annotation.task_id == task.id)
                )
                == 2
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AnnotationOperation)
                    .where(AnnotationOperation.task_id == task.id)
                )
                == 2
            )
    finally:
        async with maker() as db:
            await db.execute(delete(Annotation).where(Annotation.task_id == task.id))
            await db.execute(delete(TaskLock).where(TaskLock.task_id == task.id))
            await db.execute(delete(Task).where(Task.id == task.id))
            await db.execute(delete(Project).where(Project.id == task.project_id))
            await db.execute(delete(User).where(User.id == actor.id))
            await db.commit()
