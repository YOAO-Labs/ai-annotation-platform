"""v0.6.5 · 任务状态机锁定与撤回 / 重开流程。

覆盖场景：
1. submit / withdraw / claim / approve / reject / reopen 状态流转完整一遍
2. 编辑端点拦截：status=review/completed 时 PATCH annotation → 409 task_locked
3. 撤回门控：reviewer 已 claim 后再 withdraw → 409 task_already_claimed
4. 权限：非 assignee 调 withdraw / reopen → 403
5. reject 持久化：reject_reason 必填、写入 DB
6. audit：每个状态变更各产 1 条 audit_log

v0.6.6 起：本文件内的 test_engine / db_session / httpx_client 已回写到 conftest.py
（function-scoped engine + dependency_overrides[get_db]），不再需要 file-local override。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from tests.factory import create_membership, build_tool_bindings


async def _seed_project_and_task(
    db: AsyncSession,
    owner_id: uuid.UUID,
    assignee_id: uuid.UUID,
    reviewer_id: uuid.UUID | None = None,
) -> tuple[Project, Task]:
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-T-{suffix}",
        name="任务锁测试项目",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car", "person"]),
        review_tasks=0,
        completed_tasks=0,
    )
    db.add(project)
    await db.flush()

    # Literal employees; authority comes only from these explicit membership rows.
    await create_membership(
        db,
        project_id=project.id,
        user_id=assignee_id,
        role="annotator",
        assigned_by=owner_id,
    )
    if reviewer_id is not None and reviewer_id != assignee_id:
        await create_membership(
            db,
            project_id=project.id,
            user_id=reviewer_id,
            role="reviewer",
            assigned_by=owner_id,
        )

    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-T-{suffix}",
        name="任务锁测试批次",
        status="active",
        annotator_id=assignee_id,
        total_tasks=1,
    )
    db.add(batch)
    await db.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-T-{suffix}",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        tags=[],
        status="in_progress",
        assignee_id=assignee_id,
        batch_id=batch.id,
    )
    db.add(task)
    await db.flush()
    return project, task


async def _create_annotation(
    db: AsyncSession, task: Task, user_id: uuid.UUID
) -> Annotation:
    ann = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=task.project_id,
        user_id=user_id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        confidence=1.0,
        is_active=True,
        attributes={},
    )
    db.add(ann)
    await db.flush()
    return ann


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestTaskLockFlow:
    async def test_full_state_machine_roundtrip(
        self, httpx_client, db_session, annotator, reviewer
    ):
        ann_user, ann_token = annotator
        rev_user, rev_token = reviewer
        _, task = await _seed_project_and_task(
            db_session,
            owner_id=ann_user.id,
            assignee_id=ann_user.id,
            reviewer_id=rev_user.id,
        )
        tid = str(task.id)

        # 1) submit
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/submit", headers=_bearer(ann_token)
        )
        assert r.status_code == 200, r.text
        await db_session.refresh(task)
        assert task.status == "review"
        assert task.submitted_at is not None
        assert task.reviewer_claimed_at is None

        # 2) withdraw（reviewer 未 claim）
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/withdraw", headers=_bearer(ann_token)
        )
        assert r.status_code == 200, r.text
        await db_session.refresh(task)
        assert task.status == "in_progress"
        assert task.submitted_at is None

        # 3) submit 又一次
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/submit", headers=_bearer(ann_token)
        )
        assert r.status_code == 200

        # 4) reviewer claim
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/review/claim", headers=_bearer(rev_token)
        )
        assert r.status_code == 200
        body = r.json()
        assert body["is_self"] is True
        assert body["reviewer_id"] == str(rev_user.id)
        await db_session.refresh(task)
        assert task.reviewer_claimed_at is not None

        # 5) withdraw 已被 claim → 409 task_already_claimed
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/withdraw", headers=_bearer(ann_token)
        )
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "task_already_claimed"

        # 6) approve
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/review/approve", headers=_bearer(rev_token)
        )
        assert r.status_code == 200
        await db_session.refresh(task)
        assert task.status == "completed"
        assert task.reviewed_at is not None
        assert task.reviewer_id == rev_user.id

        # 7) reopen 由 assignee 单方面发起
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/reopen", headers=_bearer(ann_token)
        )
        assert r.status_code == 200
        await db_session.refresh(task)
        assert task.status == "in_progress"
        assert task.reopened_count == 1
        assert task.reviewer_id is None
        assert task.reviewer_claimed_at is None

    async def test_edit_endpoints_locked_in_review(
        self, httpx_client, db_session, annotator
    ):
        ann_user, ann_token = annotator
        _, task = await _seed_project_and_task(
            db_session, owner_id=ann_user.id, assignee_id=ann_user.id
        )
        ann = await _create_annotation(db_session, task, ann_user.id)
        tid, aid = str(task.id), str(ann.id)

        # 进入 review
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/submit", headers=_bearer(ann_token)
        )
        assert r.status_code == 200

        # PATCH annotation 应被拦截
        r = await httpx_client.patch(
            f"/api/v1/tasks/{tid}/annotations/{aid}",
            json={"class_name": "person"},
            headers=_bearer(ann_token),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["reason"] == "task_locked"

        # DELETE 也被拦截
        r = await httpx_client.delete(
            f"/api/v1/tasks/{tid}/annotations/{aid}",
            headers=_bearer(ann_token),
        )
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "task_locked"

        # 新增也被拦截
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/annotations",
            json={
                "annotation_type": "bbox",
                "class_name": "car",
                "geometry": {"type": "bbox", "x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1},
            },
            headers=_bearer(ann_token),
        )
        assert r.status_code == 409

    async def test_withdraw_requires_assignee(
        self, httpx_client, db_session, annotator, reviewer
    ):
        ann_user, ann_token = annotator
        rev_user, rev_token = reviewer
        _, task = await _seed_project_and_task(
            db_session,
            owner_id=ann_user.id,
            assignee_id=ann_user.id,
            reviewer_id=rev_user.id,
        )
        tid = str(task.id)

        await httpx_client.post(
            f"/api/v1/tasks/{tid}/submit", headers=_bearer(ann_token)
        )

        # reviewer 不是 assignee + 不是 admin → 403
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/withdraw", headers=_bearer(rev_token)
        )
        assert r.status_code == 403

    async def test_reject_requires_reason_type_and_persists(
        self, httpx_client, db_session, annotator, reviewer
    ):
        """v0.10.16: reject 必传 reason_type ∈ {missing, extra, wrong_label,
        wrong_geometry}，reason 自由文本变可空补充。"""
        ann_user, ann_token = annotator
        rev_user, rev_token = reviewer
        _, task = await _seed_project_and_task(
            db_session,
            owner_id=ann_user.id,
            assignee_id=ann_user.id,
            reviewer_id=rev_user.id,
        )
        tid = str(task.id)

        await httpx_client.post(
            f"/api/v1/tasks/{tid}/submit", headers=_bearer(ann_token)
        )
        claim = await httpx_client.post(
            f"/api/v1/tasks/{tid}/review/claim", headers=_bearer(rev_token)
        )
        assert claim.status_code == 200

        # 缺 reason_type → 422
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/review/reject", headers=_bearer(rev_token)
        )
        assert r.status_code == 422
        # 仅传 reason 不带 type → 422
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/review/reject",
            json={"reason": "框漏了 3 处行人"},
            headers=_bearer(rev_token),
        )
        assert r.status_code == 422
        # 非法 type → Pydantic 422
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/review/reject",
            json={"reason_type": "garbage"},
            headers=_bearer(rev_token),
        )
        assert r.status_code == 422

        # 合法 type + 自由文本 → 持久化两字段
        r = await httpx_client.post(
            f"/api/v1/tasks/{tid}/review/reject",
            json={"reason_type": "missing", "reason": "框漏了 3 处行人"},
            headers=_bearer(rev_token),
        )
        assert r.status_code == 200
        await db_session.refresh(task)
        assert task.status == "rejected"
        assert task.reject_reason_type == "missing"
        assert task.reject_reason == "框漏了 3 处行人"

    async def test_state_transitions_emit_audit_logs(
        self, httpx_client, db_session, annotator, reviewer
    ):
        ann_user, ann_token = annotator
        rev_user, rev_token = reviewer
        _, task = await _seed_project_and_task(
            db_session,
            owner_id=ann_user.id,
            assignee_id=ann_user.id,
            reviewer_id=rev_user.id,
        )
        tid = str(task.id)

        await httpx_client.post(
            f"/api/v1/tasks/{tid}/submit", headers=_bearer(ann_token)
        )
        await httpx_client.post(
            f"/api/v1/tasks/{tid}/withdraw", headers=_bearer(ann_token)
        )
        await httpx_client.post(
            f"/api/v1/tasks/{tid}/submit", headers=_bearer(ann_token)
        )
        await httpx_client.post(
            f"/api/v1/tasks/{tid}/review/claim", headers=_bearer(rev_token)
        )
        await httpx_client.post(
            f"/api/v1/tasks/{tid}/review/approve", headers=_bearer(rev_token)
        )
        await httpx_client.post(
            f"/api/v1/tasks/{tid}/reopen", headers=_bearer(ann_token)
        )

        rows = (
            (
                await db_session.execute(
                    select(AuditLog)
                    .where(AuditLog.target_type == "task", AuditLog.target_id == tid)
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
        actions = [r.action for r in rows]
        assert actions == [
            "task.submit",
            "task.withdraw",
            "task.submit",
            "task.review_claim",
            "task.approve",
            "task.reopen",
        ]
        # reopen 的 detail 含 original_reviewer_id
        reopen_log = rows[-1]
        assert reopen_log.detail_json.get("original_reviewer_id") is not None
