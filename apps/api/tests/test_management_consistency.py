"""Exercise reviewed plans against actual task rows and intervening changes."""

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import select
from app.db.models.task_batch import TaskBatch
from app.db.models.user_invitation import UserInvitation
from tests.factory import create_membership, create_project, create_user, create_task

pytestmark = pytest.mark.asyncio


async def test_distribution_counts_real_tasks_and_applies_identical_tied_order(
    httpx_client, super_admin, db_session
):
    admin, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    project = await create_project(db_session, owner_id=admin.id)
    users = [
        await create_user(db_session, "employee", f"load-{i}@e.test", f"Worker {i}")
        for i in range(2)
    ]
    for user in users:
        await create_membership(
            db_session,
            project_id=project.id,
            user_id=user.id,
            role="annotator",
            assigned_by=admin.id,
        )
    timestamp = datetime.now(timezone.utc)
    batches = []
    # Insert opposite to UUID order; priority and timestamps intentionally tie.
    for index in (2, 1):
        batch = TaskBatch(
            id=uuid.UUID(int=index),
            project_id=project.id,
            display_id=f"B-LOAD-{index}",
            name=f"Batch {index}",
            status="draft",
            created_at=timestamp,
            total_tasks=900,
        )
        db_session.add(batch)
        await db_session.flush()
        for status in ("pending", "in_progress", "completed"):
            task = await create_task(db_session, project_id=project.id, status=status)
            task.batch_id = batch.id
        batches.append(batch)
    backlog = await create_task(db_session, project_id=project.id, status="rejected")
    backlog.assignee_id = users[0].id
    await db_session.flush()
    body = {"annotator_ids": [str(u.id) for u in users], "only_unassigned": True}
    url = f"/api/v1/projects/{project.id}/batches"
    preview = await httpx_client.post(
        f"{url}/distribution-preview", json=body, headers=headers
    )
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert [row["batch_id"] for row in plan["items"]] == [
        str(uuid.UUID(int=i)) for i in (1, 2)
    ]
    assert all(row["task_count"] == 3 for row in plan["items"])
    assert [
        (r["new_task_count"], r["existing_backlog_count"])
        for r in plan["recipient_summary"]
    ] == [(2, 1), (2, 0)]
    applied = await httpx_client.post(
        f"{url}/distribution-apply",
        json={**body, "preview_version": plan["preview_version"]},
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["annotator_per_batch"] == {
        row["batch_id"]: row["after_annotator_id"] for row in plan["items"]
    }


async def test_distribution_rejects_stale_preview_without_changing_assignment(
    httpx_client, super_admin, db_session
):
    admin, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    project = await create_project(db_session, owner_id=admin.id)
    user = await create_user(db_session, "employee", "stale-plan@e.test", "Worker")
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=user.id,
        role="annotator",
        assigned_by=admin.id,
    )
    batch = TaskBatch(
        project_id=project.id, display_id="B-STALE-PLAN", name="Plan", status="draft"
    )
    db_session.add(batch)
    await db_session.flush()
    body = {"annotator_ids": [str(user.id)]}
    url = f"/api/v1/projects/{project.id}/batches"
    preview = await httpx_client.post(
        f"{url}/distribution-preview", json=body, headers=headers
    )
    assert preview.status_code == 200, preview.text
    task = await create_task(db_session, project_id=project.id)
    task.batch_id = batch.id
    await db_session.flush()
    failed = await httpx_client.post(
        f"{url}/distribution-apply",
        json={**body, "preview_version": preview.json()["preview_version"]},
        headers=headers,
    )
    assert failed.status_code == 409, failed.text
    await db_session.refresh(batch)
    assert batch.annotator_id is None
    fresh = await httpx_client.post(
        f"{url}/distribution-preview", json=body, headers=headers
    )
    assert fresh.json()["preview_version"] != preview.json()["preview_version"]


