"""Task overrides survive changes to inherited batch assignments."""

from __future__ import annotations

import uuid

import pytest

from app.db.models.task_batch import TaskBatch
from app.schemas.batch import BatchUpdate
from app.schemas.data_manager_actions import (
    DataManagerTaskAssignmentApplyRequest,
    DataManagerTaskAssignmentRequest,
)
from app.services.batch import BatchService
from app.services.data_management.actions import DataManagerTaskActionService
from app.services.scheduler import get_next_task
from tests.factory import create_membership, create_project, create_user
from tests.test_task_assignment_visibility import _task


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("method", ["update", "bulk", "distribute"])
@pytest.mark.parametrize("pin_current_default", [False, True])
async def test_selected_assignments_survive_batch_reassignment(
    db_session, super_admin, annotator, reviewer, method, pin_current_default
):
    owner, _ = super_admin
    old_annotator, _ = annotator
    old_reviewer, _ = reviewer
    project = await create_project(db_session, owner_id=owner.id)
    selected_annotator = await create_user(
        db_session, "employee", "selected-annotator@test.local", "Selected annotator"
    )
    selected_reviewer = await create_user(
        db_session, "employee", "selected-reviewer@test.local", "Selected reviewer"
    )
    new_annotator = await create_user(
        db_session, "employee", "new-annotator@test.local", "New annotator"
    )
    new_reviewer = await create_user(
        db_session, "employee", "new-reviewer@test.local", "New reviewer"
    )
    # Project roles are explicit; never inferred from the literal employee account.
    for member, role in (
        (old_annotator, "annotator"),
        (old_reviewer, "reviewer"),
        (selected_annotator, "annotator"),
        (selected_reviewer, "reviewer"),
        (new_annotator, "annotator"),
        (new_reviewer, "reviewer"),
    ):
        await create_membership(
            db_session,
            project_id=project.id,
            user_id=member.id,
            role=role,
            assigned_by=owner.id,
        )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-OVERRIDE-{uuid.uuid4().hex[:8]}",
        name="Inherited defaults",
        status="active",
        created_by=owner.id,
    )
    db_session.add(batch)
    await db_session.flush()
    selected = await _task(
        db_session,
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-OVERRIDE-{uuid.uuid4().hex[:8]}",
    )
    sibling = await _task(
        db_session,
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-INHERITED-{uuid.uuid4().hex[:8]}",
    )
    selected.status = "review"
    batches = BatchService(db_session)
    await batches.update(
        batch.id,
        BatchUpdate(annotator_id=old_annotator.id, reviewer_id=old_reviewer.id),
        project_id=project.id,
    )
    chosen_annotator = old_annotator if pin_current_default else selected_annotator
    chosen_reviewer = old_reviewer if pin_current_default else selected_reviewer
    actions = DataManagerTaskActionService(db_session)

    async def assign_selected(annotator_id, reviewer_id):
        payload = DataManagerTaskAssignmentRequest(
            task_ids=[selected.id], annotator_id=annotator_id, reviewer_id=reviewer_id
        )
        preview = await actions.preview_assignment(project.id, payload, actor=owner)
        result = await actions.apply_assignment(
            project.id,
            DataManagerTaskAssignmentApplyRequest(
                **payload.model_dump(), preview_version=preview.preview_version
            ),
            actor=owner,
        )
        assert result.succeeded == [selected.id]

    await assign_selected(chosen_annotator.id, chosen_reviewer.id)
    assigned_at = selected.assigned_at
    if method == "update":
        await batches.update(
            batch.id,
            BatchUpdate(annotator_id=new_annotator.id, reviewer_id=new_reviewer.id),
            project_id=project.id,
        )
    elif method == "bulk":
        await batches.bulk_reassign(
            project.id,
            [batch.id],
            annotator_id=new_annotator.id,
            reviewer_id=new_reviewer.id,
            annotator_set=True,
            reviewer_set=True,
        )
    else:
        await batches.distribute_batches_in_project(
            project.id,
            batch_ids=[batch.id],
            annotator_ids=[new_annotator.id],
            reviewer_ids=[new_reviewer.id],
            only_unassigned=False,
        )
    await db_session.refresh(selected)
    await db_session.refresh(sibling)
    assert selected.assignee_id == chosen_annotator.id
    assert selected.reviewer_id == chosen_reviewer.id
    assert selected.assigned_at == assigned_at
    assert sibling.assignee_id == new_annotator.id
    assert sibling.reviewer_id == new_reviewer.id

    # Clearing a task override restores inheritance for subsequent batch edits.
    await assign_selected(None, None)
    await batches.update(
        batch.id,
        BatchUpdate(annotator_id=old_annotator.id, reviewer_id=old_reviewer.id),
        project_id=project.id,
    )
    await db_session.refresh(selected)
    assert selected.assignee_id == old_annotator.id
    assert selected.reviewer_id == old_reviewer.id


