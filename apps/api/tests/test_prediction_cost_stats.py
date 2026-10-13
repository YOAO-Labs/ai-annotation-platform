"""v0.8.6 F4 · 预测成本卡片端点 GET /admin/prediction-cost-stats 单测。

覆盖：
- 空数据返回零值结构
- 5 个 prediction + 2 failed_prediction → 主聚合数字、failure_rate、total_cost
- by_backend 维度按 backend 分组
- 7d / 30d range 切换正确（数据落在 cutoff 之外的不计入）
- 非 super_admin 角色 403
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID) -> Project:
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-CS-{suffix}",
        name=f"cost-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(proj)
    await db.flush()
    return proj


async def _seed_task(db: AsyncSession, project_id: uuid.UUID) -> Task:
    suffix = uuid.uuid4().hex[:8]
    t = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id=f"T-CS-{suffix}",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        tags=[],
        status="in_progress",
    )
    db.add(t)
    await db.flush()
    return t


async def _seed_backend(
    db: AsyncSession, project_id: uuid.UUID, name: str
) -> MLBackendRegistry:
    # v0.19.0 ADR-0044 · backend 上提为全局注册项 (url unique); cost-stats 按 ml_backend_id
    # join registry 取 backend_name, 无需项目启用关联。
    b = MLBackendRegistry(
        id=uuid.uuid4(),
        name=name,
        url=f"http://example/{name}-{uuid.uuid4().hex[:8]}",
        is_interactive=True,
    )
    db.add(b)
    await db.flush()
    return b


async def _seed_prediction(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    ml_backend_id: uuid.UUID | None,
    cost: float,
    tokens: int,
    inference_ms: int,
    created_at: datetime | None = None,
) -> None:
    p = Prediction(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        ml_backend_id=ml_backend_id,
        result={"boxes": []},
    )
    if created_at:
        p.created_at = created_at
    db.add(p)
    await db.flush()
    db.add(
        PredictionMeta(
            id=uuid.uuid4(),
            prediction_id=p.id,
            inference_time_ms=inference_ms,
            total_cost=cost,
            total_tokens=tokens,
        )
    )
    await db.flush()


async def _seed_failed(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    ml_backend_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> None:
    f = FailedPrediction(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        ml_backend_id=ml_backend_id,
        error_type="TIMEOUT",
        message="boom",
    )
    if created_at:
        f.created_at = created_at
    db.add(f)
    await db.flush()


async def test_prediction_cost_stats_empty_returns_zeros(httpx_client, auth_headers):
    resp = await httpx_client.get(
        "/api/v1/dashboard/admin/prediction-cost-stats?range=30d", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["range"] == "30d"
    assert data["total_predictions"] == 0
    assert data["failed_predictions"] == 0
    assert data["failure_rate"] == 0.0
    assert data["total_cost"] == 0.0
    assert data["total_tokens"] == 0
    assert data["by_backend"] == []


async def test_prediction_cost_stats_aggregates_with_data(
    httpx_client, auth_headers, db_session, super_admin
):
    user, _ = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend_a = await _seed_backend(db_session, proj.id, "alpha")
    backend_b = await _seed_backend(db_session, proj.id, "beta")

    # 3 success on backend_a
    for _ in range(3):
        await _seed_prediction(
            db_session,
            project_id=proj.id,
            task_id=task.id,
            ml_backend_id=backend_a.id,
            cost=0.10,
            tokens=100,
            inference_ms=200,
        )
    # 2 success on backend_b
    for _ in range(2):
        await _seed_prediction(
            db_session,
            project_id=proj.id,
            task_id=task.id,
            ml_backend_id=backend_b.id,
            cost=0.25,
            tokens=200,
            inference_ms=400,
        )
    # 2 failed on backend_a
    for _ in range(2):
        await _seed_failed(
            db_session,
            project_id=proj.id,
            task_id=task.id,
            ml_backend_id=backend_a.id,
        )
    await db_session.flush()
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/dashboard/admin/prediction-cost-stats?range=30d", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_predictions"] == 5
    assert data["failed_predictions"] == 2
    # 5 / (5 + 2) = 0.2857 → fail_rate = 2/7
    assert abs(data["failure_rate"] - (2 / 7)) < 1e-3
    # 3*0.10 + 2*0.25 = 0.80
    assert abs(data["total_cost"] - 0.80) < 1e-6
    # 3*100 + 2*200 = 700
    assert data["total_tokens"] == 700
    # avg_inference_time_ms = (3*200 + 2*400) / 5 = 280
    assert abs(data["avg_inference_time_ms"] - 280.0) < 1.0

    # by_backend
    by = {b["backend_name"]: b for b in data["by_backend"]}
    assert by["alpha"]["predictions"] == 3
    assert by["alpha"]["failures"] == 2
    assert abs(by["alpha"]["total_cost"] - 0.30) < 1e-6
    assert by["beta"]["predictions"] == 2
    assert by["beta"]["failures"] == 0


async def test_prediction_cost_stats_range_7d_excludes_old_rows(
    httpx_client, auth_headers, db_session, super_admin
):
    user, _ = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id, "gamma")

    # 1 row 5 days ago — 在 7d 内
    await _seed_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        ml_backend_id=backend.id,
        cost=0.05,
        tokens=50,
        inference_ms=100,
        created_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    # 1 row 20 days ago — 7d 之外, 30d 内
    await _seed_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        ml_backend_id=backend.id,
        cost=0.50,
        tokens=500,
        inference_ms=1000,
        created_at=datetime.now(timezone.utc) - timedelta(days=20),
    )
    await db_session.commit()

    r7 = (
        await httpx_client.get(
            "/api/v1/dashboard/admin/prediction-cost-stats?range=7d",
            headers=auth_headers,
        )
    ).json()
    assert r7["total_predictions"] == 1
    assert abs(r7["total_cost"] - 0.05) < 1e-6

    r30 = (
        await httpx_client.get(
            "/api/v1/dashboard/admin/prediction-cost-stats?range=30d",
            headers=auth_headers,
        )
    ).json()
    assert r30["total_predictions"] == 2
    assert abs(r30["total_cost"] - 0.55) < 1e-6


async def test_prediction_cost_stats_invalid_range_returns_422(
    httpx_client, auth_headers
):
    resp = await httpx_client.get(
        "/api/v1/dashboard/admin/prediction-cost-stats?range=90d",
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_prediction_cost_stats_requires_super_admin(httpx_client, annotator):
    _, token = annotator
    resp = await httpx_client.get(
        "/api/v1/dashboard/admin/prediction-cost-stats?range=30d",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_prediction_cost_stats_p50_p95_p99(
    httpx_client, auth_headers, db_session, super_admin
):
    """v0.8.7 F2 · PERCENTILE_CONT 聚合 inference_time_ms。"""
    user, _ = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id, "perc")

    # 100 个预测，inference_ms 从 10 到 1000 step 10
    for ms in range(10, 1010, 10):
        await _seed_prediction(
            db_session,
            project_id=proj.id,
            task_id=task.id,
            ml_backend_id=backend.id,
            cost=0.0,
            tokens=0,
            inference_ms=ms,
        )
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/dashboard/admin/prediction-cost-stats?range=30d",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_predictions"] == 100
    # 100 个均匀样本：P50 ≈ 505，P95 ≈ 955，P99 ≈ 991（PERCENTILE_CONT 线性插值）
    assert abs(data["p50_inference_time_ms"] - 505.0) <= 5.0
    assert abs(data["p95_inference_time_ms"] - 955.0) <= 5.0
    assert abs(data["p99_inference_time_ms"] - 991.0) <= 5.0
