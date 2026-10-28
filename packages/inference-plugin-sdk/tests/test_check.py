import json

import pytest

from aap_inference_plugin.check import _assert_matches, load_fixtures
from aap_inference_plugin.qualification import _validate_native


def fixture():
    return {"multi": {"image": "multi.png", "box_tolerance": 1, "score_tolerance": .05,
                      "detections": [{"native_class_index": 0, "label": "cat", "box": [10, 10, 20, 20], "score": .9},
                                     {"native_class_index": 1, "label": "dog", "box": [40, 40, 30, 30], "score": .8}]},
            "empty": {"image": "empty.png", "box_tolerance": 0, "score_tolerance": 0, "detections": []}}


def actual(index, label, x, y, w, h, score):
    return {"type": "rectanglelabels", "native_class_index": index, "score": score,
            "value": {"x": x, "y": y, "width": w, "height": h, "rectanglelabels": [label]}}


def test_fixtures_require_real_multibox_and_empty_expectations(tmp_path):
    path = tmp_path / "fixtures.json"
    cases = fixture()
    path.write_text(json.dumps(cases))
    loaded, images = load_fixtures(path)
    assert loaded == cases
    assert images["multi"] == tmp_path / "multi.png"
    cases["multi"]["detections"] = []
    path.write_text(json.dumps(cases))
    with pytest.raises(ValueError, match="at least two"):
        load_fixtures(path)


def test_matches_all_detections_regardless_of_order_and_rejects_mismatch():
    case = fixture()["multi"]
    detections = [actual(1, "dog", 40, 40, 30, 30, .8), actual(0, "cat", 10.5, 10, 20, 20, .9)]
    _assert_matches("multi", detections, case)
    detections[0]["native_class_index"] = 0
    with pytest.raises(ValueError, match="did not match"):
        _assert_matches("multi", detections, case)
    with pytest.raises(ValueError, match="expected 2"):
        _assert_matches("multi", [], case)


def test_native_report_rejects_missing_or_failed_evidence():
    names = ("missing_weights", "wrong_device", "gpu_residency", "native_multi", "native_empty", "gpu_release")
    report = {"mode": "local-isolated", "device": "cuda:0", "gpu_uuid": "GPU-123", "platform_managed_qualified": False,
              "checks": {name: {"passed": True, "detail": "observed"} for name in names}}
    _validate_native(report, "cuda:0", "GPU-123")
    report["checks"]["gpu_release"]["passed"] = False
    with pytest.raises(ValueError, match="gpu_release"):
        _validate_native(report, "cuda:0", "GPU-123")
    report["checks"]["gpu_release"]["passed"] = True
    report["platform_managed_qualified"] = True
    with pytest.raises(ValueError, match="scope"):
        _validate_native(report, "cuda:0", "GPU-123")
