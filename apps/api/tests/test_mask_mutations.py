from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import (
    AnnotationLineageEdge,
    AnnotationOperation,
)
from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.project import Project
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.schemas.mask_mutation import MaskMutationCommitRequest, MaskMutationScope
from app.services.mask_mutation import (
    MaskMutationError,
    MaskMutationService,
    _AlgebraBudget,
    _combine_rles,
    scope_fingerprint,
)
from app.services.raster_mask_storage import (
    build_rle_reference,
    prepare_mask_payload_for_write,
)
from tests.factory import create_membership


RLE_A = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 3]}
RLE_B = {"encoding": "coco_rle", "size": [2, 3], "counts": [0, 1, 5]}
RLE_COMPONENT = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 1, 4]}
RLE_JOINED = {"encoding": "coco_rle", "size": [2, 3], "counts": [0, 3, 3]}
RLE_EMPTY = {"encoding": "coco_rle", "size": [2, 3], "counts": [6]}


async def _seed_image_task(db, owner_id: uuid.UUID):
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(
        display_id=f"DS-MM-{suffix}",
        name=f"mask-mutation-{suffix}",
        data_type="image",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=f"mask-{suffix}.png",
        file_path=f"mask-mutation/{suffix}.png",
        file_type="image",
        width=3,
        height=2,
    )
    db.add(item)
    await db.flush()
    project = Project(
        display_id=f"P-MM-{suffix}",
        name=f"mask-mutation-{suffix}",
        type_label="图像分割",
        type_key="image-seg",
        data_type="image",
        owner_id=owner_id,
        raster_mask_native_editing_enabled=True,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "object"}, {"name": "other"}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(project)
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-MM-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return task


async def _seed_annotation(
    db,
    task: Task,
    owner_id: uuid.UUID,
    *,
    class_name: str = "object",
    locked: bool = False,
    rle: dict = RLE_A,
) -> Annotation:
    reference = build_rle_reference(rle)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner_id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name=class_name,
        geometry={"type": "raster_mask", "mask": reference},
        is_locked=locked,
        version=1,
    )
    db.add(annotation)
    db.add(
        RasterMaskUpload(
            task_id=task.id,
            object_key=reference["object_key"],
            linked_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()
    return annotation


async def _seed_video_task(db, owner_id: uuid.UUID):
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(
        display_id=f"DS-MMV-{suffix}",
        name=f"mask-mutation-video-{suffix}",
        data_type="video",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=f"mask-{suffix}.mp4",
        file_path=f"mask-mutation/{suffix}.mp4",
        file_type="video",
        metadata_={
            "video": {
                "width": 3,
                "height": 2,
                "fps": 30,
                "frame_count": 11,
            }
        },
    )
    db.add(item)
    await db.flush()
    project = Project(
        display_id=f"P-MMV-{suffix}",
        name=f"mask-mutation-video-{suffix}",
        type_label="视频分割",
        type_key="video-seg",
        data_type="video",
        owner_id=owner_id,
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
        dataset_item_id=item.id,
        display_id=f"T-MMV-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="video",
        status="pending",
    )
    db.add(task)
    segment = VideoSegment(
        dataset_item_id=item.id,
        segment_index=0,
        start_frame=0,
        end_frame=10,
        assignee_id=owner_id,
        locked_by=owner_id,
        lock_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    db.add(segment)
    await db.flush()
    return task, segment


async def _seed_video_annotation(
    db,
    task: Task,
    owner_id: uuid.UUID,
    *,
    track_id: str = "trk_source",
    first_rle: dict = RLE_A,
    second_rle: dict = RLE_B,
) -> Annotation:
    first_reference = build_rle_reference(first_rle)
    second_reference = build_rle_reference(second_rle)
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner_id,
        source="manual",
        annotation_type="video_track_mask",
        tool_unit_id="region",
        class_name="object",
        geometry={
            "type": "video_track_mask",
            "track_id": track_id,
            "semantic_label": "object",
            "keyframes": [
                {
                    "frame_index": 0,
                    "mask": first_reference,
                    "source": "manual",
                    "occluded": False,
                },
                {
                    "frame_index": 5,
                    "mask": second_reference,
                    "source": "manual",
                    "occluded": False,
                },
            ],
            "outside": [],
        },
        track_id=track_id,
        version=1,
    )
    db.add(annotation)
    for reference in (first_reference, second_reference):
        existing_upload = await db.scalar(
            select(RasterMaskUpload.id).where(
                RasterMaskUpload.task_id == task.id,
                RasterMaskUpload.object_key == reference["object_key"],
            )
        )
        if existing_upload is None:
            db.add(
                RasterMaskUpload(
                    task_id=task.id,
                    object_key=reference["object_key"],
                    linked_at=datetime.now(timezone.utc),
                )
            )
    await db.flush()
    return annotation


def _scope(
    *, strict: bool = False, instance_filter: str = "same_class"
) -> MaskMutationScope:
    return MaskMutationScope(
        media="image",
        instance_filter=instance_filter,
        class_name="object" if instance_filter == "same_class" else None,
        overlap_policy="allow",
        strict_non_overlap=strict,
    )


def _payload(
    source: Annotation,
    scope: MaskMutationScope,
    *,
    reference: dict,
    key: str = "mask-mutation-key-0001",
    fingerprint: str | None = None,
    expected_versions: list[dict] | None = None,
) -> dict:
    return {
        "idempotency_key": key,
        "operation": "copy_component",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": fingerprint or scope_fingerprint(scope, [source]),
        "expected_versions": expected_versions
        or [{"annotation_id": str(source.id), "version": int(source.version or 1)}],
        "mutations": [
            {
                "kind": "create",
                "source_annotation_ids": [str(source.id)],
                "geometry": {"type": "raster_mask", "mask": reference},
            }
        ],
        "report": {
            "source_areas": [2],
            "result_areas": [2, 1],
            "connectivity": 4,
        },
    }


@pytest.fixture
def mask_content(monkeypatch):
    async def load(reference: dict):
        by_digest = {
            build_rle_reference(RLE_A)["sha256"]: RLE_A,
            build_rle_reference(RLE_B)["sha256"]: RLE_B,
            build_rle_reference(RLE_COMPONENT)["sha256"]: RLE_COMPONENT,
            build_rle_reference(RLE_JOINED)["sha256"]: RLE_JOINED,
            build_rle_reference(RLE_EMPTY)["sha256"]: RLE_EMPTY,
        }
        return by_digest[reference["sha256"]]

    mock = AsyncMock(side_effect=load)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", mock)
    monkeypatch.setattr("app.services.mask_mutation.load_coco_rle", mock)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    return mock


@pytest.mark.asyncio
async def test_copy_component_commits_once_and_replays_same_response(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id)
    new_reference = build_rle_reference(RLE_COMPONENT)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=new_reference["object_key"])
    )
    await db_session.flush()
    payload = _payload(source, _scope(), reference=new_reference)
    headers = {"Authorization": f"Bearer {token}"}

    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["idempotent_replay"] is False
    assert body["updated_annotations"] == []
    assert len(body["created_annotations"]) == 1
    assert body["lineage_edges"][0]["source_annotation_id"] == str(source.id)
    assert "geometry" not in body["created_annotations"][0]

    replay = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["operation_id"] == body["operation_id"]
    assert replay.json()["idempotent_replay"] is True

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationOperation)
            .where(AnnotationOperation.task_id == task.id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationLineageEdge)
            .where(
                AnnotationLineageEdge.operation_id == uuid.UUID(body["operation_id"])
            )
        )
        == 1
    )
    operation = await db_session.get(
        AnnotationOperation, uuid.UUID(body["operation_id"])
    )
    assert "geometry" not in operation.response_json["created_annotations"][0]
    assert "counts" not in str(operation.response_json)
    assert "counts" not in str(operation.report)


