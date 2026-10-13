from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aap_protocol_v2 import NativeMaskCandidate, NativeMaskCandidateValue
from sqlalchemy import delete, func, select

from app.config import settings
from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.ml_backend_pool import MLBackendPoolMember
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.services.ai_mask_receipt import issue_ai_mask_receipt
from app.services.raster_mask_storage import build_rle_reference
from app.services.video_tracks import resolve_track_at_frame

from tests.conftest import create_registry_with_pool
from tests.factory import create_membership

RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 3]}
ALT_RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [2, 1, 3]}


async def _seed(
    db,
    *,
    owner_id: uuid.UUID,
    media_type: str = "image",
) -> tuple[Task, object, object]:
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(
        display_id=f"DS-AIM-{suffix}",
        name=f"ai-mask-{suffix}",
        data_type=media_type,
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=f"sample.{'png' if media_type == 'image' else 'mp4'}",
        file_path=f"{media_type}/{suffix}",
        file_type=media_type,
        width=3,
        height=2,
        metadata_=(
            {"video": {"width": 3, "height": 2, "frame_count": 12}}
            if media_type == "video"
            else {}
        ),
    )
    db.add(item)
    await db.flush()
    project = Project(
        display_id=f"P-AIM-{suffix}",
        name=f"ai-mask-{suffix}",
        type_label="视频分割" if media_type == "video" else "图像分割",
        type_key="video-seg" if media_type == "video" else "image-seg",
        data_type=media_type,
        owner_id=owner_id,
        ai_interactive_enabled=True,
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
    backend, pool = await create_registry_with_pool(
        db,
        name=f"ai-mask-{suffix}",
        url=f"http://ai-mask-{suffix}.test:9999",
    )
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-AIM-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type=media_type,
        status="pending",
    )
    db.add(task)
    await db.flush()
    return task, backend, pool


def _body(
    task: Task,
    backend,
    pool,
    *,
    rle: dict = RLE,
    key: str | None = None,
    class_name: str = "object",
    frame_index: int | None = None,
    source: Annotation | None = None,
    candidate_index: int = 0,
) -> dict:
    from aap_protocol_v2 import CocoRlePayload, native_mask_candidate_id

    prompt_revision = "rev:test:1"
    rle_model = CocoRlePayload.model_validate(rle)
    candidate_id = native_mask_candidate_id(
        rle_model, prompt_revision=prompt_revision, candidate_index=candidate_index
    )
    candidate = NativeMaskCandidate(
        value=NativeMaskCandidateValue(rle=rle_model, masklabels=["object"]),
        score=0.91,
        candidate_id=candidate_id,
    )
    routing = {
        "requested_backend_id": str(backend.id),
        "backend_pool_id": str(pool.id),
        "backend_instance_id": str(backend.id),
        "model_id": "sam-image",
    }
    inference = {
        "model_version": "sam-test-1",
        "inference_time_ms": 12.0,
        "cache_hit": False,
        "model_load_ms": 4.0,
    }
    content_digest = build_rle_reference(rle)["sha256"]
    target = {
        "mode": "refine" if source else "create",
        "source_annotation_id": None,
        "source_version": None,
        "frame_index": frame_index,
    }
    prompt_source = None
    if source:
        target.update(
            {
                "source_annotation_id": str(source.id),
                "source_version": source.version,
            }
        )
        if (source.geometry or {}).get("type") == "raster_mask":
            source_mask = source.geometry["mask"]
        else:
            resolved = resolve_track_at_frame(source.geometry, frame_index)
            source_mask = resolved["mask"] if resolved is not None else None
        if source_mask is not None:
            prompt_source = {
                "source_annotation_id": str(source.id),
                "source_version": source.version,
                "source_digest": source_mask["sha256"],
            }
    receipt = issue_ai_mask_receipt(
        {
            "task_id": str(task.id),
            "frame_index": frame_index,
            "candidate_id": candidate_id,
            "candidate_index": candidate_index,
            "content_digest": content_digest,
            "prompt_revision": prompt_revision,
            "score": 0.91,
            "routing": routing,
            "inference": inference,
            "prompt_summary": {
                "family": "point",
                "positive_points": 1,
                "negative_points": 0,
                "boxes": 0,
                "positive_scribbles": 0,
                "negative_scribbles": 0,
                "multimask": True,
                "parameters_digest": None,
            },
            "prompt_source": prompt_source,
            "accept_target": target,
        }
    )
    return {
        "idempotency_key": key or f"accept-{uuid.uuid4()}",
        "candidate": {
            "candidate": candidate.model_dump(mode="json"),
            "candidate_index": candidate_index,
            "prompt_revision": prompt_revision,
            "receipt": receipt,
        },
        "class_name": class_name,
        "target": target,
        "prompt_summary": {
            "family": "point",
            "positive_points": 1,
            "multimask": True,
        },
        "routing": routing,
        "inference": inference,
    }


