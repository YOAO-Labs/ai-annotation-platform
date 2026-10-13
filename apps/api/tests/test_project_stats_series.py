from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task


@pytest.mark.asyncio
async def test_project_stats_returns_database_backed_series(
    httpx_client,
    db_session,
    super_admin,
):
    user, token = super_admin
    suffix = uuid.uuid4().hex[:6]
    now = datetime.now(timezone.utc)
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-ST-{suffix}",
        name=f"stats-{suffix}",
        type_label="图像",
        type_key="image-det",
        owner_id=user.id,
        total_tasks=3,
        completed_tasks=1,
        review_tasks=1,
    )
    db_session.add(project)
    await db_session.flush()

    old_task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-ST-A-{suffix}",
        file_name="old.jpg",
        file_path="/tmp/old.jpg",
        file_type="image",
        tags=[],
        status="pending",
        created_at=now - timedelta(weeks=20),
        updated_at=now - timedelta(weeks=20),
    )
    completed_task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-ST-B-{suffix}",
        file_name="done.jpg",
        file_path="/tmp/done.jpg",
        file_type="image",
        tags=[],
        status="completed",
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=1),
        reviewed_at=now - timedelta(days=1),
    )
    review_task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-ST-C-{suffix}",
        file_name="review.jpg",
        file_path="/tmp/review.jpg",
        file_type="image",
        tags=[],
        status="review",
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=1),
        submitted_at=now - timedelta(days=1),
    )
    db_session.add_all([old_task, completed_task, review_task])
    await db_session.flush()

    db_session.add_all(
        [
            Annotation(
                id=uuid.uuid4(),
                task_id=old_task.id,
                project_id=project.id,
                user_id=user.id,
                class_name="car",
                geometry={"type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1},
                created_at=now - timedelta(weeks=20),
            ),
            Annotation(
                id=uuid.uuid4(),
                task_id=completed_task.id,
                project_id=project.id,
                user_id=user.id,
                class_name="car",
                geometry={"type": "rectangle", "x": 1, "y": 1, "width": 1, "height": 1},
                parent_prediction_id=uuid.uuid4(),
                created_at=now - timedelta(days=1),
            ),
        ]
    )
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/projects/stats",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total_data"] == 3
    assert body["completed"] == 1
    assert body["pending_review"] == 1
    assert body["ai_rate"] == 50.0
    assert len(body["total_data_series"]) == 12
    assert len(body["completed_series"]) == 12
    assert len(body["pending_review_series"]) == 12
    assert len(body["ai_rate_series"]) == 12
    assert body["total_data_series"][0] == 1
    assert body["total_data_series"][-1] == 3
    assert body["completed_series"][-1] == 1
    assert body["pending_review_series"][-1] == 1
    assert body["ai_rate_series"][-1] == 50.0