@pytest.mark.asyncio
async def test_same_idempotency_key_with_changed_request_conflicts(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id)
    reference = build_rle_reference(RLE_COMPONENT)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=reference["object_key"])
    )
    await db_session.flush()
    payload = _payload(source, _scope(), reference=reference)
    headers = {"Authorization": f"Bearer {token}"}
    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 200, first.text

    payload["report"]["result_areas"] = [2, 2]
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_missing_expected_version_returns_428(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    first = await _seed_annotation(db_session, task, user.id)
    second = await _seed_annotation(db_session, task, user.id, rle=RLE_B)
    scope = _scope()
    payload = _payload(
        first,
        scope,
        reference=first.geometry["mask"],
        fingerprint=scope_fingerprint(
            scope, sorted([first, second], key=lambda item: str(item.id))
        ),
        expected_versions=[{"annotation_id": str(first.id), "version": 1}],
    )
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 428
    assert response.json()["detail"]["reason"] == "expected_versions_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_mode", ["omitted", "empty"])
async def test_missing_expected_versions_request_returns_stable_428(
    missing_mode, db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id)
    payload = _payload(source, _scope(), reference=source.geometry["mask"])
    if missing_mode == "omitted":
        payload.pop("expected_versions")
    else:
        payload["expected_versions"] = []

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 428
    assert response.json()["detail"]["reason"] == "expected_versions_missing"


@pytest.mark.asyncio
async def test_empty_image_result_returns_stable_empty_result(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id)
    empty_reference = build_rle_reference(RLE_EMPTY)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=empty_reference["object_key"])
    )
    await db_session.flush()
    payload = _payload(source, _scope(), reference=empty_reference)

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "empty_result"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"scope_fingerprint": "0" * 64}, "scope_stale"),
        ({"expected_versions": "wrong"}, "version_mismatch"),
    ],
)
async def test_scope_and_version_conflicts_are_distinct(
    override, reason, db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id)
    payload = _payload(source, _scope(), reference=source.geometry["mask"])
    if override.get("expected_versions"):
        payload["expected_versions"] = [
            {"annotation_id": str(source.id), "version": 99}
        ]
    else:
        payload.update(override)
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == reason


@pytest.mark.asyncio
async def test_locked_source_cannot_be_copied(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id, locked=True)
    payload = _payload(source, _scope(), reference=source.geometry["mask"])
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "annotation_locked"


