import asyncio

from sqlalchemy import select, text

from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import Prediction
from tests.conftest import create_registry_with_pool
from tests.inference_execution_test_ports import FakeModelExecutionPort
from tests.managed_image_inference_fixtures import (
    db_session as db_session,
    input_world as input_world,
    sync_world as sync_world,
)
from tests.test_frame_upload_cap import _jpeg_bytes
from tests.test_video_derivative_task_fence import _http_authority, _wait_for_blocking
from tests.test_video_frame_service import _make_video_task


async def test_frame_prediction_persists_after_video_reader(
    httpx_client, db_session, super_admin, sync_world, monkeypatch
):
    actor, token = super_admin
    task, _ = await _make_video_task(db_session, actor.id)
    backend, pool = await create_registry_with_pool(
        db_session, name="frame-reader", enabled_pool=True
    )
    db_session.add(
        ProjectMLBackendPool(project_id=task.project_id, pool_id=pool.id, enabled=True)
    )
    await db_session.commit()

    calls = []
    async with sync_world.factory() as reader, asyncio.TaskGroup() as group:

        async def finish_read(holder_pid, writer_pid):
            async with asyncio.timeout(10):
                await _wait_for_blocking(reader, holder_pid, writer_pid, request)
            await reader.commit()

        async def infer(_operation, task_input):
            calls.append(task_input["id"])
            await _http_authority(reader, task, actor)
            group.create_task(
                finish_read(
                    await reader.scalar(text("SELECT pg_backend_pid()")),
                    await db_session.scalar(text("SELECT pg_backend_pid()")),
                )
            )
            return [
                {
                    "type": "rectanglelabels",
                    "value": {
                        "x": 0.1,
                        "y": 0.2,
                        "width": 0.3,
                        "height": 0.4,
                        "rectanglelabels": ["car"],
                    },
                }
            ]

        monkeypatch.setattr(
            "app.api.v1.ml_backends.model_execution_port",
            lambda _db: FakeModelExecutionPort(infer, pool_id=pool.id),
        )
        request = group.create_task(
            httpx_client.post(
                f"/api/v1/projects/{task.project_id}/ml-backends/{backend.id}/predict-frame",
                headers={"Authorization": f"Bearer {token}"},
                files={"frame": ("frame.jpg", _jpeg_bytes(), "image/jpeg")},
                data={"task_id": str(task.id), "frame_index": "0"},
            )
        )
        response = await request
        assert response.status_code == 200, response.text

    assert calls == [str(task.id)]
    async with sync_world.factory() as observer:
        saved = (
            await observer.scalars(
                select(Prediction).where(Prediction.task_id == task.id)
            )
        ).one()
        assert response.json() == {
            "prediction_id": str(saved.id),
            "candidate_count": 1,
            "frame_index": 0,
        }
        assert saved.result[0]["class_name"] == "car"
        assert saved.result[0]["geometry"] == {
            "type": "video_bbox",
            "frame_index": 0,
            "x": 0.1,
            "y": 0.2,
            "w": 0.3,
            "h": 0.4,
        }
