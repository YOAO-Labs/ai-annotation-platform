"""Frozen review-evidence guard on batch decisions (B2-owned regression).

Covers the single-batch ``transition`` and ``reject`` paths, the bulk
approve/reject summary shape, and a concurrent/stale batch row.  A successful
decision requires complete frozen non-self contributor evidence; unknown legacy
evidence (409) and self review (403) block the decision before any mutation.

Requires the isolated PostgreSQL test database.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from tests.factory import (
    create_membership,
    create_batch,
    create_project,
    create_task,
    create_user,
)

pytestmark = pytest.mark.asyncio


def _headers(user: User) -> dict[str, str]:
    token = create_access_token(subject=str(user.id), role=user.role)
    return {"Authorization": f"Bearer {token}"}


def _headers_for(user_id, role: str) -> dict[str, str]:
    token = create_access_token(subject=str(user_id), role=role)
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    db,
    owner: User,
    *,
    evidence: str,
    batch_status: str = "reviewing",
    batch_name: str = "evidence batch",
):
    """One reviewer membership, one batch and one review task.

    ``evidence="frozen"`` freezes a non-self round attributed to ``owner``;
    ``"unknown"`` leaves the legacy task without evidence; ``"self"`` freezes a
    round whose contributor is the acting reviewer.
    """
    reviewer = await create_user(
        db, "employee", f"batch-rev-{uuid.uuid4()}@test.local", "Reviewer"
    )
    project = await create_project(db, owner_id=owner.id, name="Batch evidence")
    await create_membership(
        db,
        project_id=project.id,
        user_id=reviewer.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    batch = await create_batch(db, project_id=project.id, status=batch_status)
    batch.name = batch_name
    task = await create_task(db, project_id=project.id, status="review")
    task.batch_id = batch.id
    task.reviewer_id = reviewer.id
    if evidence == "frozen":
        task.review_round_id = uuid.uuid4()
        task.annotation_contributor_ids = []
        task.review_contributor_ids = [str(owner.id)]
        task.review_submitter_id = owner.id
    elif evidence == "self":
        task.review_round_id = uuid.uuid4()
        task.annotation_contributor_ids = []
        task.review_contributor_ids = [str(reviewer.id)]
        task.review_submitter_id = reviewer.id
    await db.flush()
    return reviewer, project, batch, task


async def test_transition_to_approved_blocks_unknown_evidence(
    httpx_client: httpx.AsyncClient, super_admin, db_session
):
    owner, _ = super_admin
    reviewer, project, batch, _task = await _seed(db_session, owner, evidence="unknown")
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/batches/{batch.id}/transition",
        headers=_headers(reviewer),
        json={"target_status": "approved"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "review_contributors_unknown"
    await db_session.refresh(batch)
    assert batch.status == "reviewing"


async def test_transition_to_rejected_blocks_self_review(
    httpx_client: httpx.AsyncClient, super_admin, db_session
):
    owner, _ = super_admin
    reviewer, project, batch, _task = await _seed(db_session, owner, evidence="self")
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/batches/{batch.id}/transition",
        headers=_headers(reviewer),
        json={"target_status": "rejected"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == "self_review_denied"
    await db_session.refresh(batch)
    assert batch.status == "reviewing"


async def test_bulk_approve_preserves_per_batch_summary(
    httpx_client: httpx.AsyncClient, super_admin, db_session
):
    owner, _ = super_admin
    reviewer, project, good_batch, _good = await _seed(
        db_session, owner, evidence="frozen", batch_name="good"
    )
    # A second batch in the SAME project with unknown evidence.
    bad_batch = await create_batch(
        db_session, project_id=project.id, status="reviewing"
    )
    bad_batch.name = "bad"
    bad_task = await create_task(db_session, project_id=project.id, status="review")
    bad_task.batch_id = bad_batch.id
    bad_task.reviewer_id = reviewer.id
    await db_session.flush()
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/batches/bulk-approve",
        headers=_headers(reviewer),
        json={"batch_ids": [str(good_batch.id), str(bad_batch.id)]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [str(value) for value in body["succeeded"]] == [str(good_batch.id)]
    assert body["failed"] == [
        {"batch_id": str(bad_batch.id), "reason": "review_contributors_unknown"}
    ]
    await db_session.refresh(good_batch)
    await db_session.refresh(bad_batch)
    assert good_batch.status == "approved"
    assert bad_batch.status == "reviewing"


async def test_batch_decision_busy_returns_conflict(test_engine, app_module):
    """An independently committed row lock makes the decision return 409.

    The dataset is created and truly committed in its own session so two
    independent connections (the writer and the HTTP request) observe it.
    """
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        owner = await create_user(
            setup, "super_admin", f"busy-owner-{uuid.uuid4()}@test.local", "Owner"
        )
        reviewer = await create_user(
            setup, "employee", f"busy-rev-{uuid.uuid4()}@test.local", "Reviewer"
        )
        project = await create_project(setup, owner_id=owner.id, name="Busy decision")
        await create_membership(
            setup,
            project_id=project.id,
            user_id=reviewer.id,
            role="reviewer",
            assigned_by=owner.id,
        )
        batch = await create_batch(setup, project_id=project.id, status="reviewing")
        task = await create_task(setup, project_id=project.id, status="review")
        task.batch_id = batch.id
        task.reviewer_id = reviewer.id
        task.review_round_id = uuid.uuid4()
        task.annotation_contributor_ids = []
        task.review_contributor_ids = [str(owner.id)]
        task.review_submitter_id = owner.id
        await setup.commit()
        ids = {
            "project": project.id,
            "batch": batch.id,
            "task": task.id,
            "owner": owner.id,
            "reviewer": reviewer.id,
        }

    try:
        async with maker() as writer:
            # Lock a real, committed, visible row on a separate connection.
            locked = await writer.scalar(
                select(TaskBatch.id)
                .where(TaskBatch.id == ids["batch"])
                .with_for_update()
            )
            assert locked == ids["batch"]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/v1/projects/{ids['project']}/batches/{ids['batch']}/transition",
                    headers=_headers_for(ids["reviewer"], "employee"),
                    json={"target_status": "approved"},
                )
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["reason"] == "batch_decision_busy"
            await writer.rollback()
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Task).where(Task.id == ids["task"]))
            await cleanup.execute(delete(TaskBatch).where(TaskBatch.id == ids["batch"]))
            await cleanup.execute(
                delete(ProjectMember).where(ProjectMember.project_id == ids["project"])
            )
            await cleanup.execute(delete(Project).where(Project.id == ids["project"]))
            await cleanup.execute(
                delete(User).where(User.id.in_([ids["owner"], ids["reviewer"]]))
            )
            await cleanup.commit()
