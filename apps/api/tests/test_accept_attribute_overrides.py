"""v0.18.3 · 采纳预测时带 attribute_overrides (工作台候选属性审阅 + 分步采纳).

多阶段预标的 select 属性可在采纳前改值, 经 accept 端点 attribute_overrides 原子落库;
内部键 (_shape_index) 不被 override 干扰。
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.display_id import next_display_id
from tests.factory import build_tool_bindings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_prediction(db, owner_id):
    project = Project(
        id=uuid.uuid4(),
        display_id=await next_display_id(db, "projects"),
        name="accept-override",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db.add(project)
    await db.flush()
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-{uuid.uuid4().hex[:8]}",
        file_name="s.jpg",
        file_path="/tmp/s.jpg",
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    pred = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        model_version="test",
        score=0.9,
        result=[
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 0,
                    "y": 0,
                    "width": 10,
                    "height": 10,
                    "rectanglelabels": ["car"],
                },
                "score": 0.9,
                "attributes": {"color": "blue", "vehicle_type": "bus"},
            }
        ],
    )
    db.add(pred)
    await db.commit()
    return project, task, pred


@pytest.mark.asyncio
async def test_accept_without_overrides_keeps_predicted_attrs(
    httpx_client, db_session, super_admin
):
    owner, token = super_admin
    _proj, task, pred = await _seed_prediction(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{pred.id}/accept?shape_index=0",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # v0.20.22 · 响应仅含本次新建 (单 shape → 1 条), 不再是整题全量。
    assert len(data) == 1
    ann = data[0]
    assert ann["source"] == "prediction_based"
    assert ann["attributes"]["color"] == "blue"
    assert ann["attributes"]["vehicle_type"] == "bus"


@pytest.mark.asyncio
async def test_accept_with_overrides_applies_edited_values(
    httpx_client, db_session, super_admin
):
    owner, token = super_admin
    _proj, task, pred = await _seed_prediction(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{pred.id}/accept?shape_index=0",
        headers=_bearer(token),
        # 审阅时把 color 改 blue→white; vehicle_type 保持
        json={"attribute_overrides": {"color": "white", "_shape_index": 999}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1  # v0.20.22 · 只返回本次新建
    ann = data[0]
    assert ann["source"] == "prediction_based"
    assert ann["attributes"]["color"] == "white"  # 改后值落库
    assert ann["attributes"]["vehicle_type"] == "bus"  # 未改保留
    # 内部键不被 override 干扰 (权威 _shape_index=0, 非 override 的 999)
    assert ann["attributes"]["_shape_index"] == 0
