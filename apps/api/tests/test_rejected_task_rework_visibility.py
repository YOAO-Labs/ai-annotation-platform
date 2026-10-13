"""A task-level rejection must remain reachable during batch review."""

from tests.factory import (
    create_membership,
    create_batch,
    create_project,
    create_task,
    create_user,
)


async def test_assignee_can_resume_rejected_task_while_peers_remain_in_review(
    db_session, httpx_client, annotator, reviewer, super_admin
):
    assignee, token = annotator
    review_user, review_token = reviewer
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id)
    # Literal employees: project roles are explicit, never derived from account role.
    for user, role in ((assignee, "annotator"), (review_user, "reviewer")):
        await create_membership(
            db_session, project_id=project.id, user_id=user.id, role=role
        )
    batch = await create_batch(db_session, project_id=project.id, status="reviewing")
    batch.annotator_id, batch.reviewer_id = assignee.id, review_user.id
    rejected = await create_task(db_session, project_id=project.id, status="rejected")
    peer = await create_task(db_session, project_id=project.id, status="review")
    for task in (rejected, peer):
        task.batch_id, task.assignee_id, task.reviewer_id = (
            batch.id,
            assignee.id,
            review_user.id,
        )
    await db_session.flush()
    await db_session.refresh(rejected)
    await db_session.refresh(peer)
    auth = {"Authorization": f"Bearer {token}"}
    route = f"/api/v1/tasks?project_id={project.id}&batch_id={batch.id}"
    listed = await httpx_client.get(route, headers=auth)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["items"]] == [str(rejected.id)]
    assert (
        await httpx_client.get(f"/api/v1/tasks/{rejected.id}", headers=auth)
    ).status_code == 200
    assert (
        await httpx_client.get(f"/api/v1/tasks/{peer.id}", headers=auth)
    ).status_code == 404
    resumed = await httpx_client.post(
        f"/api/v1/tasks/{rejected.id}/accept-rejection", headers=auth
    )
    assert resumed.status_code == 200, resumed.text
    await db_session.refresh(rejected)
    assert (
        await httpx_client.get(f"/api/v1/tasks/{rejected.id}", headers=auth)
    ).status_code == 200
    review_list = await httpx_client.get(
        route, headers={"Authorization": f"Bearer {review_token}"}
    )
    assert len(review_list.json()["items"]) == 2
    await db_session.refresh(batch)
    assert batch.status == "reviewing"

    outsider = await create_user(
        db_session, "employee", "rework-outsider@test.local", "Outsider"
    )
    await create_membership(
        db_session, project_id=project.id, user_id=outsider.id, role="annotator"
    )
    await db_session.flush()
    from app.core.security import create_access_token

    other_auth = {
        "Authorization": f"Bearer {create_access_token(subject=str(outsider.id), role=outsider.role)}"
    }
    assert (await httpx_client.get(route, headers=other_auth)).json()["items"] == []
    assert (
        await httpx_client.get(f"/api/v1/tasks/{rejected.id}", headers=other_auth)
    ).status_code == 404