async def test_bulk_invite_failed_middle_item_does_not_break_following_or_leave_preview_rows(
    httpx_client, super_admin, db_session
):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    existing = await create_user(
        db_session, "employee", "bulk-exists@e.test", "Existing"
    )
    body = {
        "items": [
            {"email": email, "role": "employee"}
            for email in ("bulk-first@e.test", existing.email, "bulk-last@e.test")
        ]
    }
    for preview in (True, False):
        result = await httpx_client.post(
            "/api/v1/users/bulk-invite" + ("/preview" if preview else ""),
            json=body,
            headers=headers,
        )
        assert result.status_code == 200, result.text
        assert [item["ok"] for item in result.json()["items"]] == [True, False, True], (
            result.text
        )
        rows = (
            (
                await db_session.execute(
                    select(UserInvitation).where(
                        UserInvitation.email.in_(
                            ["bulk-first@e.test", "bulk-last@e.test"]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == (0 if preview else 2)


async def test_management_previews_do_not_reveal_out_of_scope_users(
    httpx_client, project_admin, db_session
):
    _, token = project_admin
    headers = {"Authorization": f"Bearer {token}"}
    # Viewers stay outside a project admin's visibility and manage scope
    # (enabled annotators/reviewers are operable since the #115 revision).
    outside = await create_user(
        db_session, "viewer", "secret-member@e.test", "Hidden Person"
    )
    group = await httpx_client.post(
        "/api/v1/users/groups/bulk/preview",
        json={"user_ids": [str(outside.id)], "group_id": None},
        headers=headers,
    )
    assert group.status_code == 200, group.text
    item = group.json()["items"][0]
    assert item["ok"] is False
    assert item["email"] is None and item["name"] is None
    # Platform-role preview is super-admin only; a project administrator is
    # denied before any out-of-scope detail is disclosed.
    impact = await httpx_client.get(
        f"/api/v1/users/{outside.id}/role/preview?role=employee", headers=headers
    )
    assert impact.status_code == 403, impact.text
    assert outside.email not in impact.text and outside.name not in impact.text


async def test_role_preview_is_super_admin_only_and_hides_project_names(
    httpx_client, project_admin, super_admin, db_session
):
    manager, token = project_admin
    admin, admin_token = super_admin
    user = await create_user(db_session, "employee", "shared-role@e.test", "Shared")
    for owner, name in ((manager, "Visible"), (admin, "Private project name")):
        project = await create_project(db_session, owner_id=owner.id, name=name)
        await create_membership(
            db_session,
            project_id=project.id,
            user_id=user.id,
            role="annotator",
            assigned_by=owner.id,
        )
    await db_session.flush()

    denied = await httpx_client.get(
        f"/api/v1/users/{user.id}/role/preview?role=employee",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 403, denied.text
    assert "Private project name" not in denied.text

    allowed = await httpx_client.get(
        f"/api/v1/users/{user.id}/role/preview?role=employee",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert allowed.status_code == 200, allowed.text
    # A super administrator legitimately sees every project; only the denied
    # project administrator response must not disclose it.
    assert "Private project name" in allowed.text


async def test_management_csv_treats_user_and_invitation_fields_as_literal_text(
    httpx_client, super_admin, db_session
):
    import csv
    from datetime import timedelta

    admin, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    await create_user(db_session, "employee", "csv-injection@e.test", "=1+1")
    db_session.add(
        UserInvitation(
            email="csv-invite@e.test",
            role="employee",
            group_name="@SUM(1,2)",
            token="csv-test-token",
            invited_by=admin.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    )
    await db_session.flush()
    for endpoint, field, expected in (
        ("users", "name", "'=1+1"),
        ("invitations", "group_name", "'@SUM(1,2)"),
    ):
        result = await httpx_client.get(
            f"/api/v1/{endpoint}/export?format=csv&search=csv-", headers=headers
        )
        assert result.status_code == 200, result.text
        lines = [
            line
            for line in result.text.lstrip("\ufeff").splitlines()
            if not line.startswith("#")
        ]
        rows = list(csv.DictReader(lines))
        assert len(rows) == 1
        assert rows[0][field] == expected


async def test_bulk_invite_validation_reports_invalid_email_per_item(
    httpx_client, super_admin
):
    _, token = super_admin
    result = await httpx_client.post(
        "/api/v1/users/bulk-invite/preview",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "items": [
                {"email": "valid-bulk@e.test", "role": "employee"},
                {"email": "invalid", "role": "employee"},
                {"email": " VALID-BULK@e.test ", "role": "employee"},
            ]
        },
    )
    assert result.status_code == 200, result.text
    assert [row["ok"] for row in result.json()["items"]] == [True, False, False]
    assert "邮箱格式不正确" in result.json()["items"][1]["error"]
    assert "重复" in result.json()["items"][2]["error"]


async def test_single_batch_preview_counts_review_backlog_and_applies_exact_mapping(
    httpx_client, super_admin, db_session
):
    admin, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    project = await create_project(db_session, owner_id=admin.id)
    anno = await create_user(db_session, "employee", "single-anno@e.test", "Annotator")
    review = await create_user(
        db_session, "employee", "single-review@e.test", "Reviewer"
    )
    for user, project_role in ((anno, "annotator"), (review, "reviewer")):
        await create_membership(
            db_session,
            project_id=project.id,
            user_id=user.id,
            role=project_role,
            assigned_by=admin.id,
        )
    batch = TaskBatch(
        project_id=project.id,
        display_id="B-SINGLE",
        name="Single",
        status="draft",
        total_tasks=100,
    )
    db_session.add(batch)
    await db_session.flush()
    tasks = []
    for status in ("pending", "review", "completed"):
        task = await create_task(db_session, project_id=project.id, status=status)
        task.batch_id = batch.id
        tasks.append(task)
    reviewed = await create_task(db_session, project_id=project.id, status="completed")
    reviewed.reviewer_id = review.id
    await db_session.flush()
    url = f"/api/v1/projects/{project.id}/batches/{batch.id}"
    body = {"annotator_id": str(anno.id), "reviewer_id": str(review.id)}
    response = await httpx_client.post(
        f"{url}/assignment-preview", headers=headers, json=body
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["items"][0]["task_count"] == 3
    assert [
        (row["new_task_count"], row["existing_backlog_count"])
        for row in preview["recipient_summary"]
    ] == [(1, 0), (1, 0)]
    response = await httpx_client.post(
        f"{url}/assignment-apply",
        headers=headers,
        json={**body, "preview_version": preview["preview_version"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["annotator_id"] == str(anno.id)
    assert response.json()["reviewer_id"] == str(review.id)
    for task in tasks:
        await db_session.refresh(task)
        assert task.assignee_id == anno.id
        assert task.reviewer_id == review.id
    clear = {"annotator_id": None, "reviewer_id": None}
    preview = (
        await httpx_client.post(
            f"{url}/assignment-preview", headers=headers, json=clear
        )
    ).json()
    assert preview["items"][0]["before_annotator_id"] == str(anno.id)
    assert preview["items"][0]["after_annotator_id"] is None
    response = await httpx_client.post(
        f"{url}/assignment-apply",
        headers=headers,
        json={**clear, "preview_version": preview["preview_version"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["annotator_id"] is response.json()["reviewer_id"] is None


async def test_single_batch_apply_refuses_stale_preview_and_other_project_batch(
    httpx_client, super_admin, db_session
):
    admin, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    project = await create_project(db_session, owner_id=admin.id)
    other = await create_project(db_session, owner_id=admin.id)
    batch = TaskBatch(
        project_id=project.id,
        display_id="B-STALE-SINGLE",
        name="Original",
        status="draft",
    )
    db_session.add(batch)
    await db_session.flush()
    body = {"annotator_id": None, "reviewer_id": None}
    url = f"/api/v1/projects/{project.id}/batches/{batch.id}"
    preview = (
        await httpx_client.post(f"{url}/assignment-preview", headers=headers, json=body)
    ).json()
    batch.name = "Changed by another manager"
    await db_session.flush()
    result = await httpx_client.post(
        f"{url}/assignment-apply",
        headers=headers,
        json={**body, "preview_version": preview["preview_version"]},
    )
    assert result.status_code == 409, result.text
    assert batch.annotator_id is batch.reviewer_id is None
    result = await httpx_client.post(
        f"/api/v1/projects/{other.id}/batches/{batch.id}/assignment-preview",
        headers=headers,
        json=body,
    )
    assert result.status_code in (400, 404)


async def test_distribution_can_target_only_selected_batches(
    httpx_client, super_admin, db_session
):
    admin, token = super_admin
    project = await create_project(db_session, owner_id=admin.id)
    user = await create_user(
        db_session, "employee", "selected-batches@e.test", "Worker"
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=user.id,
        role="annotator",
        assigned_by=admin.id,
    )
    batches = [
        TaskBatch(
            project_id=project.id,
            display_id=f"B-SUB-{i}",
            name=f"Batch {i}",
            status="draft",
        )
        for i in range(2)
    ]
    db_session.add_all(batches)
    await db_session.flush()
    body = {"batch_ids": [str(batches[1].id)], "annotator_ids": [str(user.id)]}
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/projects/{project.id}/batches"
    result = await httpx_client.post(
        f"{url}/distribution-preview", json=body, headers=headers
    )
    assert result.status_code == 200, result.text
    preview = result.json()
    assert [row["batch_id"] for row in preview["items"]] == [str(batches[1].id)]
    result = await httpx_client.post(
        f"{url}/distribution-apply",
        json={**body, "preview_version": preview["preview_version"]},
        headers=headers,
    )
    assert result.status_code == 200, result.text
    assert batches[0].annotator_id is None
    assert batches[1].annotator_id == user.id
    result = await httpx_client.post(
        f"{url}/distribution-preview",
        json={**body, "batch_ids": [str(uuid.uuid4())]},
        headers=headers,
    )
    assert result.status_code == 409
