"""mock-v2 示例 contract tests — 保活, 防示例与 ml-backend-protocol v2.1 脱节。

覆盖: /health、/setup 多模型目录与 v2.1 字段、/predict 批量 / 交互式 / OCR、
variants 422 / 503 错误形态、/warmup 统一响应形态。
"""

import main
import pytest
from fastapi.testclient import TestClient

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_warmed() -> None:
    """每个测试前清空预热标记, 让 cache_hit 行为可预测。"""
    main._warmed.clear()


def test_health_returns_200() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_setup_declares_protocol_v21_and_warmup() -> None:
    body = client.get("/setup").json()
    assert body["name"] == "mock-v2-backend"
    assert body["version"]
    assert body["model_version"]
    assert body["protocol_version"] == "2.1"
    assert body["compat_protocol_versions"] == ["2.0"]
    assert body["warmup_endpoint"] is True
    assert body["infra"] == "onnx"
    assert body["is_interactive"] is True


def test_setup_models_catalog() -> None:
    models = client.get("/setup").json()["models"]
    ids = {m["id"] for m in models}
    assert ids == {
        "yolo-detect",
        "yolo-segment",
        "yolo-pose",
        "yolo-obb",
        "yolo-classify",
        "ppocr",
        "doclayout",
        "screenshot-interactive",
        "screenshot-tracker",
    }
    for m in models:
        assert m["task"]
        assert m["supported_geometric_outputs"]
    by_id = {m["id"]: m for m in models}
    assert by_id["yolo-detect"]["infra"] == "pytorch"
    assert by_id["ppocr"]["infra"] == "paddle"
    assert by_id["ppocr"]["output_attribute_types"] == ["text", "language"]
    assert by_id["screenshot-interactive"]["supported_prompts"] == [
        "point",
        "interactive_box",
        "exemplar",
    ]
    assert by_id["screenshot-tracker"]["supported_trackers"] == [
        "sam3_video_interactive",
        "sam2_video",
    ]


def test_setup_yolo_entries_carry_v2_1_variant_fields() -> None:
    models = client.get("/setup").json()["models"]
    detect = next(m for m in models if m["id"] == "yolo-detect")
    assert detect["default_variants"] == {"series": "yolo11", "size": "s"}
    combos = detect["variant_combinations"]
    assert ["yolo11", "x"] in combos
    assert ["yolo12", "l"] not in combos
    axes = {
        a["key"]: [v["value"] for v in a["variants"]]
        for a in detect["supported_variants"]
    }
    for series, size in combos:
        assert series in axes["series"]
        assert size in axes["size"]


def test_predict_batch_shape_with_runtime_observability() -> None:
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "a.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "s"},
            },
        },
    )
    assert resp.status_code == 200
    r = resp.json()["results"][0]
    assert r["task"] == "t1"
    assert r["result"][0]["type"] == "rectanglelabels"
    assert r["cache_hit"] is False
    assert r["model_load_ms"] > 0
    assert set(r["pool_state"]) == {"current_size", "cap"}


def test_predict_interactive_single_has_no_results_array() -> None:
    resp = client.post(
        "/predict",
        json={
            "task": {"id": "t1", "file_path": "a.jpg"},
            "context": {"type": "detection"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "results" not in body
    assert body["result"][0]["type"] == "rectanglelabels"


def test_predict_ocr_carries_attributes_text() -> None:
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "a.jpg"}],
            "context": {"type": "ocr"},
        },
    )
    shapes = resp.json()["results"][0]["result"]
    assert all("text" in s["attributes"] for s in shapes)


def test_predict_video_tracker_returns_frame_geometry() -> None:
    resp = client.post(
        "/predict",
        json={
            "task": {"id": "t1", "file_path": "clip.mp4"},
            "context": {
                "type": "video_tracker",
                "from_frame": 2,
                "to_frame": 4,
                "source_geometry": {
                    "type": "bbox",
                    "x": 0.1,
                    "y": 0.2,
                    "w": 0.3,
                    "h": 0.4,
                },
            },
        },
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert [item["frame_index"] for item in result] == [2, 3, 4]
    assert all(item["geometry"]["type"] == "bbox" for item in result)


def test_predict_legacy_context_variants_normalized() -> None:
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "a.jpg"}],
            "context": {
                "type": "detection",
                "variants": {"series": "yolov8", "size": "m"},
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["model_version"] == "mock-yolov8-m"


def test_predict_invalid_variant_value_returns_422() -> None:
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "a.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolov99", "size": "s"},
            },
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_code"] == "variant_not_supported"
    assert detail["axis"] == "series"
    assert detail["value"] == "yolov99"
    assert detail["allowed"] == ["yolov8", "yolo11", "yolo12"]


def test_predict_invalid_combination_returns_422() -> None:
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "a.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo12", "size": "l"},
            },
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_code"] == "variant_not_supported"
    assert detail["axis"] == "size"
    assert detail["allowed"] == ["n", "s", "m"]


def test_predict_size_x_returns_503_model_unavailable() -> None:
    resp = client.post(
        "/predict",
        json={
            "tasks": [{"id": "t1", "file_path": "a.jpg"}],
            "context": {
                "type": "detection",
                "model_variants": {"series": "yolo11", "size": "x"},
            },
        },
    )
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"
    detail = resp.json()["detail"]
    assert detail["error_code"] == "model_unavailable"
    assert detail["key"] == "yolo11/x"


def test_warmup_cold_then_cache_hit() -> None:
    body = {"task": "detection", "variants": {"series": "yolo11", "size": "s"}}
    first = client.post("/warmup", json=body)
    assert first.status_code == 200
    assert first.json() == {"ok": True, "cache_hit": False, "model_load_ms": 120}
    second = client.post("/warmup", json=body)
    assert second.json() == {"ok": True, "cache_hit": True, "model_load_ms": None}


def test_warmup_invalid_variant_returns_422() -> None:
    resp = client.post("/warmup", json={"variants": {"series": "yolo12", "size": "x"}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "variant_not_supported"


def test_warmup_size_x_returns_503() -> None:
    resp = client.post("/warmup", json={"variants": {"series": "yolov8", "size": "x"}})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "model_unavailable"
