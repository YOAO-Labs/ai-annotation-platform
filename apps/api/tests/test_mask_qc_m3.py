from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, literal_column, select, update

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.mask_annotation_revision import MaskAnnotationRevision
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.raster_mask_storage import build_rle_reference
from app.services.mask_qc.execution import _region_bbox_xyxy
from app.utils.raster_mask_rle import encode_coco_rle
from tests.media_owner_fixtures import media_owner as media_owner


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rle(rows: list[str]) -> dict:
    height = len(rows)
    width = len(rows[0])
    pixels = [1 if value == "#" else 0 for row in rows for value in row]
    return encode_coco_rle(pixels, width, height)


async def _seed_image_compare(db, *, owner_id: uuid.UUID):
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        display_id=f"P-CMP-{suffix}",
        name=f"Mask compare {suffix}",
        type_label="图像-分割",
        type_key="image-seg",
        owner_id=owner_id,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "object", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(project)
    await db.flush()
    dataset = Dataset(
        display_id=f"D-CMP-{suffix}",
        name=f"Mask compare dataset {suffix}",
        data_type="image",
        is_temporal=False,
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="compare.png",
        file_path="compare.png",
        file_type="image",
        width=4,
        height=4,
        metadata_={},
    )
    db.add(item)
    await db.flush()
    task = Task(
        project_id=project.id,
        display_id=f"T-CMP-{suffix}",
        file_name="compare.png",
        file_path="compare.png",
        file_type="image",
        dataset_item_id=item.id,
        status="review",
        assignee_id=owner_id,
    )
    db.add(task)
    await db.flush()
    previous = _rle(["....", ".#..", "....", "...."])
    current = _rle(["....", ".##.", "....", "...."])
    previous_ref = build_rle_reference(previous)
    current_ref = build_rle_reference(current)
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=owner_id,
        source="manual",
        annotation_type="mask",
        tool_unit_id="region",
        class_name="object",
        geometry={"type": "raster_mask", "mask": current_ref},
        is_active=True,
        was_cancelled=False,
        version=2,
    )
    db.add(annotation)
    await db.flush()
    db.add(
        MaskAnnotationRevision(
            project_id=project.id,
            task_id=task.id,
            annotation_id=annotation.id,
            annotation_version=1,
            geometry={"type": "raster_mask", "mask": previous_ref},
            geometry_digest="a" * 64,
            source_kind="manual",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    await db.flush()
    return project, task, annotation, previous, current, previous_ref, current_ref


def _patch_rle_loaders(monkeypatch, *rles: dict) -> None:
    by_digest = {build_rle_reference(rle)["sha256"]: rle for rle in rles}

    async def fake_load(reference):
        return by_digest[reference["sha256"]]

    monkeypatch.setattr("app.services.mask_qc.compare.load_coco_rle", fake_load)
    monkeypatch.setattr("app.api.v1.annotations.load_coco_rle", fake_load)
    monkeypatch.setattr("app.api.v1.video_tracker_jobs.load_coco_rle", fake_load)


@pytest.mark.asyncio
@pytest.mark.parametrize("unbounded_retention", [False, True])
async def test_previous_version_compare_returns_authorized_immutable_locators(
    httpx_client, super_admin, db_session, monkeypatch, unbounded_retention
):
    user, token = super_admin
    (
        _project,
        _task,
        annotation,
        previous,
        current,
        _previous_ref,
        _current_ref,
    ) = await _seed_image_compare(db_session, owner_id=user.id)
    _patch_rle_loaders(monkeypatch, previous, current)
    if unbounded_retention:
        await db_session.execute(
            update(MaskAnnotationRevision)
            .where(MaskAnnotationRevision.annotation_id == annotation.id)
            .values(expires_at=literal_column("'infinity'::timestamptz"))
        )
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-compare",
        params={"baseline": "previous_version", "annotation_version": 2},
        headers=_bearer(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metrics"] == {
        "current_area_pixels": 2,
        "baseline_area_pixels": 1,
        "intersection_pixels": 1,
        "union_pixels": 2,
        "changed_pixels": 1,
        "added_pixels": 1,
        "removed_pixels": 0,
        "iou_numerator": 1,
        "iou_denominator": 2,
        "dice_numerator": 2,
        "dice_denominator": 3,
    }
    assert "object_key" not in str(body)
    assert body["current"]["content_path"].startswith(
        f"/annotations/{annotation.id}/mask-compare/content?"
    )
    assert body["baseline"]["annotation_version"] == 1

    content = await httpx_client.get(
        f"/api/v1{body['baseline']['content_path']}",
        headers=_bearer(token),
    )
    assert content.status_code == 200, content.text
    assert content.json() == previous


@pytest.mark.asyncio
async def test_compare_reports_expired_previous_version(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    _project, _task, annotation, *_rest = await _seed_image_compare(
        db_session, owner_id=user.id
    )
    revision = (
        await db_session.execute(
            select(MaskAnnotationRevision).where(
                MaskAnnotationRevision.annotation_id == annotation.id,
                MaskAnnotationRevision.annotation_version == 1,
            )
        )
    ).scalar_one()
    revision.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-compare",
        params={"baseline": "previous_version", "annotation_version": 2},
        headers=_bearer(token),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "baseline_expired"


@pytest.mark.asyncio
async def test_neighbor_compare_excludes_the_current_held_keyframe(
    httpx_client, super_admin, db_session, media_owner, monkeypatch
):
    user, token = super_admin
    project, task, annotation, first, second, *_refs = await _seed_image_compare(
        db_session, owner_id=user.id
    )
    first_ref = build_rle_reference(first)
    second_ref = build_rle_reference(second)
    item = await db_session.get(DatasetItem, task.dataset_item_id)
    assert item is not None
    dataset = await db_session.get(Dataset, item.dataset_id)
    assert dataset is not None
    dataset.data_type = "video"
    dataset.is_temporal = True
    item.file_type = "video"
    item.metadata_ = {"video": {"frame_count": 20}}
    task.file_type = "video"
    await media_owner.seed_video_item(item, user.id)
    annotation.version = 1
    annotation.geometry = {
        "type": "video_track_mask",
        "keyframes": [
            {"frame_index": 0, "mask": first_ref, "source": "manual"},
            {"frame_index": 10, "mask": second_ref, "source": "prediction"},
        ],
        "outside": [],
    }
    await db_session.execute(
        delete(MaskAnnotationRevision).where(
            MaskAnnotationRevision.annotation_id == annotation.id
        )
    )
    _patch_rle_loaders(monkeypatch, first, second)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-compare",
        params={
            "baseline": "neighbor_keyframe",
            "annotation_version": 1,
            "frame_index": 4,
        },
        headers=_bearer(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["current"]["state"] == "held"
    assert response.json()["baseline"]["frame_index"] == 10
    assert response.json()["baseline"]["source"] == "prediction"
    assert response.json()["current"]["digest"] == first_ref["sha256"]
    assert response.json()["baseline"]["digest"] == second_ref["sha256"]
    assert response.json()["current"]["annotation_id"] == str(annotation.id)
    assert response.json()["baseline"]["annotation_id"] == str(annotation.id)
    assert response.json()["current"]["annotation_version"] == 1
    assert response.json()["baseline"]["annotation_version"] == 1
    assert response.json()["baseline_kind"] == "neighbor_keyframe"
    assert response.json()["loss"] == []
    assert project.id == task.project_id


@pytest.mark.asyncio
async def test_tracker_compare_requires_exact_job_revision_and_candidate_digest(
    httpx_client, super_admin, db_session, media_owner, monkeypatch
):
    user, token = super_admin
    project, task, annotation, baseline, candidate, *_refs = await _seed_image_compare(
        db_session, owner_id=user.id
    )
    dataset = Dataset(
        display_id=f"D-CMP-{uuid.uuid4().hex[:8]}",
        name="compare video",
        data_type="video",
        is_temporal=True,
        created_by=user.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="compare.mp4",
        file_path="compare.mp4",
        file_type="video",
        width=4,
        height=4,
        metadata_={"video": {"frame_count": 20}},
    )
    db_session.add(item)
    await db_session.flush()
    await media_owner.seed_video_item(item, user.id)
    task.file_type = "video"
    task.dataset_item_id = item.id
    annotation.version = 1
    annotation.geometry = {
        "type": "video_track_mask",
        "keyframes": [
            {
                "frame_index": 3,
                "mask": build_rle_reference(baseline),
                "source": "manual",
            }
        ],
        "outside": [],
    }
    candidate_ref = build_rle_reference(candidate)
    candidate_digest = f"sha256:{'c' * 64}"
    job = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=item.id,
        annotation_id=annotation.id,
        created_by=user.id,
        status="pending_review",
        model_key="test",
        direction="forward",
        from_frame=3,
        to_frame=3,
        prompt={
            "seeds": [
                {
                    "obj_id": 7,
                    "source_annotation_id": str(annotation.id),
                    "geometry": {"type": "mask", "mask": build_rle_reference(baseline)},
                }
            ]
        },
        staged_result={
            "source_instance_map": {"7": str(annotation.id)},
            "results": [
                {
                    "frame_index": 3,
                    "geometry": {"type": "mask", "mask": candidate_ref},
                    "geometry_digest": candidate_digest,
                    "instance_id": "7",
                    "outside": False,
                },
                {
                    "frame_index": 3,
                    "geometry": {"type": "mask", "mask": candidate_ref},
                    "geometry_digest": candidate_digest,
                    "instance_id": "8",
                    "source_annotation_id": str(annotation.id),
                    "outside": False,
                },
            ],
        },
        revision=4,
        event_channel=f"tracker:{uuid.uuid4()}",
    )
    db_session.add(job)
    await db_session.execute(
        delete(MaskAnnotationRevision).where(
            MaskAnnotationRevision.annotation_id == annotation.id
        )
    )
    _patch_rle_loaders(monkeypatch, baseline, candidate)
    await db_session.commit()

    params = {
        "baseline": "tracker_candidate",
        "annotation_version": 1,
        "frame_index": 3,
        "candidate_job_id": str(job.id),
        "candidate_job_revision": 3,
        "candidate_digest": candidate_digest,
    }
    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-compare",
        params=params,
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == (
        "mask_compare_candidate_revision_conflict"
    )

    params["candidate_job_revision"] = 4
    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-compare",
        params=params,
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "mask_compare_candidate_ambiguous"

    params["candidate_instance_id"] = "7"
    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-compare",
        params=params,
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["baseline"]["candidate_job_id"] == str(job.id)
    assert response.json()["baseline"]["candidate_digest"] == candidate_digest
    assert response.json()["baseline"]["candidate_instance_id"] == "7"
    assert response.json()["baseline"]["content_path"] == (
        f"/video-tracker-jobs/{job.id}/mask-content/{candidate_ref['sha256']}"
    )
    assert project.id == task.project_id


@pytest.mark.asyncio
async def test_issue_region_content_and_feedback_anchor_round_trip(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, task, annotation, region, current, *_refs = await _seed_image_compare(
        db_session, owner_id=user.id
    )
    region_ref = build_rle_reference(region)
    region_bbox = _region_bbox_xyxy(region)
    assert region_bbox == {"x0": 0.25, "y0": 0.25, "x1": 0.5, "y1": 0.5}
    issue = MaskQCIssue(
        project_id=project.id,
        task_id=task.id,
        annotation_id=annotation.id,
        annotation_version=2,
        related_annotation_ids=[annotation.id],
        source_versions={str(annotation.id): 2},
        code="small_island",
        severity="warning",
        severity_rank=1,
        status="open",
        metric={},
        threshold={},
        region_bbox=region_bbox,
        region_mask_ref=region_ref,
        region_digest=region_ref["sha256"],
        dedupe_key="d" * 64,
        source={},
    )
    db_session.add(issue)
    _patch_rle_loaders(monkeypatch, region, current)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/mask-qc/issues/{issue.id}/region-content",
        params={"digest": region_ref["sha256"]},
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.json() == region

    anchor = {
        "x": 0.375,
        "y": 0.375,
        "region_bbox": [0.25, 0.25, 0.5, 0.5],
        "region_digest": region_ref["sha256"],
        "boundary_digest": "e" * 64,
        "mask_qc_issue_id": str(issue.id),
        "compare_locator": {
            "baseline_kind": "previous_version",
            "mode": "boundary",
            "current_digest": "a" * 64,
            "baseline_digest": "b" * 64,
        },
    }
    response = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "pixel",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "annotation_id": str(annotation.id),
            "anchor_position": anchor,
            "body": "compare boundary",
        },
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    feedback_id = response.json()["id"]
    stored_anchor = response.json()["anchor_position"]
    assert stored_anchor == {
        **anchor,
        "frame": None,
        "compare_locator": {
            **anchor["compare_locator"],
            "candidate_job_id": None,
            "candidate_job_revision": None,
            "candidate_digest": None,
            "candidate_instance_id": None,
        },
    }

    annotation.version = 3
    await db_session.commit()
    response = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "pixel",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "annotation_id": str(annotation.id),
            "anchor_position": anchor,
            "body": "stale compare",
        },
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "mask_qc_issue_stale"

    response = await httpx_client.post(
        f"/api/v1/feedbacks/{feedback_id}/replies",
        json={"body": "stale reply"},
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "mask_qc_issue_stale"

    invalid = {**anchor, "region_digest": None}
    response = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "pixel",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "annotation_id": str(annotation.id),
            "anchor_position": invalid,
            "body": "invalid",
        },
        headers=_bearer(token),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_compare_content_endpoints_require_annotations_read_scope(
    httpx_client, super_admin
):
    _user, token = super_admin
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        json={"name": "mask-compare-scope", "scopes": ["datasets:read"]},
        headers=_bearer(token),
    )
    assert created.status_code == 201, created.text
    headers = _bearer(created.json()["plaintext"])
    annotation_id = uuid.uuid4()
    issue_id = uuid.uuid4()

    responses = [
        await httpx_client.get(
            f"/api/v1/annotations/{annotation_id}/mask-compare",
            params={"baseline": "previous_version", "annotation_version": 1},
            headers=headers,
        ),
        await httpx_client.get(
            f"/api/v1/annotations/{annotation_id}/mask-compare/content",
            params={"annotation_version": 1, "digest": "a" * 64},
            headers=headers,
        ),
        await httpx_client.get(
            f"/api/v1/mask-qc/issues/{issue_id}/region-content",
            params={"digest": "a" * 64},
            headers=headers,
        ),
        await httpx_client.get(
            f"/api/v1/video-tracker-jobs/{uuid.uuid4()}/mask-content/{'a' * 64}",
            headers=headers,
        ),
    ]
    assert [response.status_code for response in responses] == [403, 403, 403, 403]
    assert all(
        "annotations:read" in response.json()["detail"] for response in responses
    )
