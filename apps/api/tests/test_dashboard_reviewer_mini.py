"""v0.8.7 F5.3 · GET /dashboard/reviewer/today-mini 单测。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.task import Task
from tests.factory import create_membership, create_user


async def _add_member(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    assigned_by: uuid.UUID,
) -> None:
    await create_membership(
        db,
        project_id=project_id,
        user_id=user_id,
        role=role,
        assigned_by=assigned_by,
    )


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID) -> Project:
    p = Project(
        id=uuid.uuid4(),
        display_id=f"P-RM-{uuid.uuid4().hex[:6]}",
        name="reviewer-mini-test",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(p)
    await db.flush()
    return p


async def _seed_task(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    reviewer_id: uuid.UUID | None = None,
    status: str = "completed",
    reject_reason: str | None = None,
    reviewer_claimed_at: datetime | None = None,
    reviewed_at: datetime | None = None,
) -> Task:
    t = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id=f"T-RM-{uuid.uuid4().hex[:6]}",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        tags=[],
        status=status,
        reject_reason=reject_reason,
        reviewer_id=reviewer_id,
        reviewer_claimed_at=reviewer_claimed_at,
        reviewed_at=reviewed_at,
    )
    db.add(t)
    await db.flush()
    return t


@pytest.mark.asyncio
async def test_reviewer_mini_zeros_when_no_data(httpx_client, super_admin):
    user, token = super_admin
    resp = await httpx_client.get(
        "/api/v1/dashboard/reviewer/today-mini",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["approved_today"] == 0
    assert data["rejected_today"] == 0
    assert data["avg_review_seconds"] is None
    _ = user


@pytest.mark.asyncio
async def test_reviewer_mini_today_counts(httpx_client, db_session, super_admin):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    now = datetime.now(timezone.utc)

    # 3 approved today (claimed 100s ago, reviewed now → ~100s)
    for i in range(3):
        await _seed_task(
            db_session,
            project_id=proj.id,
            reviewer_id=user.id,
            status="completed",
            reviewer_claimed_at=now - timedelta(seconds=100 + i * 10),
            reviewed_at=now - timedelta(seconds=i * 10),
        )
    # 1 rejected today（rejected 流程：status=in_progress + reject_reason 非空）
    await _seed_task(
        db_session,
        project_id=proj.id,
        reviewer_id=user.id,
        status="in_progress",
        reject_reason="类别错误",
        reviewer_claimed_at=now - timedelta(seconds=200),
        reviewed_at=now - timedelta(seconds=10),
    )
    # 1 approved yesterday (should be excluded)
    yest = now - timedelta(days=1)
    await _seed_task(
        db_session,
        project_id=proj.id,
        reviewer_id=user.id,
        status="completed",
        reviewer_claimed_at=yest - timedelta(seconds=50),
        reviewed_at=yest,
    )
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/dashboard/reviewer/today-mini",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["approved_today"] == 3
    assert data["rejected_today"] == 1
    assert data["avg_review_seconds"] is not None
    # 100~120 + 200 → 平均 ≈ 130，宽松断言
    assert 60 <= data["avg_review_seconds"] <= 220


@pytest.mark.asyncio
async def test_reviewer_mini_other_user_isolated(httpx_client, db_session, super_admin):
    """v0.8.7 · 不同 reviewer 看到自己的数。"""
    admin_user, admin_token = super_admin
    proj = await _seed_project(db_session, admin_user.id)
    rev_user = await create_user(
        db_session, "employee", f"mini-rev-{uuid.uuid4()}@test.local", "MiniRev"
    )
    await _add_member(
        db_session,
        project_id=proj.id,
        user_id=rev_user.id,
        role="reviewer",
        assigned_by=admin_user.id,
    )
    rev_token = create_access_token(subject=str(rev_user.id), role=rev_user.role)
    now = datetime.now(timezone.utc)

    # admin reviewer 当日审过 2 个；rev_user 0 个
    for _ in range(2):
        await _seed_task(
            db_session,
            project_id=proj.id,
            reviewer_id=admin_user.id,
            status="completed",
            reviewer_claimed_at=now - timedelta(seconds=80),
            reviewed_at=now,
        )
    await db_session.commit()

    r_admin = await httpx_client.get(
        "/api/v1/dashboard/reviewer/today-mini",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    r_rev = await httpx_client.get(
        "/api/v1/dashboard/reviewer/today-mini",
        headers={"Authorization": f"Bearer {rev_token}"},
    )
    assert r_admin.json()["approved_today"] == 2
    assert r_rev.json()["approved_today"] == 0
    _ = rev_user


@pytest.mark.asyncio
async def test_reviewer_mini_rejects_legacy_global_annotator(httpx_client, db_session):
    """Legacy global annotator must not access the reviewer mini endpoint."""
    legacy = await create_user(
        db_session, "annotator", f"legacy-mini-{uuid.uuid4()}@test.local", "Legacy"
    )
    await db_session.commit()
    token = create_access_token(subject=str(legacy.id), role=legacy.role)
    resp = await httpx_client.get(
        "/api/v1/dashboard/reviewer/today-mini",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reviewer_mini_employee_without_review_membership_is_empty(
    httpx_client, db_session
):
    """An employee with no reviewer membership gets an empty (not forbidden) set."""
    employee = await create_user(
        db_session, "employee", f"mini-emp-{uuid.uuid4()}@test.local", "Emp"
    )
    await db_session.commit()
    token = create_access_token(subject=str(employee.id), role=employee.role)
    resp = await httpx_client.get(
        "/api/v1/dashboard/reviewer/today-mini",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["approved_today"] == 0
