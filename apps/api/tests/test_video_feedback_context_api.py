"""Video feedback persistence and task authorization against the disposable test database."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.dataset import Dataset, DatasetItem
from tests.factory import create_membership, create_batch, create_project, create_task


@pytest.fixture
async def video_feedback_data(db_session, super_admin):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="video-det")
    dataset = Dataset(
        display_id=f"VD-{uuid4().hex[:12]}",
        name="Video context",
        data_type="video",
        created_by=user.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="issue-context.mp4",
        file_path="test/video-feedback-context.mp4",
        file_type="video",
        width=160,
        height=120,
        metadata_={
            "video": {"frame_count": 180, "fps": 30, "width": 160, "height": 120}
        },
    )
    db_session.add(item)
    await db_session.flush()
    task = await create_task(db_session, project_id=project.id)
    task.file_type = "video"
    task.dataset_item_id = item.id
    annotation = Annotation(
        project_id=project.id,
        task_id=task.id,
        user_id=user.id,
        class_name="car",
        annotation_type="bbox",
        geometry={"type": "bbox", "x": 10, "y": 10, "width": 20, "height": 30},
        track_id="car-track/left",
        version=7,
    )
    db_session.add(annotation)
    await db_session.flush()
    return {
        "project": project,
        "task": task,
        "annotation": annotation,
        "headers": {"Authorization": f"Bearer {token}"},
        "owner": user,
    }


def issue_payload(data, frame=140):
    return {
        "kind": "issue",
        "anchor_type": "pixel",
        "project_id": str(data["project"].id),
        "task_id": str(data["task"].id),
        "annotation_id": str(data["annotation"].id),
        "body": "Inspect the captured video context",
        "anchor_position": {
            "x": 0.4,
            "y": 0.6,
            "frame": frame,
            "region_bbox": [0.3, 0.4, 0.5, 0.8],
            "video_context": {
                "schema_version": 1,
                "track_id": "car-track/left",
                "annotation_version": 2,
                "frame_range": {
                    "from_frame": 120 if frame else 0,
                    "to_frame": 160 if frame else 0,
                },
                "viewport": {"center_x": -0.25, "center_y": 1.25, "zoom": 2.5},
                "timeline_window": {"from": 110.5 if frame else 0, "to": 170.25},
            },
        },
    }


@pytest.mark.parametrize("frame", [0, 140])
@pytest.mark.asyncio
async def test_video_feedback_context_round_trip_survives_object_change(
    httpx_client, db_session, video_feedback_data, frame
):
    data = video_feedback_data
    source = issue_payload(data, frame)
    response = await httpx_client.post(
        "/api/v1/feedbacks", json=source, headers=data["headers"]
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["anchor_position"]["frame"] == frame
    assert (
        saved["anchor_position"]["video_context"]
        == source["anchor_position"]["video_context"]
    )
    assert saved["anchor_position"]["video_context"]["annotation_version"] == 2
    assert data["annotation"].version == 7
    data["annotation"].version = 8
    data["annotation"].is_active = False
    await db_session.flush()
    project_id, task_id = source["project_id"], source["task_id"]
    db_session.expire_all()
    response = await httpx_client.get(
        "/api/v1/feedbacks",
        params={"project_id": project_id, "task_id": task_id},
        headers=data["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["anchor_position"] == saved["anchor_position"]
    assert response.json()["items"][0]["annotation_id"] == source["annotation_id"]


@pytest.mark.asyncio
async def test_unknown_video_context_is_readable_and_replyable_but_not_writable(
    httpx_client, db_session, video_feedback_data
):
    data = video_feedback_data
    source = issue_payload(data)
    response = await httpx_client.post(
        "/api/v1/feedbacks", json=source, headers=data["headers"]
    )
    assert response.status_code == 200, response.text
    from uuid import UUID

    feedback_id = UUID(response.json()["id"])
    entry = await db_session.get(AnnotationFeedback, feedback_id)
    anchor = deepcopy(entry.anchor_position)
    anchor["video_context"] = {
        "schema_version": 99,
        "future_view": {"matrix": [1, 2, 3]},
    }
    entry.anchor_position = anchor
    data["annotation"].is_active = False
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/feedbacks",
        params={"project_id": source["project_id"]},
        headers=data["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["anchor_position"] == anchor
    response = await httpx_client.post(
        f"/api/v1/feedbacks/{feedback_id}/replies",
        json={"body": "Still relevant"},
        headers=data["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["anchor_position"] == anchor
    source["anchor_position"] = anchor
    response = await httpx_client.post(
        "/api/v1/feedbacks", json=source, headers=data["headers"]
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("failure", ["range", "window", "image", "foreign_object"])
@pytest.mark.asyncio
async def test_video_context_api_rejects_invalid_media_or_object_relationship(
    httpx_client, db_session, video_feedback_data, failure
):
    data = video_feedback_data
    source = issue_payload(data)
    if failure == "range":
        source["anchor_position"]["video_context"]["frame_range"]["to_frame"] = 180
    elif failure == "window":
        source["anchor_position"]["video_context"]["timeline_window"] = {
            "from": 170,
            "to": 110,
        }
    elif failure == "image":
        data["task"].file_type = "image"
    else:
        other_task = await create_task(db_session, project_id=data["project"].id)
        data["annotation"].task_id = other_task.id
    await db_session.flush()
    response = await httpx_client.post(
        "/api/v1/feedbacks", json=source, headers=data["headers"]
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_video_feedback_uses_real_batch_permission_for_create_list_and_reply(
    httpx_client, db_session, video_feedback_data, annotator
):
    data = video_feedback_data
    user, token = annotator
    project, task = data["project"], data["task"]
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=user.id,
        role="annotator",
        assigned_by=data["owner"].id,
    )
    batch = await create_batch(db_session, project_id=project.id, status="active")
    batch.annotator_id = data["owner"].id
    task.batch_id = batch.id
    await db_session.flush()
    source = issue_payload(data)
    owner_response = await httpx_client.post(
        "/api/v1/feedbacks", json=source, headers=data["headers"]
    )
    assert owner_response.status_code == 200, owner_response.text
    issue_id = owner_response.json()["id"]
    headers = {"Authorization": f"Bearer {token}"}
    denied = await httpx_client.post("/api/v1/feedbacks", json=source, headers=headers)
    assert denied.status_code == 404, denied.text
    denied_reply = await httpx_client.post(
        f"/api/v1/feedbacks/{issue_id}/replies",
        json={"body": "Hidden task"},
        headers=headers,
    )
    assert denied_reply.status_code == 404, denied_reply.text
    listed = await httpx_client.get(
        "/api/v1/feedbacks", params={"project_id": str(project.id)}, headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []
    batch.annotator_id = user.id
    await db_session.flush()
    allowed = await httpx_client.post("/api/v1/feedbacks", json=source, headers=headers)
    assert allowed.status_code == 200, allowed.text
    listed = await httpx_client.get(
        "/api/v1/feedbacks", params={"project_id": str(project.id)}, headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) == 2
