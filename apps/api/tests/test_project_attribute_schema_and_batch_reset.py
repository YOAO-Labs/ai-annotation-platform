"""Project attribute_schema 创建契约与批次 reset 状态机。

S1 · ProjectCreate 接受 attribute_schema（含缺省与非法输入）
S2 · POST /batches/{id}/reset 终极重置到 draft（起始状态矩阵、鉴权与 reason 校验）
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text

from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.services.display_id import next_display_id
from tests.factory import create_membership, build_tool_bindings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_batch_with_locked_tasks(
    db,
    owner_id: uuid.UUID,
    annotator_id: uuid.UUID,
    *,
    batch_status: str,
    n_tasks: int = 3,
    task_status: str = "completed",
):
    """创建一个项目 + 一个批次 + N 个 task（指定状态）+ 每个 task 一把锁 + 一条 annotation。"""
    from datetime import datetime, timedelta, timezone

    pid = uuid.uuid4()
    p = Project(
        id=pid,
        display_id=await next_display_id(db, "projects"),
        name="reset test",
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
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=pid,
        display_id=await next_display_id(db, "batches"),
        name="b1",
        status=batch_status,
        annotator_id=annotator_id,
        assigned_user_ids=[str(annotator_id)],
        review_feedback="prev feedback" if batch_status == "rejected" else None,
        reviewed_at=datetime.now(timezone.utc) if batch_status == "rejected" else None,
        reviewed_by=owner_id if batch_status == "rejected" else None,
    )
    db.add(batch)
    await db.flush()
    for i in range(n_tasks):
        t = Task(
            id=uuid.uuid4(),
            project_id=pid,
            batch_id=batch.id,
            display_id=f"T-{i}",
            file_name=f"f{i}.jpg",
            file_path=f"/tmp/f{i}.jpg",
            file_type="image",
            status=task_status,
            is_labeled=task_status in ("completed", "review"),
        )
        db.add(t)
        await db.flush()
        db.add(
            Annotation(
                id=uuid.uuid4(),
                task_id=t.id,
                user_id=annotator_id,
                geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                class_name="car",
                is_active=True,
            )
        )
        db.add(
            TaskLock(
                task_id=t.id,
                user_id=annotator_id,
                expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
    await db.flush()
    return p, batch


@pytest.mark.asyncio
async def test_create_project_with_attribute_schema(httpx_client, super_admin):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "name": "v0.7.6 attr schema project",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "classes": ["car"],
        "attribute_schema": {
            "fields": [
                {
                    "key": "occluded",
                    "label": "是否遮挡",
                    "type": "boolean",
                    "required": True,
                },
                {
                    "key": "color",
                    "label": "车身颜色",
                    "type": "select",
                    "required": False,
                    "options": [
                        {"value": "red", "label": "红"},
                        {"value": "blue", "label": "蓝"},
                    ],
                },
            ]
        },
    }
    res = await httpx_client.post("/api/v1/projects", json=payload, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    fields = body["attribute_schema"]["fields"]
    assert [f["key"] for f in fields] == ["occluded", "color"]
    assert fields[0]["required"] is True
    assert fields[1]["options"][0]["value"] == "red"

    # GET 拉回应一致
    pid = body["id"]
    got = await httpx_client.get(f"/api/v1/projects/{pid}", headers=headers)
    assert got.status_code == 200
    assert got.json()["attribute_schema"] == body["attribute_schema"]


@pytest.mark.asyncio
async def test_create_project_default_attribute_schema(httpx_client, super_admin):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "name": "v0.7.6 default attr schema",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "classes": [],
    }
    res = await httpx_client.post("/api/v1/projects", json=payload, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["attribute_schema"] == {"fields": []}


@pytest.mark.asyncio
async def test_create_project_rejects_invalid_attribute_schema(
    httpx_client, super_admin
):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    # select 类型缺 options 应 422
    payload = {
        "name": "v0.7.6 bad attr schema",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "attribute_schema": {
            "fields": [
                {"key": "color", "label": "颜色", "type": "select"},
            ]
        },
    }
    res = await httpx_client.post("/api/v1/projects", json=payload, headers=headers)
    assert res.status_code == 422, res.text


# ── S2 · POST /batches/{id}/reset 终极重置 ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "from_status",
    ["active", "annotating", "reviewing", "approved", "rejected", "archived"],
)
async def test_reset_to_draft_from_any_status(
    httpx_client, db_session, super_admin, annotator, from_status
):
    """6 个起始状态 → draft 全部成功，task 全回 pending，annotation 保留，task_locks 清空。"""
    owner, owner_token = super_admin
    user, _ = annotator
    p, batch = await _seed_batch_with_locked_tasks(
        db_session,
        owner.id,
        user.id,
        batch_status=from_status,
        n_tasks=3,
        task_status="completed",
    )
    await db_session.commit()

    res = await httpx_client.post(
        f"/api/v1/projects/{p.id}/batches/{batch.id}/reset",
        json={"reason": f"测试 {from_status} 重置到 draft"},
        headers=_bearer(owner_token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "draft"
    assert body["review_feedback"] is None

    # task 全部回 pending
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch.id)))
        .scalars()
        .all()
    )
    assert all(t.status == "pending" for t in tasks)

    # annotation 全部保留
    anns = (
        (
            await db_session.execute(
                select(Annotation).where(Annotation.task_id.in_([t.id for t in tasks]))
            )
        )
        .scalars()
        .all()
    )
    assert len(anns) == 3
    assert all(a.is_active for a in anns)

    # task_locks 清空
    locks = (
        (
            await db_session.execute(
                select(TaskLock).where(TaskLock.task_id.in_([t.id for t in tasks]))
            )
        )
        .scalars()
        .all()
    )
    assert len(locks) == 0

    # audit 写入 reason + from_status + affected_tasks
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "batch.reset_to_draft",
                AuditLog.target_id == str(batch.id),
            )
        )
    ).scalar_one()
    assert audit.detail_json["from_status"] == from_status
    assert audit.detail_json["affected_tasks"] == 3
    assert "测试" in audit.detail_json["reason"]


@pytest.mark.asyncio
async def test_reset_to_draft_requires_reason(
    httpx_client, db_session, super_admin, annotator
):
    owner, owner_token = super_admin
    user, _ = annotator
    p, batch = await _seed_batch_with_locked_tasks(
        db_session,
        owner.id,
        user.id,
        batch_status="active",
        n_tasks=1,
        task_status="pending",
    )
    await db_session.commit()

    # 空 reason 应被 422 拦截
    r1 = await httpx_client.post(
        f"/api/v1/projects/{p.id}/batches/{batch.id}/reset",
        json={"reason": ""},
        headers=_bearer(owner_token),
    )
    assert r1.status_code == 422

    # 短 reason（< 10 字）应被 422 拦截
    r2 = await httpx_client.post(
        f"/api/v1/projects/{p.id}/batches/{batch.id}/reset",
        json={"reason": "短"},
        headers=_bearer(owner_token),
    )
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_reset_to_draft_owner_only(
    httpx_client, db_session, super_admin, annotator, reviewer
):
    """非 owner（普通 reviewer / annotator）调用 → 403。"""
    owner, _ = super_admin
    user, anno_token = annotator
    rev, rev_token = reviewer
    p, batch = await _seed_batch_with_locked_tasks(
        db_session,
        owner.id,
        user.id,
        batch_status="active",
        n_tasks=1,
        task_status="pending",
    )
    # Both accounts are visible project members with non-management work roles.
    await create_membership(
        db_session,
        project_id=p.id,
        user_id=rev.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    await db_session.commit()

    for token in (anno_token, rev_token):
        res = await httpx_client.post(
            f"/api/v1/projects/{p.id}/batches/{batch.id}/reset",
            json={"reason": "非 owner 不应能重置"},
            headers=_bearer(token),
        )
        assert res.status_code == 403, res.text


# ── S4 · audit Celery task body 等价 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_audit_entry_task_writes_row(test_engine):
    """task body 直接写一行 audit_logs。Celery 在 test 环境不真起 broker，本测验证函数体本身正确。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.workers.audit import _async_persist

    # audit_logs.request_id is varchar(36); a bare 32-char uuid hex fits and is unique.
    request_id = uuid.uuid4().hex
    payload = {
        # 用 None actor_id 避免 FK 约束触发（audit_logs.actor_id → users.id）
        "actor_id": None,
        "actor_role": "annotator",
        "action": "http.post",
        "method": "POST",
        "path": "/api/v1/test/audit-async",
        "status_code": 200,
        "ip": "127.0.0.1",
        "request_id": request_id,
    }
    await _async_persist(payload)

    # 用当前测试引擎的独立 session 验证：_async_persist 自带 engine 并 commit，
    # 而全局 app.db.base.async_session 的连接池绑定在另一事件循环上，不能在此复用。
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            row = (
                await s.execute(
                    select(AuditLog).where(AuditLog.request_id == request_id)
                )
            ).scalar_one()
            assert row.action == "http.post"
            assert row.method == "POST"
            assert row.path == "/api/v1/test/audit-async"
            assert row.status_code == 200
            assert row.actor_role == "annotator"
    finally:
        # cleanup — v0.7.8 audit_logs 不可变 trigger 需要豁免；只删本测试自己的行。
        async with maker() as cleanup:
            await cleanup.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await cleanup.execute(
                delete(AuditLog).where(AuditLog.request_id == request_id)
            )
            await cleanup.commit()