@pytest.fixture
def accept_storage_mocks(monkeypatch):
    store = AsyncMock()
    prepare_prediction = AsyncMock()
    prepare_annotation = AsyncMock()
    monkeypatch.setattr(
        "app.services.ai_mask_accept.store_mask_reference_objects", store
    )
    monkeypatch.setattr(
        "app.services.prediction.prepare_mask_payload_for_write", prepare_prediction
    )
    monkeypatch.setattr(
        "app.services.annotation.prepare_mask_geometry_for_annotation_write",
        prepare_annotation,
    )
    return store, prepare_prediction, prepare_annotation


def _headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


@pytest.mark.parametrize("media_type", ["image", "video"])
async def test_exemplar_both_accept_uses_original_mask_index(
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
    accept_storage_mocks,
    media_type,
):
    from app.api.v1.ml_backends import _interactive_response
    from app.services.ml_client import PredictionResult

    user, token = super_admin
    task, backend, pool = await _seed(
        db_session, owner_id=user.id, media_type=media_type
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    frame_index = 7 if media_type == "video" else None
    bodies = [
        _body(task, backend, pool, frame_index=frame_index, candidate_index=index)
        for index in (1, 3)
    ]
    raw = []
    for body in bodies:
        raw.extend(
            [
                {
                    "type": "rectanglelabels",
                    "value": {
                        "x": 0.0,
                        "y": 0.0,
                        "width": 1.0,
                        "height": 1.0,
                        "rectanglelabels": ["object"],
                    },
                    "score": 0.91,
                },
                body["candidate"]["candidate"],
            ]
        )
    response = _interactive_response(
        PredictionResult(task_id=str(task.id), result=raw, **bodies[1]["inference"]),
        context={
            "type": "exemplar",
            "output": "both",
            "output_geometry": "mask",
            "prompt_revision": "rev:test:1",
        },
        expected_size=(3, 2),
        client=SimpleNamespace(pool_id=pool.id, last_instance_id=backend.id),
        requested_backend_id=backend.id,
        model_id="sam-image",
        task_id=task.id,
        frame_index=frame_index,
    )
    body = bodies[1]
    body["candidate"]["receipt"] = response["accept_receipts"][raw[3]["candidate_id"]]
    body["prompt_summary"] = response["prompt_summary"]
    accepted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["content_digest"] == build_rle_reference(RLE)["sha256"]
    geometry = accepted.json()["annotation"]["geometry"]
    if media_type == "video":
        assert geometry["type"] == "video_track_mask"
        assert geometry["keyframes"][0]["frame_index"] == 7
    else:
        assert geometry["type"] == "raster_mask"


async def test_image_accept_is_atomic_and_replays_exact_result(
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    body = _body(task, backend, pool, key="accept-image-exactly-once")

    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["replayed"] is False
    assert payload["annotation"]["geometry"]["type"] == "raster_mask"
    assert (
        payload["annotation"]["geometry"]["mask"]["sha256"] == payload["content_digest"]
    )
    assert payload["annotation"]["parent_prediction_id"] == payload["prediction"]["id"]
    assert payload["prediction"]["source"] == "interactive_accept"
    assert first.headers["etag"] == 'W/"1"'

    pending = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions",
        headers=_headers(token),
    )
    assert pending.status_code == 200, pending.text
    assert pending.json() == []

    legacy_accept = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{payload['prediction']['id']}/accept",
        headers=_headers(token),
    )
    assert legacy_accept.status_code == 404, legacy_accept.text
    annotation_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.task_id == task.id)
        )
    ).scalar_one()
    assert annotation_count == 1

    replay = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )
    assert replay.status_code == 200, replay.text
    replay_payload = replay.json()
    assert replay_payload["replayed"] is True
    assert replay_payload["annotation"]["id"] == payload["annotation"]["id"]
    assert replay_payload["prediction"]["id"] == payload["prediction"]["id"]

    deleted = await httpx_client.delete(
        f"/api/v1/tasks/{task.id}/annotations/{payload['annotation']['id']}",
        headers=_headers(token),
    )
    assert deleted.status_code == 204, deleted.text
    pending_after_delete = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions",
        headers=_headers(token),
    )
    assert pending_after_delete.status_code == 200, pending_after_delete.text
    assert pending_after_delete.json() == []

    for model in (AiMaskAcceptDecision, Prediction, PredictionMeta, Annotation):
        count = (
            await db_session.execute(select(func.count()).select_from(model))
        ).scalar_one()
        assert count == 1
    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "mask_ai.candidate_accept")
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert "counts" not in str(audits[0].detail_json)

    # A live decision protects its replay-only content reference from GC; once
    # expired, the same snapshot no longer participates in the reference scan.
    from app.workers import cleanup

    decision = (await db_session.execute(select(AiMaskAcceptDecision))).scalar_one()
    replay_only_key = f"raster-masks/sha256/ff/ff/{'f' * 64}.json"
    decision.response_json = {"replay_only": {"object_key": replay_only_key}}
    await db_session.flush()
    assert replay_only_key in await cleanup._referenced_raster_mask_keys(db_session)
    decision.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.flush()
    assert replay_only_key not in await cleanup._referenced_raster_mask_keys(db_session)

    expired = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )
    assert expired.status_code == 409
    assert expired.json()["detail"]["reason"] == "idempotency_expired"


