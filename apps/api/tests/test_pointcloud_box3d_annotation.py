"""点云 3D 框 / 分割标注 service 层校验.

标注 CRUD 链路支持 Box3DGeometry 和 lidar_box_3d 工具单位。
Geometry 判别联合的 box_3d / point_mask_3d 分发由 test_jsonb_strong_types 覆盖。
本测试约束 service 层的创建行为：

- create: lidar_box_3d 配了类集合且 class_name 命中 → 落库, geometry 原样
  存为 box_3d (center/size/rotation 各 len 3)
- create: class_name 不在 lidar_box_3d 类集合内 → 422 (与 2D 同 _validate_class_name)
- create: 空 tool_bindings → 类集合空 → 放行 (向后兼容, 与 2D 同语义)
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.annotation import AnnotationService
from tests.factory import create_project, create_task

# 一个 7-DoF 框: center=[x,y,z] (米), size=[长,宽,高], rotation=[rx,ry,yaw] (弧度, 仅 yaw 非零)。
_BOX_GEOM = {
    "type": "box_3d",
    "center": [12.0, -3.5, 0.8],
    "size": [4.0, 1.8, 1.6],
    "rotation": [0.0, 0.0, 0.5],
}


def _lidar_tb(classes: list[dict]) -> dict:
    return {
        "lidar_box_3d": {
            "enabled": True,
            "classes": classes,
            "attribute_schema": {"fields": []},
        }
    }


def _lidar_with_point_mask_tb(
    box_classes: list[dict], mask_classes: list[dict]
) -> dict:
    return {
        "lidar_box_3d": {
            "enabled": True,
            "classes": box_classes,
            "attribute_schema": {"fields": []},
        },
        "point_mask_3d": {
            "enabled": True,
            "classes": mask_classes,
            "attribute_schema": {"fields": []},
        },
    }


@pytest.mark.asyncio
async def test_create_box3d_allowed_class_passes(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="lidar")
    proj.tool_bindings = _lidar_tb(
        [{"name": "car", "order": 0}, {"name": "pedestrian", "order": 1}]
    )
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    ann = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="box_3d",
        class_name="car",
        geometry=_BOX_GEOM,
        tool_unit_id="lidar_box_3d",
    )
    assert ann.class_name == "car"
    assert ann.tool_unit_id == "lidar_box_3d"
    assert ann.annotation_type == "box_3d"
    # geometry 原样落库 (JSONB); PSR 三元组各 len 3。
    assert ann.geometry["type"] == "box_3d"
    assert ann.geometry["center"] == [12.0, -3.5, 0.8]
    assert len(ann.geometry["size"]) == 3
    assert len(ann.geometry["rotation"]) == 3


@pytest.mark.asyncio
async def test_create_point_mask3d_uses_point_mask_unit_classes(
    db_session, super_admin
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="lidar")
    proj.tool_bindings = _lidar_with_point_mask_tb(
        box_classes=[{"name": "car", "order": 0}],
        mask_classes=[{"name": "road-surface", "order": 0}],
    )
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    geometry = {
        "type": "point_mask_3d",
        "point_indices": [2, 4, 8],
        "convention_at_create": "iso_8855",
        "decimate_stride": 2,
        "source_point_count": 100,
    }

    svc = AnnotationService(db_session)
    ann = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="point_mask_3d",
        class_name="road-surface",
        geometry=geometry,
        tool_unit_id="point_mask_3d",
    )

    assert ann.class_name == "road-surface"
    assert ann.tool_unit_id == "point_mask_3d"
    assert ann.annotation_type == "point_mask_3d"
    assert ann.geometry == geometry


@pytest.mark.asyncio
async def test_create_box3d_class_not_in_lidar_unit_raises_422(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="lidar")
    proj.tool_bindings = _lidar_tb([{"name": "car", "order": 0}])
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc_info:
        await svc.create(
            task_id=task.id,
            user_id=user.id,
            annotation_type="box_3d",
            class_name="airplane",  # 不在 lidar_box_3d 的 allowed=["car"] 内
            geometry=_BOX_GEOM,
            tool_unit_id="lidar_box_3d",
        )
    assert exc_info.value.status_code == 422
    assert "airplane" in exc_info.value.detail
    assert "lidar_box_3d" in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_box3d_empty_bindings_skips_validation(db_session, super_admin):
    """未配 tool_bindings → 类集合空 → 放行 (与 2D 同向后兼容语义)."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="lidar")
    proj.tool_bindings = {}
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    ann = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="box_3d",
        class_name="anything",
        geometry=_BOX_GEOM,
        tool_unit_id="lidar_box_3d",
    )
    assert ann.class_name == "anything"
    assert ann.tool_unit_id == "lidar_box_3d"
