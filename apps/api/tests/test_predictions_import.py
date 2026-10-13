"""v0.10.15 · POST /projects/{id}/predictions/import 端点测试.

覆盖:
- AAP JSON happy path (bbox / polygon / multi_polygon)
- COCO Detection happy path
- schema_version > 1.x → 422
- task_match display_id 命中 / file_path 命中 / 都 miss → errors[]
- 跨项目 display_id (display_id 全局唯一但 project_id 不符) → fallback / miss
- dry_run 不入库
- overwrite_existing flag 行为
- 未知 geometry kind → errors[] 不让整批挂
- 权限 annotator → 403
- 老 prediction 默认 source='ml_backend'; 新导入行 source='external_import'
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.audit_log import AuditLog
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.annotation import AnnotationService
from app.services.exporting.packaging import _rotated_corners_norm
from app.services.prediction import PredictionService, to_internal_shape
from app.services.predictions_import import (
    import_aap_json,
    import_coco,
    internal_geometry_to_ls_shape,
)
from app.services.raster_mask_storage import build_rle_reference
from app.utils.raster_mask_rle import encode_coco_rle
from tests.factory import create_membership

pytestmark = pytest.mark.asyncio


# ── helpers ──────────────────────────────────────────────────────────


async def _seed_project_with_tasks(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    n_tasks: int = 2,
    type_key: str = "image-det",
) -> tuple[Project, list[Task]]:
    short = uuid.uuid4().hex[:6]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-PI{short}",
        name=f"PredImp {short}",
        type_key=type_key,
        type_label="测试",
        owner_id=owner_id,
        status="in_progress",
        raster_mask_native_editing_enabled=True,
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [{"name": "car"}, {"name": "truck"}],
            },
            "region": {
                "enabled": True,
                "classes": [{"name": "car"}, {"name": "truck"}],
            },
        },
    )
    db.add(project)
    await db.flush()

    tasks: list[Task] = []
    for i in range(n_tasks):
        t = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            display_id=f"T-PI{short}-{i}",
            file_name=f"img_{i}.jpg",
            file_path=f"datasets/{short}/img_{i}.jpg",
            file_type="image",
            status="pending",
        )
        db.add(t)
        tasks.append(t)
    await db.flush()
    return project, tasks


async def _seed_video_project_task(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    frame_count: int | None = 12,
) -> tuple[Project, Task]:
    short = uuid.uuid4().hex[:6]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-PIV{short}",
        name=f"Video PredImp {short}",
        type_key="video-det",
        type_label="视频测试",
        data_type="video",
        owner_id=owner_id,
        status="in_progress",
        raster_mask_native_editing_enabled=True,
        tool_bindings={
            unit: {"enabled": True, "classes": [{"name": "car"}]}
            for unit in ("bbox", "region", "polyline")
        },
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-PIV{short}",
        name=f"Video PredImp {short}",
        data_type="video",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    video_meta = {"width": 4, "height": 2}
    if frame_count is not None:
        video_meta["frame_count"] = frame_count
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name=f"video-{short}.mp4",
        file_path=f"datasets/{short}/video.mp4",
        file_type="video",
        width=4,
        height=2,
        metadata_={"video": video_meta},
    )
    db.add(item)
    await db.flush()
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-PIV{short}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="video",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return project, task


def _aap_envelope(tasks_payload: list[dict], schema_version: str = "1.0") -> bytes:
    return json.dumps(
        {
            "schema_version": schema_version,
            "exported_at": "2026-05-19T10:00:00Z",
            "exported_from": {"platform": "aap"},
            "project": {"name": "test", "type_key": "image-det"},
            "tasks": tasks_payload,
        }
    ).encode("utf-8")


def _upload_files(content: bytes, filename: str = "test.json") -> dict:
    return {"file": (filename, io.BytesIO(content), "application/json")}


def _upload_multi_files(items: list[tuple[str, bytes]]) -> list[tuple[str, tuple]]:
    return [
        ("file", (filename, io.BytesIO(content), "application/json"))
        for filename, content in items
    ]


def _zip_files(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _upload_zip(content: bytes, filename: str = "labels.zip") -> dict:
    return {"file": (filename, io.BytesIO(content), "application/zip")}


async def _seed_yolo_project(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    rel_paths: list[str] | None = None,
    size: tuple[int, int] = (200, 100),
) -> tuple[Project, list[Task]]:
    rel_paths = rel_paths or ["img_0.jpg"]
    short = uuid.uuid4().hex[:6]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-YOLO{short}",
        name=f"YoloImp {short}",
        type_key="image-det",
        type_label="测试",
        owner_id=owner_id,
        status="in_progress",
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [
                    {"name": "car", "order": 0},
                    {"name": "truck", "order": 1},
                ],
            }
        },
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-YOLO{short}",
        name=f"Yolo Dataset {short}",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()

    tasks: list[Task] = []
    for i, rel_path in enumerate(rel_paths):
        item = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            file_name=rel_path.rsplit("/", 1)[-1],
            file_path=f"datasets/{short}/{rel_path}",
            file_type="image",
            width=size[0],
            height=size[1],
        )
        task = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            dataset_item_id=item.id,
            display_id=f"T-YOLO{short}-{i}",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="image",
            status="pending",
        )
        db.add_all([item, task])
        tasks.append(task)
    await db.flush()
    return project, tasks


# ── AAP JSON happy paths ─────────────────────────────────────────────


async def test_import_aap_json_bbox_happy(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "annotations": [],
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.2,
                            "w": 0.3,
                            "h": 0.4,
                        },
                        "class_name": "car",
                        "confidence": 0.92,
                        "model_version": "ext-yolo-v1",
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["imported"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == []
    assert result["dry_run"] is False

    # 验证写入: source='external_import', ml_backend_id=None
    rows = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == tasks[0].id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    pred = rows[0]
    assert pred.source == "external_import"
    assert pred.ml_backend_id is None
    assert pred.model_version == "ext-yolo-v1"
    # result 列存 LS shape 数组
    assert isinstance(pred.result, list)
    assert pred.result[0]["type"] == "rectanglelabels"
    assert pred.result[0]["value"]["rectanglelabels"] == ["car"]


async def test_import_aap_json_polygon(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    poly_points = [[0.1, 0.1], [0.5, 0.1], [0.3, 0.4]]
    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {"type": "polygon", "points": poly_points},
                        "class_name": "car",
                        "confidence": 0.8,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1

    rows = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == tasks[0].id)
            )
        )
        .scalars()
        .all()
    )
    assert rows[0].result[0]["type"] == "polygonlabels"
    assert len(rows[0].result[0]["value"]["points"]) == 3


async def test_import_aap_json_multi_polygon(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "multi_polygon",
                            "polygons": [
                                {
                                    "type": "polygon",
                                    "points": [[0.1, 0.1], [0.5, 0.1], [0.3, 0.4]],
                                },
                                {
                                    "type": "polygon",
                                    "points": [[0.6, 0.6], [0.9, 0.6], [0.7, 0.9]],
                                },
                            ],
                        },
                        "class_name": "car",
                        "confidence": 0.7,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1


async def test_import_aap_json_polyline_and_rotated_bbox(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "polyline",
                            "points": [[0.1, 0.2], [0.4, 0.5], [0.8, 0.7]],
                        },
                        "class_name": "car",
                        "confidence": 0.71,
                    },
                    {
                        "geometry": {
                            "type": "rotated_bbox",
                            "cx": 0.5,
                            "cy": 0.45,
                            "w": 0.2,
                            "h": 0.12,
                            "angle": 30,
                        },
                        "class_name": "truck",
                        "confidence": 0.82,
                    },
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2

    rows = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == tasks[0].id)
            )
        )
        .scalars()
        .all()
    )
    by_type = {row.result[0]["type"]: row for row in rows}
    assert by_type["polylinelabels"].tool_unit_id == "polyline"
    assert by_type["polylinelabels"].result[0]["value"]["points"] == [
        [10.0, 20.0],
        [40.0, 50.0],
        [80.0, 70.0],
    ]
    rotated = by_type["rectanglelabels"]
    assert rotated.tool_unit_id == "rotated_bbox"
    assert rotated.result[0]["value"]["rotation"] == 30.0

    read_resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/predictions",
        headers=headers,
    )
    assert read_resp.status_code == 200, read_resp.text
    assert {p["tool_unit_id"] for p in read_resp.json()} == {
        "polyline",
        "rotated_bbox",
    }


async def test_import_aap_json_keypoint(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "keypoint",
                            "points": [
                                {"x": 0.1, "y": 0.2, "v": 2},
                                {"x": 0.4, "y": 0.5, "v": 1},
                                {"x": 0.7, "y": 0.8, "v": 0},
                            ],
                        },
                        "class_name": "car",
                        "confidence": 0.83,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1

    rows = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == tasks[0].id)
            )
        )
        .scalars()
        .all()
    )
    assert rows[0].tool_unit_id == "keypoint"
    assert rows[0].result[0]["type"] == "keypointlabels"

    read_resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/predictions",
        headers=headers,
    )
    assert read_resp.status_code == 200, read_resp.text
    body = read_resp.json()
    assert body[0]["tool_unit_id"] == "keypoint"
    assert body[0]["result"][0]["geometry"]["type"] == "keypoint"
    assert body[0]["result"][0]["geometry"]["points"][1]["v"] == 1


async def test_import_aap_json_video_predictions_dry_run_and_accept(
    super_admin,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    user, _ = super_admin
    project, task = await _seed_video_project_task(db_session, user.id)
    rle = encode_coco_rle([0, 1, 1, 0, 0, 1, 0, 0], 4, 2)
    reference = build_rle_reference(rle)
    geometries = {
        "video_bbox": {
            "type": "video_bbox",
            "frame_index": 3,
            "x": 0.1,
            "y": 0.2,
            "w": 0.3,
            "h": 0.4,
        },
        "video_track_bbox": {
            "type": "video_track_bbox",
            "track_id": "external-bbox",
            "keyframes": [
                {"frame_index": 1, "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
                {"frame_index": 5, "bbox": {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}},
            ],
            "outside": [{"from": 6, "to": 7, "source": "manual"}],
        },
        "video_track_polygon": {
            "type": "video_track_polygon",
            "track_id": "external-polygon",
            "keyframes": [
                {"frame_index": 1, "points": [[0.1, 0.1], [0.4, 0.1], [0.3, 0.4]]},
                {"frame_index": 5, "points": [[0.2, 0.2], [0.5, 0.2], [0.4, 0.5]]},
            ],
        },
        "video_track_polyline": {
            "type": "video_track_polyline",
            "track_id": "external-polyline",
            "keyframes": [
                {"frame_index": 1, "points": [[0.1, 0.1], [0.4, 0.4]]},
                {"frame_index": 5, "points": [[0.2, 0.2], [0.5, 0.5]]},
            ],
        },
        "video_track_mask": {
            "type": "video_track_mask",
            "track_id": "external-mask",
            "keyframes": [{"frame_index": 1, "mask": reference, "source": "manual"}],
            "outside": [{"from": 8, "to": 9, "source": "manual"}],
        },
    }
    envelope = {
        "schema_version": "1.3",
        "mask_objects": {reference["sha256"]: rle},
        "tasks": [
            {
                "task_match": {"display_id": task.display_id},
                "media_type": "video",
                "predictions": [
                    {
                        "class_name": "car",
                        "confidence": 0.9,
                        "shapes": [
                            geometries["video_bbox"],
                            geometries["video_track_bbox"],
                        ],
                    },
                    {
                        "class_name": "car",
                        "confidence": 0.8,
                        "shapes": [
                            geometries["video_track_polygon"],
                            geometries["video_track_mask"],
                        ],
                    },
                    {
                        "geometry": geometries["video_track_polyline"],
                        "class_name": "car",
                        "confidence": 0.7,
                    },
                ],
            }
        ],
    }
    payload = json.dumps(envelope).encode()
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    store = AsyncMock(return_value=reference)
    load = AsyncMock(return_value=rle)
    monkeypatch.setattr("app.services.raster_mask_storage.store_coco_rle", store)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)

    dry_run = await import_aap_json(db_session, project.id, payload, dry_run=True)
    assert dry_run.imported == 3
    assert dry_run.errors == []
    assert (
        not (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    store.assert_not_awaited()

    imported = await import_aap_json(db_session, project.id, payload)
    assert imported.imported == 3
    assert imported.errors == []
    predictions = list(
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert {prediction.tool_unit_id for prediction in predictions} == {
        "bbox",
        "region",
        "polyline",
    }
    imported_shapes = [
        to_internal_shape(shape) for pred in predictions for shape in pred.result
    ]
    assert {shape["geometry"]["type"] for shape in imported_shapes} == set(geometries)
    for shape in imported_shapes:
        geometry = shape["geometry"]
        for keyframe in geometry.get("keyframes", []):
            assert keyframe["source"] == "prediction"
        for outside in geometry.get("outside", []):
            assert outside["source"] == "prediction"

    service = AnnotationService(db_session)
    accepted = [
        annotation
        for prediction in predictions
        for annotation in (
            await service.accept_prediction(prediction.id, user.id) or []
        )
    ]
    assert {annotation.annotation_type for annotation in accepted} == set(geometries)
    assert {annotation.track_id for annotation in accepted if annotation.track_id} == {
        "external-bbox",
        "external-polygon",
        "external-polyline",
        "external-mask",
    }
    assert all(annotation.parent_prediction_id for annotation in accepted)
    imported_by_type = {shape["geometry"]["type"]: shape for shape in imported_shapes}
    for annotation in accepted:
        source = imported_by_type[annotation.annotation_type]
        assert annotation.geometry == source["geometry"]
        assert annotation.confidence == source["confidence"]


async def test_import_aap_json_video_prediction_contract_errors(
    super_admin,
    db_session: AsyncSession,
):
    user, _ = super_admin
    project, task = await _seed_video_project_task(db_session, user.id)
    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": task.display_id},
                "media_type": "video",
                "predictions": [
                    {
                        "geometry": {
                            "type": "video_bbox",
                            "frame_index": 12,
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                    },
                    {
                        "geometry": {
                            "type": "video_track_bbox",
                            "track_id": "duplicate",
                            "keyframes": [
                                {
                                    "frame_index": 2,
                                    "bbox": {"x": 0, "y": 0, "w": 1, "h": 1},
                                },
                                {
                                    "frame_index": 2,
                                    "bbox": {"x": 0, "y": 0, "w": 1, "h": 1},
                                },
                            ],
                        },
                        "class_name": "car",
                    },
                    {
                        "class_name": "car",
                        "shapes": [
                            {
                                "type": "video_bbox",
                                "frame_index": 1,
                                "x": 0,
                                "y": 0,
                                "w": 1,
                                "h": 1,
                            },
                            {
                                "type": "video_track_polyline",
                                "track_id": "mixed",
                                "keyframes": [
                                    {"frame_index": 1, "points": [[0, 0], [1, 1]]}
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
        schema_version="1.3",
    )

    result = await import_aap_json(db_session, project.id, payload, dry_run=True)

    assert result.imported == 0
    assert result.skipped == 3
    reasons = [error.reason for error in result.errors]
    assert any("frame_index must be <" in reason for reason in reasons)
    assert any("strictly increasing" in reason for reason in reasons)
    assert any("one tool unit" in reason for reason in reasons)


async def test_import_aap_json_video_prediction_requires_video_context(
    super_admin,
    db_session: AsyncSession,
):
    user, _ = super_admin
    project, task = await _seed_video_project_task(db_session, user.id)
    prediction = {
        "geometry": {
            "type": "video_bbox",
            "frame_index": 1,
            "x": 0.1,
            "y": 0.1,
            "w": 0.2,
            "h": 0.2,
        },
        "class_name": "car",
    }

    missing_media_type = await import_aap_json(
        db_session,
        project.id,
        _aap_envelope(
            [
                {
                    "task_match": {"display_id": task.display_id},
                    "predictions": [prediction],
                }
            ],
            schema_version="1.3",
        ),
        dry_run=True,
    )
    assert missing_media_type.imported == 0
    assert "media_type=video" in missing_media_type.errors[0].reason

    item = await db_session.get(DatasetItem, task.dataset_item_id)
    assert item is not None
    item.metadata_ = {"video": {"width": 4, "height": 2}}
    missing_frame_count = await import_aap_json(
        db_session,
        project.id,
        _aap_envelope(
            [
                {
                    "task_match": {"display_id": task.display_id},
                    "media_type": "video",
                    "predictions": [prediction],
                }
            ],
            schema_version="1.3",
        ),
        dry_run=True,
    )
    assert missing_frame_count.imported == 0
    assert "frame_count is required" in missing_frame_count.errors[0].reason

    task.file_type = "image"
    wrong_task_type = await import_aap_json(
        db_session,
        project.id,
        _aap_envelope(
            [
                {
                    "task_match": {"display_id": task.display_id},
                    "media_type": "video",
                    "predictions": [prediction],
                }
            ],
            schema_version="1.3",
        ),
        dry_run=True,
    )
    assert wrong_task_type.imported == 0
    assert "video task and dataset item" in wrong_task_type.errors[0].reason


async def test_prediction_video_mask_content_is_task_scoped_and_frame_resolved(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    user, token = super_admin
    project, task = await _seed_video_project_task(db_session, user.id)
    rle = encode_coco_rle([0, 1, 1, 0, 0, 1, 0, 0], 4, 2)
    reference = build_rle_reference(rle)
    load = AsyncMock(return_value=rle)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    prediction = await PredictionService(db_session).create_from_ml_result(
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        source="external_import",
        result=[
            {
                "type": "video_track_mask",
                "class_name": "car",
                "confidence": 0.9,
                "tool_unit_id": "region",
                "attributes": {},
                "geometry": {
                    "type": "video_track_mask",
                    "track_id": "mask-candidate",
                    "keyframes": [
                        {"frame_index": 1, "mask": reference, "source": "prediction"}
                    ],
                    "outside": [{"from": 8, "to": 9, "source": "prediction"}],
                },
            }
        ],
    )
    endpoint_load = AsyncMock(return_value=rle)
    monkeypatch.setattr("app.api.v1.tasks.predictions.load_coco_rle", endpoint_load)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    visible = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/mask-content/0/5",
        headers=headers,
    )
    outside = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/mask-content/0/8",
        headers=headers,
    )
    wrong_task = await httpx_client.get(
        f"/api/v1/tasks/{uuid.uuid4()}/predictions/{prediction.id}/mask-content/0/5",
        headers=headers,
    )

    assert visible.status_code == 200
    assert visible.json() == rle
    assert outside.status_code == 404
    assert wrong_task.status_code == 404
    endpoint_load.assert_awaited_once_with(reference)


async def test_prediction_import_polyline_round_trip():
    geometry = {"type": "polyline", "points": [[0.1, 0.2], [0.4, 0.5]]}
    ls_shape = internal_geometry_to_ls_shape(geometry, "lane", 0.77)
    assert ls_shape is not None

    out = to_internal_shape(ls_shape)
    assert out["class_name"] == "lane"
    assert out["confidence"] == 0.77
    assert out["geometry"] == geometry
    assert out["tool_unit_id"] == "polyline"


async def test_prediction_import_keypoint_round_trip():
    geometry = {
        "type": "keypoint",
        "points": [
            {"x": 0.1, "y": 0.2, "v": 2},
            {"x": 0.4, "y": 0.5, "v": 1},
            {"x": 0.7, "y": 0.8, "v": 0},
        ],
    }
    ls_shape = internal_geometry_to_ls_shape(geometry, "person", 0.81)
    assert ls_shape is not None
    assert ls_shape["type"] == "keypointlabels"
    assert ls_shape["value"]["points"] == [
        {"x": 10.0, "y": 20.0, "v": 2},
        {"x": 40.0, "y": 50.0, "v": 1},
        {"x": 70.0, "y": 80.0, "v": 0},
    ]

    out = to_internal_shape(ls_shape)
    assert out["class_name"] == "person"
    assert out["confidence"] == 0.81
    assert out["geometry"] == geometry
    assert out["tool_unit_id"] == "keypoint"


@pytest.mark.parametrize("angle", [0, 30, 90, 180, 270, 359])
async def test_prediction_import_rotated_bbox_round_trip(angle: int):
    geometry = {
        "type": "rotated_bbox",
        "cx": 0.52,
        "cy": 0.47,
        "w": 0.23,
        "h": 0.11,
        "angle": angle,
    }
    ls_shape = internal_geometry_to_ls_shape(geometry, "car", 0.66)
    assert ls_shape is not None
    assert ls_shape["type"] == "rectanglelabels"
    assert ls_shape["value"]["rotation"] == float(angle)

    out = to_internal_shape(ls_shape)
    assert out["tool_unit_id"] == "rotated_bbox"
    got = out["geometry"]
    assert got["type"] == "rotated_bbox"
    assert got["cx"] == pytest.approx(geometry["cx"], abs=1e-6)
    assert got["cy"] == pytest.approx(geometry["cy"], abs=1e-6)
    assert got["w"] == pytest.approx(geometry["w"], abs=1e-6)
    assert got["h"] == pytest.approx(geometry["h"], abs=1e-6)
    assert got["angle"] == pytest.approx(float(angle), abs=1e-6)


# ── schema_version 守门 ─────────────────────────────────────────────


async def test_import_aap_json_future_version_rejected(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, _ = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope([], schema_version="2.0")

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 422
    assert "schema_version" in r.json()["detail"]


# ── task_match 行为 ──────────────────────────────────────────────────


async def test_import_aap_json_file_path_match(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    # 仅给 file_path, 不给 display_id
    payload = _aap_envelope(
        [
            {
                "task_match": {"file_path": tasks[1].file_path},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.5,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1


async def test_import_aap_json_relative_path_match_nested(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    """v0.10.27 导入对称: file_path 带相对目录时按相对路径匹配, 同名跨目录不误匹配.

    库内 task.file_path 带 dataset 前缀 (`{ds}/animals/{kind}/001.jpg`); 外部入参是
    去前缀的相对路径 (`animals/cat/001.jpg`). 相对路径层应精确命中 cat, 不命中 dog.
    """
    user, token = super_admin
    project, _ = await _seed_project_with_tasks(db_session, user.id, n_tasks=0)
    headers = {"Authorization": f"Bearer {token}"}

    short = uuid.uuid4().hex[:6]
    cat_task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-CAT-{short}",
        file_name="001.jpg",
        file_path=f"ds{short}/animals/cat/001.jpg",
        file_type="image",
        status="pending",
    )
    dog_task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-DOG-{short}",
        file_name="001.jpg",
        file_path=f"ds{short}/animals/dog/001.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add_all([cat_task, dog_task])
    await db_session.flush()

    payload = _aap_envelope(
        [
            {
                "task_match": {"file_path": "animals/cat/001.jpg"},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.5,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1

    # 命中 cat, 未误匹配 dog.
    cat_count = (
        await db_session.execute(
            select(func.count(Prediction.id)).where(Prediction.task_id == cat_task.id)
        )
    ).scalar()
    dog_count = (
        await db_session.execute(
            select(func.count(Prediction.id)).where(Prediction.task_id == dog_task.id)
        )
    ).scalar()
    assert cat_count == 1
    assert dog_count == 0


async def test_import_aap_json_task_miss(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, _ = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": "T-NOTEXIST-XYZ"},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.5,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 0
    assert body["skipped"] == 1
    assert len(body["errors"]) == 1
    assert "task not found" in body["errors"][0]["reason"].lower()


async def test_import_aap_json_cross_project_display_id_not_matched(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    """display_id 全局唯一但属于其他项目时, 不允许偷换项目."""
    user, token = super_admin
    project_a, tasks_a = await _seed_project_with_tasks(db_session, user.id)
    project_b, _ = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    # 用 project_b 的 display_id 但导入到 project_a; 应 fallback 到 file_path,
    # 但 file_path 也只在 project_b 下, 故对 project_a miss.
    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks_a[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.5,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project_b.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 0
    assert body["skipped"] == 1


# ── dry_run ──────────────────────────────────────────────────────────


async def test_import_aap_json_dry_run_not_persisted(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.5,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import"
        "?format=aap_json&dry_run=true",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 1
    assert body["dry_run"] is True

    count = (
        await db_session.execute(
            select(func.count(Prediction.id)).where(Prediction.task_id == tasks[0].id)
        )
    ).scalar()
    assert count == 0


# ── overwrite_existing ───────────────────────────────────────────────


async def test_import_aap_json_default_overwrite_replaces_external_only(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}
    db_session.add(
        Prediction(
            id=uuid.uuid4(),
            task_id=tasks[0].id,
            project_id=project.id,
            ml_backend_id=None,
            result=[],
            source="ml_backend",
        )
    )
    await db_session.flush()

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.5,
                    }
                ],
            }
        ]
    )

    # 第一次: 无旧 external_import, 只写入新导入.
    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # 第二次不显式传 overwrite_existing: v0.10.57 起默认替换 external_import.
    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text

    external_count = (
        await db_session.execute(
            select(func.count(Prediction.id)).where(
                Prediction.task_id == tasks[0].id,
                Prediction.source == "external_import",
            )
        )
    ).scalar()
    ml_count = (
        await db_session.execute(
            select(func.count(Prediction.id)).where(
                Prediction.task_id == tasks[0].id,
                Prediction.source == "ml_backend",
            )
        )
    ).scalar()
    assert external_count == 1
    assert ml_count == 1


async def test_import_aap_json_explicit_overwrite_false_appends(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.5,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        data={"overwrite_existing": "false"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    count = (
        await db_session.execute(
            select(func.count(Prediction.id)).where(
                Prediction.task_id == tasks[0].id,
                Prediction.source == "external_import",
            )
        )
    ).scalar()
    assert count == 2


async def test_import_aap_json_multi_file_overwrite_shares_purge_scope(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}
    db_session.add_all(
        [
            Prediction(
                id=uuid.uuid4(),
                task_id=tasks[0].id,
                project_id=project.id,
                ml_backend_id=None,
                result=[],
                source="external_import",
                model_version="old-task-0",
            ),
            Prediction(
                id=uuid.uuid4(),
                task_id=tasks[1].id,
                project_id=project.id,
                ml_backend_id=None,
                result=[],
                source="external_import",
                model_version="old-task-1",
            ),
        ]
    )
    await db_session.flush()

    file_a = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.5,
                        "model_version": "new-a",
                    }
                ],
            }
        ]
    )
    file_b = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.3,
                            "y": 0.3,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "truck",
                        "confidence": 0.6,
                        "model_version": "new-b",
                    }
                ],
            },
            {
                "task_match": {"display_id": tasks[1].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.4,
                            "y": 0.4,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.7,
                        "model_version": "new-c",
                    }
                ],
            },
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_multi_files([("a.json", file_a), ("b.json", file_b)]),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 3

    rows = (
        (
            await db_session.execute(
                select(Prediction).where(
                    Prediction.project_id == project.id,
                    Prediction.source == "external_import",
                )
            )
        )
        .scalars()
        .all()
    )
    by_task: dict[uuid.UUID, list[Prediction]] = {}
    for row in rows:
        by_task.setdefault(row.task_id, []).append(row)
    assert {row.model_version for row in by_task[tasks[0].id]} == {"new-a", "new-b"}
    assert {row.model_version for row in by_task[tasks[1].id]} == {"new-c"}


async def test_purge_predictions_by_source_scope_counts_and_audits(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}
    external = Prediction(
        id=uuid.uuid4(),
        task_id=tasks[0].id,
        project_id=project.id,
        ml_backend_id=None,
        result=[],
        source="external_import",
    )
    ml = Prediction(
        id=uuid.uuid4(),
        task_id=tasks[0].id,
        project_id=project.id,
        ml_backend_id=None,
        result=[],
        source="ml_backend",
    )
    external_other_task = Prediction(
        id=uuid.uuid4(),
        task_id=tasks[1].id,
        project_id=project.id,
        ml_backend_id=None,
        result=[],
        source="external_import",
    )
    db_session.add_all([external, ml, external_other_task])
    await db_session.flush()
    meta = PredictionMeta(
        id=uuid.uuid4(),
        prediction_id=external.id,
        prediction_created_at=external.created_at,
        total_cost=0.1,
    )
    db_session.add(meta)
    await db_session.flush()

    preview = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/purge",
        json={
            "source_scope": "external_import",
            "task_ids": [str(tasks[0].id)],
            "dry_run": True,
        },
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["counts"] == {
        "ml_backend": 0,
        "external_import": 1,
        "unknown": 0,
        "total": 1,
    }
    assert (
        await db_session.scalar(
            select(func.count(Prediction.id)).where(Prediction.project_id == project.id)
        )
    ) == 3

    purge = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/purge",
        json={
            "source_scope": "external_import",
            "task_ids": [str(tasks[0].id)],
        },
        headers=headers,
    )
    assert purge.status_code == 200, purge.text
    assert purge.json()["counts"]["total"] == 1

    remaining = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert {row.id for row in remaining} == {ml.id, external_other_task.id}
    assert (
        await db_session.scalar(
            select(func.count(PredictionMeta.id)).where(
                PredictionMeta.prediction_id == external.id
            )
        )
    ) == 0
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "predictions.purge")
    )
    assert audit is not None
    assert audit.detail_json["source_scope"] == "external_import"
    assert audit.detail_json["counts"]["total"] == 1


# ── 未知几何 kind 不让整批挂 ────────────────────────────────────────


async def test_import_aap_json_unknown_kind_in_errors(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    # 1 个不支持
                    {
                        "geometry": {"type": "ellipse", "cx": 0.5, "cy": 0.5},
                        "class_name": "car",
                        "confidence": 0.5,
                    },
                    # 1 个 ok
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "class_name": "car",
                        "confidence": 0.5,
                    },
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 1
    assert body["skipped"] == 1
    assert any("ellipse" in err["reason"] for err in body["errors"])


async def test_import_aap_json_entry_shapes_merge_into_one_prediction(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    payload = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "predictions": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.9,
                            "y": 0.9,
                            "w": 0.05,
                            "h": 0.05,
                        },
                        "shapes": [
                            {
                                "type": "bbox",
                                "x": 0.1,
                                "y": 0.1,
                                "w": 0.2,
                                "h": 0.2,
                            },
                            {
                                "type": "bbox",
                                "x": 0.4,
                                "y": 0.5,
                                "w": 0.2,
                                "h": 0.1,
                                "confidence": 0.61,
                            },
                            {"type": "ellipse", "cx": 0.5, "cy": 0.5},
                        ],
                        "class_name": "car",
                        "confidence": 0.7,
                        "score": 0.42,
                    }
                ],
            }
        ]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 1
    assert body["skipped"] == 1
    assert any("ellipse" in err["reason"] for err in body["errors"])

    rows = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == tasks[0].id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    pred = rows[0]
    assert pred.score == 0.42
    assert len(pred.result) == 2
    assert [shape["type"] for shape in pred.result] == [
        "rectanglelabels",
        "rectanglelabels",
    ]
    assert pred.result[0]["value"]["x"] == pytest.approx(10.0)
    assert pred.result[1]["score"] == 0.61


# ── 权限 ────────────────────────────────────────────────────────────


async def test_import_aap_json_forbidden_for_annotator(
    httpx_client: httpx.AsyncClient,
    super_admin,
    annotator,
    db_session: AsyncSession,
):
    user, _ = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    _annotator_user, annotator_token = annotator
    # Visible project member with the annotator role: import stays a management
    # capability, so the denial must be 403 rather than an invisible 404.
    await create_membership(
        db_session, project_id=project.id, user_id=_annotator_user.id, role="annotator"
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {annotator_token}"}

    payload = _aap_envelope(
        [{"task_match": {"display_id": tasks[0].display_id}, "predictions": []}]
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=aap_json",
        files=_upload_files(payload),
        headers=headers,
    )
    assert r.status_code == 403


# ── COCO importer ──────────────────────────────────────────────────


async def test_import_coco_happy(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    coco_payload = json.dumps(
        {
            "info": {"description": "external"},
            "images": [
                {
                    "id": 1,
                    "file_name": tasks[0].file_path,
                    "width": 1920,
                    "height": 1080,
                }
            ],
            "categories": [{"id": 0, "name": "car"}],
            "annotations": [
                {
                    "id": 1,
                    "image_id": 1,
                    "category_id": 0,
                    "bbox": [192, 108, 576, 432],
                    "area": 0,
                    "iscrowd": 0,
                    "score": 0.88,
                }
            ],
        }
    ).encode("utf-8")

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=coco",
        files=_upload_files(coco_payload, "coco.json"),
        data={"image_width": "1", "image_height": "1"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 1

    rows = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == tasks[0].id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    bbox = rows[0].result[0]["value"]
    # 192/1920=0.1, 108/1080=0.1, 576/1920=0.3, 432/1080=0.4 -> *100
    assert bbox["x"] == pytest.approx(10.0, rel=0.01)
    assert bbox["width"] == pytest.approx(30.0, rel=0.01)


async def test_import_coco_uses_image_size_hint_when_missing_dimensions(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}

    coco_payload = json.dumps(
        {
            "images": [{"id": 1, "file_name": tasks[0].file_path}],
            "categories": [{"id": 0, "name": "car"}],
            "annotations": [
                {
                    "id": 1,
                    "image_id": 1,
                    "category_id": 0,
                    "bbox": [10, 20, 30, 40],
                    "score": 0.88,
                }
            ],
        }
    ).encode("utf-8")

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=coco",
        files=_upload_files(coco_payload, "coco-no-size.json"),
        data={"image_width": "100", "image_height": "200"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1

    rows = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == tasks[0].id)
            )
        )
        .scalars()
        .all()
    )
    bbox = rows[0].result[0]["value"]
    assert bbox["x"] == pytest.approx(10.0)
    assert bbox["y"] == pytest.approx(10.0)
    assert bbox["width"] == pytest.approx(30.0)
    assert bbox["height"] == pytest.approx(20.0)


async def test_import_coco_rle_builds_portable_prediction_and_reports_invalid_items(
    super_admin,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    user, _ = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    single = encode_coco_rle([0, 0, 0, 0, 0, 1], 3, 2)
    empty = encode_coco_rle([0] * 6, 3, 2)
    payload = json.dumps(
        {
            "images": [
                {
                    "id": 1,
                    "file_name": tasks[0].file_path,
                    "width": 3,
                    "height": 2,
                }
            ],
            "categories": [{"id": 0, "name": "car"}],
            "annotations": [
                {
                    "id": 1,
                    "image_id": 1,
                    "category_id": 0,
                    "segmentation": {"size": [2, 3], "counts": single["counts"]},
                },
                {
                    "id": 2,
                    "image_id": 1,
                    "category_id": 0,
                    "segmentation": {"size": [2, 3], "counts": empty["counts"]},
                },
                {
                    "id": 3,
                    "image_id": 1,
                    "category_id": 0,
                    "bbox": [0, 0, 3, 2],
                    "segmentation": [[0, 0, 3, 0, 3, 2]],
                },
                {
                    "id": 4,
                    "image_id": 1,
                    "category_id": 0,
                    "segmentation": {"size": [2, 3], "counts": [5]},
                },
                {
                    "id": 5,
                    "image_id": 1,
                    "category_id": 0,
                    "segmentation": {"size": [2, 3], "counts": "encoded"},
                },
                {
                    "id": 6,
                    "image_id": 1,
                    "category_id": 0,
                    "segmentation": {"size": [2, 3], "counts": single["counts"]},
                },
            ],
        }
    ).encode()

    import app.services.predictions_import as predictions_import

    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)

    async def _store(rle):
        return build_rle_reference(rle)

    store = AsyncMock(side_effect=_store)
    create = AsyncMock()
    monkeypatch.setattr(predictions_import, "store_coco_rle", store)
    monkeypatch.setattr(
        predictions_import.PredictionService,
        "create_from_ml_result",
        create,
    )

    result = await import_coco(db_session, project.id, payload)

    assert result.imported == 3
    assert result.skipped == 3
    assert store.await_count == 1
    assert create.await_count == 3
    assert "foreground pixels" in result.errors[0].reason
    assert "sum(counts)" in result.errors[1].reason
    assert "compressed COCO RLE" in result.errors[2].reason

    raster_shape = create.await_args_list[0].kwargs["result"][0]
    assert raster_shape["type"] == "raster_mask"
    assert raster_shape["tool_unit_id"] == "region"
    assert raster_shape["geometry"]["type"] == "raster_mask"
    assert "counts" not in raster_shape["geometry"]["mask"]
    assert to_internal_shape(raster_shape)["geometry"] == raster_shape["geometry"]
    assert create.await_args_list[1].kwargs["result"][0]["type"] == "polygonlabels"
    assert create.await_args_list[2].kwargs["result"][0]["type"] == "raster_mask"


async def test_import_coco_rle_respects_create_flag(
    super_admin,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    user, _ = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    rle = encode_coco_rle([0, 1, 0, 0, 0, 0], 3, 2)
    payload = json.dumps(
        {
            "images": [
                {
                    "id": 1,
                    "file_name": tasks[0].file_path,
                    "width": 3,
                    "height": 2,
                }
            ],
            "categories": [{"id": 0, "name": "car"}],
            "annotations": [
                {
                    "image_id": 1,
                    "category_id": 0,
                    "segmentation": {"size": [2, 3], "counts": rle["counts"]},
                }
            ],
        }
    ).encode()

    import app.services.predictions_import as predictions_import

    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    store = AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(predictions_import, "store_coco_rle", store)
    monkeypatch.setattr(
        predictions_import.PredictionService,
        "create_from_ml_result",
        create,
    )

    result = await import_coco(db_session, project.id, payload)

    assert result.imported == 0
    assert result.skipped == 1
    assert result.errors[0].reason.endswith("raster mask creation is disabled")
    store.assert_not_awaited()
    create.assert_not_awaited()


# ── YOLO zip importer ────────────────────────────────────────────────


async def test_import_yolo_det_zip_happy(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_yolo_project(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}
    payload = _zip_files(
        {
            "classes.txt": "car\ntruck\n",
            "labels/img_0.txt": "0 0.500000 0.500000 0.200000 0.400000\n",
        }
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=yolo&yolo_variant=det",
        files=_upload_zip(payload),
        data={"model_version": "ext-yolo-det"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1

    rows = (
        (
            await db_session.execute(
                select(Prediction).where(Prediction.task_id == tasks[0].id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].source == "external_import"
    assert rows[0].model_version == "ext-yolo-det"
    shape = to_internal_shape(rows[0].result[0])
    assert shape["type"] == "rectanglelabels"
    assert shape["geometry"]["type"] == "bbox"
    assert shape["geometry"]["x"] == pytest.approx(0.4)
    assert shape["geometry"]["y"] == pytest.approx(0.3)
    assert shape["geometry"]["w"] == pytest.approx(0.2)
    assert shape["geometry"]["h"] == pytest.approx(0.4)


async def test_import_yolo_seg_matches_relative_stem_path(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_yolo_project(
        db_session,
        user.id,
        rel_paths=["animals/cat/img_0.jpg"],
    )
    headers = {"Authorization": f"Bearer {token}"}
    payload = _zip_files(
        {
            "data.yaml": "names:\n  0: car\n  1: truck\n",
            "labels/animals/cat/img_0.txt": "1 0.100000 0.200000 0.300000 0.200000 0.300000 0.500000\n",
        }
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=yolo&yolo_variant=seg",
        files=_upload_zip(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1

    pred = await db_session.scalar(
        select(Prediction).where(Prediction.task_id == tasks[0].id)
    )
    assert pred is not None
    shape = to_internal_shape(pred.result[0])
    assert shape["class_name"] == "truck"
    assert shape["type"] == "polygonlabels"
    assert shape["geometry"]["type"] == "polygon"
    assert shape["geometry"]["points"] == [[0.1, 0.2], [0.3, 0.2], [0.3, 0.5]]


async def test_import_yolo_obb_round_trips_export_corner_order(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, tasks = await _seed_yolo_project(db_session, user.id, size=(200, 100))
    headers = {"Authorization": f"Bearer {token}"}
    source_geom = {
        "type": "rotated_bbox",
        "cx": 0.5,
        "cy": 0.5,
        "w": 0.2,
        "h": 0.4,
        "angle": 30,
    }
    corners = _rotated_corners_norm(source_geom, 200, 100)
    payload = _zip_files(
        {
            "classes.txt": "car\ntruck\n",
            "labels/img_0.txt": "0 " + " ".join(f"{v:.6f}" for v in corners),
        }
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=yolo&yolo_variant=obb",
        files=_upload_zip(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1

    pred = await db_session.scalar(
        select(Prediction).where(Prediction.task_id == tasks[0].id)
    )
    assert pred is not None
    shape = to_internal_shape(pred.result[0])
    assert shape["type"] == "rectanglelabels"
    assert shape["geometry"]["type"] == "rotated_bbox"
    assert shape["geometry"]["cx"] == pytest.approx(source_geom["cx"], abs=1e-5)
    assert shape["geometry"]["cy"] == pytest.approx(source_geom["cy"], abs=1e-5)
    assert shape["geometry"]["w"] == pytest.approx(source_geom["w"], abs=1e-5)
    assert shape["geometry"]["h"] == pytest.approx(source_geom["h"], abs=1e-5)
    assert shape["geometry"]["angle"] == pytest.approx(source_geom["angle"], abs=1e-3)


async def test_import_yolo_ambiguous_leaf_stem_is_error(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    project, _ = await _seed_yolo_project(
        db_session,
        user.id,
        rel_paths=["a/img.jpg", "b/img.png"],
    )
    headers = {"Authorization": f"Bearer {token}"}
    payload = _zip_files(
        {
            "classes.txt": "car\ntruck\n",
            "labels/img.txt": "0 0.500000 0.500000 0.200000 0.200000\n",
        }
    )

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/predictions/import?format=yolo&yolo_variant=det&dry_run=true",
        files=_upload_zip(payload),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 0
    assert body["skipped"] == 1
    assert "ambiguous task file stem" in body["errors"][0]["reason"]


# ── 老 prediction 默认 source ────────────────────────────────────────


async def test_existing_predictions_have_default_source(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    """alembic 0069 默认值: 老数据 source='ml_backend' (无需显式查 SQL,
    直接插入不指定 source 走 default)."""
    user, _ = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, user.id)
    pred = Prediction(
        id=uuid.uuid4(),
        task_id=tasks[0].id,
        project_id=project.id,
        ml_backend_id=None,
        result=[],
    )
    db_session.add(pred)
    await db_session.flush()
    assert pred.source == "ml_backend"
