"""v0.7.2 · 治理可视化 + 全局导航 端到端覆盖

核心场景：
1. TestProjectDistributeBatches — 项目级 batch 圆周分派（一 batch 一标注员 + 一审核员）+ task 联动
2. TestAnnotationAuditTrail — annotation create / update / delete 写入 audit_logs
3. TestGlobalSearch — /search 跨实体可见性 + 模糊匹配
4. TestAnnotationHistoryEndpoint — GET /annotations/{id}/history 合并 audit + comments
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.display_id import next_display_id
from tests.factory import create_membership, build_tool_bindings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_project(
    db,
    owner_id,
    *,
    n_annotators: int = 3,
    n_reviewers: int = 0,
    n_batches: int = 1,
    tasks_per_batch: int = 10,
):
    pid = uuid.uuid4()
    p = Project(
        id=pid,
        display_id=await next_display_id(db, "projects"),
        name="v0.7.2 governance",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car", "person"]),
    )
    db.add(p)
    await db.flush()

    from app.db.models.user import User
    from app.core.security import hash_password

    annotator_ids: list[uuid.UUID] = []
    reviewer_ids: list[uuid.UUID] = []
    # 先把所有 User 插入并 flush，再插 ProjectMember（避免 FK 顺序问题）
    for i in range(n_annotators):
        u = User(
            id=uuid.uuid4(),
            email=f"a{i}-{uuid.uuid4().hex[:6]}@test.local",
            name=f"Annotator {i}",
            password_hash=hash_password("Test1234"),
            role="employee",
            is_active=True,
        )
        db.add(u)
        annotator_ids.append(u.id)
    for i in range(n_reviewers):
        u = User(
            id=uuid.uuid4(),
            email=f"r{i}-{uuid.uuid4().hex[:6]}@test.local",
            name=f"Reviewer {i}",
            password_hash=hash_password("Test1234"),
            role="employee",
            is_active=True,
        )
        db.add(u)
        reviewer_ids.append(u.id)
    await db.flush()
    for uid in annotator_ids:
        await create_membership(
            db, project_id=pid, user_id=uid, role="annotator", assigned_by=owner_id
        )
    for uid in reviewer_ids:
        await create_membership(
            db, project_id=pid, user_id=uid, role="reviewer", assigned_by=owner_id
        )
    await db.flush()

    batches: list[TaskBatch] = []
    all_tasks: list[Task] = []
    for bi in range(n_batches):
        batch = TaskBatch(
            id=uuid.uuid4(),
            project_id=pid,
            display_id=await next_display_id(db, "batches"),
            name=f"b-{bi}",
            status="active",
            assigned_user_ids=[],
        )
        db.add(batch)
        await db.flush()
        for ti in range(tasks_per_batch):
            t = Task(
                id=uuid.uuid4(),
                project_id=pid,
                batch_id=batch.id,
                display_id=f"T-{bi}-{ti}",
                file_name=f"f{bi}_{ti}.jpg",
                file_path=f"/tmp/f{bi}_{ti}.jpg",
                file_type="image",
                status="pending",
            )
            db.add(t)
            all_tasks.append(t)
        batches.append(batch)
    await db.flush()
    return p, batches, all_tasks, annotator_ids, reviewer_ids


# ── 1. 项目级 batch 分派（一 batch 一人） ─────────────────────────────


class TestProjectDistributeBatches:
    @pytest.mark.asyncio
    async def test_round_robin_assigns_one_per_batch(
        self,
        db_session,
        super_admin,
        httpx_client,
    ):
        owner, token = super_admin
        p, batches, tasks, annotator_ids, reviewer_ids = await _seed_project(
            db_session,
            owner.id,
            n_annotators=3,
            n_reviewers=2,
            n_batches=7,
            tasks_per_batch=2,
        )

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/distribute-batches",
            json={
                "annotator_ids": [str(uid) for uid in annotator_ids],
                "reviewer_ids": [str(uid) for uid in reviewer_ids],
                "only_unassigned": True,
            },
            headers=_bearer(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["distributed_batches"] == 7

        # 每个 batch 都拿到 1 个 annotator + 1 个 reviewer
        for b in batches:
            await db_session.refresh(b)
            assert b.annotator_id is not None
            assert b.reviewer_id is not None
            # assigned_user_ids 派生为 [annotator_id, reviewer_id]
            assert set(map(str, b.assigned_user_ids)) == {
                str(b.annotator_id),
                str(b.reviewer_id),
            }

        # 圆周：7 batch / 3 annotator → 计数 [3, 2, 2]
        annotator_counts: dict[str, int] = {}
        for b in batches:
            annotator_counts[str(b.annotator_id)] = (
                annotator_counts.get(str(b.annotator_id), 0) + 1
            )
        assert sorted(annotator_counts.values(), reverse=True) == [3, 2, 2]

        # task 联动：所有 task.assignee_id 必须等于其 batch.annotator_id
        for t in tasks:
            await db_session.refresh(t)
            owner_batch = next(b for b in batches if b.id == t.batch_id)
            assert t.assignee_id == owner_batch.annotator_id
            assert t.reviewer_id == owner_batch.reviewer_id

        # audit_logs 中应有项目级 batch.distribute_even 一条
        audits = (
            (
                await db_session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "batch.distribute_even",
                        AuditLog.target_type == "project",
                        AuditLog.target_id == str(p.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1
        assert audits[0].detail_json["distributed_batches"] == 7

    @pytest.mark.asyncio
    async def test_only_unassigned_skips_already_assigned_batches(
        self,
        db_session,
        super_admin,
        httpx_client,
    ):
        owner, token = super_admin
        p, batches, _, annotator_ids, _ = await _seed_project(
            db_session,
            owner.id,
            n_annotators=2,
            n_batches=4,
            tasks_per_batch=1,
        )
        # 预设 batch[0] 已分派给 annotator[0]
        batches[0].annotator_id = annotator_ids[0]
        await db_session.flush()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/distribute-batches",
            json={
                "annotator_ids": [str(uid) for uid in annotator_ids],
                "reviewer_ids": [],
                "only_unassigned": True,
            },
            headers=_bearer(token),
        )
        assert resp.status_code == 200
        # 只有 3 个未分派 batch 被改写
        assert resp.json()["distributed_batches"] == 3

        await db_session.refresh(batches[0])
        # batch[0] 保持原标注员
        assert batches[0].annotator_id == annotator_ids[0]


# ── 2. annotation audit ───────────────────────────────────────────────


class TestAnnotationAuditTrail:
    @pytest.mark.asyncio
    async def test_create_update_delete_each_emits_audit(
        self,
        db_session,
        super_admin,
        httpx_client,
    ):
        owner, token = super_admin
        p, batches, tasks, _, _ = await _seed_project(
            db_session,
            owner.id,
            n_annotators=1,
            n_batches=1,
            tasks_per_batch=1,
        )
        task = tasks[0]

        # create
        resp = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/annotations",
            json={
                "annotation_type": "bbox",
                "class_name": "car",
                "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            },
            headers=_bearer(token),
        )
        assert resp.status_code == 201, resp.text
        ann_id = resp.json()["id"]

        # update (改类到项目标签集内的另一类; v0.14.17 起 PATCH 也校验 class_name,
        # 故须用 classes=["car","person"] 内的合法值, "truck" 会被 422 拒)
        resp = await httpx_client.patch(
            f"/api/v1/tasks/{task.id}/annotations/{ann_id}",
            json={"class_name": "person"},
            headers=_bearer(token),
        )
        assert resp.status_code == 200, resp.text

        # delete
        resp = await httpx_client.delete(
            f"/api/v1/tasks/{task.id}/annotations/{ann_id}",
            headers=_bearer(token),
        )
        assert resp.status_code == 204

        # 检查 audit_logs
        audits = (
            (
                await db_session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.target_type == "annotation",
                        AuditLog.target_id == str(ann_id),
                    )
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        actions = [a.action for a in audits]
        assert "annotation.create" in actions
        assert "annotation.update" in actions
        assert "annotation.delete" in actions


# ── 3. /search ─────────────────────────────────────────────────────────


class TestGlobalSearch:
    @pytest.mark.asyncio
    async def test_super_admin_finds_project_by_name(
        self,
        db_session,
        super_admin,
        httpx_client,
    ):
        owner, token = super_admin
        await _seed_project(
            db_session, owner.id, n_annotators=1, n_batches=1, tasks_per_batch=1
        )

        resp = await httpx_client.get(
            "/api/v1/search?q=governance&limit=5",
            headers=_bearer(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert any("governance" in p["name"] for p in body["projects"])


# ── 4. annotation history ─────────────────────────────────────────────


class TestAnnotationHistoryEndpoint:
    @pytest.mark.asyncio
    async def test_history_merges_audits_and_comments(
        self,
        db_session,
        super_admin,
        httpx_client,
    ):
        owner, token = super_admin
        p, batches, tasks, _, _ = await _seed_project(
            db_session,
            owner.id,
            n_annotators=1,
            n_batches=1,
            tasks_per_batch=1,
        )

        resp = await httpx_client.post(
            f"/api/v1/tasks/{tasks[0].id}/annotations",
            json={
                "annotation_type": "bbox",
                "class_name": "car",
                "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            },
            headers=_bearer(token),
        )
        ann_id = resp.json()["id"]

        await httpx_client.post(
            f"/api/v1/annotations/{ann_id}/comments",
            json={"body": "看一下这个框"},
            headers=_bearer(token),
        )

        resp = await httpx_client.get(
            f"/api/v1/annotations/{ann_id}/history",
            headers=_bearer(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        kinds = [e["kind"] for e in body["entries"]]
        assert "audit" in kinds  # at least annotation.create
        assert "comment" in kinds
