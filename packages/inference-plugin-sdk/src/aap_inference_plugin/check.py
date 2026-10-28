"""Exercise a real endpoint using explicit, locally supplied image expectations."""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .image import MAX_IMAGE_BYTES


def load_fixtures(path: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"multi", "empty"}:
        raise ValueError("fixture file must contain exactly multi and empty cases")
    paths = {}
    for name in ("multi", "empty"):
        case = raw[name]
        if not isinstance(case, dict) or set(case) != {"image", "detections", "box_tolerance", "score_tolerance"}:
            raise ValueError(f"fixtures.{name} requires image, detections, box_tolerance and score_tolerance")
        if not isinstance(case["image"], str) or not case["image"]:
            raise ValueError(f"fixtures.{name}.image must be a local image path")
        paths[name] = (path.parent / case["image"]).resolve()
        detections = case["detections"]
        if not isinstance(detections, list) or (len(detections) < 2 if name == "multi" else bool(detections)):
            raise ValueError("multi must specify at least two expected detections and empty must specify none")
        for tolerance in ("box_tolerance", "score_tolerance"):
            value = case[tolerance]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"fixtures.{name}.{tolerance} must be a nonnegative finite number")
        for item in detections:
            if not isinstance(item, dict) or set(item) != {"native_class_index", "label", "box", "score"}:
                raise ValueError(f"fixtures.{name}.detections entries require native_class_index, label, box and score")
            index, label, box, score = (item[k] for k in ("native_class_index", "label", "box", "score"))
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or not isinstance(label, str) or not label:
                raise ValueError(f"fixtures.{name} has an invalid native class identity")
            if not isinstance(box, list) or len(box) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
                raise ValueError(f"fixtures.{name} box must be four finite percentage values")
            x, y, width, height = box
            if not (0 <= x < 100 and 0 <= y < 100 and 0 < width <= 100 - x and 0 < height <= 100 - y):
                raise ValueError(f"fixtures.{name} box is outside the image")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError(f"fixtures.{name} score must be finite and in [0,1]")
    return raw, paths


def _data_url(path: Path) -> str:
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"fixture image {path} must be nonempty and at most {MAX_IMAGE_BYTES} bytes")
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _matches(actual: dict, expected: dict, box_tolerance: float, score_tolerance: float) -> bool:
    if not isinstance(actual, dict) or actual.get("type") != "rectanglelabels":
        return False
    value = actual.get("value")
    if not isinstance(value, dict) or value.get("rectanglelabels") != [expected["label"]]:
        return False
    index = actual.get("native_class_index")
    score = actual.get("score")
    if isinstance(index, bool) or not isinstance(index, int) or index != expected["native_class_index"]:
        return False
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
        return False
    if abs(score - expected["score"]) > score_tolerance:
        return False
    box = [value.get(k) for k in ("x", "y", "width", "height")]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
        return False
    x, y, w, h = box
    if not (0 <= x < 100 and 0 <= y < 100 and 0 < w <= 100 - x and 0 < h <= 100 - y):
        return False
    return all(abs(a - b) <= box_tolerance for a, b in zip(box, expected["box"]))


def _assert_matches(name: str, actual: Any, case: dict) -> None:
    expected = case["detections"]
    if not isinstance(actual, list) or len(actual) != len(expected):
        count = len(actual) if isinstance(actual, list) else "not an array"
        raise ValueError(f"{name}: expected {len(expected)} detections, received {count}; update fixtures only after inspecting real results")
    # Match as a multiset: providers need not return detections in a stable order.
    assigned: dict[int, int] = {}
    def assign(actual_index: int, seen: set[int]) -> bool:
        for expected_index, item in enumerate(expected):
            if expected_index in seen or not _matches(actual[actual_index], item, case["box_tolerance"], case["score_tolerance"]):
                continue
            seen.add(expected_index)
            if expected_index not in assigned or assign(assigned[expected_index], seen):
                assigned[expected_index] = actual_index
                return True
        return False
    for i in range(len(actual)):
        if not assign(i, set()):
            raise ValueError(f"{name}: detection {i} did not match an expected class, box and score within the stated tolerances")


def check_http(url: str, token: str, fixture_file: Path) -> dict[str, Any]:
    if len(token) < 32:
        raise ValueError("set AAP_PLUGIN_TOKEN to the configured token (at least 32 characters)")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("--url must be an HTTP(S) service URL without credentials, query or fragment")
    fixtures, paths = load_fixtures(fixture_file)
    sources = {name: _data_url(path) for name, path in paths.items()}
    with httpx.Client(base_url=url.rstrip("/") + "/", timeout=120, follow_redirects=False, trust_env=False) as client:
        response = client.get("health")
        response.raise_for_status()
        if response.json().get("status") != "ok":
            raise ValueError("/health did not report status=ok")
        if client.get("setup").status_code not in (401, 403):
            raise ValueError("/setup accepted a request without a token")
        client.headers["Authorization"] = "Bearer " + token
        setup = client.get("setup")
        setup.raise_for_status()
        models = setup.json().get("models", [])
        if not any(m.get("id") == "detect" and m.get("task") == "detect" and set(m.get("supported_inputs", [])) >= {"full_image", "crop"} for m in models):
            raise ValueError("/setup must expose detect with full_image and crop inputs")
        requests = [{"id": name, "file_path": sources[name]} for name in ("multi", "empty")]
        body = {"tasks": requests, "context": {"task": "detect", "model_id": "detect"}}
        response = client.post("predict", json=body)
        response.raise_for_status()
        results = response.json().get("results")
        if not isinstance(results, list) or len(results) != 2:
            raise ValueError("/predict must return one result for each fixture")
        by_task = {item.get("task"): item for item in results if isinstance(item, dict)}
        if set(by_task) != {"multi", "empty"}:
            raise ValueError("/predict did not preserve both distinct task ids")
        for name in ("multi", "empty"):
            _assert_matches(name, by_task[name].get("result"), fixtures[name])
        invalid = {"tasks": [requests[0]], "context": {"task": "detect", "params": {"conf": 2}}}
        if client.post("predict", json=invalid).status_code != 422:
            raise ValueError("/predict did not reject confidence outside [0,1]")
        local = {"tasks": [{"id": "local", "file_path": str(paths["multi"])}], "context": {"task": "detect"}}
        if client.post("predict", json=local).status_code not in (400, 422):
            raise ValueError("/predict accepted or failed to reject a local filesystem path")
    return {"scope": "http-endpoint", "checks": {"health": True, "authentication": True, "setup": True,
            "multi_detection": True, "empty_detection": True, "class_coordinates_score": True,
            "invalid_parameters": True, "local_path_rejected": True}, "managed_gpu_qualified": False}
