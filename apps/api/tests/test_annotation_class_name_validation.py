"""v0.10.17 · AnnotationService class_name 软校验 + accept_prediction unit-scoped alias.

覆盖:
- create: tool_unit 配置了类集合且 class_name 不在内 → 422
- create: 集合为空 (旧项目 / 未配置) → 放行
- accept_prediction: 落库前同样走 _validate_class_name 校验
- accept_prediction: alias 只读 prediction.tool_unit_id 对应 unit 的 classes
  (强隔离, 不再跨 unit 走旧 classes_config union)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.db.models.prediction import Prediction
from app.db.models.task import Task
from app.services.annotation import AnnotationService
from tests.factory import create_project, create_task
from app.services.labeling_write_scope import capture_labeling_write_scope


def _tb(unit: str, classes: list[dict], attribute_schema=None) -> dict:
    return {
        unit: {
            "enabled": True,
            "classes": classes,
            "attribute_schema": attribute_schema or {"fields": []},
        }
    }


def _accept_scope(task, actor_id):
    return capture_labeling_write_scope(
        task,
        actor_id=actor_id,
        actor_known=True,
        operation="candidate_accept",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )


@pytest.mark.asyncio
async def test_create_class_name_not_in_allowed_raises_422(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb("bbox", [{"name": "person", "order": 0}])
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc_info:
        await svc.create(
            task_id=task.id,
            user_id=user.id,
            annotation_type="bbox",
            class_name="cat",  # 不在 allowed=["person"] 内
            geometry={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            tool_unit_id="bbox",
        )
    assert exc_info.value.status_code == 422
    assert "cat" in exc_info.value.detail
    assert "bbox" in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_allowed_class_passes(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb("bbox", [{"name": "person", "order": 0}])
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    ann = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="person",
        geometry={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        tool_unit_id="bbox",
    )
    assert ann.class_name == "person"
    assert ann.tool_unit_id == "bbox"


@pytest.mark.asyncio
async def test_create_empty_allowed_skips_validation(db_session, super_admin):
    """旧项目 tool_bindings 为空 → 集合空 → 放行 (向后兼容)."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = {}
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    ann = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="anything",
        geometry={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        tool_unit_id="bbox",
    )
    assert ann.class_name == "anything"


@pytest.mark.asyncio
async def test_create_wrong_unit_does_not_match(db_session, super_admin):
    """tool_bindings 给的 bbox unit 配置, 但 create 传 tool_unit_id='region':
    allowed=[] (该 unit 未配置) → 放行 (强隔离, 不跨 unit 借).
    """
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb("bbox", [{"name": "person", "order": 0}])
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    ann = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="anything",  # 不在 bbox 的 allowed 内
        geometry={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        tool_unit_id="region",  # 但走的是未配置的 region unit
    )
    # region 未配置, allowed=[], 放行.
    assert ann.tool_unit_id == "region"


# ── accept_prediction ──────────────────────────────────────────────────────


async def _create_prediction(
    db_session, *, project_id, task_id, tool_unit_id: str, result: list[dict]
) -> Prediction:
    pred = Prediction(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        result=result,
        score=0.9,
        created_at=datetime.now(timezone.utc),
        tool_unit_id=tool_unit_id,
    )
    db_session.add(pred)
    await db_session.flush()
    return pred


@pytest.mark.asyncio
async def test_accept_prediction_alias_scoped_to_prediction_unit(
    db_session, super_admin
):
    """v0.10.17 强隔离: alias 反查必须限 prediction.tool_unit_id 对应 unit;
    旧实现走 classes_config union 会拿到错 unit 的同名类配置.
    """
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    # bbox unit: person.alias = 'person_ext'; region unit: person.alias = 'object'
    proj.tool_bindings = {
        "bbox": {
            "enabled": True,
            "classes": [
                {"name": "person", "alias": "person_ext", "order": 0},
            ],
            "attribute_schema": {"fields": []},
        },
        "region": {
            "enabled": True,
            "classes": [
                {"name": "person", "alias": "object", "order": 0},
            ],
            "attribute_schema": {"fields": []},
        },
    }
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    # prediction 来自 region (例如 SAM 候选), class_name="object" (alias) → 应回查到 person.
    pred = await _create_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        tool_unit_id="region",
        result=[
            {
                "type": "rectanglelabels",
                "geometry": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                "class_name": "object",  # region unit 的 alias
                "tool_unit_id": "region",
            }
        ],
    )

    svc = AnnotationService(db_session)
    # v0.20.22 · accept_prediction 现返回 list[Annotation] | None
    anns = await svc.accept_prediction(
        pred.id, write_scope=_accept_scope(task, user.id)
    )
    assert anns is not None and len(anns) == 1
    ann = anns[0]
    assert ann.class_name == "person"
    assert ann.tool_unit_id == "region"


