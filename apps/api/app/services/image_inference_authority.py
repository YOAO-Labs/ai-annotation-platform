"""Business authorization shared by synchronous image inference consumers."""

from app.db.models.project import Project
from app.db.models.user import User
from app.services.content_snapshot import ContentError
from app.services.project_access import resolve_project_access
from app.services.project_write_guard import assert_phase_write_allowed
from app.services.task_lock import TaskLockService


async def authorize_image_operation(db, task, *, actor_id, project_id, operation):
    """Preserve consumer business rules without adding API key read scopes."""
    from app.api.v1.tasks._shared import (
        _assert_task_visible,
        _assert_task_editable,
        _assert_review_adjustment_evidence,
        assert_annotation_write_allowed,
    )

    actor = await db.get(User, actor_id, populate_existing=True)
    project = await db.get(Project, project_id, populate_existing=True)
    if (
        actor is None
        or not actor.is_active
        or project is None
        or project.data_type != "image"
        or task.project_id != project_id
        or task.file_type != "image"
    ):
        raise ContentError("prediction_authority_revoked", 403)
    access = await resolve_project_access(db, user=actor, project=project)
    if operation == "predict_test":
        if not access.is_manager:
            raise ContentError("prediction_authority_revoked", 403)
    else:
        secondary = operation == "secondary_inference"
        if not secondary and project.ai_interactive_enabled is False:
            raise ContentError("ai_interactive_disabled", 403)
        assert_annotation_write_allowed(
            task, access, allow_review_adjustment=not secondary
        )
        if not secondary:
            await _assert_review_adjustment_evidence(db, task, actor, access)
        _assert_task_editable(task, actor, access=access)
        await assert_phase_write_allowed(db, task, actor, lock_membership=False)
        if any(
            lock.user_id != actor.id
            for lock in await TaskLockService(db).active_locks(task.id, cleanup=False)
        ):
            raise ContentError("prediction_task_locked")
    await _assert_task_visible(db, task, actor, access=access)
