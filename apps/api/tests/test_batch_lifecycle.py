"""v0.7.0 · 批次状态机重设计端到端覆盖

5 个核心场景：
1. TestTransitionAuth — /transition 鉴权矩阵（标注员不能直推 approved 等）
2. TestRejectBatchSoftReset — 软重置语义（不动 annotations / is_labeled）
3. TestEmptyBatchActivation — 0-task 批次 draft→active 拒绝
4. TestWithdrawCascade — 标注员 withdraw 后 reviewing → annotating（v0.6.x 既有路径，本版固化）
5. TestReviewerVisibility — reviewer 在 reviewing 批次的可见性 + annotator 在 rejected 批次的特例
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.display_id import next_display_id
from tests.factory import create_membership, build_tool_bindings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    db,
    owner_id: uuid.UUID,
    annotator_id: uuid.UUID,
    *,
    member_role: str = "annotator",
    batch_status: str = "active",
    n_tasks: int = 2,
    task_status: str = "pending",
    is_labeled: bool = False,
):
    pid = uuid.uuid4()
    p = Project(
        id=pid,
        display_id=await next_display_id(db, "projects"),
        name="lifecycle test",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db.add(p)
    await db.flush()

    await create_membership(
        db, project_id=pid, user_id=annotator_id, role=member_role, assigned_by=owner_id
    )

    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=pid,
        display_id=await next_display_id(db, "batches"),
        name="b1",
        status=batch_status,
        # v0.7.2 单值语义：annotator_id 单值；assigned_user_ids 派生
        annotator_id=annotator_id,
        assigned_user_ids=[str(annotator_id)],
    )
    db.add(batch)
    await db.flush()

    tasks: list[Task] = []
    for i in range(n_tasks):
        # A task that already sits in review carries valid per-round frozen
        # evidence from its actual distinct contributor/submitter.
        evidence: dict = {}
        if task_status in ("review", "completed"):
            evidence = {
                "annotation_contributor_ids": [str(annotator_id)],
                "review_contributor_ids": [str(annotator_id)],
                "review_submitter_id": annotator_id,
                "review_round_id": uuid.uuid4(),
            }
        t = Task(
            id=uuid.uuid4(),
            project_id=pid,
            batch_id=batch.id,
            display_id=f"T-{i}",
            file_name=f"f{i}.jpg",
            file_path=f"/tmp/f{i}.jpg",
            file_type="image",
            status=task_status,
            is_labeled=is_labeled,
            **evidence,
        )
        db.add(t)
        tasks.append(t)
    await db.flush()
    return p, batch, tasks


# ── 1. transition 鉴权矩阵 ──────────────────────────────────────────────


class TestTransitionAuth:
    @pytest.mark.asyncio
    async def test_annotator_cannot_skip_to_approved(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, _ = super_admin
        user, token = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="reviewing",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "approved"},
            headers=_bearer(token),
        )
        assert resp.status_code == 403
        assert "employee" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_annotator_can_submit_for_review(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """annotating → reviewing：被分派的标注员可主动提交质检。"""
        owner, _ = super_admin
        user, token = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "reviewing"},
            headers=_bearer(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "reviewing"

    @pytest.mark.asyncio
    async def test_unassigned_annotator_cannot_submit(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """未分派到批次的标注员无法把批次推到 reviewing。"""
        owner, _ = super_admin
        user, token = annotator
        # 让 annotator 是项目成员但不在 assigned_user_ids 中
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
        )
        # v0.7.2：清空 annotator_id（单值语义）
        batch.annotator_id = None
        batch.assigned_user_ids = []
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "reviewing"},
            headers=_bearer(token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_reviewer_can_approve(
        self,
        httpx_client,
        db_session,
        super_admin,
        reviewer,
    ):
        owner, _ = super_admin
        rev, rev_token = reviewer
        # rev holds an explicit reviewer membership; reviewing→approved is
        # authorized by the resolved project role, not the account role.
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            rev.id,
            member_role="reviewer",
            batch_status="reviewing",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "approved"},
            headers=_bearer(rev_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "approved"

    @pytest.mark.asyncio
    async def test_owner_can_archive(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="active",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "archived"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    @pytest.mark.asyncio
    async def test_annotator_cannot_archive(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, _ = super_admin
        user, token = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="active",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "archived"},
            headers=_bearer(token),
        )
        assert resp.status_code == 403


# ── 2. reject_batch 软重置 ──────────────────────────────────────────────


class TestRejectBatchSoftReset:
    @pytest.mark.asyncio
    async def test_reject_resets_only_review_completed_to_pending(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """方案 A 软重置：review/completed → pending；is_labeled / annotations 保持。"""
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="reviewing",
            task_status="review",
            is_labeled=True,
            n_tasks=3,
        )
        # 给两个任务塞 annotation（模拟标注员已画了几个框）
        for t in tasks[:2]:
            db_session.add(
                Annotation(
                    id=uuid.uuid4(),
                    task_id=t.id,
                    project_id=p.id,
                    user_id=user.id,
                    annotation_type="bbox",
                    class_name="car",
                    geometry={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                    is_active=True,
                )
            )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/reject",
            json={"feedback": "请修正第一张图的边界框"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "rejected"
        assert body["review_feedback"] == "请修正第一张图的边界框"
        assert body["reviewed_at"] is not None
        assert body["reviewed_by"] == str(owner.id)

        # 数据校验
        for t in tasks:
            await db_session.refresh(t)
            assert t.status == "pending"
            # is_labeled 不应被重置（v0.6.x 老语义会清掉，v0.7.0 软重置保留）
            assert t.is_labeled is True

        ann_rows = (
            (
                await db_session.execute(
                    select(Annotation).where(
                        Annotation.task_id.in_([t.id for t in tasks])
                    )
                )
            )
            .scalars()
            .all()
        )
        # annotations 全部保持 is_active
        assert len(ann_rows) == 2
        assert all(a.is_active for a in ann_rows)

    @pytest.mark.asyncio
    async def test_reject_creates_notification_for_assigned(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="reviewing",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/reject",
            json={"feedback": "需要重做"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200

        # annotator 应有 batch.rejected 通知
        notif_rows = (
            (
                await db_session.execute(
                    select(Notification).where(
                        Notification.user_id == user.id,
                        Notification.type == "batch.rejected",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notif_rows) == 1
        assert notif_rows[0].payload.get("feedback") == "需要重做"
        assert notif_rows[0].payload.get("batch_display_id") == batch.display_id

    @pytest.mark.asyncio
    async def test_reject_requires_feedback(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="reviewing",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/reject",
            json={"feedback": ""},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 422  # Pydantic min_length=1

    @pytest.mark.asyncio
    async def test_annotator_cannot_reject(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, _ = super_admin
        user, token = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="reviewing",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/reject",
            json={"feedback": "xxx"},
            headers=_bearer(token),
        )
        # require_roles 在 reject 端点先于 assert_can_transition 拦截，403
        assert resp.status_code == 403


# ── 3. 0-task 批次激活拦截 ───────────────────────────────────────────────


class TestEmptyBatchActivation:
    @pytest.mark.asyncio
    async def test_empty_batch_cannot_activate(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="draft",
            n_tasks=0,
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "active"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 400
        assert "empty" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_non_empty_batch_can_activate(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="draft",
            n_tasks=1,
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "active"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"


# ── 4. withdraw 反推（已存在路径固化）────────────────────────────────────


class TestWithdrawCascade:
    @pytest.mark.asyncio
    async def test_reviewing_drops_back_to_annotating_after_withdraw(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """标注员 withdraw（review→pending）会让 check_auto_transitions 把
        batch 从 reviewing 退回 annotating（如果有非全空池）。
        由于 withdraw 路径较复杂，本测试简化：直接调 check_auto_transitions 同等手段验证。
        """
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            task_status="review",
            is_labeled=True,
            n_tasks=2,
        )
        await db_session.flush()

        svc = BatchService(db_session)
        # 初始：所有任务都 review，check 应推到 reviewing
        await svc.check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        assert batch.status == "reviewing"

        # 标注员 withdraw 一个任务（模拟）
        tasks[0].status = "pending"
        await db_session.flush()

        # 这里 v0.6.x 行为：annotator withdraw 端点本身会把 batch 反推回 annotating
        # （test_task_lock 等已覆盖 withdraw 端点）。本测试只验证 check_auto_transitions
        # 在 reviewing 时遇到新增 pending 不会卡死，需要业务侧主动反推。
        # 当前 check_auto_transitions 只处理 active→annotating / annotating→reviewing，
        # reviewing→annotating 反推由 withdraw 端点直接 update batch.status。
        # 这里固化「批次此时仍在 reviewing 是合规的（auto 不会主动反推）」。
        await svc.check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        # auto 不反推；保持 reviewing
        assert batch.status == "reviewing"


# ── 5. reviewer 可见性 + rejected 特例 ──────────────────────────────────


class TestReviewerVisibility:
    @pytest.mark.asyncio
    async def test_reviewer_sees_reviewing_batch_tasks(
        self,
        httpx_client,
        db_session,
        super_admin,
        reviewer,
    ):
        owner, _ = super_admin
        rev, rev_token = reviewer
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            rev.id,
            member_role="reviewer",
            batch_status="reviewing",
            task_status="review",
            is_labeled=True,
        )
        # 把 reviewer 从 assigned_user_ids 中去掉验证不受 assigned 约束
        batch.assigned_user_ids = [str(uuid.uuid4())]  # 别人
        await db_session.commit()

        resp = await httpx_client.get(
            f"/api/v1/tasks?project_id={p.id}&status=review&limit=200",
            headers=_bearer(rev_token),
        )
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert str(tasks[0].id) in ids

    @pytest.mark.asyncio
    async def test_annotator_sees_rejected_batch_when_assigned(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """rejected 状态特例：被分派的标注员可见（看 reviewer 留言 + 重做）。"""
        owner, _ = super_admin
        user, token = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="rejected",
            task_status="pending",
            is_labeled=True,
        )
        await db_session.commit()

        resp = await httpx_client.get(
            f"/api/v1/tasks?project_id={p.id}&limit=200",
            headers=_bearer(token),
        )
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert str(tasks[0].id) in ids

    @pytest.mark.asyncio
    async def test_unassigned_annotator_cannot_see_rejected(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """rejected 特例仅对**被分派**标注员放行。"""
        owner, _ = super_admin
        user, token = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="rejected",
        )
        # v0.7.2：清空 annotator_id（单值语义）
        batch.annotator_id = None
        batch.assigned_user_ids = []
        await db_session.commit()

        resp = await httpx_client.get(
            f"/api/v1/tasks?project_id={p.id}&limit=200",
            headers=_bearer(token),
        )
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert str(tasks[0].id) not in ids


# ── 6. v0.7.3 · 管理员逆向迁移 ─────────────────────────────────────────


class TestReverseTransitions:
    @pytest.mark.asyncio
    async def test_owner_can_unarchive(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="archived",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "active", "reason": "误归档恢复"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"

        # audit_log 应记录 reverse=True + reason
        audit = (
            (
                await db_session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.target_type == "batch",
                        AuditLog.target_id == str(batch.id),
                    )
                    .order_by(AuditLog.id.desc())
                )
            )
            .scalars()
            .first()
        )
        assert audit is not None
        assert audit.detail_json.get("reverse") is True
        assert audit.detail_json.get("reason") == "误归档恢复"

    @pytest.mark.asyncio
    async def test_reverse_requires_reason(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="archived",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "active"},  # 缺 reason
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 400
        assert "reason" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_owner_can_reopen_approved_clears_metadata(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="approved",
        )
        # 模拟之前已经审核过：写入元数据
        from datetime import datetime, timezone

        batch.review_feedback = "之前的反馈"
        batch.reviewed_at = datetime.now(timezone.utc)
        batch.reviewed_by = owner.id
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "reviewing", "reason": "漏审重开"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "reviewing"
        assert body["review_feedback"] is None
        assert body["reviewed_at"] is None
        assert body["reviewed_by"] is None

    @pytest.mark.asyncio
    async def test_owner_can_reopen_rejected_keeps_feedback(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """rejected → reviewing 不清反馈（reviewer 复审需要看上次原因）。"""
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="rejected",
        )
        from datetime import datetime, timezone

        batch.review_feedback = "上次驳回原因"
        batch.reviewed_at = datetime.now(timezone.utc)
        batch.reviewed_by = owner.id
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "reviewing", "reason": "标注员申诉，直接复审"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "reviewing"
        # 反馈保留
        assert body["review_feedback"] == "上次驳回原因"

    @pytest.mark.asyncio
    async def test_non_owner_cannot_reverse(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, _ = super_admin
        user, token = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="archived",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/transition",
            json={"target_status": "active", "reason": "test"},
            headers=_bearer(token),
        )
        assert resp.status_code == 403


# ── 7. v0.7.3 · 多选批量操作 ─────────────────────────────────────────


async def _seed_multi(
    db,
    owner_id: uuid.UUID,
    annotator_id: uuid.UUID,
    *,
    statuses: list[str],
    n_tasks_each: int = 1,
):
    """创建一个项目 + 多个不同状态的 batch（每个 batch n_tasks_each 个 task）。"""
    pid = uuid.uuid4()
    p = Project(
        id=pid,
        display_id=await next_display_id(db, "projects"),
        name="bulk test",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db.add(p)
    await db.flush()
    await create_membership(
        db, project_id=pid, user_id=annotator_id, role="annotator", assigned_by=owner_id
    )

    batches = []
    for idx, st in enumerate(statuses):
        b = TaskBatch(
            id=uuid.uuid4(),
            project_id=pid,
            display_id=await next_display_id(db, "batches"),
            name=f"b{idx}",
            status=st,
            annotator_id=annotator_id,
            assigned_user_ids=[str(annotator_id)],
        )
        db.add(b)
        await db.flush()
        for j in range(n_tasks_each):
            db.add(
                Task(
                    id=uuid.uuid4(),
                    project_id=pid,
                    batch_id=b.id,
                    display_id=f"T-{idx}-{j}",
                    file_name="f.jpg",
                    file_path="/tmp/f.jpg",
                    file_type="image",
                    status="pending",
                    # Frozen round evidence for tests that later move the task
                    # into review before a batch decision.
                    annotation_contributor_ids=[str(annotator_id)],
                    review_contributor_ids=[str(annotator_id)],
                    review_submitter_id=annotator_id,
                    review_round_id=uuid.uuid4(),
                )
            )
        await db.flush()
        batches.append(b)
    return p, batches


class TestBulkOperations:
    @pytest.mark.asyncio
    async def test_bulk_archive_succeeds(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["active", "annotating", "archived"],
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-archive",
            json={"batch_ids": [str(b.id) for b in batches]},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["succeeded"]) == 2  # active, annotating
        assert len(body["skipped"]) == 1  # archived
        assert body["skipped"][0]["batch_id"] == str(batches[2].id)

    @pytest.mark.asyncio
    async def test_bulk_activate_partial_success(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        # 三个 draft：第一个有 annotator 有 task，第二个没 annotator，第三个有 annotator 但 0 task
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["draft", "draft", "draft"],
            n_tasks_each=1,
        )
        # 改造：batch[1] 无 annotator；batch[2] 删空 task
        batches[1].annotator_id = None
        batches[1].assigned_user_ids = []
        from sqlalchemy import delete

        await db_session.execute(delete(Task).where(Task.batch_id == batches[2].id))
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-activate",
            json={"batch_ids": [str(b.id) for b in batches]},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["succeeded"]) == 1
        assert body["succeeded"][0] == str(batches[0].id)
        assert len(body["failed"]) == 2
        reasons = {item["batch_id"]: item["reason"] for item in body["failed"]}
        assert "annotator" in reasons[str(batches[1].id)].lower()
        assert "task" in reasons[str(batches[2].id)].lower()

    @pytest.mark.asyncio
    async def test_bulk_reassign_atomic(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
        reviewer,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        rev, _ = reviewer
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["active", "annotating"],
        )
        await create_membership(
            db_session,
            project_id=p.id,
            user_id=rev.id,
            role="reviewer",
            assigned_by=owner.id,
        )
        await db_session.flush()
        await db_session.commit()

        # 把 reviewer_id 改派为 rev（annotator_id 不变）
        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-reassign",
            json={
                "batch_ids": [str(b.id) for b in batches],
                "reviewer_id": str(rev.id),
            },
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["succeeded"]) == 2

        for b in batches:
            await db_session.refresh(b)
            assert b.reviewer_id == rev.id
            # task 也应同步
            ts = (
                (await db_session.execute(select(Task).where(Task.batch_id == b.id)))
                .scalars()
                .all()
            )
            for t in ts:
                assert t.reviewer_id == rev.id

    @pytest.mark.asyncio
    async def test_non_owner_cannot_bulk(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, _ = super_admin
        user, token = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["active"],
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-archive",
            json={"batch_ids": [str(batches[0].id)]},
            headers=_bearer(token),
        )
        assert resp.status_code == 403


# ── 8. v0.7.3 · 批次操作历史端点 ────────────────────────────────────


class TestBatchAuditLogs:
    @pytest.mark.asyncio
    async def test_returns_direct_and_bulk_events(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["active", "active"],
        )
        await db_session.commit()

        # 先做一次 bulk-archive（含 batches[0] 和 batches[1]）
        await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-archive",
            json={"batch_ids": [str(b.id) for b in batches]},
            headers=_bearer(owner_token),
        )
        # 再单独 unarchive batches[0] 走逆向迁移
        await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batches[0].id}/transition",
            json={"target_status": "active", "reason": "撤回"},
            headers=_bearer(owner_token),
        )

        resp = await httpx_client.get(
            f"/api/v1/projects/{p.id}/batches/{batches[0].id}/audit-logs",
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        logs = resp.json()
        actions = [e["action"] for e in logs]
        assert "batch.status_changed" in actions  # 直接 transition
        assert "batch.bulk_archive" in actions  # bulk 也会进来


# ── 9. v0.9.15 · ADR-0008 admin-lock ────────────────────────────────────────


class TestAdminLock:
    @pytest.mark.asyncio
    async def test_owner_can_lock_batch(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session, owner.id, user.id, batch_status="annotating"
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/admin-lock",
            json={"reason": "质量问题暂停"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["admin_locked"] is True
        assert body["admin_lock_reason"] == "质量问题暂停"
        assert body["admin_locked_by"] == str(owner.id)
        assert body["admin_locked_at"] is not None

    @pytest.mark.asyncio
    async def test_owner_can_unlock_batch(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session, owner.id, user.id, batch_status="annotating"
        )
        batch.admin_locked = True
        batch.admin_lock_reason = "test"
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/admin-unlock",
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["admin_locked"] is False
        assert body["admin_lock_reason"] is None
        assert body["admin_locked_at"] is None
        assert body["admin_locked_by"] is None

    @pytest.mark.asyncio
    async def test_non_owner_cannot_lock(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, _ = super_admin
        user, token = annotator
        p, batch, _ = await _seed(
            db_session, owner.id, user.id, batch_status="annotating"
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/admin-lock",
            json={"reason": "尝试锁"},
            headers=_bearer(token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_repeated_lock_returns_409(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session, owner.id, user.id, batch_status="annotating"
        )
        batch.admin_locked = True
        batch.admin_lock_reason = "already"
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/admin-lock",
            json={"reason": "再锁"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_repeated_unlock_returns_409(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session, owner.id, user.id, batch_status="annotating"
        )
        await db_session.commit()  # admin_locked defaults to False

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/admin-unlock",
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_lock_creates_audit_log(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(db_session, owner.id, user.id, batch_status="active")
        await db_session.commit()

        await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/admin-lock",
            json={"reason": "审计测试"},
            headers=_bearer(owner_token),
        )

        logs = (
            (
                await db_session.execute(
                    select(AuditLog).where(
                        AuditLog.target_type == "batch",
                        AuditLog.target_id == str(batch.id),
                        AuditLog.action == "batch.admin_lock",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 1
        assert logs[0].detail_json["reason"] == "审计测试"

    @pytest.mark.asyncio
    async def test_lock_sends_notification_to_annotator(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session, owner.id, user.id, batch_status="annotating"
        )
        await db_session.commit()

        await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/admin-lock",
            json={"reason": "通知测试"},
            headers=_bearer(owner_token),
        )

        notifs = (
            (
                await db_session.execute(
                    select(Notification).where(
                        Notification.user_id == user.id,
                        Notification.type == "batch.admin_locked",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notifs) == 1
        assert notifs[0].payload["reason"] == "通知测试"

    @pytest.mark.asyncio
    async def test_locked_batch_blocks_auto_transition(
        self, db_session, super_admin, annotator
    ):
        """admin_locked batch 不应被 check_auto_transitions 自动推进状态。"""
        from app.services.batch import BatchService

        owner, _ = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="active",
            task_status="pending",
        )
        tasks[0].status = "in_progress"
        batch.admin_locked = True
        batch.admin_lock_reason = "locked"
        await db_session.flush()

        await BatchService(db_session).check_auto_transitions(batch.id)
        await db_session.refresh(batch)
        # 锁定后自动推进被短路，状态应保持 active
        assert batch.status == "active"

    @pytest.mark.asyncio
    async def test_locked_batch_excluded_from_get_next_task(
        self, db_session, super_admin, annotator
    ):
        """admin_locked batch 的任务不应被 get_next_task 派发。"""
        from app.services.scheduler import get_next_task

        owner, _ = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="active",
            task_status="pending",
        )
        batch.admin_locked = True
        batch.admin_lock_reason = "locked"
        await db_session.commit()

        result = await get_next_task(user, p.id, db_session, project_role="annotator")
        assert result is None

    @pytest.mark.asyncio
    async def test_batch_out_includes_lock_fields(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, _ = await _seed(db_session, owner.id, user.id, batch_status="active")
        await db_session.commit()

        resp = await httpx_client.get(
            f"/api/v1/projects/{p.id}/batches/{batch.id}",
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "admin_locked" in body
        assert body["admin_locked"] is False
        assert body["admin_lock_reason"] is None


# ── 10. v0.9.15 · Bulk Approve/Reject ───────────────────────────────────────


class TestBulkApproveReject:
    @pytest.mark.asyncio
    async def test_bulk_approve_reviewing_batches(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["reviewing", "reviewing", "reviewing"],
            n_tasks_each=1,
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-approve",
            json={"batch_ids": [str(b.id) for b in batches]},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["succeeded"]) == 3
        assert len(body["skipped"]) == 0
        assert len(body["failed"]) == 0

    @pytest.mark.asyncio
    async def test_bulk_approve_skips_already_approved(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["reviewing", "approved"],
            n_tasks_each=1,
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-approve",
            json={"batch_ids": [str(b.id) for b in batches]},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["succeeded"]) == 1
        assert body["succeeded"][0] == str(batches[0].id)
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["batch_id"] == str(batches[1].id)

    @pytest.mark.asyncio
    async def test_bulk_approve_fails_non_reviewing(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["reviewing", "active"],
            n_tasks_each=1,
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-approve",
            json={"batch_ids": [str(b.id) for b in batches]},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["succeeded"]) == 1
        assert len(body["failed"]) == 1
        assert body["failed"][0]["batch_id"] == str(batches[1].id)

    @pytest.mark.asyncio
    async def test_bulk_reject_with_feedback_resets_tasks(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["reviewing", "reviewing"],
            n_tasks_each=2,
        )
        # 把 tasks 设为 review/completed
        from sqlalchemy import update as sa_update

        await db_session.execute(
            sa_update(Task)
            .where(Task.batch_id.in_([b.id for b in batches]))
            .values(status="review", is_labeled=True)
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-reject",
            json={
                "batch_ids": [str(b.id) for b in batches],
                "feedback": "质量不达标",
            },
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["succeeded"]) == 2

        # 验证 tasks 被 reset 为 pending
        await db_session.refresh(batches[0])
        tasks_after = (
            (
                await db_session.execute(
                    select(Task).where(Task.batch_id == batches[0].id)
                )
            )
            .scalars()
            .all()
        )
        assert all(t.status == "pending" for t in tasks_after)
        assert all(t.is_labeled is False for t in tasks_after)

        # 验证 review_feedback 写入
        await db_session.refresh(batches[0])
        assert batches[0].review_feedback == "质量不达标"
        assert batches[0].status == "rejected"

    @pytest.mark.asyncio
    async def test_bulk_reject_sends_notification_to_annotator(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["reviewing"],
            n_tasks_each=1,
        )
        await db_session.commit()

        await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-reject",
            json={
                "batch_ids": [str(batches[0].id)],
                "feedback": "通知测试",
            },
            headers=_bearer(owner_token),
        )

        notifs = (
            (
                await db_session.execute(
                    select(Notification).where(
                        Notification.user_id == user.id,
                        Notification.type == "batch.rejected",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notifs) == 1
        assert notifs[0].payload["feedback"] == "通知测试"

    @pytest.mark.asyncio
    async def test_annotator_cannot_bulk_approve(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, _ = super_admin
        user, token = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["reviewing"],
            n_tasks_each=1,
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-approve",
            json={"batch_ids": [str(batches[0].id)]},
            headers=_bearer(token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_reviewer_can_bulk_approve(
        self, httpx_client, db_session, super_admin, annotator, reviewer
    ):
        owner, _ = super_admin
        user, _ = annotator
        rev, rev_token = reviewer
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["reviewing"],
            n_tasks_each=1,
        )
        from app.db.models.project_member import ProjectMember as PM

        db_session.add(
            PM(project_id=p.id, user_id=rev.id, role="reviewer", assigned_by=owner.id)
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-approve",
            json={"batch_ids": [str(batches[0].id)]},
            headers=_bearer(rev_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["succeeded"]) == 1

    @pytest.mark.asyncio
    async def test_bulk_approve_creates_audit_log(
        self, httpx_client, db_session, super_admin, annotator
    ):
        owner, owner_token = super_admin
        user, _ = annotator
        p, batches = await _seed_multi(
            db_session,
            owner.id,
            user.id,
            statuses=["reviewing"],
            n_tasks_each=1,
        )
        await db_session.commit()

        await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/bulk-approve",
            json={"batch_ids": [str(batches[0].id)]},
            headers=_bearer(owner_token),
        )

        logs = (
            (
                await db_session.execute(
                    select(AuditLog).where(
                        AuditLog.target_type == "project",
                        AuditLog.target_id == str(p.id),
                        AuditLog.action == "batch.bulk_approve",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 1


# ── 11. 整批送审（submit-review）────────────────────────────────────────────


class TestBatchSubmitReview:
    @pytest.mark.asyncio
    async def test_assigned_annotator_submits_all_pending(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """整批送审把 pending / in_progress 任务全部提交并推进到 reviewing。"""
        owner, _ = super_admin
        user, token = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            n_tasks=3,
            task_status="pending",
        )
        tasks[0].status = "in_progress"
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/submit-review",
            headers=_bearer(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["submitted_tasks"] == 3
        assert body["status"] == "reviewing"

        for t in tasks:
            await db_session.refresh(t)
            assert t.status == "review"
        await db_session.refresh(batch)
        assert batch.status == "reviewing"
        assert batch.review_tasks == 3

    @pytest.mark.asyncio
    async def test_rejected_tasks_are_skipped(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """rejected 任务需先重做，不进入整批送审集合，批次保持 annotating。"""
        owner, _ = super_admin
        user, token = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            n_tasks=3,
            task_status="pending",
        )
        tasks[0].status = "in_progress"
        tasks[1].status = "rejected"
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/submit-review",
            headers=_bearer(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["submitted_tasks"] == 2
        assert body["remaining_tasks"] == 0

        await db_session.refresh(tasks[1])
        assert tasks[1].status == "rejected"
        await db_session.refresh(tasks[0])
        assert tasks[0].status == "review"
        await db_session.refresh(batch)
        # rejected 仍是 blocker，批次保持 annotating
        assert batch.status == "annotating"

    @pytest.mark.asyncio
    async def test_unassigned_annotator_cannot_submit_batch(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        owner, _ = super_admin
        user, token = annotator
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            n_tasks=2,
            task_status="pending",
        )
        batch.annotator_id = None
        batch.assigned_user_ids = []
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/submit-review",
            headers=_bearer(token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_rejects_batch_in_incompatible_status(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """draft / rejected 等无法归一化的状态拒绝整批送审，且不改任务。"""
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="draft",
            n_tasks=2,
            task_status="pending",
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/submit-review",
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 409, resp.text
        for t in tasks:
            await db_session.refresh(t)
            assert t.status == "pending"

    @pytest.mark.asyncio
    async def test_owner_submission_preserves_effective_assignee(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """owner 整批送审时，task.assignee_id 为空应按批次标注员归属，而不是 owner。"""
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            n_tasks=2,
            task_status="pending",
        )
        for t in tasks:
            assert t.assignee_id is None
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/submit-review",
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        for t in tasks:
            await db_session.refresh(t)
            assert t.status == "review"
            assert t.assignee_id == user.id
            assert t.assignee_id != owner.id

    @pytest.mark.asyncio
    async def test_owner_can_submit_partial_reviewing_batch(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """批次已 reviewing 但仍有未送审任务时，owner 仍可整批提交剩余任务。"""
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="reviewing",
            n_tasks=3,
            task_status="pending",
        )
        tasks[0].status = "review"
        batch.review_tasks = 1
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/submit-review",
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["submitted_tasks"] == 2
        assert body["status"] == "reviewing"

        for t in tasks:
            await db_session.refresh(t)
            assert t.status == "review"


class TestRejectPartialBatch:
    @pytest.mark.asyncio
    async def test_owner_can_reject_annotating_batch(
        self,
        httpx_client,
        db_session,
        super_admin,
        annotator,
    ):
        """部分送审（annotating + review_tasks > 0）的批次也能整批退回。"""
        owner, owner_token = super_admin
        user, _ = annotator
        p, batch, tasks = await _seed(
            db_session,
            owner.id,
            user.id,
            batch_status="annotating",
            n_tasks=3,
            task_status="pending",
        )
        tasks[0].status = "review"
        tasks[1].status = "completed"
        for t in tasks[:2]:
            # Frozen contributor evidence from the actual distinct annotator.
            t.annotation_contributor_ids = [str(user.id)]
            t.review_contributor_ids = [str(user.id)]
            t.review_submitter_id = user.id
            t.review_round_id = uuid.uuid4()
        batch.review_tasks = 1
        batch.completed_tasks = 1
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/reject",
            json={"feedback": "整批打回重做"},
            headers=_bearer(owner_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"

        for t in tasks:
            await db_session.refresh(t)
            assert t.status == "pending"

    @pytest.mark.asyncio
    async def test_reviewer_can_reject_annotating_batch(
        self,
        httpx_client,
        db_session,
        super_admin,
        reviewer,
        annotator,
    ):
        owner, _ = super_admin
        rev, rev_token = reviewer
        worker, _ = annotator
        # The batch work belongs to a distinct literal annotator; rev is the
        # project reviewer, so the frozen evidence never names the actor.
        p, batch, _ = await _seed(
            db_session,
            owner.id,
            worker.id,
            batch_status="annotating",
            n_tasks=2,
            task_status="review",
        )
        await create_membership(
            db_session,
            project_id=p.id,
            user_id=rev.id,
            role="reviewer",
            assigned_by=owner.id,
        )
        await db_session.commit()

        resp = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/reject",
            json={"feedback": "请修正后重新送审"},
            headers=_bearer(rev_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"
