"""Audit project authority applies before counts, pagination and row serialization."""

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.user_invitation import UserInvitation
from app.schemas.audit import AuditLogOut
from tests.test_video_chapters_api import _video_item_with_task
from tests.factory import create_batch, create_project, create_task, create_user

pytestmark = pytest.mark.asyncio


async def test_project_admin_audit_scope_and_superadmin_global_access(
    db_session, httpx_client, super_admin
):
    _, super_token = super_admin
    owner = await create_user(
        db_session, "project_admin", f"audit-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    other = await create_user(
        db_session, "project_admin", f"audit-other-{uuid.uuid4()}@test.local", "Other"
    )
    project = await create_project(db_session, owner_id=owner.id)
    foreign = await create_project(db_session, owner_id=other.id)
    task = await create_task(db_session, project_id=project.id)
    batch = await create_batch(db_session, project_id=project.id)
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=owner.id,
        class_name="car",
        geometry={"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
    )
    db_session.add(annotation)
    await db_session.flush()

    def record(target_type, target_id=None, *, detail=None, path=None):
        return AuditLog(
            actor_id=other.id,
            actor_email=None,
            action="sdk.audit.scope",
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail_json=detail,
            path=path,
            created_at=datetime.now(timezone.utc),
        )

    visible = [
        record("project", project.id),
        record("task", task.id),
        record("batch", batch.id),
        record("annotation", annotation.id),
        record("video_tracker_job", uuid.uuid4(), detail={"task_id": str(task.id)}),
        record(None, path=f"/api/v1/projects/{project.id}/members"),
    ]
    hidden = [
        record(
            "project",
            foreign.id,
            detail={"project_id": str(project.id)},
            path=f"/api/v1/projects/{project.id}",
        ),
        record("user", other.id, detail={"project_id": str(project.id)}),
        record("system", "settings", detail={"project_id": str(project.id)}),
        record(None, path=f"/api/v1/projects/{project.id}extra/members"),
        record(
            "unknown_future_target", "opaque", detail={"project_id": str(project.id)}
        ),
    ]
    db_session.add_all([*visible, *hidden])
    await db_session.flush()
    headers = {
        "Authorization": "Bearer "
        + create_access_token(subject=str(owner.id), role=owner.role)
    }
    found = []
    cursor = None
    for _ in range(5):
        params = {"action": "sdk.audit.scope", "page_size": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await httpx_client.get(
            "/api/v1/audit-logs", params=params, headers=headers
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == len(visible)
        found.extend(data["items"])
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert {item["id"] for item in found} == {row.id for row in visible}
    assert all(item["actor_email"] == other.email for item in found)
    assert all(
        not db_session.is_modified(row) and row.actor_email is None for row in visible
    )

    all_rows = await httpx_client.get(
        "/api/v1/audit-logs",
        params={"action": "sdk.audit.scope", "page_size": 100},
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert all_rows.status_code == 200, all_rows.text
    assert all_rows.json()["total"] == len(visible) + len(hidden)


async def test_project_admin_with_no_owned_projects_has_no_global_audit(
    db_session, httpx_client
):
    actor = await create_user(
        db_session,
        "project_admin",
        f"audit-empty-{uuid.uuid4()}@test.local",
        "No projects",
    )
    db_session.add(
        AuditLog(
            actor_id=actor.id,
            actor_email=actor.email,
            action="sdk.audit.global",
            target_type="user",
            target_id=str(actor.id),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/audit-logs",
        params={"action": "sdk.audit.global"},
        headers={
            "Authorization": "Bearer "
            + create_access_token(subject=str(actor.id), role=actor.role)
        },
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0 and response.json()["items"] == []


async def test_project_admin_audit_relation_preserves_native_inet_dto_values(
    db_session, httpx_client
):
    owner = await create_user(
        db_session,
        "project_admin",
        f"inet-owner-{uuid.uuid4()}@test.local",
        "INET owner",
    )
    project = await create_project(db_session, owner_id=owner.id)
    action_prefix = f"inet.{uuid.uuid4().hex[:8]}"
    ip_values = {
        "ipv4_host": "127.0.0.1",
        "ipv6_host": "2001:db8::1",
        "ipv4_masked": "192.0.2.13/24",
        "ipv6_masked": "2001:db8::2/64",
        "ipv4_mapped_host": "::ffff:192.0.2.1",
        "ipv4_mapped_masked": "::ffff:192.0.2.1/120",
    }
    db_session.add_all(
        [
            AuditLog(
                actor_id=owner.id,
                action=f"{action_prefix}.{name}",
                target_type="project",
                target_id=str(project.id),
                ip=ip_value,
                created_at=datetime.now(timezone.utc),
            )
            for name, ip_value in ip_values.items()
        ]
    )
    await db_session.flush()

    statement = select(AuditLog).where(AuditLog.action.like(f"{action_prefix}.%"))
    loaded = (await db_session.scalars(statement)).all()
    for row in loaded:
        db_session.sync_session.expire(row, ["ip"])
    old_rows = (await db_session.scalars(statement)).all()
    expected = {row.action: AuditLogOut.model_validate(row).ip for row in old_rows}
    for row in old_rows:
        db_session.sync_session.expire(row, ["ip", "actor_email"])

    headers = {
        "Authorization": "Bearer "
        + create_access_token(subject=str(owner.id), role=owner.role)
    }
    for name in ip_values:
        action = f"{action_prefix}.{name}"
        response = await httpx_client.get(
            "/api/v1/audit-logs",
            params={"action": action, "page_size": 20},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["action"] == action
        assert items[0]["ip"] == expected[action]
        assert items[0]["actor_email"] == owner.email


async def test_project_related_history_uses_canonical_relations_and_deletion_context(
    db_session, httpx_client
):
    owner = await create_user(
        db_session, "project_admin", f"history-{uuid.uuid4()}@test.local", "Owner"
    )
    other = await create_user(
        db_session, "project_admin", f"foreign-{uuid.uuid4()}@test.local", "Other"
    )
    project = await create_project(db_session, owner_id=owner.id)
    foreign = await create_project(db_session, owner_id=other.id)
    foreign_batch = await create_batch(db_session, project_id=foreign.id)
    item = await _video_item_with_task(db_session, owner.id)
    foreign_item = await _video_item_with_task(db_session, other.id)
    feedbacks = [
        AnnotationFeedback(
            project_id=p.id,
            kind="comment",
            anchor_type="project",
            body="history",
            author_id=owner.id,
            is_active=False,
        )
        for p in (project, foreign)
    ]
    invitations = [
        UserInvitation(
            project_id=p.id,
            email=owner.email,
            role="employee",
            token=uuid.uuid4().hex,
            expires_at=datetime.now(timezone.utc),
            invited_by=owner.id,
        )
        for p in (project, foreign)
    ]
    db_session.add_all([*feedbacks, *invitations])
    await db_session.flush()

    def record(
        kind, target_id=None, *, action="sdk.audit.history", detail=None, path=None
    ):
        return AuditLog(
            actor_id=owner.id,
            action=action,
            target_type=kind,
            target_id=str(target_id) if target_id else None,
            detail_json=detail,
            path=path,
            created_at=datetime.now(timezone.utc),
        )

    visible = [
        record("feedback", feedbacks[0].id),
        record("invitation", invitations[0].id),
        record("dataset", uuid.uuid4(), detail={"project_id": str(project.id)}),
        record("video_chapter", uuid.uuid4(), detail={"dataset_item_id": str(item.id)}),
        record(
            "batch",
            uuid.uuid4(),
            action="batch.deleted",
            detail={"project_id": str(project.id)},
        ),
        record(
            "batch",
            uuid.uuid4(),
            action="batch.deleted",
            path=f"/api/v1/projects/{project.id}/batches/legacy",
        ),
        record(
            "invitation",
            action="invitation.export",
            detail={"project_id": str(project.id)},
        ),
    ]
    hidden = [
        record(
            "feedback",
            feedbacks[1].id,
            detail={"project_id": str(project.id)},
            path=f"/api/v1/projects/{project.id}",
        ),
        record(
            "invitation",
            invitations[1].id,
            detail={"project_id": str(project.id)},
            path=f"/api/v1/projects/{project.id}",
        ),
        record("dataset", uuid.uuid4()),
        record(
            "video_chapter",
            uuid.uuid4(),
            detail={"dataset_item_id": str(foreign_item.id)},
        ),
        record(
            "batch",
            foreign_batch.id,
            action="batch.deleted",
            detail={"project_id": str(project.id)},
            path=f"/api/v1/projects/{project.id}",
        ),
        record(
            "batch",
            uuid.uuid4(),
            detail={"project_id": str(project.id)},
            path=f"/api/v1/projects/{project.id}",
        ),
        record("invitation", action="invitation.export", detail={"scope": "all"}),
        record("bug_report", uuid.uuid4(), detail={"project_id": str(project.id)}),
    ]
    db_session.add_all([*visible, *hidden])
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/audit-logs",
        params={"page_size": 200},
        headers={
            "Authorization": "Bearer "
            + create_access_token(subject=str(owner.id), role=owner.role)
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == len(visible)
    assert {row["id"] for row in response.json()["items"]} == {
        row.id for row in visible
    }