async def test_same_idempotency_key_with_changed_semantics_conflicts(
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    key = "accept-image-semantic-conflict"
    first_body = _body(task, backend, pool, key=key)
    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=first_body,
        headers=_headers(token),
    )
    assert first.status_code == 200, first.text
    changed = {**first_body, "class_name": "other"}
    second = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=changed,
        headers=_headers(token),
    )
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "idempotency_conflict"


async def test_image_refine_requires_and_checks_if_match(
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="object",
        geometry={"type": "raster_mask", "mask": build_rle_reference(RLE)},
        version=1,
    )
    db_session.add(source)
    await db_session.flush()
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    body = _body(task, backend, pool, rle=ALT_RLE, source=source)

    missing = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )
    assert missing.status_code == 428
    assert missing.json()["detail"]["reason"] == "if_match_required"

    stale = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token, **{"If-Match": 'W/"2"'}),
    )
    assert stale.status_code == 400
    assert stale.json()["detail"]["reason"] == "source_version_mismatch"

    accepted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token, **{"If-Match": 'W/"1"'}),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["annotation"]["id"] == str(source.id)
    assert accepted.json()["source_version"] == 1
    assert accepted.json()["result_version"] == 2
    await db_session.refresh(source)
    assert source.source == "manual"


async def test_mask_prompt_receipt_cannot_be_accepted_as_create_or_after_digest_drift(
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id)
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="object",
        geometry={"type": "raster_mask", "mask": build_rle_reference(RLE)},
        version=1,
    )
    db_session.add(source)
    await db_session.flush()
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    body = _body(task, backend, pool, rle=ALT_RLE, source=source)

    as_create = deepcopy(body)
    as_create["target"] = {"mode": "create", "frame_index": None}
    rejected = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=as_create,
        headers=_headers(token),
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["reason"] == "candidate_receipt_mismatch"

    source.geometry = {
        "type": "raster_mask",
        "mask": build_rle_reference(ALT_RLE),
    }
    await db_session.flush()
    drifted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token, **{"If-Match": 'W/"1"'}),
    )
    assert drifted.status_code == 409
    assert drifted.json()["detail"]["reason"] == "mask_prompt_source_changed"
    accept_storage_mocks[0].assert_not_awaited()


