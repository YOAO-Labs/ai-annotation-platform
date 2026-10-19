"""v0.20.10 · 属性级溯源 (per-key origin) service 层覆盖 (Q1a).

- accept_prediction 从 PredictionMeta.extra.pipeline 提取 AI 富集键 → attributes_meta
  标 origin=ai + model_ref (含 label 前缀反推)
- 采纳前人工改过的键 (attribute_overrides) 不标 ai
- 无 pipeline meta → attributes_meta 为空
- 人工 update/bulk_update 改属性: 改值的 AI 键回落 human (删 meta), 未改保留, 删键联动
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.prediction import Prediction, PredictionMeta
from app.services import annotation as annotation_service
from aap_assets_inputs.contract import InputFailure
from app.application.candidate_decision import bind_candidate_decision_port
from app.services.candidate_decision_contract import CandidateAcceptCommand
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


async def _accept_candidate(
    db, task, actor, prediction_id, *, attribute_overrides=None
):
    return await bind_candidate_decision_port(db, task=task, actor=actor).accept(
        CandidateAcceptCommand(
            write_scope=_accept_scope(task, actor.id),
            prediction_id=prediction_id,
            shape_index=None,
            override_class_name=None,
            attribute_overrides=attribute_overrides,
            video_segment_id=None,
        )
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

    anns = await _accept_candidate(db_session, task, user, pred.id)
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

    anns = await _accept_candidate(db_session, task, user, pred.id)
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

    # 采纳前人工把 color 改成 white → color 视为 human 认领, 不标 ai
    anns = await _accept_candidate(
        db_session, task, user, pred.id, attribute_overrides={"color": "white"}
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

    anns = await _accept_candidate(db_session, task, user, pred.id)
    assert anns is not None and len(anns) == 1
    ann = anns[0]
    assert ann.attributes_meta == {}


async def test_accept_prediction_verifies_source_before_fence_and_pins_new_annotations(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    pred = await _create_prediction_with_pipeline(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result=[_bbox_shape("car", {})],
        stages=None,
    )
    input_id, origin_job_id = uuid.uuid4(), uuid.uuid4()
    digest = "b" * 64
    db_session.add(
        PredictionMeta(
            id=uuid.uuid4(),
            prediction_id=pred.id,
            extra={
                "accepted_input": {
                    "input_id": str(input_id),
                    "job_id": str(origin_job_id),
                    "task_id": str(task.id),
                    "sha256": digest,
                    "byte_size": 128,
                }
            },
        )
    )
    await db_session.flush()
    events = []
    attached = []
    receipt = SimpleNamespace(
        input_id=input_id,
        job_id=origin_job_id,
        task_id=task.id,
        project_id=proj.id,
        sha256=digest,
        byte_size=128,
    )

    class Gateway:
        async def verify_source(self, request):
            events.append(("verify", request.input_id))
            return receipt

    @asynccontextmanager
    async def fake_gateway(**_kwargs):
        yield Gateway()

    async def fake_lock(_db, scope, *, operations):
        events.append(("fence", scope.accepted_input_id))
        assert operations == ("candidate_accept", "interactive_mask_accept")
        return task, user, None

    async def fake_attach(_db, *, annotation_id, input_id, sha256):
        attached.append((annotation_id, input_id, sha256))
        return True

    monkeypatch.setattr("app.services.accepted_inputs.input_gateway", fake_gateway)
    monkeypatch.setattr(annotation_service, "lock_labeling_write", fake_lock)
    monkeypatch.setattr(
        "app.services.accepted_inputs.attach_annotation_input_source", fake_attach
    )

    anns = await AnnotationService(db_session)._accept_prediction(
        pred.id, shape_index=0, write_scope=_accept_scope(task, user.id)
    )

    assert anns is not None and len(anns) == 1
    assert events == [("verify", input_id), ("fence", input_id)]
    assert attached == [(anns[0].id, input_id, digest)]


async def test_accept_prediction_source_change_fails_before_business_fence(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()
    pred = await _create_prediction_with_pipeline(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        result=[_bbox_shape("car", {})],
        stages=None,
    )
    db_session.add(
        PredictionMeta(
            id=uuid.uuid4(),
            prediction_id=pred.id,
            extra={
                "accepted_input": {
                    "input_id": str(uuid.uuid4()),
                    "job_id": str(uuid.uuid4()),
                    "task_id": str(task.id),
                    "sha256": "c" * 64,
                    "byte_size": 128,
                }
            },
        )
    )
    await db_session.flush()
    events = []

    class Gateway:
        async def verify_source(self, _request):
            events.append("verify")
            raise InputFailure("source_changed")

    @asynccontextmanager
    async def fake_gateway(**_kwargs):
        yield Gateway()

    async def unexpected_lock(*_args, **_kwargs):
        events.append("fence")
        pytest.fail("source verification must precede the business fence")

    monkeypatch.setattr("app.services.accepted_inputs.input_gateway", fake_gateway)
    monkeypatch.setattr(annotation_service, "lock_labeling_write", unexpected_lock)

    with pytest.raises(HTTPException) as exc_info:
        await AnnotationService(db_session)._accept_prediction(
            pred.id, shape_index=0, write_scope=_accept_scope(task, user.id)
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["reason"] == "prediction_source_changed"
    assert events == ["verify"]
    assert (
        await db_session.scalar(
            select(Annotation).where(Annotation.parent_prediction_id == pred.id)
        )
        is None
    )


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
