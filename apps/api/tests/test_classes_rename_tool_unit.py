"""POST /projects/{id}/classes/rename tool-unit scope and persistence behavior.

覆盖:
- 不传 tool_unit_id: 跨所有存储的 binding 一起改，包括禁用单元
- 显式 tool_unit_id: 仅改该 unit, 其它 unit 的同名类不受影响 (强隔离)
- tool_unit_id 不存在 → 404
- 类别已存在于目标 unit → 409
- 空 / 相同名称: 400 / 200 no-op

使用 API POST /projects 建项目, 避免 ORM 直写 tool_bindings 与 conftest SAVEPOINT
事务交互导致的 greenlet 错乱.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from tests.factory import create_task


async def _create_two_unit_project(client, token: str) -> dict:
    """通过 API POST /projects 建带 bbox + region 两 unit 的项目, 两 unit 各一个 person."""
    body = {
        "name": "rename-isolation",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "tool_bindings": {
            "bbox": {
                "enabled": True,
                "classes": [{"name": "person", "color": "#aaaaaa", "order": 0}],
                "attribute_schema": {"fields": []},
            },
            "region": {
                "enabled": True,
                "classes": [{"name": "person", "color": "#bbbbbb", "order": 0}],
                "attribute_schema": {"fields": []},
            },
        },
    }
    res = await client.post(
        "/api/v1/projects",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.asyncio
async def test_rename_class_scoped_to_one_unit_leaves_other_intact(
    httpx_client, super_admin
):
    _, token = super_admin
    proj = await _create_two_unit_project(httpx_client, token)
    res = await httpx_client.post(
        f"/api/v1/projects/{proj['id']}/classes/rename",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "old_name": "person",
            "new_name": "pedestrian",
            "tool_unit_id": "bbox",
        },
    )
    assert res.status_code == 200, res.text
    out = res.json()
    bbox_names = [c["name"] for c in out["tool_bindings"]["bbox"]["classes"]]
    region_names = [c["name"] for c in out["tool_bindings"]["region"]["classes"]]
    assert bbox_names == ["pedestrian"]
    # region unit 的 person 不受影响 (强隔离).
    assert region_names == ["person"]


@pytest.mark.asyncio
async def test_rename_class_without_unit_id_changes_all_units(
    httpx_client, super_admin
):
    _, token = super_admin
    proj = await _create_two_unit_project(httpx_client, token)
    res = await httpx_client.post(
        f"/api/v1/projects/{proj['id']}/classes/rename",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_name": "person", "new_name": "ped"},
    )
    assert res.status_code == 200
    out = res.json()
    assert out["tool_bindings"]["bbox"]["classes"][0]["name"] == "ped"
    assert out["tool_bindings"]["region"]["classes"][0]["name"] == "ped"


@pytest.mark.asyncio
async def test_rename_class_unknown_unit_returns_404(httpx_client, super_admin):
    _, token = super_admin
    proj = await _create_two_unit_project(httpx_client, token)
    res = await httpx_client.post(
        f"/api/v1/projects/{proj['id']}/classes/rename",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "old_name": "person",
            "new_name": "ped",
            "tool_unit_id": "lidar_box_3d",
        },
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_rename_class_conflict_within_unit_returns_409(httpx_client, super_admin):
    _, token = super_admin
    # 单 bbox unit, 两个类 person + pedestrian, 然后 rename person → pedestrian → 409.
    body = {
        "name": "rename-conflict",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "tool_bindings": {
            "bbox": {
                "enabled": True,
                "classes": [
                    {"name": "person", "order": 0},
                    {"name": "pedestrian", "order": 1},
                ],
                "attribute_schema": {"fields": []},
            }
        },
    }
    res = await httpx_client.post(
        "/api/v1/projects",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    pid = res.json()["id"]

    res = await httpx_client.post(
        f"/api/v1/projects/{pid}/classes/rename",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "old_name": "person",
            "new_name": "pedestrian",
            "tool_unit_id": "bbox",
        },
    )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_rename_class_missing_old_returns_404(httpx_client, super_admin):
    _, token = super_admin
    proj = await _create_two_unit_project(httpx_client, token)
    res = await httpx_client.post(
        f"/api/v1/projects/{proj['id']}/classes/rename",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_name": "missing_cls", "new_name": "x"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_rename_class_empty_names_rejected(httpx_client, super_admin):
    _, token = super_admin
    proj = await _create_two_unit_project(httpx_client, token)
    for old, new in (("", "x"), ("person", "  ")):
        res = await httpx_client.post(
            f"/api/v1/projects/{proj['id']}/classes/rename",
            headers={"Authorization": f"Bearer {token}"},
            json={"old_name": old, "new_name": new},
        )
        assert res.status_code == 400


@pytest.mark.asyncio
async def test_rename_class_no_op_when_old_eq_new(httpx_client, super_admin):
    _, token = super_admin
    proj = await _create_two_unit_project(httpx_client, token)
    res = await httpx_client.post(
        f"/api/v1/projects/{proj['id']}/classes/rename",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_name": "person", "new_name": "person"},
    )
    assert res.status_code == 200
    # tool_bindings 应保持不变 (API 返回值反映当前状态).
    out = res.json()
    assert out["tool_bindings"]["bbox"]["classes"][0]["name"] == "person"


@pytest.mark.asyncio
async def test_scoped_rename_updates_annotations_across_project_units(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    project = await _create_two_unit_project(httpx_client, token)
    project_id = uuid.UUID(project["id"])
    task = await create_task(db_session, project_id=project_id)
    db_session.add_all(
        [
            Annotation(
                task_id=task.id,
                project_id=project_id,
                user_id=user.id,
                tool_unit_id=unit_id,
                class_name="person",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                version=7,
            )
            for unit_id in ("bbox", "region")
        ]
    )
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project['id']}/classes/rename",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "old_name": "person",
            "new_name": "pedestrian",
            "tool_unit_id": "bbox",
        },
    )
    assert response.status_code == 200, response.text
    bindings = response.json()["tool_bindings"]
    assert bindings["bbox"]["classes"][0]["name"] == "pedestrian"
    assert bindings["region"]["classes"][0]["name"] == "person"

    db_session.expire_all()
    rows = (
        await db_session.scalars(
            select(Annotation)
            .where(Annotation.project_id == project_id)
            .order_by(Annotation.tool_unit_id)
        )
    ).all()
    assert {(row.tool_unit_id, row.class_name, row.version) for row in rows} == {
        ("bbox", "pedestrian", 7),
        ("region", "pedestrian", 7),
    }
