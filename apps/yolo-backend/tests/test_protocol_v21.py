"""v2.1 request and error contract tests."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module", autouse=True)
def _stub_modules() -> None:
    sys.modules.setdefault(
        "torch", MagicMock(cuda=MagicMock(is_available=MagicMock(return_value=False)))
    )
    sys.modules.setdefault("ultralytics", MagicMock())


@pytest.fixture()
def app_client(tmp_path):
    os.environ["YOLO_CHECKPOINTS_DIR"] = str(tmp_path)
    import importlib
    import main

    main = importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield main, client


class _PredictorOk:
    async def predict_one(self, file_path, ctx):
        return [], True, None, 1


class _PredictorMissingWeight:
    async def predict_one(self, file_path, ctx):
        raise FileNotFoundError("missing yolo11s.pt")


class _PredictorOneBox:
    async def predict_one(self, file_path, ctx):
        item = {
            "type": "rectanglelabels",
            "value": {
                "x": 1,
                "y": 1,
                "width": 1,
                "height": 1,
                "rectanglelabels": ["object0"],
            },
            "score": 0.9,
        }
        return [item], True, None, 5


class _PredictorPoolBusy:
    async def predict_one(self, file_path, ctx):
        from model_pool import PoolBusyError

        raise PoolBusyError("all model pool slots are active")


class _PredictorHTTPError:
    async def predict_one(self, file_path, ctx):
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="unsupported output")


def test_predict_singular_task_wire_returns_singular_shape(
    app_client, monkeypatch
) -> None:
    """平台交互调用发单数 {task, context}; 响应须为单数形 (顶层 result, 无 results),
    否则平台 predict_interactive 读 data["result"] 拿不到结果 (exemplar 候选丢失)。"""
    main, client = app_client
    monkeypatch.setattr(main, "_predictor", _PredictorOneBox())
    resp = client.post(
        "/predict",
        json={
            "task": {"id": "t1", "file_path": "unused.jpg"},
            "context": {
                "type": "exemplar",
                "exemplars": [{"bbox": [0.1, 0.1, 0.3, 0.3], "label": True}],
                "output": "box",
                "model_variants": {"series": "yoloe-11", "size": "s"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "result" in data and "results" not in data  # 单数形
    assert len(data["result"]) == 1
    assert data["result"][0]["type"] == "rectanglelabels"


def test_predict_plural_tasks_wire_returns_batch_shape(app_client, monkeypatch) -> None:
    """复数 {tasks, context} (批量) 仍回 BatchPredictResponse (results[])。"""
    main, client = app_client
    monkeypatch.setattr(main, "_predictor", _PredictorOneBox())
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "s"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "results" in data and "result" not in data  # 复数形
    assert len(data["results"]) == 1


def test_predict_accepts_model_variants(app_client, monkeypatch) -> None:
    main, client = app_client
    monkeypatch.setattr(main, "_predictor", _PredictorOk())
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "s"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["model_version"] == "yolo11s"


def test_predict_pool_busy_returns_retryable_503(app_client, monkeypatch) -> None:
    main, client = app_client
    monkeypatch.setattr(main, "_predictor", _PredictorPoolBusy())

    response = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "s"},
            },
        },
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.json()["detail"]["error_code"] == "model_unavailable"


def test_predict_preserves_http_exception_status(app_client, monkeypatch) -> None:
    main, client = app_client
    monkeypatch.setattr(main, "_predictor", _PredictorHTTPError())

    response = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "s"},
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "unsupported output"


def test_predict_accepts_legacy_variants_with_deprecation_warning(
    app_client, monkeypatch, caplog
) -> None:
    main, client = app_client
    monkeypatch.setattr(main, "_predictor", _PredictorOk())
    caplog.set_level("WARNING")
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "variants": {"series": "yolo11", "size": "s"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    assert "context.variants -> context.model_variants" in caplog.text


def test_predict_invalid_combo_returns_standard_422(app_client, monkeypatch) -> None:
    main, client = app_client
    monkeypatch.setattr(main, "_predictor", _PredictorOk())
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolov9", "size": "n"},
            },
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == {
        "error_code": "variant_not_supported",
        "axis": "size",
        "value": "n",
        "allowed": ["t", "s", "m", "c", "e"],
    }


def test_predict_missing_weight_returns_503_retry_after(
    app_client, monkeypatch
) -> None:
    main, client = app_client
    monkeypatch.setattr(main, "_predictor", _PredictorMissingWeight())
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "unused.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "s"},
            },
        },
    )
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"
    assert resp.json()["detail"]["error_code"] == "model_unavailable"
