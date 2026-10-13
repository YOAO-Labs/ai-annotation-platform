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


@pytest.mark.asyncio
async def test_predictions_include_original_shape_index_after_confidence_filter(
    httpx_client,
    db_session,
    super_admin,
):
    owner, token = super_admin
    project = Project(
        id=uuid.uuid4(),
        display_id=await next_display_id(db_session, "projects"),
        name="shape-index",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner.id,
        tool_bindings=build_tool_bindings(
            [{"name": "商品", "alias": "person"}, {"name": "价签", "alias": "car"}]
        ),
    )
    db_session.add(project)
    await db_session.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-{uuid.uuid4().hex[:8]}",
        file_name="sample.jpg",
        file_path="/tmp/sample.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    prediction = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        model_version="test",
        score=None,
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
                "score": 0.4,
            },
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 20,
                    "y": 20,
                    "width": 10,
                    "height": 10,
                    "rectanglelabels": ["person"],
                },
                "score": 0.9,
            },
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 40,
                    "y": 40,
                    "width": 10,
                    "height": 10,
                    "rectanglelabels": ["car"],
                },
                "score": 0.8,
            },
        ],
    )
    db_session.add(prediction)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions?min_confidence=0.5",
        headers=_bearer(token),
    )

    assert resp.status_code == 200
    shapes = resp.json()[0]["result"]
    assert [s["shape_index"] for s in shapes] == [1, 2]
    assert [s["class_name"] for s in shapes] == ["person", "car"]


@pytest.mark.asyncio
async def test_predictions_expose_source_for_ml_and_external_import(
    httpx_client,
    db_session,
    super_admin,
):
    owner, token = super_admin
    project = Project(
        id=uuid.uuid4(),
        display_id=await next_display_id(db_session, "projects"),
        name="prediction-source",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(project)
    await db_session.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-{uuid.uuid4().hex[:8]}",
        file_name="sample.jpg",
        file_path="/tmp/source-sample.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    result = [
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
        }
    ]
    ml_prediction = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        model_version="ml-v1",
        score=None,
        source="ml_backend",
        result=result,
    )
    imported_prediction = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        model_version="import-v1",
        score=None,
        source="external_import",
        result=result,
    )
    db_session.add_all([ml_prediction, imported_prediction])
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions",
        headers=_bearer(token),
    )

    assert resp.status_code == 200
    by_id = {row["id"]: row for row in resp.json()}
    assert by_id[str(ml_prediction.id)]["source"] == "ml_backend"
    assert by_id[str(imported_prediction.id)]["source"] == "external_import"
