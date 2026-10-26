"""v0.23.3 ADR-0050 §5.4 / §C.4 · dual-ID recording on Prediction / FailedPrediction.

Verifies the first-party call-path wiring records the requested pool id alongside the
selected instance id. In off/observe mode the actual instance is unchanged (= v0.23.2);
the pool id is derived from the instance's singleton pool via MLBackendService.
"""

from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.db.models.prediction import Prediction
from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.ml_backend import MLBackendService
from app.services.prediction import PredictionService
from tests.conftest import create_registry_with_pool
from tests.factory import create_project, create_task


def _tiny_jpeg() -> bytes:
    """v0.23.5 WS-D D2 requires PIL-decodable frames on /predict-frame."""
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_apply_prediction_result_records_pool_and_instance(
    db_session, super_admin
) -> None:
    """Prediction gets both ml_backend_id (instance) and ml_backend_pool_id (requested pool)."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    backend, pool = await create_registry_with_pool(db_session, name="gsam2")
    await db_session.flush()

    task = await create_task(db_session, project_id=proj.id)
    write_scope = capture_labeling_write_scope(
        task,
        actor_id=user.id,
        actor_known=True,
        operation="interactive_frame_prediction",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )
    pred_svc = PredictionService(db_session)
    receipt = await pred_svc.apply_prediction_result(
        ApplyPredictionResultRequest(
            task_id=task.id,
            project_id=proj.id,
            ml_backend_id=backend.id,
            result=[{"type": "rectangle", "points": [0, 0, 1, 1]}],
            ml_backend_pool_id=pool.id,
            write_scope=write_scope,
        )
    )
    await db_session.flush()
    prediction = await db_session.scalar(
        select(Prediction).where(Prediction.id == receipt.id)
    )

    # Both columns written.
    assert prediction.ml_backend_id == backend.id
    assert prediction.ml_backend_pool_id == pool.id


@pytest.mark.asyncio
async def test_create_failed_records_pool_and_instance(db_session, super_admin) -> None:
    """FailedPrediction records pool + instance (instance may be null for pre-selection failures)."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    backend, pool = await create_registry_with_pool(db_session, name="gsam2")
    await db_session.flush()

    pred_svc = PredictionService(db_session)
    failed = await pred_svc.create_failed(
        task_id=await _make_task(db_session, proj.id),
        project_id=proj.id,
        ml_backend_id=backend.id,
        error_type="ConnectError",
        message="backend unreachable",
        ml_backend_pool_id=pool.id,
    )
    await db_session.flush()
    assert failed.ml_backend_id == backend.id
    assert failed.ml_backend_pool_id == pool.id


@pytest.mark.asyncio
async def test_create_failed_pool_set_when_instance_null(
    db_session, super_admin
) -> None:
    """§5.4: a failure before instance selection records pool but instance may be null."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    backend, pool = await create_registry_with_pool(db_session, name="gsam2")
    await db_session.flush()

    pred_svc = PredictionService(db_session)
    failed = await pred_svc.create_failed(
        task_id=None,
        project_id=proj.id,
        ml_backend_id=None,  # no instance selected yet
        error_type="PoolUnavailable",
        message="all members circuit-open",
        ml_backend_pool_id=pool.id,
    )
    await db_session.flush()
    assert failed.ml_backend_id is None
    assert failed.ml_backend_pool_id == pool.id


@pytest.mark.asyncio
async def test_pool_id_for_registry_resolves_singleton(db_session, super_admin) -> None:
    """MLBackendService.pool_id_for_registry resolves a registry to its singleton pool id."""
    user, _ = super_admin
    await create_project(db_session, owner_id=user.id)
    backend, pool = await create_registry_with_pool(db_session, name="gsam2")
    await db_session.flush()

    svc = MLBackendService(db_session)
    resolved = await svc.pool_id_for_registry(backend.id)
    assert resolved == pool.id


@pytest.mark.asyncio
async def test_pool_id_for_registry_returns_none_for_unknown(
    db_session, super_admin
) -> None:
    """An unknown registry id resolves to None (no pool)."""
    user, _ = super_admin
    await create_project(db_session, owner_id=user.id)
    svc = MLBackendService(db_session)
    assert await svc.pool_id_for_registry(uuid.uuid4()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [None, "{}"])
async def test_predict_frame_route_records_dual_id(
    httpx_client, db_session, super_admin, monkeypatch, config
) -> None:
    """The /predict-frame route records both pool + instance on the persisted Prediction."""
    from aap_inference_contract.execution import ModelTaskResult
    from tests.inference_execution_test_ports import FakeModelExecutionPort

    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="video-track")
    proj.data_type = "video"
    backend, pool = await create_registry_with_pool(
        db_session, name="gsam2-video", enabled_pool=True
    )
    from app.db.models.ml_backend_registry import ProjectMLBackendPool

    db_session.add(
        ProjectMLBackendPool(project_id=proj.id, pool_id=pool.id, enabled=True)
    )
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    execution_port = FakeModelExecutionPort(
        lambda _operation, task_input: ModelTaskResult(
            task_id=task_input["id"],
            result=({"type": "rectangle", "points": [0.1, 0.1, 0.5, 0.5]},),
            score=0.9,
            model_version="v1",
            inference_time_ms=10,
        ),
        instance_id=backend.id,
        pool_id=pool.id,
    )
    monkeypatch.setattr(
        "app.api.v1.ml_backends.model_execution_port",
        lambda *_args, **_kwargs: execution_port,
    )
    monkeypatch.setattr(
        "app.api.v1.ml_backends.StorageService.upload_crop_bytes",
        lambda *_args: "http://storage.test/frame.jpg",
    )

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/predict-frame",
        headers={"Authorization": f"Bearer {token}"},
        files={"frame": ("f.jpg", _tiny_jpeg(), "image/jpeg")},
        data={
            "task_id": str(task.id),
            "frame_index": "0",
            **({"config": config} if config is not None else {}),
        },
    )
    assert resp.status_code == 200, resp.text

    stmt = select(Prediction).where(Prediction.task_id == task.id)
    pred = (await db_session.execute(stmt)).scalar_one()
    assert pred.ml_backend_id == backend.id
    assert pred.ml_backend_pool_id == pool.id


async def _make_task(db_session, project_id) -> uuid.UUID:
    from tests.factory import create_task

    task = await create_task(db_session, project_id=project_id)
    return task.id
