"""成员绩效项目级范围 + RBAC。

核心保证:project 给定时聚合**按项目切分**(非全局);project_admin 强制其项目范围。
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_event import TaskEvent
from tests.factory import create_membership, create_task


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID, name: str) -> Project:
    p = Project(
        id=uuid.uuid4(),
        display_id=f"P-SC-{uuid.uuid4().hex[:6]}",
        name=name,
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(p)
    await db.flush()
    return p


async def _seed_annotations(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    n: int,
    *,
    created_at: datetime | None = None,
) -> None:
    created_at = created_at or datetime.now(timezone.utc)
    for _ in range(n):
        t = Task(
            id=uuid.uuid4(),
            project_id=project_id,
            display_id=f"T-SC-{uuid.uuid4().hex[:6]}",
            file_name="x.jpg",
            file_path="/tmp/x.jpg",
            file_type="image",
            tags=[],
            status="completed",
        )
        db.add(t)
        await db.flush()
        db.add(
            Annotation(
                id=uuid.uuid4(),
                task_id=t.id,
                project_id=project_id,
                user_id=user_id,
                class_name="car",
                geometry={"x": 0, "y": 0, "w": 1, "h": 1},
                created_at=created_at,
            )
        )
    await db.flush()


@pytest.mark.asyncio
async def test_detail_aggregation_sliced_by_project(
    httpx_client, db_session, super_admin, annotator
):
    """对账:全局 throughput = 各项目之和;project 过滤后 = 单项目数字。"""
    admin_user, admin_token = super_admin
    ann_user, _ = annotator
    p1 = await _seed_project(db_session, admin_user.id, "P1")
    p2 = await _seed_project(db_session, admin_user.id, "P2")
    await _seed_annotations(db_session, p1.id, ann_user.id, 3)
    await _seed_annotations(db_session, p2.id, ann_user.id, 2)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    g = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}", headers=headers
    )
    assert g.status_code == 200
    assert g.json()["throughput"] == 5
    s1 = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}?project={p1.id}",
        headers=headers,
    )
    assert s1.status_code == 200
    assert s1.json()["throughput"] == 3
    s2 = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}?project={p2.id}",
        headers=headers,
    )
    assert s2.json()["throughput"] == 2
    assert g.json()["composite_score"] == 5
    assert s1.json()["composite_score"] == 3
    assert s2.json()["composite_score"] == 2
    assert s1.json()["first_pass_yield"] is None


@pytest.mark.asyncio
async def test_detail_score_uses_selected_person_project_and_period_without_activity(
    httpx_client, db_session, project_admin, super_admin, annotator, reviewer
):
    owner, token = project_admin
    admin, _ = super_admin
    first, _ = annotator
    second, _ = reviewer
    own = await _seed_project(db_session, owner.id, "Own score scope")
    other = await _seed_project(db_session, admin.id, "Other score scope")
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    yesterday = today - timedelta(days=1)
    for person in (first, second):
        await create_membership(
            db_session, project_id=own.id, user_id=person.id, role="annotator"
        )
        await _seed_annotations(db_session, own.id, person.id, 2, created_at=today)
        for reopened in (0, 1):
            task = await create_task(db_session, project_id=own.id)
            task.assignee_id = person.id
            task.submitted_at = today
            task.reopened_count = reopened

    await _seed_annotations(db_session, own.id, first.id, 1, created_at=yesterday)
    await _seed_annotations(
        db_session, own.id, first.id, 1, created_at=today + timedelta(days=1)
    )
    await _seed_annotations(db_session, other.id, first.id, 1, created_at=today)
    for project, submitted_at, reopened in (
        (own, yesterday, 0),
        (own, today + timedelta(days=1), 0),
        (other, today, 1),
    ):
        task = await create_task(db_session, project_id=project.id)
        task.assignee_id = first.id
        task.submitted_at = submitted_at
        task.reopened_count = reopened

    for person, duration_minutes in ((first, 45), (second, 2)):
        task = await create_task(db_session, project_id=own.id)
        db_session.add(
            TaskEvent(
                task_id=task.id,
                project_id=own.id,
                user_id=person.id,
                kind="annotate",
                started_at=yesterday,
                ended_at=yesterday + timedelta(minutes=duration_minutes),
                duration_ms=duration_minutes * 60_000,
            )
        )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {token}"}
    base = "/api/v1/dashboard/admin/people"
    result = await httpx_client.get(
        base,
        params={"project": str(own.id), "period": "7d", "sort": "activity"},
        headers=headers,
    )
    assert result.status_code == 200, result.text
    scores = {
        item["user_id"]: item["activity_score"] for item in result.json()["items"]
    }
    assert scores[str(first.id)] > scores[str(second.id)]

    exported = await httpx_client.get(
        f"{base}/export",
        params={"project": str(own.id), "period": "7d", "sort": "activity"},
        headers=headers,
    )
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.text.removeprefix("\ufeff"))))
    assert {row["user_id"]: int(row["activity_score"]) for row in rows} == scores
    assert all("composite_score" not in row for row in rows)

    for person, period, expected in (
        (first, "today", (2, 50, 0.5, 26)),
        (second, "today", (2, 50, 0.5, 26)),
        (first, "7d", (3, 67, 0.667, 35)),
        (second, "7d", (2, 50, 0.5, 26)),
    ):
        response = await httpx_client.get(
            f"{base}/{person.id}",
            params={"project": str(own.id), "period": period},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        detail = response.json()
        assert (
            detail["throughput"],
            detail["quality_score"],
            detail["first_pass_yield"],
            detail["composite_score"],
        ) == expected
        assert detail["active_minutes"] is None


@pytest.mark.asyncio
async def test_list_main_metric_sliced_by_project(
    httpx_client, db_session, super_admin, annotator
):
    """list 端点:project 过滤后 main_metric 反映单项目(非全局 5)。"""
    admin_user, admin_token = super_admin
    ann_user, _ = annotator
    p1 = await _seed_project(db_session, admin_user.id, "P1")
    p2 = await _seed_project(db_session, admin_user.id, "P2")
    await _seed_annotations(db_session, p1.id, ann_user.id, 3)
    await _seed_annotations(db_session, p2.id, ann_user.id, 2)
    db_session.add(
        ProjectMember(
            id=uuid.uuid4(), project_id=p1.id, user_id=ann_user.id, role="annotator"
        )
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    r = await httpx_client.get(
        f"/api/v1/dashboard/admin/people?project={p1.id}&period=4w", headers=headers
    )
    assert r.status_code == 200
    items = {it["user_id"]: it for it in r.json()["items"]}
    assert str(ann_user.id) in items
    assert items[str(ann_user.id)]["main_metric"] == 3


@pytest.mark.asyncio
async def test_super_admin_global_unchanged(
    httpx_client, db_session, super_admin, annotator
):
    """回归:super_admin 不带 project → 全局数字(5)不变。"""
    admin_user, admin_token = super_admin
    ann_user, _ = annotator
    p1 = await _seed_project(db_session, admin_user.id, "P1")
    p2 = await _seed_project(db_session, admin_user.id, "P2")
    await _seed_annotations(db_session, p1.id, ann_user.id, 3)
    await _seed_annotations(db_session, p2.id, ann_user.id, 2)
    await db_session.commit()

    r = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.json()["throughput"] == 5


@pytest.mark.asyncio
async def test_project_admin_scoped_to_own_project(
    httpx_client, db_session, super_admin, project_admin, annotator
):
    """project_admin:仅自有项目可见 + 强制项目范围。"""
    admin_user, _ = super_admin
    pm_user, pm_token = project_admin
    ann_user, _ = annotator
    own = await _seed_project(db_session, pm_user.id, "Own")
    other = await _seed_project(db_session, admin_user.id, "Other")
    await _seed_annotations(db_session, own.id, ann_user.id, 3)
    db_session.add(
        ProjectMember(
            id=uuid.uuid4(), project_id=own.id, user_id=ann_user.id, role="annotator"
        )
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {pm_token}"}
    ok = await httpx_client.get(
        f"/api/v1/dashboard/admin/people?project={own.id}&period=4w", headers=headers
    )
    assert ok.status_code == 200
    no_proj = await httpx_client.get("/api/v1/dashboard/admin/people", headers=headers)
    assert no_proj.status_code == 403
    forbidden = await httpx_client.get(
        f"/api/v1/dashboard/admin/people?project={other.id}", headers=headers
    )
    assert forbidden.status_code == 404


@pytest.mark.asyncio
async def test_project_admin_detail_blocks_non_member(
    httpx_client, db_session, super_admin, project_admin, annotator
):
    """安全:project_admin 查非本项目成员的详情 → 404(防 IDOR / 跨项目枚举)。"""
    admin_user, _ = super_admin
    pm_user, pm_token = project_admin
    ann_user, _ = annotator
    own = await _seed_project(db_session, pm_user.id, "Own")
    other = await _seed_project(db_session, admin_user.id, "Other")
    await _seed_annotations(db_session, other.id, ann_user.id, 2)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}?project={own.id}",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_project_admin_member_of_others_project_blocked(
    httpx_client, db_session, super_admin, project_admin
):
    """安全:project_admin 是他人项目的 ProjectMember 但非 owner 时,仍 → 404。

    _resolve_people_scope 严格按 owner 校验,**不复用** assert_project_visible
    的 ProjectMember fallback,防止「owner ∪ member」的 IDOR 漏窗。
    """
    admin_user, _ = super_admin
    pm_user, pm_token = project_admin
    other = await _seed_project(db_session, admin_user.id, "Other")
    db_session.add(
        ProjectMember(
            id=uuid.uuid4(),
            project_id=other.id,
            user_id=pm_user.id,
            role="annotator",
        )
    )
    await db_session.commit()

    r_list = await httpx_client.get(
        f"/api/v1/dashboard/admin/people?project={other.id}",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert r_list.status_code == 404
    r_export = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/export?project={other.id}",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert r_export.status_code == 404


@pytest.mark.asyncio
async def test_annotator_still_denied(httpx_client, annotator):
    """普通 annotator 无权访问成员绩效。"""
    _, token = annotator
    r = await httpx_client.get(
        "/api/v1/dashboard/admin/people",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
