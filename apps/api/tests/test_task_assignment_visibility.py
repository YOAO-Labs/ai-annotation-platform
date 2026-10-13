"""Task-level assignment must control the annotator workbench scope."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from tests.factory import create_membership, create_project, create_user


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _task(
    db,
    *,
    project_id: uuid.UUID,
    display_id: str,
    batch_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
) -> Task:
    task = Task(
        project_id=project_id,
        batch_id=batch_id,
        assignee_id=assignee_id,
        display_id=display_id,
        file_name=f"{display_id}.jpg",
        file_path=f"items/{display_id}.jpg",
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return task


async def _apply_annotator_assignment(
    client,
    *,
    project_id: uuid.UUID,
    owner_token: str,
    task_ids: list[uuid.UUID],
    annotator_id: uuid.UUID,
) -> dict:
    payload = {
        "task_ids": [str(task_id) for task_id in task_ids],
        "annotator_id": str(annotator_id),
    }
    preview = await client.post(
        f"/api/v1/projects/{project_id}/data-manager/tasks/assignment-preview",
        headers=_bearer(owner_token),
        json=payload,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible_count"] == len(task_ids)

    apply_payload = {**payload, "preview_version": preview.json()["preview_version"]}
    applied = await client.post(
        f"/api/v1/projects/{project_id}/data-manager/tasks/assignment-apply",
        headers=_bearer(owner_token),
        json=apply_payload,
    )
    assert applied.status_code == 200, applied.text
    assert set(applied.json()["succeeded"]) == {str(task_id) for task_id in task_ids}
    return applied.json()


async def _apply_reviewer_assignment(
    client,
    *,
    project_id: uuid.UUID,
    owner_token: str,
    task_id: uuid.UUID,
    reviewer_id: uuid.UUID | None,
) -> dict:
    payload = {
        "task_ids": [str(task_id)],
        "reviewer_id": str(reviewer_id) if reviewer_id is not None else None,
    }
    preview = await client.post(
        f"/api/v1/projects/{project_id}/data-manager/tasks/assignment-preview",
        headers=_bearer(owner_token),
        json=payload,
    )
    assert preview.status_code == 200, preview.text
    apply_payload = {**payload, "preview_version": preview.json()["preview_version"]}
    applied = await client.post(
        f"/api/v1/projects/{project_id}/data-manager/tasks/assignment-apply",
        headers=_bearer(owner_token),
        json=apply_payload,
    )
    assert applied.status_code == 200, applied.text
    return applied.json()


@pytest.mark.asyncio
async def test_task_assignment_scopes_batch_query_get_and_next(
    httpx_client, db_session, super_admin, annotator
):
    owner, owner_token = super_admin
    batch_assignee, batch_assignee_token = annotator
    target = await create_user(
        db_session,
        "employee",
        f"task-scope-target-{uuid.uuid4().hex[:8]}@test.local",
        "Task Scope Target",
    )
    target_token = create_access_token(subject=str(target.id), role=target.role)

    project = await create_project(db_session, owner_id=owner.id)
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=batch_assignee.id,
                role="annotator",
                assigned_by=owner.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=owner.id,
            ),
        ]
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-TASK-SCOPE-{uuid.uuid4().hex[:8]}",
        name="task scope",
        status="active",
        annotator_id=batch_assignee.id,
    )
    db_session.add(batch)
    await db_session.flush()
    selected = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-TASK-SELECTED-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    sibling = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-TASK-SIBLING-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    await db_session.commit()

    assignment = await _apply_annotator_assignment(
        httpx_client,
        project_id=project.id,
        owner_token=owner_token,
        task_ids=[selected.id],
        annotator_id=target.id,
    )
    assignment_item = assignment["items"][0]
    assert assignment_item["effective_before_annotator_id"] == str(batch_assignee.id)
    assert assignment_item["effective_after_annotator_id"] == str(target.id)

    target_list = await httpx_client.get(
        f"/api/v1/tasks?project_id={project.id}&limit=200",
        headers=_bearer(target_token),
    )
    assert target_list.status_code == 200, target_list.text
    assert {item["id"] for item in target_list.json()["items"]} == {str(selected.id)}
    assert target_list.json()["total"] == 1

    target_query = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=_bearer(target_token),
        json={"filter_json": {}},
    )
    assert target_query.status_code == 200, target_query.text
    assert {item["id"] for item in target_query.json()["items"]} == {str(selected.id)}
    assert target_query.json()["total"] == 1
    assert target_query.json()["items"][0]["effective_assignee"]["id"] == str(target.id)

    # The owner sees batch defaults in both the table projection and filters,
    # while the physical task override remains null for untouched siblings.
    batch_scope = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=_bearer(owner_token),
        json={
            "filter_json": {
                "field": "task.assignee",
                "op": "eq",
                "value": str(batch_assignee.id),
            }
        },
    )
    assert batch_scope.status_code == 200, batch_scope.text
    assert batch_scope.json()["total"] == 1
    projected = batch_scope.json()["items"][0]
    assert projected["id"] == str(sibling.id)
    assert projected["assignee_id"] is None
    assert projected["effective_assignee"]["id"] == str(batch_assignee.id)

    assert (
        await httpx_client.get(
            f"/api/v1/tasks/{selected.id}", headers=_bearer(target_token)
        )
    ).status_code == 200
    assert (
        await httpx_client.get(
            f"/api/v1/tasks/{sibling.id}", headers=_bearer(target_token)
        )
    ).status_code == 404

    claimed = await httpx_client.get(
        f"/api/v1/tasks/next?project_id={project.id}&batch_id={batch.id}",
        headers=_bearer(target_token),
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["id"] == str(selected.id)

    # The old batch assignee cannot claim the reassigned task. Marking the
    # untouched sibling labeled makes a None result prove the selected task
    # itself was excluded from the candidate query.
    sibling.is_labeled = True
    await db_session.flush()
    old_assignee_next = await httpx_client.get(
        f"/api/v1/tasks/next?project_id={project.id}&batch_id={batch.id}",
        headers=_bearer(batch_assignee_token),
    )
    assert old_assignee_next.status_code == 200, old_assignee_next.text
    assert old_assignee_next.json() is None
    assert (
        await httpx_client.post(
            f"/api/v1/tasks/{selected.id}/lock",
            headers=_bearer(batch_assignee_token),
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_explicitly_assigned_unbatched_task_is_visible_and_claimable(
    httpx_client, db_session, super_admin, annotator
):
    owner, owner_token = super_admin
    old_assignee, old_assignee_token = annotator
    target = await create_user(
        db_session,
        "employee",
        f"unbatched-target-{uuid.uuid4().hex[:8]}@test.local",
        "Unbatched Target",
    )
    target_token = create_access_token(subject=str(target.id), role=target.role)
    project = await create_project(db_session, owner_id=owner.id)
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=old_assignee.id,
                role="annotator",
                assigned_by=owner.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=owner.id,
            ),
        ]
    )
    task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-TASK-UNBATCHED-{uuid.uuid4().hex[:8]}",
    )
    await db_session.commit()

    await _apply_annotator_assignment(
        httpx_client,
        project_id=project.id,
        owner_token=owner_token,
        task_ids=[task.id],
        annotator_id=target.id,
    )

    listed = await httpx_client.get(
        f"/api/v1/tasks?project_id={project.id}&unbatched=true&limit=200",
        headers=_bearer(target_token),
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [str(task.id)]
    query = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=_bearer(target_token),
        json={"filter_json": {}},
    )
    assert query.status_code == 200, query.text
    assert [item["id"] for item in query.json()["items"]] == [str(task.id)]
    assert (
        await httpx_client.get(
            f"/api/v1/tasks/{task.id}", headers=_bearer(target_token)
        )
    ).status_code == 200

    claimed = await httpx_client.get(
        f"/api/v1/tasks/next?project_id={project.id}",
        headers=_bearer(target_token),
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["id"] == str(task.id)
    old_assignee_next = await httpx_client.get(
        f"/api/v1/tasks/next?project_id={project.id}",
        headers=_bearer(old_assignee_token),
    )
    assert old_assignee_next.status_code == 200, old_assignee_next.text
    assert old_assignee_next.json() is None


@pytest.mark.asyncio
async def test_removed_member_cannot_reuse_assigned_task_url_or_write(
    httpx_client, db_session, super_admin, annotator
):
    owner, owner_token = super_admin
    member, member_token = annotator
    project = await create_project(db_session, owner_id=owner.id)
    membership = ProjectMember(
        project_id=project.id,
        user_id=member.id,
        role="annotator",
        assigned_by=owner.id,
    )
    db_session.add(membership)
    task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-REMOVED-MEMBER-{uuid.uuid4().hex[:8]}",
    )
    await db_session.commit()

    await _apply_annotator_assignment(
        httpx_client,
        project_id=project.id,
        owner_token=owner_token,
        task_ids=[task.id],
        annotator_id=member.id,
    )
    await db_session.refresh(task)
    member_headers = _bearer(member_token)
    assert (
        await httpx_client.get(f"/api/v1/tasks/{task.id}", headers=member_headers)
    ).status_code == 200

    # Member removal is blocked while unfinished work remains, so hand the task
    # off to a second annotator first; the removed account must still lose read
    # and write access to its former URL.
    receiver = await create_user(
        db_session,
        "employee",
        f"removed-receiver-{uuid.uuid4().hex[:8]}@test.local",
        "Removed Receiver",
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=receiver.id,
        role="annotator",
        assigned_by=owner.id,
    )
    await db_session.flush()
    await _apply_annotator_assignment(
        httpx_client,
        project_id=project.id,
        owner_token=owner_token,
        task_ids=[task.id],
        annotator_id=receiver.id,
    )
    removed = await httpx_client.delete(
        f"/api/v1/projects/{project.id}/members/{membership.id}",
        headers=_bearer(owner_token),
    )
    assert removed.status_code == 204, removed.text
    await db_session.refresh(task)
    assert task.assignee_id == receiver.id

    assert (
        await httpx_client.get(f"/api/v1/tasks/{task.id}", headers=member_headers)
    ).status_code == 404
    denied_write = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        headers=member_headers,
        json={
            "annotation_type": "bbox",
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
    )
    assert denied_write.status_code == 404, denied_write.text


@pytest.mark.parametrize(
    ("endpoint", "status"),
    [("withdraw", "review"), ("reopen", "completed"), ("accept-rejection", "rejected")],
)
@pytest.mark.asyncio
async def test_batch_fallback_controls_lifecycle_ownership(
    endpoint, status, httpx_client, db_session, super_admin, annotator
):
    owner, _ = super_admin
    member, member_token = annotator
    project = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=member.id,
        role="annotator",
        assigned_by=owner.id,
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-FALLBACK-{uuid.uuid4().hex[:8]}",
        name="effective assignment fallback",
        status="active",
        annotator_id=member.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-FALLBACK-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    task.status = status
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/{endpoint}", headers=_bearer(member_token)
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_batch_fallback_prevents_reviewer_from_submitting_for_annotator(
    httpx_client, db_session, super_admin, annotator, reviewer
):
    owner, _ = super_admin
    member, member_token = annotator
    review_user, review_token = reviewer
    project = await create_project(db_session, owner_id=owner.id)
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=member.id,
                role="annotator",
                assigned_by=owner.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=review_user.id,
                role="reviewer",
                assigned_by=owner.id,
            ),
        ]
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-SUBMIT-FALLBACK-{uuid.uuid4().hex[:8]}",
        name="submit fallback",
        status="active",
        annotator_id=member.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-SUBMIT-FALLBACK-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    task.status = "in_progress"
    await db_session.commit()

    reviewer_submit = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/submit", headers=_bearer(review_token)
    )
    assert reviewer_submit.status_code == 403, reviewer_submit.text
    await db_session.refresh(task)
    assert task.status == "in_progress"

    annotator_submit = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/submit", headers=_bearer(member_token)
    )
    assert annotator_submit.status_code == 200, annotator_submit.text


@pytest.mark.asyncio
async def test_batch_fallback_assignee_can_take_over_task_lock(
    httpx_client, db_session, super_admin, annotator, reviewer
):
    owner, _ = super_admin
    member, member_token = annotator
    other, _ = reviewer
    project = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=member.id,
        role="annotator",
        assigned_by=owner.id,
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-LOCK-FALLBACK-{uuid.uuid4().hex[:8]}",
        name="lock fallback",
        status="active",
        annotator_id=member.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-LOCK-FALLBACK-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=other.id,
            expire_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/lock", headers=_bearer(member_token)
    )
    assert response.status_code == 200, response.text
    locks = list(
        (await db_session.execute(select(TaskLock).where(TaskLock.task_id == task.id)))
        .scalars()
        .all()
    )
    assert [lock.user_id for lock in locks] == [member.id]


@pytest.mark.asyncio
async def test_reviewer_assignment_reserves_review_claim_and_can_be_cleared(
    httpx_client, db_session, super_admin, reviewer
):
    owner, owner_token = super_admin
    first_reviewer, _ = reviewer
    reserved_reviewer = await create_user(
        db_session,
        "employee",
        f"reserved-reviewer-{uuid.uuid4().hex[:8]}@test.local",
        "Reserved Reviewer",
    )
    reserved_token = create_access_token(
        subject=str(reserved_reviewer.id), role=reserved_reviewer.role
    )
    first_token = create_access_token(
        subject=str(first_reviewer.id), role=first_reviewer.role
    )
    project = await create_project(db_session, owner_id=owner.id)
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=first_reviewer.id,
                role="reviewer",
                assigned_by=owner.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=reserved_reviewer.id,
                role="reviewer",
                assigned_by=owner.id,
            ),
        ]
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-REVIEW-SCOPE-{uuid.uuid4().hex[:8]}",
        name="review scope",
        status="reviewing",
        reviewer_id=first_reviewer.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-REVIEW-SCOPE-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    task.status = "review"
    pool_task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-REVIEW-POOL-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    pool_task.status = "review"
    pending_task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-REVIEW-PENDING-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    # Frozen round evidence names a distinct submitter so the reviewer claims
    # reach the assignment/reservation checks instead of failing closed unknown.
    for review_task in (task, pool_task):
        review_task.annotation_contributor_ids = [str(owner.id)]
        review_task.review_contributor_ids = [str(owner.id)]
        review_task.review_submitter_id = owner.id
        review_task.review_round_id = uuid.uuid4()
    await db_session.commit()

    reserved = await _apply_reviewer_assignment(
        httpx_client,
        project_id=project.id,
        owner_token=owner_token,
        task_id=task.id,
        reviewer_id=reserved_reviewer.id,
    )
    assert reserved["succeeded"] == [str(task.id)]
    reserved_item = reserved["items"][0]
    assert reserved_item["effective_before_reviewer_id"] == str(first_reviewer.id)
    assert reserved_item["effective_after_reviewer_id"] == str(reserved_reviewer.id)

    other_claim = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/claim",
        headers=_bearer(first_token),
    )
    assert other_claim.status_code == 409, other_claim.text
    assert other_claim.json()["detail"]["reason"] == "task_review_assigned_to_other"
    own_claim = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/claim",
        headers=_bearer(reserved_token),
    )
    assert own_claim.status_code == 200, own_claim.text
    assert own_claim.json()["reviewer_id"] == str(reserved_reviewer.id)

    cleared = await _apply_reviewer_assignment(
        httpx_client,
        project_id=project.id,
        owner_token=owner_token,
        task_id=task.id,
        reviewer_id=None,
    )
    assert cleared["succeeded"] == [str(task.id)]
    assert cleared["items"][0]["effective_after_reviewer_id"] == str(first_reviewer.id)
    reopened_claim = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/claim",
        headers=_bearer(first_token),
    )
    assert reopened_claim.status_code == 200, reopened_claim.text
    assert reopened_claim.json()["reviewer_id"] == str(first_reviewer.id)

    pool_claim = await httpx_client.post(
        f"/api/v1/tasks/{pool_task.id}/review/claim",
        headers=_bearer(first_token),
    )
    assert pool_claim.status_code == 200, pool_claim.text
    pending_preview = await httpx_client.post(
        f"/api/v1/projects/{project.id}/data-manager/tasks/assignment-preview",
        headers=_bearer(owner_token),
        json={
            "task_ids": [str(pending_task.id)],
            "reviewer_id": str(reserved_reviewer.id),
        },
    )
    assert pending_preview.status_code == 200, pending_preview.text
    assert pending_preview.json()["eligible_count"] == 0
    assert (
        pending_preview.json()["items"][0]["reason"]
        == "reviewer_assignment_requires_review"
    )


@pytest.mark.asyncio
async def test_unbatched_reviewer_assignment_is_visible_and_claimable(
    httpx_client, db_session, super_admin, reviewer
):
    owner, owner_token = super_admin
    other_reviewer, other_token = reviewer
    assigned_reviewer = await create_user(
        db_session,
        "employee",
        f"unbatched-reviewer-{uuid.uuid4().hex[:8]}@test.local",
        "Unbatched Reviewer",
    )
    assigned_token = create_access_token(
        subject=str(assigned_reviewer.id), role=assigned_reviewer.role
    )
    project = await create_project(db_session, owner_id=owner.id)
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=other_reviewer.id,
                role="reviewer",
                assigned_by=owner.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=assigned_reviewer.id,
                role="reviewer",
                assigned_by=owner.id,
            ),
        ]
    )
    task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-UNBATCHED-REVIEW-{uuid.uuid4().hex[:8]}",
    )
    task.status = "review"
    # Frozen round evidence from a distinct submitter so the claim is authorized.
    task.annotation_contributor_ids = [str(owner.id)]
    task.review_contributor_ids = [str(owner.id)]
    task.review_submitter_id = owner.id
    task.review_round_id = uuid.uuid4()
    await db_session.commit()

    assigned = await _apply_reviewer_assignment(
        httpx_client,
        project_id=project.id,
        owner_token=owner_token,
        task_id=task.id,
        reviewer_id=assigned_reviewer.id,
    )
    assert assigned["succeeded"] == [str(task.id)]

    query = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=_bearer(assigned_token),
        json={"filter_json": {}},
    )
    assert query.status_code == 200, query.text
    assert [item["id"] for item in query.json()["items"]] == [str(task.id)]
    assert (
        await httpx_client.get(
            f"/api/v1/tasks/{task.id}", headers=_bearer(assigned_token)
        )
    ).status_code == 200

    other_query = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=_bearer(other_token),
        json={"filter_json": {}},
    )
    assert other_query.status_code == 200, other_query.text
    assert other_query.json()["items"] == []
    assert (
        await httpx_client.get(f"/api/v1/tasks/{task.id}", headers=_bearer(other_token))
    ).status_code == 404
    other_claim = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/claim",
        headers=_bearer(other_token),
    )
    assert other_claim.status_code == 404, other_claim.text

    claim = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/claim",
        headers=_bearer(assigned_token),
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["reviewer_id"] == str(assigned_reviewer.id)