async def test_video_accept_updates_only_current_keyframe_and_outside(
    httpx_client,
    db_session,
    super_admin,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id, media_type="video")
    source = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=user.id,
        source="manual",
        annotation_type="video_track_mask",
        tool_unit_id="region",
        class_name="object",
        track_id="trk_keep",
        geometry={
            "type": "video_track_mask",
            "track_id": "trk_keep",
            "keyframes": [
                {
                    "frame_index": 1,
                    "mask": build_rle_reference(RLE),
                    "source": "manual",
                    "occluded": False,
                },
                {
                    "frame_index": 6,
                    "mask": build_rle_reference(RLE),
                    "source": "manual",
                    "occluded": True,
                },
            ],
            "outside": [
                {"from": 3, "to": 5, "source": "manual"},
                {"from": 8, "to": 9, "source": "prediction"},
            ],
        },
        version=1,
    )
    db_session.add(source)
    await db_session.flush()
    before_first = dict(source.geometry["keyframes"][0])
    before_last = dict(source.geometry["keyframes"][1])
    body = _body(
        task,
        backend,
        pool,
        rle=ALT_RLE,
        frame_index=4,
        source=source,
    )

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token, **{"If-Match": 'W/"1"'}),
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(source)
    stored_geometry = source.geometry
    assert stored_geometry["track_id"] == "trk_keep"
    assert stored_geometry["keyframes"][0] == before_first
    assert stored_geometry["keyframes"][2] == before_last
    assert stored_geometry["keyframes"][1]["frame_index"] == 4
    assert stored_geometry["keyframes"][1]["source"] == "prediction"
    assert (
        stored_geometry["keyframes"][1]["mask"]["sha256"]
        == build_rle_reference(ALT_RLE)["sha256"]
    )
    assert stored_geometry["outside"] == [
        {"from": 3, "to": 3, "source": "manual"},
        {"from": 5, "to": 5, "source": "manual"},
        {"from": 8, "to": 9, "source": "prediction"},
    ]
    assert source.source == "manual"


async def test_video_accept_create_builds_one_current_frame_keyframe(
    httpx_client,
    db_session,
    super_admin,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id, media_type="video")
    body = _body(task, backend, pool, frame_index=7)

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    annotation = response.json()["annotation"]
    assert annotation["geometry"]["type"] == "video_track_mask"
    assert annotation["geometry"]["outside"] == []
    assert len(annotation["geometry"]["keyframes"]) == 1
    keyframe = annotation["geometry"]["keyframes"][0]
    assert keyframe["frame_index"] == 7
    assert keyframe["mask"]["sha256"] == build_rle_reference(RLE)["sha256"]
    assert keyframe["source"] == "prediction"
    assert keyframe["occluded"] is False