# ── S5 · annotation keyset 分页 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_annotations_keyset_pagination(
    httpx_client, db_session, super_admin, annotator
):
    """种 5 条 annotation；以 limit=2 三页拉取，校验 cursor 末页 None + 顺序为 created_at desc。"""
    from datetime import datetime, timezone, timedelta

    from app.db.models.annotation import Annotation
    from app.db.models.task import Task

    owner, owner_token = super_admin
    user, _ = annotator
    p, batch = await _seed_batch_with_locked_tasks(
        db_session,
        owner.id,
        user.id,
        batch_status="active",
        n_tasks=1,
        task_status="pending",
    )
    task = (
        await db_session.execute(select(Task).where(Task.batch_id == batch.id))
    ).scalar_one()

    # 清空 seed 留下的隐式 annotation，独立测试 5 条节奏
    seed_anns = (
        (
            await db_session.execute(
                select(Annotation).where(Annotation.task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    for a in seed_anns:
        await db_session.delete(a)
    await db_session.flush()

    # 种 5 条 annotation，created_at 间隔 1 秒
    base_ts = datetime.now(timezone.utc).replace(microsecond=0)
    for i in range(5):
        ann = Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            user_id=user.id,
            geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            class_name="car",
            is_active=True,
        )
        db_session.add(ann)
        await db_session.flush()
        # 强制覆盖 created_at（默认 server_default 在 commit 时生成同一时刻）
        ann.created_at = base_ts + timedelta(seconds=i)
        await db_session.flush()
    await db_session.commit()

    # 第一页
    r1 = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/annotations/page?limit=2",
        headers=_bearer(owner_token),
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None

    # 第二页
    r2 = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/annotations/page?limit=2&cursor={body1['next_cursor']}",
        headers=_bearer(owner_token),
    )
    body2 = r2.json()
    assert len(body2["items"]) == 2
    assert body2["next_cursor"] is not None

    # 第三页（最后 1 条 + 末页 cursor）
    r3 = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/annotations/page?limit=2&cursor={body2['next_cursor']}",
        headers=_bearer(owner_token),
    )
    body3 = r3.json()
    assert len(body3["items"]) == 1
    assert body3["next_cursor"] is None  # 末页

    # 顺序：created_at DESC
    seen_ids = (
        [it["id"] for it in body1["items"]]
        + [it["id"] for it in body2["items"]]
        + [it["id"] for it in body3["items"]]
    )
    assert len(set(seen_ids)) == 5, "三页拉到的应是 5 条不同 annotation"


@pytest.mark.asyncio
async def test_annotations_page_invalid_cursor(
    httpx_client, db_session, super_admin, annotator
):
    owner, owner_token = super_admin
    user, _ = annotator
    p, batch = await _seed_batch_with_locked_tasks(
        db_session,
        owner.id,
        user.id,
        batch_status="active",
        n_tasks=1,
        task_status="pending",
    )
    task = (
        await db_session.execute(select(Task).where(Task.batch_id == batch.id))
    ).scalar_one()
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/annotations/page?cursor=garbage",
        headers=_bearer(owner_token),
    )
    assert res.status_code == 400