@pytest.mark.asyncio
async def test_accept_prediction_raises_when_class_not_in_unit(db_session, super_admin):
    """v0.10.17 共享校验: accept_prediction 也必须过 _validate_class_name."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    # bbox unit 仅有 "person", 没有 "ghost".
    proj.tool_bindings = _tb("bbox", [{"name": "person", "order": 0}])
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    pred = await _create_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        tool_unit_id="bbox",
        result=[
            {
                "type": "rectanglelabels",
                "geometry": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                "class_name": "ghost",  # 不在 bbox unit 内
                "tool_unit_id": "bbox",
            }
        ],
    )

    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc_info:
        await svc.accept_prediction(pred.id, write_scope=_accept_scope(task, user.id))
    assert exc_info.value.status_code == 422
    assert "ghost" in exc_info.value.detail


@pytest.mark.asyncio
async def test_accept_prediction_override_class_name_lands_project_label(
    db_session, super_admin
):
    """v0.14.17 · 采纳时选类: 预测类名不在标签集 (会 422), 但传 override → 落项目标签成功."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb("bbox", [{"name": "行人", "order": 0}])
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    # YOLO 输出原生类名 "person" (不在项目标签 "行人"、且无 alias) → 直接采纳会 422.
    pred = await _create_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        tool_unit_id="bbox",
        result=[
            {
                "type": "rectanglelabels",
                "geometry": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                "class_name": "person",
                "tool_unit_id": "bbox",
            }
        ],
    )

    svc = AnnotationService(db_session)
    anns = await svc.accept_prediction(
        pred.id,
        override_class_name="行人",
        write_scope=_accept_scope(task, user.id),
    )
    assert anns is not None and len(anns) == 1
    ann = anns[0]
    assert ann.class_name == "行人"


@pytest.mark.asyncio
async def test_accept_prediction_override_not_in_allowed_raises_422(
    db_session, super_admin
):
    """override 仍走软校验: 选了标签集外的值 → 422."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb("bbox", [{"name": "行人", "order": 0}])
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    pred = await _create_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        tool_unit_id="bbox",
        result=[
            {
                "type": "rectanglelabels",
                "geometry": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                "class_name": "person",
                "tool_unit_id": "bbox",
            }
        ],
    )

    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc_info:
        await svc.accept_prediction(
            pred.id,
            override_class_name="车",
            write_scope=_accept_scope(task, user.id),
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_accept_prediction_no_match_pass_through_class_name(
    db_session, super_admin
):
    """无 alias 配置时 class_name 不变 (旧行为不破坏)."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb("bbox", [{"name": "person", "order": 0}])
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    pred = await _create_prediction(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        tool_unit_id="bbox",
        result=[
            {
                "type": "rectanglelabels",
                "geometry": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                "class_name": "person",
                "tool_unit_id": "bbox",
            }
        ],
    )

    svc = AnnotationService(db_session)
    anns = await svc.accept_prediction(
        pred.id, write_scope=_accept_scope(task, user.id)
    )
    assert anns is not None and len(anns) == 1
    ann = anns[0]
    assert ann.class_name == "person"


# ── update (PATCH 改类) ─────────────────────────────────────────────────────


async def _make_annotation(
    db_session, *, user, proj, tool_unit_id="bbox", class_name="person"
):
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    svc = AnnotationService(db_session)
    ann = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name=class_name,
        geometry={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        tool_unit_id=tool_unit_id,
    )
    return svc, ann


@pytest.mark.asyncio
async def test_update_class_name_not_in_allowed_raises_422(db_session, super_admin):
    """v0.14.17 · PATCH 改类与 create / accept 对齐: 改成项目标签集外的值 → 422."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb("bbox", [{"name": "person", "order": 0}])
    svc, ann = await _make_annotation(db_session, user=user, proj=proj)

    with pytest.raises(HTTPException) as exc_info:
        await svc.update(ann.id, class_name="ghost")
    assert exc_info.value.status_code == 422
    assert "ghost" in exc_info.value.detail


@pytest.mark.asyncio
async def test_patch_restores_annotation_error_at_http_boundary(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb("bbox", [{"name": "person", "order": 0}])
    _, ann = await _make_annotation(db_session, user=user, proj=proj)
    task = await db_session.get(Task, ann.task_id)
    task.status = "in_progress"
    await db_session.flush()
    with pytest.raises(HTTPException) as rejected:
        await AnnotationService(db_session).update(ann.id, class_name="ghost")

    response = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{ann.id}",
        json={"class_name": "ghost"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == rejected.value.detail
    await db_session.refresh(ann)
    assert ann.class_name == "person"


@pytest.mark.asyncio
async def test_update_class_name_allowed_passes(db_session, super_admin):
    """改成项目标签集内的值 → 通过 (采纳后改类的正常流程不被破坏)."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb(
        "bbox", [{"name": "person", "order": 0}, {"name": "car", "order": 1}]
    )
    svc, ann = await _make_annotation(db_session, user=user, proj=proj)

    updated = await svc.update(ann.id, class_name="car")
    assert updated is not None
    assert updated.class_name == "car"


@pytest.mark.asyncio
async def test_update_without_class_name_skips_validation(db_session, super_admin):
    """只改 geometry、不带 class_name → 不触发校验."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    proj.tool_bindings = _tb("bbox", [{"name": "person", "order": 0}])
    svc, ann = await _make_annotation(db_session, user=user, proj=proj)

    updated = await svc.update(
        ann.id, geometry={"x": 0.3, "y": 0.3, "w": 0.1, "h": 0.1}
    )
    assert updated is not None
    assert updated.class_name == "person"