async def test_video_accept_persists_claimed_collaboration_segment(
    httpx_client,
    db_session,
    super_admin,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id, media_type="video")
    project = await db_session.get(Project, task.project_id)
    project.video_collaboration = {"enabled": True, "overlap_frames": 2}
    segment = VideoSegment(
        dataset_item_id=task.dataset_item_id,
        segment_index=0,
        start_frame=0,
        end_frame=11,
        assignee_id=user.id,
        status="in_progress",
        locked_by=user.id,
        locked_at=datetime.now(timezone.utc),
        lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(segment)
    await db_session.flush()
    body = _body(task, backend, pool, frame_index=7)
    body["video_segment_id"] = str(segment.id)

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["annotation"]["video_segment_id"] == str(segment.id)


async def test_video_candidate_receipt_is_bound_to_the_requested_frame(
    httpx_client,
    db_session,
    super_admin,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(
        db_session,
        owner_id=user.id,
        media_type="video",
    )
    body = _body(task, backend, pool, frame_index=7)
    body["target"]["frame_index"] = 8

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "candidate_receipt_mismatch"
    accept_storage_mocks[0].assert_not_awaited()


async def test_task_status_is_rechecked_after_route_preflight(
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id)
    body = _body(task, backend, pool)
    from app.api.v1.tasks import ai_masks as route_module

    original = route_module.accept_ai_mask_candidate

    async def drift_then_accept(db, **kwargs):
        current = await db.get(Task, kwargs["task_id"])
        current.status = "completed"
        await db.flush()
        return await original(db, **kwargs)

    monkeypatch.setattr(route_module, "accept_ai_mask_candidate", drift_then_accept)

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "reason": "task_locked",
        "message": "task is no longer editable",
        "status": "completed",
    }
    accept_storage_mocks[0].assert_not_awaited()


async def test_signed_historical_route_survives_instance_removal(
    httpx_client,
    db_session,
    super_admin,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id)
    body = _body(task, backend, pool)
    backend_id = backend.id
    pool.legacy_instance_id = None
    await db_session.execute(
        delete(MLBackendPoolMember).where(MLBackendPoolMember.registry_id == backend_id)
    )
    await db_session.delete(backend)
    await db_session.flush()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["prediction"]["ml_backend_id"] is None
    meta = (await db_session.execute(select(PredictionMeta))).scalar_one()
    lineage = meta.extra["mask_ai_accept"]["routing"]
    assert lineage["backend_instance_id"] == str(backend_id)
    assert lineage["backend_pool_id"] == str(pool.id)


async def test_idempotent_replay_rechecks_actor_ownership(
    httpx_client,
    db_session,
    super_admin,
    annotator,
    accept_storage_mocks,
):
    admin, admin_token = super_admin
    other, other_token = annotator
    task, backend, pool = await _seed(db_session, owner_id=admin.id)
    body = _body(task, backend, pool, key="accept-replay-owner-check")
    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(admin_token),
    )
    assert first.status_code == 200, first.text
    project = await db_session.get(Project, task.project_id)
    project.owner_id = other.id
    # Literal employee owner: ownership alone is not management. An explicit
    # annotator membership plus the task assignment make the replayed task
    # visible so the request reaches the idempotency owner check.
    await create_membership(
        db_session, project_id=project.id, user_id=other.id, role="annotator"
    )
    task.assignee_id = other.id
    await db_session.flush()

    replay = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(other_token),
    )

    assert replay.status_code == 403
    assert replay.json()["detail"]["reason"] == "idempotency_owner_conflict"


async def test_failure_after_prediction_flush_leaves_no_partial_decision(
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
    accept_storage_mocks,
):
    user, token = super_admin
    task, backend, pool = await _seed(db_session, owner_id=user.id)
    body = _body(task, backend, pool)

    async def fail_annotation_create(*_args, **_kwargs):
        raise RuntimeError("injected annotation failure")

    monkeypatch.setattr(
        "app.services.ai_mask_accept.AnnotationService.create",
        fail_annotation_create,
    )
    with pytest.raises(RuntimeError, match="injected annotation failure"):
        await httpx_client.post(
            f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
            json=body,
            headers=_headers(token),
        )
    await db_session.rollback()

    for model in (AiMaskAcceptDecision, Prediction, PredictionMeta, Annotation):
        count = (
            await db_session.execute(select(func.count()).select_from(model))
        ).scalar_one()
        assert count == 0


async def test_write_gate_and_other_user_task_lock_fail_before_storage(
    httpx_client,
    db_session,
    super_admin,
    annotator,
    monkeypatch,
    accept_storage_mocks,
):
    user, token = super_admin
    other, _ = annotator
    task, backend, pool = await _seed(db_session, owner_id=user.id)
    body = _body(task, backend, pool)
    store = accept_storage_mocks[0]
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)

    gated = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json=body,
        headers=_headers(token),
    )
    assert gated.status_code == 409
    assert gated.json()["detail"]["reason"] == "raster_mask_create_disabled"
    store.assert_not_awaited()

    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=other.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            unique_id=uuid.uuid4(),
        )
    )
    await db_session.flush()
    locked = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/ai-mask-candidates/accept",
        json={**body, "idempotency_key": "accept-task-lock-conflict"},
        headers=_headers(token),
    )
    assert locked.status_code == 409
    assert locked.json()["detail"]["reason"] == "task_lock_conflict"
    store.assert_not_awaited()
