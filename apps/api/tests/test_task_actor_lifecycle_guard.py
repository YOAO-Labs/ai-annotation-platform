"""A principal authenticated before suspension cannot enter task mutations."""

from types import SimpleNamespace

from fastapi import Depends, Request
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks import lifecycle as task_lifecycle_api
from app.api.v1.tasks import review as task_review_api
from app.application.task_review import TaskReviewWorkflow
from app.application.task_submission import TaskSubmissionWorkflow
from app.db.models.user import User
from app.deps import (
    get_current_principal,
    get_current_user,
    get_db,
    get_identity_gateway,
)
from tests.test_task_lock import _create_annotation, _seed_project_and_task


@pytest.mark.parametrize(
    "operation",
    [
        "submit",
        "bulk_update",
        "api_key",
        "tracker",
        "feedback",
        "chapter",
        "comment",
        "point_quality",
        "mask_quality",
    ],
)
async def test_suspended_actor_is_rechecked_before_task_handler(
    operation, httpx_client, app_module, db_session, annotator
):
    user, token = annotator
    # _seed_project_and_task adds the explicit annotator membership for the worker.
    _, task = await _seed_project_and_task(db_session, user.id, user.id)
    annotation = await _create_annotation(db_session, task, user.id)
    stale_principal = SimpleNamespace(id=user.id, role=user.role, is_active=True)
    await db_session.execute(
        update(User)
        .where(User.id == user.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )

    async def previously_authenticated_user():
        return stale_principal

    previous_override = app_module.dependency_overrides.get(get_current_user)
    app_module.dependency_overrides[get_current_user] = previously_authenticated_user
    try:
        if operation == "submit":
            response = await httpx_client.post(
                f"/api/v1/tasks/{task.id}/submit",
                headers={"Authorization": f"Bearer {token}"},
            )
        elif operation == "bulk_update":
            response = await httpx_client.post(
                "/api/v1/annotations/bulk-update",
                json={"ids": [str(annotation.id)], "patch": {"class_name": "person"}},
                headers={"Authorization": f"Bearer {token}"},
            )
        elif operation == "api_key":
            response = await httpx_client.post(
                "/api/v1/me/api-keys",
                json={"name": "No stale key", "scopes": ["annotations:write"]},
                headers={"Authorization": f"Bearer {token}"},
            )
        elif operation in {"point_quality", "mask_quality"}:
            prefix = (
                "point-cloud-quality" if operation == "point_quality" else "mask-qc"
            )
            response = await httpx_client.patch(
                f"/api/v1/{prefix}/issues/{annotation.id}",
                json={"status": "resolved"},
                headers={"Authorization": f"Bearer {token}"},
            )
        else:
            paths = {
                "tracker": f"/api/v1/video-tracker-jobs/{task.id}",
                "feedback": f"/api/v1/feedbacks/{annotation.id}",
                "chapter": f"/api/v1/videos/{task.id}/chapters/{annotation.id}",
                "comment": f"/api/v1/comments/{annotation.id}",
            }
            response = await httpx_client.delete(
                paths[operation], headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 401, response.text
        await db_session.refresh(task)
        await db_session.refresh(annotation)
        assert task.status == "in_progress"
        assert annotation.class_name == "car"
    finally:
        if previous_override is None:
            app_module.dependency_overrides.pop(get_current_user, None)
        else:
            app_module.dependency_overrides[get_current_user] = previous_override


async def test_task_actor_id_and_access_dependencies_share_request_auth_and_session(
    httpx_client, app_module, db_session, annotator, reviewer, monkeypatch
):
    annotator_user, annotator_token = annotator
    reviewer_user, reviewer_token = reviewer
    _, task = await _seed_project_and_task(
        db_session,
        owner_id=annotator_user.id,
        assignee_id=annotator_user.id,
        reviewer_id=reviewer_user.id,
    )
    await _create_annotation(db_session, task, annotator_user.id)

    counts = {"auth": 0, "db": 0}
    request_sessions: list[AsyncSession] = []
    workflow_sessions: list[AsyncSession] = []
    actor_ids = []
    original_get_current_user = get_current_user
    original_get_db_override = app_module.dependency_overrides[get_db]

    async def counted_get_db():
        counts["db"] += 1
        async for session in original_get_db_override():
            request_sessions.append(session)
            yield session

    async def counted_get_current_user(
        request: Request,
        principal=Depends(get_current_principal),
        db: AsyncSession = Depends(get_db),
        identity=Depends(get_identity_gateway),
    ):
        counts["auth"] += 1
        return await original_get_current_user(request, principal, db, identity)

    class TrackingSubmissionWorkflow(TaskSubmissionWorkflow):
        def __init__(self, db: AsyncSession) -> None:
            workflow_sessions.append(db)
            super().__init__(db)

        async def submit_task(self, task_id, **kwargs):
            actor_ids.append(kwargs["actor_id"])
            return await super().submit_task(task_id, **kwargs)

    class TrackingReviewWorkflow(TaskReviewWorkflow):
        def __init__(self, db: AsyncSession) -> None:
            workflow_sessions.append(db)
            super().__init__(db)

        async def claim_review(self, task_id, **kwargs):
            actor_ids.append(kwargs["actor_id"])
            return await super().claim_review(task_id, **kwargs)

    previous_user_override = app_module.dependency_overrides.get(get_current_user)
    app_module.dependency_overrides[get_db] = counted_get_db
    app_module.dependency_overrides[get_current_user] = counted_get_current_user
    monkeypatch.setattr(
        task_lifecycle_api, "TaskSubmissionWorkflow", TrackingSubmissionWorkflow
    )
    monkeypatch.setattr(task_review_api, "TaskReviewWorkflow", TrackingReviewWorkflow)
    try:
        submitted = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/submit",
            headers={"Authorization": f"Bearer {annotator_token}"},
        )
        claimed = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/review/claim",
            headers={"Authorization": f"Bearer {reviewer_token}"},
        )
    finally:
        if previous_user_override is None:
            app_module.dependency_overrides.pop(get_current_user, None)
        else:
            app_module.dependency_overrides[get_current_user] = previous_user_override
        app_module.dependency_overrides[get_db] = original_get_db_override

    assert submitted.status_code == 200, submitted.text
    assert claimed.status_code == 200, claimed.text
    assert counts == {"auth": 2, "db": 2}
    assert request_sessions == [db_session, db_session]
    assert workflow_sessions == [db_session, db_session]
    assert actor_ids == [annotator_user.id, reviewer_user.id]
    await db_session.refresh(task)
    assert task.review_submitter_id == annotator_user.id
    assert task.reviewer_id == reviewer_user.id