@pytest.mark.asyncio
async def test_non_assigned_annotator_cannot_mutate_hidden_task(
    db_session, httpx_client, super_admin, annotator, mask_content
):
    owner, _ = super_admin
    actor, token = annotator
    task = await _seed_image_task(db_session, owner.id)
    batch = TaskBatch(
        project_id=task.project_id,
        display_id=f"B-MM-{uuid.uuid4().hex[:8]}",
        name="mask mutation assignment",
        status="active",
        annotator_id=owner.id,
        created_by=owner.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    source = await _seed_annotation(db_session, task, owner.id)
    reference = build_rle_reference(RLE_COMPONENT)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=reference["object_key"])
    )
    await db_session.flush()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=_payload(
            source, _scope(), reference=reference, key="mask-hidden-task-key-0001"
        ),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_task_lock_owned_by_another_user_blocks_atomic_mutation(
    db_session, httpx_client, super_admin, annotator, mask_content
):
    actor, token = super_admin
    lock_owner, _ = annotator
    task = await _seed_image_task(db_session, actor.id)
    source = await _seed_annotation(db_session, task, actor.id)
    reference = build_rle_reference(RLE_COMPONENT)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=reference["object_key"])
    )
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=lock_owner.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await db_session.flush()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=_payload(
            source, _scope(), reference=reference, key="mask-task-lock-key-0001"
        ),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "task_lock_conflict"


@pytest.mark.asyncio
async def test_video_segment_lease_must_belong_to_non_privileged_actor(
    db_session, httpx_client, super_admin, annotator, mask_content
):
    owner, _ = super_admin
    actor, token = annotator
    task, segment = await _seed_video_task(db_session, owner.id)
    await create_membership(
        db_session,
        project_id=task.project_id,
        user_id=actor.id,
        role="annotator",
        assigned_by=owner.id,
    )
    batch = TaskBatch(
        project_id=task.project_id,
        display_id=f"B-MMV-{uuid.uuid4().hex[:8]}",
        name="mask mutation segment lease",
        status="active",
        annotator_id=actor.id,
        created_by=owner.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    task.assignee_id = actor.id
    segment.assignee_id = actor.id
    segment.locked_by = owner.id
    source = await _seed_video_annotation(db_session, task, actor.id)
    scope = MaskMutationScope(
        media="video",
        frame_index=3,
        segment_id=segment.id,
        instance_filter="same_class",
        class_name="object",
        overlap_policy="allow",
    )
    reference = build_rle_reference(RLE_COMPONENT)
    payload = {
        "idempotency_key": "mask-segment-lock-key-0001",
        "operation": "copy_component",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, [source]),
        "expected_versions": [{"annotation_id": str(source.id), "version": 1}],
        "mutations": [
            {
                "kind": "create",
                "source_annotation_ids": [str(source.id)],
                "geometry": {
                    "type": "video_track_mask",
                    "track_id": "client_generated",
                    "semantic_label": "object",
                    "keyframes": [
                        {
                            "frame_index": 3,
                            "mask": reference,
                            "source": "manual",
                            "occluded": False,
                        }
                    ],
                    "outside": [],
                },
            }
        ],
        "report": {"connectivity": 4},
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "segment_lock_conflict"


@pytest.mark.asyncio
async def test_new_reference_must_be_reserved_by_same_task(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id)
    payload = _payload(source, _scope(), reference=build_rle_reference(RLE_B))
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "missing_ref"


@pytest.mark.asyncio
async def test_atomic_copy_respects_project_native_mask_opt_in(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    project = await db_session.get(Project, task.project_id)
    assert project is not None
    project.raster_mask_native_editing_enabled = False
    source = await _seed_annotation(db_session, task, user.id)
    payload = _payload(source, _scope(), reference=source.geometry["mask"])

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "raster_mask_create_disabled"


@pytest.mark.asyncio
async def test_strict_overlap_failure_rolls_back_created_annotation(
    db_session, super_admin, mask_content
):
    user, _ = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id)
    scope = _scope(strict=True)
    request = _payload(source, scope, reference=source.geometry["mask"])
    request["report"]["connectivity"] = 8

    with pytest.raises(MaskMutationError) as exc_info:
        async with db_session.begin_nested():
            from app.schemas.mask_mutation import MaskMutationCommitRequest

            await MaskMutationService(db_session).commit(
                task.id,
                MaskMutationCommitRequest.model_validate(request),
                user,
            )

    assert exc_info.value.detail["reason"] == "overlap_conflict"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(
                Annotation.task_id == task.id,
                Annotation.is_active.is_(True),
            )
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationOperation)
            .where(AnnotationOperation.task_id == task.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_join_masks_updates_survivor_deletes_sources_and_records_lineage(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    first = await _seed_annotation(db_session, task, user.id, rle=RLE_A)
    second = await _seed_annotation(db_session, task, user.id, rle=RLE_B)
    scope = _scope()
    members = sorted([first, second], key=lambda item: str(item.id))
    reference = build_rle_reference(RLE_JOINED)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=reference["object_key"])
    )
    await db_session.flush()
    payload = {
        "idempotency_key": "mask-join-key-0001",
        "operation": "join_masks",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, members),
        "expected_versions": [
            {"annotation_id": str(item.id), "version": 1} for item in members
        ],
        "mutations": [
            {
                "kind": "update",
                "annotation_id": str(first.id),
                "geometry": {"type": "raster_mask", "mask": reference},
            },
            {"kind": "delete", "annotation_id": str(second.id)},
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated_annotations"] == [{"id": str(first.id), "version": 2}]
    assert body["deleted_annotation_ids"] == [str(second.id)]
    assert len(body["lineage_edges"]) == 2
    assert {edge["source_annotation_id"] for edge in body["lineage_edges"]} == {
        str(first.id),
        str(second.id),
    }
    await db_session.refresh(second)
    assert second.is_active is False
    assert second.version == 2


@pytest.mark.asyncio
async def test_join_cannot_delete_source_with_active_child(
    db_session, httpx_client, super_admin, mask_content
):
    actor, token = super_admin
    task = await _seed_image_task(db_session, actor.id)
    first = await _seed_annotation(db_session, task, actor.id, rle=RLE_A)
    second = await _seed_annotation(db_session, task, actor.id, rle=RLE_B)
    child = await _seed_annotation(db_session, task, actor.id, rle=RLE_COMPONENT)
    child.parent_annotation_id = second.id
    reference = build_rle_reference(RLE_JOINED)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=reference["object_key"])
    )
    await db_session.flush()
    scope = _scope()
    members = sorted([first, second, child], key=lambda item: str(item.id))
    payload = {
        "idempotency_key": "mask-join-parent-key-0001",
        "operation": "join_masks",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, members),
        "expected_versions": [
            {"annotation_id": str(item.id), "version": 1} for item in members
        ],
        "mutations": [
            {
                "kind": "update",
                "annotation_id": str(first.id),
                "geometry": {"type": "raster_mask", "mask": reference},
            },
            {"kind": "delete", "annotation_id": str(second.id)},
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_operation"
    assert response.json()["detail"]["child_annotation_id"] == str(child.id)


@pytest.mark.asyncio
async def test_overlap_erase_all_can_delete_cross_class_mask(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    primary = await _seed_annotation(db_session, task, user.id, rle=RLE_A)
    erased = await _seed_annotation(
        db_session,
        task,
        user.id,
        class_name="other",
        rle=RLE_COMPONENT,
    )
    scope = MaskMutationScope(
        media="image",
        instance_filter="all",
        class_name=None,
        overlap_policy="erase_all",
        strict_non_overlap=True,
    )
    members = sorted([primary, erased], key=lambda item: str(item.id))
    payload = {
        "idempotency_key": "mask-overlap-key-0001",
        "operation": "overlap",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, members),
        "expected_versions": [
            {"annotation_id": str(item.id), "version": 1} for item in members
        ],
        "mutations": [
            {
                "kind": "update",
                "annotation_id": str(primary.id),
                "geometry": primary.geometry,
            },
            {"kind": "delete", "annotation_id": str(erased.id)},
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    deleted_edge = next(
        edge
        for edge in response.json()["lineage_edges"]
        if edge["source_annotation_id"] == str(erased.id)
    )
    assert deleted_edge["result_annotation_id"] is None
    assert deleted_edge["relation"] == "overlap_erased"


@pytest.mark.asyncio
async def test_copy_rejects_pixels_outside_source(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id, rle=RLE_A)
    outside_reference = build_rle_reference(RLE_B)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=outside_reference["object_key"])
    )
    await db_session.flush()
    payload = _payload(source, _scope(), reference=outside_reference)

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_operation"
    assert (
        await db_session.scalar(select(func.count()).select_from(AnnotationOperation))
        == 0
    )


@pytest.mark.asyncio
async def test_copy_rejects_incomplete_connected_component(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id, rle=RLE_A)
    partial_reference = build_rle_reference(RLE_COMPONENT)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=partial_reference["object_key"])
    )
    await db_session.flush()
    payload = _payload(source, _scope(), reference=partial_reference)
    # The two source pixels are one diagonal component under 8-connectivity;
    # returning only one of them is a subset but not a complete component.
    payload["report"]["connectivity"] = 8

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_operation"


