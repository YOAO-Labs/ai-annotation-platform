"""A principal authenticated before suspension cannot enter task mutations."""

from types import SimpleNamespace

import pytest
from sqlalchemy import update

from app.db.models.user import User
from app.deps import get_current_user
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
