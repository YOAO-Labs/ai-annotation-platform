"""interactive-annotating 的 DINO 阈值透传契约。

v0.9.2 曾让平台把 project 级 box_threshold / text_threshold 注入 context；自
59dfffa(AI 参数迁至工作台悬浮面板, 按后端 /setup.params 动态渲染并按用户持久化)起,
平台**不再**注入项目级阈值——那会把 gsam2 专属参数塞给 sam3 等不支持的后端。阈值现由
前端随 context 透传, 客户端给什么就用什么, 平台只做透传。
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.ml_client import PredictionResult
from tests.conftest import create_registry_with_pool


async def _seed(db, owner_id, *, box=0.4, text=0.3):
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-IT-{suffix}",
        name=f"it-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
        box_threshold=box,
        text_threshold=text,
    )
    db.add(proj)
    await db.flush()

    # ADR-0044 · 全局注册项 + 为项目启用 (交互端点授权读 ProjectMLBackendPool.enabled)。
    backend, pool = await create_registry_with_pool(
        db,
        name="dino-sam2",
        url=f"http://example-{suffix}/",
        is_interactive=True,
        state="connected",
        enabled_pool=True,
    )
    db.add(ProjectMLBackendPool(project_id=proj.id, pool_id=pool.id, enabled=True))
    await db.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id=f"T-IT-{suffix}",
        file_name="img.jpg",
        file_path="http://example/img.jpg",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return proj, backend, task


@pytest.fixture
def patched_client():
    """Mock MLBackendClient.predict_interactive 抓取调用上下文。"""
    captured: dict = {}

    async def fake_setup(self):
        return {
            "models": [
                {
                    "id": "interactive",
                    "task": "interactive_seg",
                    "is_interactive": True,
                    "supported_prompts": ["point", "interactive_box", "text"],
                    "supported_inputs": [
                        "full_image",
                        "point_prompt",
                        "bbox_prompt",
                    ],
                    "supported_geometric_outputs": ["polygon"],
                }
            ]
        }

    async def fake_predict_interactive(self, task_data, context):
        captured["task_data"] = task_data
        captured["context"] = context
        return PredictionResult(
            task_id=task_data["id"],
            result=[],
            score=None,
            model_version="mock",
            inference_time_ms=1,
            cache_hit=False,
            model_load_ms=123,
        )

    with (
        patch(
            "app.services.ml_client.MLBackendClient.predict_interactive",
            new=fake_predict_interactive,
        ),
        patch(
            "app.services.ml_client.MLBackendClient.setup",
            new=fake_setup,
        ),
    ):
        yield captured


async def test_text_prompt_does_not_inject_project_thresholds(
    httpx_client, super_admin, db_session, patched_client
):
    """自 59dfffa 起平台不再注入项目级阈值; 客户端未给阈值时 context 里就没有。"""
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id, box=0.42, text=0.18)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/interactive-annotating",
        json={"task_id": str(task.id), "context": {"type": "text", "text": "person"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    ctx = patched_client["context"]
    assert ctx["type"] == "text"
    assert "box_threshold" not in ctx
    assert "text_threshold" not in ctx


async def test_text_prompt_respects_explicit_client_thresholds(
    httpx_client, super_admin, db_session, patched_client
):
    """客户端显式传值则尊重客户端，不被 project 默认覆盖。"""
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id, box=0.5, text=0.5)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/interactive-annotating",
        json={
            "task_id": str(task.id),
            "context": {
                "type": "text",
                "text": "car",
                "box_threshold": 0.7,
                "text_threshold": 0.6,
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    ctx = patched_client["context"]
    assert ctx["box_threshold"] == pytest.approx(0.7)
    assert ctx["text_threshold"] == pytest.approx(0.6)


async def test_point_prompt_does_not_inject_thresholds(
    httpx_client, super_admin, db_session, patched_client
):
    """point/bbox 不走 DINO，不应注入阈值（避免污染缓存键 / 协议噪声）。"""
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/interactive-annotating",
        json={
            "task_id": str(task.id),
            "context": {"type": "point", "points": [[0.5, 0.5]], "labels": [1]},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    ctx = patched_client["context"]
    assert "box_threshold" not in ctx
    assert "text_threshold" not in ctx
    data = resp.json()
    assert data["cache_hit"] is False
    assert data["model_load_ms"] == 123