@pytest.mark.asyncio
async def test_split_rejects_duplicate_or_incomplete_results(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id, rle=RLE_A)
    component_reference = build_rle_reference(RLE_COMPONENT)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=component_reference["object_key"])
    )
    await db_session.flush()
    scope = _scope()
    payload = {
        "idempotency_key": "mask-split-invalid-key-0001",
        "operation": "split_components",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, [source]),
        "expected_versions": [{"annotation_id": str(source.id), "version": 1}],
        "mutations": [
            {
                "kind": "update",
                "annotation_id": str(source.id),
                "geometry": {"type": "raster_mask", "mask": component_reference},
            },
            {
                "kind": "create",
                "source_annotation_ids": [str(source.id)],
                "geometry": {"type": "raster_mask", "mask": component_reference},
            },
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_operation"
    assert (
        await db_session.scalar(select(func.count()).select_from(AnnotationOperation))
        == 0
    )


@pytest.mark.asyncio
async def test_join_rejects_result_that_is_not_source_union(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    first = await _seed_annotation(db_session, task, user.id, rle=RLE_A)
    second = await _seed_annotation(db_session, task, user.id, rle=RLE_B)
    scope = _scope()
    members = sorted([first, second], key=lambda item: str(item.id))
    payload = {
        "idempotency_key": "mask-join-invalid-key-0001",
        "operation": "join_masks",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, members),
        "expected_versions": [
            {"annotation_id": str(item.id), "version": 1} for item in members
        ],
        "mutations": [
            {
                "kind": "update",
                "annotation_id": str(first.id),
                "geometry": first.geometry,
            },
            {"kind": "delete", "annotation_id": str(second.id)},
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_operation"
    assert (
        await db_session.scalar(select(func.count()).select_from(AnnotationOperation))
        == 0
    )


@pytest.mark.asyncio
async def test_overlap_rejects_deleting_disjoint_annotation(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    primary = await _seed_annotation(db_session, task, user.id, rle=RLE_A)
    disjoint = await _seed_annotation(db_session, task, user.id, rle=RLE_B)
    scope = MaskMutationScope(
        media="image",
        instance_filter="same_class",
        class_name="object",
        overlap_policy="erase_same_class",
    )
    members = sorted([primary, disjoint], key=lambda item: str(item.id))
    payload = {
        "idempotency_key": "mask-overlap-invalid-key-0001",
        "operation": "overlap",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, members),
        "expected_versions": [
            {"annotation_id": str(item.id), "version": 1} for item in members
        ],
        "mutations": [
            {
                "kind": "update",
                "annotation_id": str(primary.id),
                "geometry": primary.geometry,
            },
            {"kind": "delete", "annotation_id": str(disjoint.id)},
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_operation"
    assert (
        await db_session.scalar(select(func.count()).select_from(AnnotationOperation))
        == 0
    )


@pytest.mark.asyncio
async def test_video_copy_only_creates_current_frame_with_server_track_id(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task, segment = await _seed_video_task(db_session, user.id)
    source = await _seed_video_annotation(db_session, task, user.id)
    component_reference = build_rle_reference(RLE_B)
    scope = MaskMutationScope(
        media="video",
        frame_index=3,
        segment_id=segment.id,
        instance_filter="same_class",
        class_name="object",
        overlap_policy="allow",
    )
    payload = {
        "idempotency_key": "mask-video-copy-key-0001",
        "operation": "copy_component",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, [source]),
        "expected_versions": [{"annotation_id": str(source.id), "version": 1}],
        "mutations": [
            {
                "kind": "create",
                "source_annotation_ids": [str(source.id)],
                "geometry": {
                    "type": "video_track_mask",
                    "track_id": "trk_client_supplied",
                    "semantic_label": "object",
                    "keyframes": [
                        {
                            "frame_index": 3,
                            "mask": component_reference,
                            "source": "prediction",
                            "occluded": False,
                        }
                    ],
                    "outside": [],
                },
            }
        ],
        "report": {"connectivity": 4},
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    created_id = uuid.UUID(response.json()["created_annotations"][0]["id"])
    created = await db_session.get(Annotation, created_id)
    assert created is not None
    assert created.geometry["track_id"] != "trk_client_supplied"
    assert created.geometry["track_id"] == created.track_id
    assert created.geometry["outside"] == []
    assert len(created.geometry["keyframes"]) == 1
    keyframe = created.geometry["keyframes"][0]
    assert keyframe["frame_index"] == 3
    assert keyframe["mask"]["sha256"] == component_reference["sha256"]
    assert keyframe["source"] == "manual"


@pytest.mark.asyncio
async def test_video_copy_keyframe_can_copy_external_held_source_atomically(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task, segment = await _seed_video_task(db_session, user.id)
    source = await _seed_video_annotation(db_session, task, user.id)
    source.geometry = {
        **source.geometry,
        "outside": [{"from": 3, "to": 3, "source": "manual"}],
    }
    await db_session.flush()
    await db_session.refresh(source)
    source_version = int(source.version or 1)
    scope = MaskMutationScope(
        media="video",
        frame_index=3,
        segment_id=segment.id,
        instance_filter="same_class",
        class_name="object",
        overlap_policy="allow",
    )
    source_reference = source.geometry["keyframes"][1]["mask"]
    payload = {
        "idempotency_key": "mask-video-copy-keyframe-0001",
        "operation": "copy_keyframe",
        "scope": scope.model_dump(mode="json"),
        "source_frame_index": 5,
        "scope_fingerprint": scope_fingerprint(scope, [source]),
        "expected_versions": [
            {"annotation_id": str(source.id), "version": source_version}
        ],
        "mutations": [
            {
                "kind": "create",
                "source_annotation_ids": [str(source.id)],
                "geometry": {
                    "type": "video_track_mask",
                    "track_id": "trk_client_copy",
                    "semantic_label": "object",
                    "keyframes": [
                        {
                            "frame_index": 3,
                            "mask": source_reference,
                            "source": "manual",
                            "occluded": False,
                        }
                    ],
                    "outside": [],
                },
            }
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated_annotations"] == []
    assert body["deleted_annotation_ids"] == []
    assert body["lineage_edges"] == [
        {
            "source_annotation_id": str(source.id),
            "result_annotation_id": body["created_annotations"][0]["id"],
            "relation": "keyframe_copied",
            "source_version": source_version,
            "result_version": 1,
            "frame_index": 3,
        }
    ]
    created = await db_session.get(
        Annotation, uuid.UUID(body["created_annotations"][0]["id"])
    )
    assert created is not None
    assert created.geometry["outside"] == []
    assert len(created.geometry["keyframes"]) == 1
    created_keyframe = created.geometry["keyframes"][0]
    assert created_keyframe["frame_index"] == 3
    assert created_keyframe["mask"]["sha256"] == source_reference["sha256"]
    assert created_keyframe["source"] == "manual"
    assert created_keyframe["occluded"] is False
    await db_session.refresh(source)
    assert source.version == source_version
    assert [item["frame_index"] for item in source.geometry["keyframes"]] == [0, 5]
    operation = await db_session.get(
        AnnotationOperation, uuid.UUID(body["operation_id"])
    )
    assert operation is not None
    assert operation.kind == "copy_keyframe"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["version", "content"])
async def test_video_copy_keyframe_rejects_stale_or_changed_source(
    failure, db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task, segment = await _seed_video_task(db_session, user.id)
    source = await _seed_video_annotation(db_session, task, user.id)
    scope = MaskMutationScope(
        media="video",
        frame_index=3,
        segment_id=segment.id,
        instance_filter="same_class",
        class_name="object",
        overlap_policy="allow",
    )
    result_reference = (
        source.geometry["keyframes"][0]["mask"]
        if failure == "content"
        else source.geometry["keyframes"][1]["mask"]
    )
    payload = {
        "idempotency_key": f"mask-video-copy-keyframe-{failure}-0001",
        "operation": "copy_keyframe",
        "scope": scope.model_dump(mode="json"),
        "source_frame_index": 5,
        "scope_fingerprint": scope_fingerprint(scope, [source]),
        "expected_versions": [
            {
                "annotation_id": str(source.id),
                "version": 99 if failure == "version" else 1,
            }
        ],
        "mutations": [
            {
                "kind": "create",
                "source_annotation_ids": [str(source.id)],
                "geometry": {
                    "type": "video_track_mask",
                    "track_id": "trk_client_copy",
                    "keyframes": [
                        {
                            "frame_index": 3,
                            "mask": result_reference,
                            "source": "manual",
                            "occluded": False,
                        }
                    ],
                    "outside": [],
                },
            }
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code in {409, 422}
    assert response.json()["detail"]["reason"] == (
        "version_mismatch" if failure == "version" else "invalid_operation"
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationOperation)
            .where(AnnotationOperation.task_id == task.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_video_join_creates_current_frame_copy_and_preserves_source_tracks(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task, segment = await _seed_video_task(db_session, user.id)
    first = await _seed_video_annotation(
        db_session, task, user.id, track_id="trk_first"
    )
    second = await _seed_video_annotation(
        db_session,
        task,
        user.id,
        track_id="trk_second",
        first_rle=RLE_B,
        second_rle=RLE_A,
    )
    before_first = deepcopy(first.geometry)
    before_second = deepcopy(second.geometry)
    scope = MaskMutationScope(
        media="video",
        frame_index=3,
        segment_id=segment.id,
        instance_filter="same_class",
        class_name="object",
        overlap_policy="allow",
    )
    members = sorted([first, second], key=lambda item: str(item.id))
    joined_reference = build_rle_reference(RLE_JOINED)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=joined_reference["object_key"])
    )
    await db_session.flush()
    payload = {
        "idempotency_key": "mask-video-join-key-0001",
        "operation": "join_masks",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, members),
        "expected_versions": [
            {"annotation_id": str(item.id), "version": 1} for item in members
        ],
        "mutations": [
            {
                "kind": "create",
                "source_annotation_ids": [str(first.id), str(second.id)],
                "geometry": {
                    "type": "video_track_mask",
                    "track_id": "trk_client_join",
                    "keyframes": [
                        {
                            "frame_index": 3,
                            "mask": joined_reference,
                            "source": "manual",
                        }
                    ],
                    "outside": [],
                },
            }
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated_annotations"] == []
    assert body["deleted_annotation_ids"] == []
    assert len(body["created_annotations"]) == 1
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.is_active is True and first.geometry == before_first
    assert second.is_active is True and second.geometry == before_second
    created = await db_session.get(
        Annotation, uuid.UUID(body["created_annotations"][0]["id"])
    )
    assert created is not None
    assert created.geometry["track_id"] != "trk_client_join"
    assert created.geometry["outside"] == []
    assert [item["frame_index"] for item in created.geometry["keyframes"]] == [3]
    assert created.geometry["keyframes"][0]["source"] == "manual"
    assert {edge["result_annotation_id"] for edge in body["lineage_edges"]} == {
        str(created.id)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_change",
    ["missing_current_keyframe", "semantic_label", "prediction_source"],
)
async def test_video_update_rejects_changes_outside_current_mask_pixels(
    invalid_change, db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task, segment = await _seed_video_task(db_session, user.id)
    source = await _seed_video_annotation(db_session, task, user.id)
    geometry = deepcopy(source.geometry)
    if invalid_change != "missing_current_keyframe":
        geometry["keyframes"] = sorted(
            [
                *geometry["keyframes"],
                {
                    "frame_index": 3,
                    "mask": build_rle_reference(RLE_B),
                    "source": (
                        "prediction"
                        if invalid_change == "prediction_source"
                        else "manual"
                    ),
                    "occluded": False,
                },
            ],
            key=lambda item: item["frame_index"],
        )
    if invalid_change == "semantic_label":
        geometry["semantic_label"] = "changed"
    scope = MaskMutationScope(
        media="video",
        frame_index=3,
        segment_id=segment.id,
        instance_filter="same_class",
        class_name="object",
        overlap_policy="erase_same_class",
    )
    payload = {
        "idempotency_key": f"mask-video-update-{invalid_change}-0001",
        "operation": "overlap",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, [source]),
        "expected_versions": [{"annotation_id": str(source.id), "version": 1}],
        "mutations": [
            {
                "kind": "update",
                "annotation_id": str(source.id),
                "geometry": geometry,
            }
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_geometry"


@pytest.mark.asyncio
async def test_video_join_rejects_destructive_track_delete_shape(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task, segment = await _seed_video_task(db_session, user.id)
    first = await _seed_video_annotation(
        db_session, task, user.id, track_id="trk_first"
    )
    second = await _seed_video_annotation(
        db_session,
        task,
        user.id,
        track_id="trk_second",
        first_rle=RLE_B,
        second_rle=RLE_A,
    )
    scope = MaskMutationScope(
        media="video",
        frame_index=3,
        segment_id=segment.id,
        instance_filter="same_class",
        class_name="object",
        overlap_policy="allow",
    )
    members = sorted([first, second], key=lambda item: str(item.id))
    payload = {
        "idempotency_key": "mask-video-join-delete-key-0001",
        "operation": "join_masks",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, members),
        "expected_versions": [
            {"annotation_id": str(item.id), "version": 1} for item in members
        ],
        "mutations": [
            {
                "kind": "update",
                "annotation_id": str(first.id),
                "geometry": first.geometry,
            },
            {"kind": "delete", "annotation_id": str(second.id)},
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_operation"


@pytest.mark.asyncio
async def test_video_copy_rejects_empty_current_frame(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task, segment = await _seed_video_task(db_session, user.id)
    source = await _seed_video_annotation(db_session, task, user.id)
    reference = build_rle_reference(RLE_EMPTY)
    db_session.add(
        RasterMaskUpload(task_id=task.id, object_key=reference["object_key"])
    )
    await db_session.flush()
    scope = MaskMutationScope(
        media="video",
        frame_index=3,
        segment_id=segment.id,
        instance_filter="same_class",
        class_name="object",
        overlap_policy="allow",
    )
    payload = {
        "idempotency_key": "mask-video-empty-key-0001",
        "operation": "copy_component",
        "scope": scope.model_dump(mode="json"),
        "scope_fingerprint": scope_fingerprint(scope, [source]),
        "expected_versions": [{"annotation_id": str(source.id), "version": 1}],
        "mutations": [
            {
                "kind": "create",
                "source_annotation_ids": [str(source.id)],
                "geometry": {
                    "type": "video_track_mask",
                    "track_id": "trk_empty",
                    "keyframes": [
                        {
                            "frame_index": 3,
                            "mask": reference,
                            "source": "manual",
                        }
                    ],
                    "outside": [],
                },
            }
        ],
    }

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "empty_result"


@pytest.mark.asyncio
async def test_report_rejects_arbitrary_or_inline_mask_payload(
    db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    source = await _seed_annotation(db_session, task, user.id)
    payload = _payload(source, _scope(), reference=source.geometry["mask"])
    payload["report"]["counts"] = [1, 2, 3]

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationOperation)
            .where(AnnotationOperation.task_id == task.id)
        )
        == 0
    )


def test_schema_rejects_policy_mismatch_and_duplicate_targets():
    annotation_id = uuid.uuid4()
    reference = build_rle_reference(RLE_A)
    base = {
        "idempotency_key": "mask-schema-key-0001",
        "operation": "overlap",
        "scope": {
            "media": "image",
            "frame_index": None,
            "segment_id": None,
            "instance_filter": "all",
            "class_name": None,
            "overlap_policy": "erase_same_class",
            "strict_non_overlap": True,
        },
        "scope_fingerprint": "0" * 64,
        "expected_versions": [{"annotation_id": str(annotation_id), "version": 1}],
        "mutations": [
            {
                "kind": "update",
                "annotation_id": str(annotation_id),
                "geometry": {"type": "raster_mask", "mask": reference},
            },
            {"kind": "delete", "annotation_id": str(annotation_id)},
        ],
    }

    with pytest.raises(ValueError, match="erase_same_class requires same_class"):
        MaskMutationCommitRequest.model_validate(base)

    base["scope"] = {
        **base["scope"],
        "instance_filter": "same_class",
        "class_name": "object",
    }
    with pytest.raises(ValueError, match="only be updated or deleted once"):
        MaskMutationCommitRequest.model_validate(base)

    base["mutations"] = [base["mutations"][0]]
    parsed = MaskMutationCommitRequest.model_validate(base)
    round_tripped = MaskMutationCommitRequest.model_validate(
        parsed.model_dump(mode="json", exclude_none=True)
    )
    assert round_tripped.cut_path is None
    with pytest.raises(ValueError, match="cut_path is only valid for slice_mask"):
        MaskMutationCommitRequest.model_validate({**base, "cut_path": None})
    with pytest.raises(ValueError, match="cut_path is only valid for slice_mask"):
        MaskMutationCommitRequest.model_validate(
            {**base, "cut_path": [[0.1, 0.1], [0.9, 0.9]]}
        )


def test_derived_rle_run_budget_returns_stable_operation_error(monkeypatch):
    monkeypatch.setattr("app.services.mask_mutation.MAX_MASK_RUNS", 3)
    alternating = {
        "encoding": "coco_rle",
        "size": [1, 6],
        "counts": [0, 1, 1, 1, 1, 1, 1],
    }
    empty = {"encoding": "coco_rle", "size": [1, 6], "counts": [6]}

    with pytest.raises(MaskMutationError) as captured:
        _combine_rles(alternating, empty, "or")

    assert captured.value.status_code == 422
    assert captured.value.detail["reason"] == "operation_too_large"


def test_cumulative_algebra_budget_returns_stable_operation_error(monkeypatch):
    monkeypatch.setattr("app.services.mask_mutation.MAX_MASK_MUTATION_ALGEBRA_STEPS", 1)
    budget = _AlgebraBudget()

    with pytest.raises(MaskMutationError) as captured:
        _combine_rles(RLE_A, RLE_B, "or", budget)

    assert captured.value.status_code == 422
    assert captured.value.detail["reason"] == "operation_too_large"


@pytest.mark.asyncio
async def test_large_image_mutation_rejects_before_pixel_algebra(
    monkeypatch, db_session, httpx_client, super_admin
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    large_rle = {
        "encoding": "coco_rle",
        "size": [8192, 8192],
        "counts": [0, 1, 67_108_863],
    }
    source = await _seed_annotation(db_session, task, user.id, rle=large_rle)
    item = await db_session.get(DatasetItem, task.dataset_item_id)
    assert item is not None
    item.width = 8192
    item.height = 8192
    reference = source.geometry["mask"]
    load = AsyncMock(return_value=large_rle)
    monkeypatch.setattr("app.services.mask_mutation.load_coco_rle", load)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=_payload(source, _scope(), reference=reference),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "large_mask_full_scan_required"
    load.assert_awaited()


@pytest.mark.asyncio
async def test_scope_candidate_limit_rejects_before_loading_unbounded_task(
    monkeypatch, db_session, httpx_client, super_admin, mask_content
):
    user, token = super_admin
    task = await _seed_image_task(db_session, user.id)
    first = await _seed_annotation(db_session, task, user.id, rle=RLE_A)
    await _seed_annotation(db_session, task, user.id, rle=RLE_B)
    monkeypatch.setattr("app.services.mask_mutation.MAX_MASK_MUTATION_SCOPE_MEMBERS", 1)
    payload = _payload(first, _scope(), reference=first.geometry["mask"])

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations/mask-mutations:commit",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "operation_too_large"


@pytest.mark.asyncio
async def test_ordinary_mask_prepare_waits_for_task_before_rle_advisory(
    monkeypatch, test_engine
):
    """Two real sessions prove ordinary writes cannot hold RLE while waiting Task."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    user_id = uuid.uuid4()
    task_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    dataset_id: uuid.UUID | None = None
    reference = build_rle_reference(RLE_COMPONENT)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    monkeypatch.setattr(
        "app.services.raster_mask_storage.load_coco_rle",
        AsyncMock(return_value=RLE_COMPONENT),
    )

    try:
        async with maker() as seed:
            seed.add(
                User(
                    id=user_id,
                    email=f"mask-lock-{user_id}@example.test",
                    name="mask lock order",
                    password_hash="unused",
                    role="super_admin",
                )
            )
            await seed.flush()
            task = await _seed_image_task(seed, user_id)
            task_id = task.id
            project_id = task.project_id
            item = await seed.get(DatasetItem, task.dataset_item_id)
            assert item is not None
            dataset_id = item.dataset_id
            seed.add(
                RasterMaskUpload(
                    task_id=task.id,
                    object_key=reference["object_key"],
                )
            )
            await seed.commit()

        async with maker() as atomic_session, maker() as ordinary_session:
            await atomic_session.execute(
                select(Task).where(Task.id == task_id).with_for_update()
            )
            ordinary_task = await ordinary_session.get(Task, task_id)
            assert ordinary_task is not None

            writer = asyncio.create_task(
                prepare_mask_payload_for_write(
                    ordinary_session,
                    ordinary_task,
                    {"type": "raster_mask", "mask": reference},
                    required_upload_keys={reference["object_key"]},
                )
            )
            await asyncio.sleep(0.05)
            acquired = await atomic_session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"aap:raster-mask:{reference['object_key']}"},
            )
            assert acquired is True
            await atomic_session.rollback()
            await asyncio.wait_for(writer, timeout=2)
            await ordinary_session.rollback()
    finally:
        if task_id and project_id and dataset_id:
            async with maker() as cleanup:
                await cleanup.execute(delete(Task).where(Task.id == task_id))
                await cleanup.execute(delete(Project).where(Project.id == project_id))
                await cleanup.execute(delete(Dataset).where(Dataset.id == dataset_id))
                await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.commit()
