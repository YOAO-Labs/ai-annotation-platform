"""Atomic handoff, credential retirement, and concurrent resource guards."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.deps import get_current_user, get_db
from app.services.api_key_service import create_key
from app.services.password_reset import PasswordResetService
from tests.factory import (
    create_membership,
    create_batch,
    create_project,
    create_task,
    create_user,
)


def headers(user):
    return {
        "Authorization": f"Bearer {create_access_token(subject=str(user.id), role=user.role)}"
    }


async def seed_handoff(db, actor, role="annotator", project_count=2):
    # Post-cutover staff are platform employees; the project role is carried by
    # the membership only.  Owner retains the administrative identity.
    account_role = "project_admin" if role == "owner" else "employee"
    target = await create_user(
        db, account_role, f"leaving-{uuid.uuid4()}@test.local", "Leaving"
    )
    receiver = await create_user(
        db, account_role, f"receiver-{uuid.uuid4()}@test.local", "Receiver"
    )
    projects, batches, tasks = [], [], []
    for _ in range(project_count):
        project = await create_project(
            db, owner_id=target.id if role == "owner" else actor.id
        )
        batch = await create_batch(
            db,
            project_id=project.id,
            status="reviewing" if role == "reviewer" else "active",
        )
        task = await create_task(
            db,
            project_id=project.id,
            status="review" if role == "reviewer" else "in_progress",
        )
        task.batch_id = batch.id
        if role != "owner":
            for user in (target, receiver):
                await create_membership(
                    db, project_id=project.id, user_id=user.id, role=role
                )
            setattr(batch, f"{role}_id", target.id)
            setattr(
                task, "assignee_id" if role == "annotator" else "reviewer_id", target.id
            )
        if role == "reviewer":
            # A reviewable post-cutover task carries frozen contributor evidence.
            # The outgoing reviewer submitted the round; the receiver is not a
            # contributor, so the handoff decision is not a self-review.
            task.review_round_id = uuid.uuid4()
            task.annotation_contributor_ids = []
            task.review_contributor_ids = [str(target.id)]
            task.review_submitter_id = target.id
        projects.append(project)
        batches.append(batch)
        tasks.append(task)
    await db.flush()
    return target, receiver, projects, batches, tasks


async def preview(client, actor, target):
    response = await client.get(
        f"/api/v1/users/{target.id}/offboarding-preview", headers=headers(actor)
    )
    assert response.status_code == 200, response.text
    return response.json()


def handoff_body(snapshot, receiver, role="annotator"):
    return {
        "mode": "handoff",
        "reason": "Personnel handoff",
        "preview_version": snapshot["preview_version"],
        "projects": [
            {"project_id": row["project_id"], f"{role}_receiver_id": str(receiver.id)}
            for row in snapshot["projects"]
        ],
    }


async def test_lock_without_assignment_is_previewed_and_released(
    httpx_client, db_session, super_admin
):
    actor, _ = super_admin
    target = await create_user(
        db_session, "project_admin", f"lock-{uuid.uuid4()}@test.local", "Lock holder"
    )
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id, status="completed")
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=target.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await db_session.flush()
    snapshot = await preview(httpx_client, actor, target)
    assert len(snapshot["projects"]) == 1
    assert snapshot["projects"][0]["locked_task_count"] == 1
    assert not any(
        role["present"] for role in snapshot["projects"][0]["roles"].values()
    )
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/offboarding",
        headers=headers(actor),
        json={
            "mode": "handoff",
            "reason": "End access",
            "projects": [],
            "preview_version": snapshot["preview_version"],
        },
    )
    assert response.status_code == 200, response.text
    assert (
        await db_session.scalar(select(TaskLock).where(TaskLock.user_id == target.id))
        is None
    )
    await db_session.refresh(task)
    assert task.status == "completed" and task.assignee_id is None


@pytest.mark.parametrize("account_status", ["inactive", "all"])
async def test_manager_cannot_list_unrelated_disabled_candidates(
    account_status, httpx_client, db_session, project_admin, super_admin
):
    manager, token = project_admin
    admin, _ = super_admin
    target, _, projects, _, _ = await seed_handoff(db_session, admin, project_count=1)
    target.is_active = False
    target.disabled_kind = "suspended"
    await db_session.flush()
    response = await httpx_client.get(
        f"/api/v1/users?role=employee&status={account_status}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert str(target.id) not in {row["id"] for row in response.json()}
    projects[0].owner_id = manager.id
    await db_session.flush()
    response = await httpx_client.get(
        f"/api/v1/users?role=employee&status={account_status}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert str(target.id) in {row["id"] for row in response.json()}


@pytest.mark.parametrize("operation", ["delete", "deactivate"])
async def test_legacy_lifecycle_cannot_overwrite_a_concurrent_deletion(
    operation, httpx_client, db_session, super_admin, annotator
):
    actor, token = super_admin
    target, _ = annotator
    await db_session.execute(
        update(User)
        .where(User.id == target.id)
        .values(
            is_active=False,
            disabled_kind="deleted",
            disabled_reason="Irreversible deletion",
        )
        .execution_options(synchronize_session=False)
    )
    assert target.is_active  # The request's identity map predates the deletion.
    path = f"/api/v1/users/{target.id}"
    auth = {"Authorization": f"Bearer {token}"}
    response = await (
        httpx_client.delete(path, headers=auth)
        if operation == "delete"
        else httpx_client.post(f"{path}/deactivate", headers=auth)
    )
    assert response.status_code == 200, response.text
    assert response.json()["disabled_kind"] == "deleted"
    assert response.json()["disabled_reason"] == "Irreversible deletion"


@pytest.mark.parametrize(
    "change", ["receiver_inactive", "membership_removed", "task_changed"]
)
async def test_preview_changes_never_partially_transfer(
    change, httpx_client, db_session, super_admin
):
    actor, _ = super_admin
    target, receiver, projects, batches, tasks = await seed_handoff(db_session, actor)
    snapshot = await preview(httpx_client, actor, target)
    if change == "receiver_inactive":
        receiver.is_active = False
    elif change == "membership_removed":
        await db_session.execute(
            delete(ProjectMember).where(
                ProjectMember.project_id == projects[-1].id,
                ProjectMember.user_id == receiver.id,
            )
        )
    else:
        tasks[-1].version += 1
    await db_session.flush()
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/offboarding",
        headers=headers(actor),
        json=handoff_body(snapshot, receiver),
    )
    assert response.status_code == 409, response.text
    assert target.is_active
    for batch, task in zip(batches, tasks):
        await db_session.refresh(batch)
        await db_session.refresh(task)
        assert batch.annotator_id == target.id
        assert task.assignee_id == target.id


async def test_emergency_handoff_reactivation_retires_old_credentials(
    httpx_client, db_session, super_admin
):
    actor, _ = super_admin
    target, receiver, projects, batches, tasks = await seed_handoff(db_session, actor)
    key, token = await create_key(db_session, target, "Export integration", ["*"])
    old_headers = headers(target)
    snapshot = await preview(httpx_client, actor, target)
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/offboarding",
        headers=headers(actor),
        json={
            "mode": "emergency_suspend",
            "preview_version": snapshot["preview_version"],
            "reason": "Urgent suspension",
            "projects": [],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["unresolved"]
    assert str(key.id) in response.json()["revoked_api_key_ids"]
    for credential in (old_headers, {"Authorization": f"Bearer {token}"}):
        assert (
            await httpx_client.get("/api/v1/auth/me", headers=credential)
        ).status_code == 401
    snapshot = await preview(httpx_client, actor, target)
    assert snapshot["can_commit"]
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/offboarding",
        headers=headers(actor),
        json=handoff_body(snapshot, receiver),
    )
    assert response.status_code == 200, response.text
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/reactivate",
        headers=headers(actor),
        json={"reason": "Returned"},
    )
    assert response.status_code == 200, response.text
    for credential in (old_headers, {"Authorization": f"Bearer {token}"}):
        assert (
            await httpx_client.get("/api/v1/auth/me", headers=credential)
        ).status_code == 401
    for batch, task in zip(batches, tasks):
        await db_session.refresh(batch)
        await db_session.refresh(task)
        assert batch.annotator_id == receiver.id
        assert task.assignee_id == receiver.id
    login = await httpx_client.post(
        "/api/v1/auth/login", json={"email": target.email, "password": "Test1234"}
    )
    assert login.status_code == 200, login.text


async def test_legacy_suspension_retires_keys_and_recovery_links(
    httpx_client, db_session, super_admin
):
    actor, _ = super_admin
    target, _, _, _, _ = await seed_handoff(db_session, actor)
    _, key = await create_key(db_session, target, "Old integration", ["*"])
    token = await PasswordResetService(db_session).create_token(target.email)
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/deactivate", headers=headers(actor)
    )
    assert response.status_code == 200, response.text
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/reactivate", headers=headers(actor)
    )
    assert response.status_code == 200, response.text
    assert (
        await httpx_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {key}"}
        )
    ).status_code == 401
    response = await httpx_client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": "ResetStrong1"},
    )
    assert response.status_code == 400, response.text


async def test_legacy_delete_requires_handoff_for_rejected_work(
    httpx_client, db_session, super_admin
):
    actor, _ = super_admin
    target, _, _, _, tasks = await seed_handoff(db_session, actor, project_count=1)
    tasks[0].status = "rejected"
    await db_session.flush()
    response = await httpx_client.delete(
        f"/api/v1/users/{target.id}", headers=headers(actor)
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["pending_task_count"] == 1
    await db_session.refresh(target)
    assert target.is_active


@pytest.mark.parametrize("operation", ["create", "update", "delete", "bulk_update"])
async def test_restored_employee_cannot_replay_into_handed_off_task(
    operation, httpx_client, db_session, super_admin
):
    from tests.test_task_lock import _create_annotation

    actor, _ = super_admin
    target, receiver, _, _, tasks = await seed_handoff(
        db_session, actor, project_count=1
    )
    task = tasks[0]
    annotation = await _create_annotation(db_session, task, target.id)
    snapshot = await preview(httpx_client, actor, target)
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/offboarding",
        headers=headers(actor),
        json=handoff_body(snapshot, receiver),
    )
    assert response.status_code == 200, response.text
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/reactivate", headers=headers(actor)
    )
    assert response.status_code == 200, response.text
    login = await httpx_client.post(
        "/api/v1/auth/login", json={"email": target.email, "password": "Test1234"}
    )
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    route = f"/api/v1/tasks/{task.id}/annotations"
    if operation == "create":
        response = await httpx_client.post(
            route,
            headers=auth,
            json={
                "annotation_type": "bbox",
                "class_name": "car",
                "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            },
        )
    elif operation == "update":
        response = await httpx_client.patch(
            f"{route}/{annotation.id}", headers=auth, json={"class_name": "person"}
        )
    elif operation == "delete":
        response = await httpx_client.delete(f"{route}/{annotation.id}", headers=auth)
    else:
        response = await httpx_client.post(
            "/api/v1/annotations/bulk-update",
            headers=auth,
            json={"ids": [str(annotation.id)], "patch": {"class_name": "person"}},
        )
    assert response.status_code in {403, 404}, response.text
    await db_session.refresh(annotation)
    await db_session.refresh(task)
    assert annotation.is_active and annotation.class_name == "car"
    assert task.assignee_id == receiver.id


@pytest.mark.parametrize("change", ["inactive", "demoted"])
async def test_reactivate_rechecks_actor_after_authentication(
    change, httpx_client, db_session, super_admin, app_module
):
    actor, _ = super_admin
    target, _, _, _, _ = await seed_handoff(db_session, actor)
    target.is_active = False
    target.disabled_kind = "suspended"
    stale_actor = SimpleNamespace(id=actor.id, role=actor.role, is_active=True)
    if change == "inactive":
        actor.is_active = False
    else:
        actor.role = "viewer"
    await db_session.flush()

    async def prior_actor():
        return stale_actor

    previous = app_module.dependency_overrides.get(get_current_user)
    app_module.dependency_overrides[get_current_user] = prior_actor
    try:
        response = await httpx_client.post(
            f"/api/v1/users/{target.id}/reactivate", headers=headers(stale_actor)
        )
        assert response.status_code == 403, response.text
        await db_session.refresh(target)
        assert not target.is_active
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_current_user, None)
        else:
            app_module.dependency_overrides[get_current_user] = previous


@pytest.mark.parametrize("role", ["owner", "reviewer", "annotator"])
async def test_each_role_can_handoff_two_projects_through_http(
    role, httpx_client, db_session, super_admin
):
    actor, _ = super_admin
    target, receiver, projects, batches, tasks = await seed_handoff(
        db_session, actor, role
    )
    snapshot = await preview(httpx_client, actor, target)
    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/offboarding",
        headers=headers(actor),
        json=handoff_body(snapshot, receiver, role),
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["transfers"]) == 2
    for project, batch, task in zip(projects, batches, tasks):
        await db_session.refresh(project)
        await db_session.refresh(batch)
        await db_session.refresh(task)
        if role == "owner":
            assert project.owner_id == receiver.id
        elif role == "reviewer":
            assert batch.reviewer_id == receiver.id
            assert task.reviewer_id == receiver.id
        else:
            assert batch.annotator_id == receiver.id
            assert task.assignee_id == receiver.id
        response = await httpx_client.get(
            f"/api/v1/projects/{project.id}", headers=headers(receiver)
        )
        assert response.status_code == 200, response.text
        if role == "owner":
            continued = await httpx_client.patch(
                f"/api/v1/projects/{project.id}",
                headers=headers(receiver),
                json={"name": "Receiver managed project"},
            )
            assert continued.status_code == 200, continued.text
        elif role == "reviewer":
            claimed = await httpx_client.post(
                f"/api/v1/tasks/{task.id}/review/claim", headers=headers(receiver)
            )
            assert claimed.status_code == 200, claimed.text
            continued = await httpx_client.post(
                f"/api/v1/tasks/{task.id}/review/approve", headers=headers(receiver)
            )
            assert continued.status_code == 200, continued.text
            await db_session.refresh(task)
            assert task.status == "completed"
        else:
            continued = await httpx_client.post(
                f"/api/v1/tasks/{task.id}/annotations",
                headers=headers(receiver),
                json={
                    "annotation_type": "bbox",
                    "class_name": "car",
                    "geometry": {
                        "type": "bbox",
                        "x": 0.1,
                        "y": 0.1,
                        "w": 0.2,
                        "h": 0.2,
                    },
                },
            )
            assert continued.status_code == 201, continued.text


async def test_busy_task_returns_conflict_then_handoff_succeeds(
    test_engine, app_module
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        actor = await create_user(
            setup, "super_admin", f"actor-{uuid.uuid4()}@test.local", "Actor"
        )
        target, receiver, projects, batches, tasks = await seed_handoff(
            setup, actor, project_count=1
        )
        await setup.commit()

    async def request_db():
        async with maker() as db:
            yield db

    previous = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = request_db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            snapshot = await preview(client, actor, target)
            async with maker() as writer:
                await writer.execute(
                    select(Task).where(Task.id == tasks[0].id).with_for_update()
                )
                response = await client.post(
                    f"/api/v1/users/{target.id}/offboarding",
                    headers=headers(actor),
                    json=handoff_body(snapshot, receiver),
                )
                assert response.status_code == 409, response.text
                assert response.json()["detail"]["code"] == "offboarding_busy"
                await writer.rollback()
            response = await client.post(
                f"/api/v1/users/{target.id}/offboarding",
                headers=headers(actor),
                json=handoff_body(snapshot, receiver),
            )
            assert response.status_code == 200, response.text
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous
        async with maker() as cleanup:
            await cleanup.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await cleanup.execute(
                delete(AuditLog).where(
                    AuditLog.actor_id.in_([actor.id, target.id, receiver.id])
                )
            )
            await cleanup.execute(
                delete(Task).where(Task.id.in_([task.id for task in tasks]))
            )
            await cleanup.execute(
                delete(TaskBatch).where(
                    TaskBatch.id.in_([batch.id for batch in batches])
                )
            )
            await cleanup.execute(
                delete(Project).where(Project.id.in_([p.id for p in projects]))
            )
            await cleanup.execute(
                delete(User).where(User.id.in_([actor.id, target.id, receiver.id]))
            )
            await cleanup.commit()
