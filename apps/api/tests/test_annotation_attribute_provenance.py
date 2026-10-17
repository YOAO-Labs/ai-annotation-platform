"""v0.20.10 · 属性级溯源 (per-key origin) service 层覆盖 (Q1a).

- accept_prediction 从 PredictionMeta.extra.pipeline 提取 AI 富集键 → attributes_meta
  标 origin=ai + model_ref (含 label 前缀反推)
- 采纳前人工改过的键 (attribute_overrides) 不标 ai
- 无 pipeline meta → attributes_meta 为空
- 人工 update/bulk_update 改属性: 改值的 AI 键回落 human (删 meta), 未改保留, 删键联动
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.db.models.prediction import Prediction, PredictionMeta
from app.services.annotation import AnnotationService, _sync_attributes_meta
from tests.factory import create_project, create_task
from app.services.labeling_write_scope import capture_labeling_write_scope


async def _create_prediction_with_pipeline(
    db,
    *,
    project_id,
    task_id,
    result: list[dict],
    stages: list[dict] | None,
) -> Prediction:
    pred = Prediction(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        result=result,
        score=0.9,
        created_at=datetime.now(timezone.utc),
        tool_unit_id="bbox",
    )
    db.add(pred)
    await db.flush()
    if stages is not None:
        db.add(
            PredictionMeta(
                id=uuid.uuid4(),
                prediction_id=pred.id,
                extra={"pipeline": {"stages": stages}},
            )
        )
        await db.flush()
    return pred


def _bbox_shape(class_name: str, attributes: dict) -> dict:
    return {
        "type": "rectanglelabels",
        "geometry": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        "class_name": class_name,
        "tool_unit_id": "bbox",
        "attributes": attributes,
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
async def test_accept_prediction_marks_ai_attributes(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    pred = await _create_prediction_with_pipeline(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result=[_bbox_shape("car", {"color": "blue", "vehicle_type": "sedan"})],
        stages=[
            {"stage": 0, "write_target": None},
            {
                "stage": 1,
                "write_target": "attributes",
                "label": None,
                "write_keys": ["color", "vehicle_type"],
                "ml_backend_id": "be-1",
                "model_id": "cls",
            },
        ],
    )

    svc = AnnotationService(db_session)
    # v0.20.22 · accept_prediction 现返回 list[Annotation] | None
    anns = await svc.accept_prediction(
        pred.id, write_scope=_accept_scope(task, user.id)
    )
    assert anns is not None and len(anns) == 1
    ann = anns[0]
    assert ann.attributes_meta["color"]["origin"] == "ai"
    assert ann.attributes_meta["color"]["model_ref"] == {
        "backend_id": "be-1",
        "model_id": "cls",
    }
    assert ann.attributes_meta["vehicle_type"]["origin"] == "ai"
    # _shape_index 内部键不进 meta
    assert "_shape_index" not in ann.attributes_meta


@pytest.mark.asyncio
async def test_accept_prediction_label_prefix(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    # label='sub' + write_keys=['color'] → 富集键 'sub_color' (与 tasks.py 前缀逻辑一致)
    pred = await _create_prediction_with_pipeline(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result=[_bbox_shape("car", {"sub_color": "red"})],
        stages=[
            {
                "stage": 1,
                "write_target": "attributes",
                "label": "sub",
                "write_keys": ["color"],
                "ml_backend_id": "be-2",
                "model_id": "clf2",
            },
        ],
    )

    svc = AnnotationService(db_session)
    anns = await svc.accept_prediction(
        pred.id, write_scope=_accept_scope(task, user.id)
    )
    assert anns is not None and len(anns) == 1
    ann = anns[0]
    assert ann.attributes_meta["sub_color"]["origin"] == "ai"
    assert ann.attributes_meta["sub_color"]["model_ref"]["model_id"] == "clf2"


@pytest.mark.asyncio
async def test_accept_prediction_human_override_not_ai(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    pred = await _create_prediction_with_pipeline(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result=[_bbox_shape("car", {"color": "blue", "vehicle_type": "sedan"})],
        stages=[
            {
                "stage": 1,
                "write_target": "attributes",
                "label": None,
                "write_keys": ["color", "vehicle_type"],
                "ml_backend_id": "be-1",
                "model_id": "cls",
            },
        ],
    )

    svc = AnnotationService(db_session)
    # 采纳前人工把 color 改成 white → color 视为 human 认领, 不标 ai
    anns = await svc.accept_prediction(
        pred.id,
        attribute_overrides={"color": "white"},
        write_scope=_accept_scope(task, user.id),
    )
    assert anns is not None and len(anns) == 1
    ann = anns[0]
    assert "color" not in ann.attributes_meta
    assert ann.attributes_meta["vehicle_type"]["origin"] == "ai"


@pytest.mark.asyncio
async def test_accept_prediction_no_pipeline_no_meta(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    pred = await _create_prediction_with_pipeline(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result=[_bbox_shape("car", {"color": "blue"})],
        stages=None,  # 无 PredictionMeta
    )

    svc = AnnotationService(db_session)
    anns = await svc.accept_prediction(
        pred.id, write_scope=_accept_scope(task, user.id)
    )
    assert anns is not None and len(anns) == 1
    ann = anns[0]
    assert ann.attributes_meta == {}


# ── update / bulk_update meta 键同步 ─────────────────────────────────────────


def test_sync_meta_helper():
    old_attrs = {"color": "blue", "vehicle_type": "sedan"}
    old_meta = {
        "color": {"origin": "ai", "model_ref": {"model_id": "cls"}},
        "vehicle_type": {"origin": "ai", "model_ref": {"model_id": "cls"}},
    }
    # 改 color、保留 vehicle_type、删无关: color 掉 meta, vehicle_type 保留
    new = _sync_attributes_meta(
        old_attrs, old_meta, {"color": "red", "vehicle_type": "sedan"}
    )
    assert "color" not in new
    assert new["vehicle_type"]["origin"] == "ai"
    # 删除 vehicle_type 键 → meta 一并消失
    new2 = _sync_attributes_meta(old_attrs, old_meta, {"color": "blue"})
    assert new2 == {"color": {"origin": "ai", "model_ref": {"model_id": "cls"}}}
    # 内部键跳过
    new3 = _sync_attributes_meta(
        old_attrs,
        old_meta,
        {"color": "blue", "vehicle_type": "sedan", "_shape_index": 0},
    )
    assert "_shape_index" not in new3


@pytest.mark.asyncio
async def test_update_flips_ai_to_human_on_change(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    ann = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="__unknown",
        geometry={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        attributes={"color": "blue", "vehicle_type": "sedan"},
    )
    # 手动模拟 AI 溯源 (accept 后的状态)
    ann.attributes_meta = {
        "color": {"origin": "ai", "model_ref": {"model_id": "cls"}},
        "vehicle_type": {"origin": "ai", "model_ref": {"model_id": "cls"}},
    }
    await db_session.flush()

    # 人工改 color、保留 vehicle_type
    updated = await svc.update(
        ann.id, attributes={"color": "white", "vehicle_type": "sedan"}
    )
    assert "color" not in updated.attributes_meta
    assert updated.attributes_meta["vehicle_type"]["origin"] == "ai"


@pytest.mark.asyncio
async def test_bulk_update_syncs_meta(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    ann = await svc.create(
        task_id=task.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="__unknown",
        geometry={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        attributes={"color": "blue"},
    )
    ann.attributes_meta = {"color": {"origin": "ai", "model_ref": {"model_id": "cls"}}}
    await db_session.flush()

    rows = await svc.bulk_update([ann.id], attributes={"color": "green"})
    assert rows[0].attributes_meta == {}  # color 改值 → 掉 AI 溯源