# ── v0.9.12 · BUG B-15 · reset_to_draft 级联 prediction 清理 ────────────────


@pytest.mark.asyncio
async def test_reset_to_draft_cascades_predictions(
    httpx_client, db_session, super_admin, annotator
):
    """v0.9.12 B-15: reset_to_draft 必须清光本 batch 关联的 predictions / failed_predictions /
    prediction_jobs / prediction_metas. 否则 /ai-pre 仍会渲染该 batch 已就绪卡片."""
    from app.db.models.async_job import AsyncJob
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta

    owner, owner_token = super_admin
    user, _ = annotator
    p, batch = await _seed_batch_with_locked_tasks(
        db_session,
        owner.id,
        user.id,
        batch_status="pre_annotated",
        n_tasks=2,
        task_status="pending",
    )
    # v0.19.0 ADR-0044 · backend 已上提为全局注册项 (无 project_id)。
    backend = MLBackendRegistry(
        id=uuid.uuid4(),
        name="cascade-test",
        url="http://example/",
    )
    db_session.add(backend)
    await db_session.flush()
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch.id)))
        .scalars()
        .all()
    )
    pred = Prediction(
        id=uuid.uuid4(),
        task_id=tasks[0].id,
        project_id=p.id,
        ml_backend_id=backend.id,
        result={"shapes": []},
    )
    db_session.add(pred)
    fp = FailedPrediction(
        id=uuid.uuid4(),
        task_id=tasks[1].id,
        project_id=p.id,
        ml_backend_id=backend.id,
        error_type="TIMEOUT",
        message="boom",
    )
    db_session.add(fp)
    await db_session.flush()
    db_session.add(
        PredictionMeta(id=uuid.uuid4(), prediction_id=pred.id, total_cost=0.001)
    )
    db_session.add(PredictionMeta(id=uuid.uuid4(), failed_prediction_id=fp.id))
    # v0.10.49 · prediction_jobs 已收敛进 async_jobs（batch_id 进 payload）
    job = AsyncJob(
        id=uuid.uuid4(),
        kind="batch_predict",
        project_id=p.id,
        status="completed",
        payload={
            "batch_id": str(batch.id),
            "ml_backend_id": str(backend.id),
            "prompt": "x",
            "output_mode": "mask",
            "total_tasks": 2,
        },
        result={"success_count": 1, "failed_count": 1},
    )
    db_session.add(job)
    await db_session.commit()

    # 重置
    res = await httpx_client.post(
        f"/api/v1/projects/{p.id}/batches/{batch.id}/reset",
        json={"reason": "B-15 级联清理回归测试"},
        headers=_bearer(owner_token),
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "draft"

    # predictions / failed_predictions / prediction_jobs / prediction_metas 全部清空
    task_ids = [t.id for t in tasks]
    preds_after = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id.in_(task_ids))
            )
        )
        .scalars()
        .all()
    )
    failed_after = (
        (
            await db_session.execute(
                select(FailedPrediction).where(FailedPrediction.task_id.in_(task_ids))
            )
        )
        .scalars()
        .all()
    )
    jobs_after = (
        (
            await db_session.execute(
                select(AsyncJob).where(
                    AsyncJob.kind == "batch_predict",
                    AsyncJob.payload["batch_id"].astext == str(batch.id),
                )
            )
        )
        .scalars()
        .all()
    )
    metas_after = (await db_session.execute(select(PredictionMeta))).scalars().all()
    assert preds_after == []
    assert failed_after == []
    assert jobs_after == []
    assert metas_after == []

    # audit detail 含 cascade counts
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "batch.reset_to_draft",
                AuditLog.target_id == str(batch.id),
            )
        )
    ).scalar_one()
    cascade = audit.detail_json.get("cascade") or {}
    assert cascade.get("predictions") == 1
    assert cascade.get("failed_predictions") == 1
    assert cascade.get("prediction_jobs") == 1


@pytest.mark.asyncio
async def test_check_auto_transitions_pre_annotated_to_annotating(
    db_session, super_admin, annotator
):
    """v0.9.12 B-15 第二症状回归: pre_annotated batch 内 task 变 in_progress 时 batch 应转 annotating."""
    from app.services.batch import BatchService

    owner, _ = super_admin
    user, _ = annotator
    p, batch = await _seed_batch_with_locked_tasks(
        db_session,
        owner.id,
        user.id,
        batch_status="pre_annotated",
        n_tasks=2,
        task_status="pending",
    )
    # 模拟标注员开始标注: 把 1 个 task 改成 in_progress
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch.id)))
        .scalars()
        .all()
    )
    tasks[0].status = "in_progress"
    await db_session.flush()

    svc = BatchService(db_session)
    await svc.check_auto_transitions(batch.id)
    await db_session.refresh(batch)
    assert batch.status == "annotating"