@pytest.mark.parametrize("transition", ["submit", "skip", "reopen"])
async def test_new_review_round_releases_previous_reviewer_override(
    httpx_client, db_session, super_admin, annotator, reviewer, transition
):
    owner, _ = super_admin
    actor, actor_token = annotator
    default_reviewer, reviewer_token = reviewer
    project = await create_project(db_session, owner_id=owner.id)
    pinned_reviewer = await create_user(
        db_session, "employee", "previous-reviewer@test.local", "Previous reviewer"
    )
    for member, role in (
        (actor, "annotator"),
        (default_reviewer, "reviewer"),
        (pinned_reviewer, "reviewer"),
    ):
        await create_membership(
            db_session,
            project_id=project.id,
            user_id=member.id,
            role=role,
            assigned_by=owner.id,
        )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-ROUND-{uuid.uuid4().hex[:8]}",
        name="Review rounds",
        status="active",
        created_by=owner.id,
        annotator_id=actor.id,
        reviewer_id=default_reviewer.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task = await _task(
        db_session,
        project_id=project.id,
        batch_id=batch.id,
        assignee_id=actor.id,
        display_id=f"T-ROUND-{uuid.uuid4().hex[:8]}",
    )
    task.status = "review"
    actions = DataManagerTaskActionService(db_session)
    payload = DataManagerTaskAssignmentRequest(
        task_ids=[task.id],
        reviewer_id=pinned_reviewer.id,
    )
    preview = await actions.preview_assignment(project.id, payload, actor=owner)
    applied = await actions.apply_assignment(
        project.id,
        DataManagerTaskAssignmentApplyRequest(
            **payload.model_dump(exclude_unset=True),
            preview_version=preview.preview_version,
        ),
        actor=owner,
    )
    assert applied.succeeded == [task.id]
    # Start from a prior round's completed or returned task, carrying the
    # assignment intent produced by the real selected-task operation.
    task.status = "completed" if transition == "reopen" else "in_progress"
    await db_session.flush()
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/{transition}",
        headers={"Authorization": f"Bearer {actor_token}"},
        **({"json": {"reason": "no_target"}} if transition == "skip" else {}),
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(task)
    assert task.reviewer_is_override is False
    if transition == "reopen":
        response = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/submit",
            headers={"Authorization": f"Bearer {actor_token}"},
        )
        assert response.status_code == 200, response.text
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/claim",
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert response.status_code == 200, response.text
    await BatchService(db_session).update(
        batch.id,
        BatchUpdate(reviewer_id=pinned_reviewer.id),
        project_id=project.id,
    )
    await db_session.refresh(task)
    assert task.reviewer_id == pinned_reviewer.id


@pytest.mark.parametrize("sampling", ["sequential", "uniform", "uncertainty"])
async def test_prioritized_batch_precedes_assigned_unbatched_work(
    db_session, super_admin, annotator, sampling
):
    owner, _ = super_admin
    actor, _ = annotator
    project = await create_project(db_session, owner_id=owner.id)
    project.sampling = sampling
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=actor.id,
        role="annotator",
        assigned_by=owner.id,
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-PRIORITY-{uuid.uuid4().hex[:8]}",
        name="Urgent batch",
        status="active",
        priority=100,
        annotator_id=actor.id,
        created_by=owner.id,
    )
    db_session.add(batch)
    await db_session.flush()
    unbatched = await _task(
        db_session,
        project_id=project.id,
        assignee_id=actor.id,
        display_id=f"T-UNBATCHED-{uuid.uuid4().hex[:8]}",
    )
    prioritized = await _task(
        db_session,
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-PRIORITY-{uuid.uuid4().hex[:8]}",
    )
    result = await get_next_task(
        actor, project.id, db_session, project_role="annotator"
    )
    assert result is not None and result.id == prioritized.id
    prioritized.is_labeled = True
    await db_session.flush()
    result = await get_next_task(
        actor, project.id, db_session, project_role="annotator"
    )
    assert result is not None and result.id == unbatched.id
