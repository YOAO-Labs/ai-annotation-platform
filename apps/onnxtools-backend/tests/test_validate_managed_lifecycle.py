from __future__ import annotations

import hashlib

import pytest

from scripts import validate_managed_lifecycle as validator


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _approved_fixtures(tmp_path, monkeypatch) -> None:
    for prefix, content in (
        ("VALIDATION_IMAGE", b"vehicle"),
        ("VALIDATION_EMPTY_IMAGE", b"empty"),
        ("VALIDATION_BOUNDARY_IMAGE", b"edge-vehicle"),
    ):
        path = tmp_path / f"{prefix}.jpg"
        path.write_bytes(content)
        monkeypatch.setenv(f"{prefix}_PATH", str(path))
        monkeypatch.setenv(f"{prefix}_SHA256", _sha256(content))


def test_artifact_gate_accepts_only_approved_model_hashes(
    tmp_path,
    monkeypatch,
) -> None:
    detector = tmp_path / "det.onnx"
    classifier = tmp_path / "va.onnx"
    _approved_fixtures(tmp_path, monkeypatch)
    detector.write_bytes(b"approved-detector")
    classifier.write_bytes(b"approved-classifier")

    monkeypatch.setenv("ONNXTOOLS_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("ONNXTOOLS_DET_MODEL", detector.name)
    monkeypatch.setenv("ONNXTOOLS_VA_MODEL", classifier.name)
    monkeypatch.setenv("VALIDATION_DET_SHA256", _sha256(detector.read_bytes()))
    monkeypatch.setenv("VALIDATION_VA_SHA256", _sha256(classifier.read_bytes()))
    monkeypatch.setenv("VALIDATION_MODEL_APPROVAL_REF", "approval:test")

    artifacts = validator._validate_artifacts()

    assert artifacts["approval_ref"] == "approval:test"
    assert artifacts["detector"]["sha256"] == _sha256(detector.read_bytes())
    assert artifacts["vehicle_attribute"]["sha256"] == _sha256(classifier.read_bytes())


def test_artifact_gate_rejects_unapproved_model_hash(tmp_path, monkeypatch) -> None:
    detector = tmp_path / "det.onnx"
    classifier = tmp_path / "va.onnx"
    _approved_fixtures(tmp_path, monkeypatch)
    detector.write_bytes(b"candidate-detector")
    classifier.write_bytes(b"candidate-classifier")

    monkeypatch.setenv("ONNXTOOLS_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("ONNXTOOLS_DET_MODEL", detector.name)
    monkeypatch.setenv("ONNXTOOLS_VA_MODEL", classifier.name)
    monkeypatch.setenv("VALIDATION_DET_SHA256", "0" * 64)
    monkeypatch.setenv("VALIDATION_VA_SHA256", _sha256(classifier.read_bytes()))
    monkeypatch.setenv("VALIDATION_MODEL_APPROVAL_REF", "approval:test")

    with pytest.raises(RuntimeError, match="detector model SHA-256 is not approved"):
        validator._validate_artifacts()


def test_fixture_gate_rejects_changed_approved_fixture(tmp_path, monkeypatch) -> None:
    _approved_fixtures(tmp_path, monkeypatch)
    monkeypatch.setenv("VALIDATION_MODEL_APPROVAL_REF", "approval:test")
    monkeypatch.setenv("VALIDATION_BOUNDARY_IMAGE_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="boundary fixture SHA-256 is not approved"):
        validator._validate_artifacts()


def test_empty_and_edge_fixture_outputs_are_checked() -> None:
    validator._assert_empty_result("vehicle-detect", {"results": [{"result": []}]})
    with pytest.raises(AssertionError):
        validator._assert_empty_result(
            "vehicle-detect", {"results": [{"result": [{}]}]}
        )

    def payload(x, width):
        return {
            "results": [
                {"result": [{"value": {"x": x, "y": 20, "width": width, "height": 40}}]}
            ]
        }

    validator._assert_boundary_result("vehicle-detect", payload(0, 30))
    with pytest.raises(AssertionError):
        validator._assert_boundary_result("vehicle-detect", payload(20, 30))
    with pytest.raises(AssertionError):
        validator._assert_boundary_result("vehicle-detect", payload(95, 10))


def test_positive_classification_requires_meaningful_attributes() -> None:
    validator._assert_predict_results(
        "vehicle-attr",
        {
            "results": [
                {"result": [{"attributes": {"vehicle_type": "car", "color": "red"}}]}
            ]
        },
    )
    with pytest.raises(AssertionError):
        validator._assert_predict_results(
            "vehicle-attr", {"results": [{"result": [{"attributes": {}}]}]}
        )


def test_gpu_identity_rejects_wrong_or_multiple_visible_devices(monkeypatch) -> None:
    monkeypatch.setattr(
        validator, "_gpu_query", lambda _fields: [["GPU-a", "1", "2", "driver"]]
    )
    assert validator._gpu_row("GPU-a")[0] == "GPU-a"
    with pytest.raises(RuntimeError, match="not the sole visible GPU"):
        validator._gpu_row("GPU-b")
    monkeypatch.setattr(
        validator,
        "_gpu_query",
        lambda _fields: [["GPU-a", "1", "2", "driver"], ["GPU-b", "1", "2", "driver"]],
    )
    with pytest.raises(RuntimeError, match="exactly one visible GPU"):
        validator._gpu_row("GPU-a")


def test_memory_gate_requires_ninety_percent_working_set_recovery() -> None:
    accepted = {
        "gpu_total_mb": 24576,
        "context_baseline_mb": [100, 100, 100],
        "cycle_1_loaded_mb": [1100, 1100, 1100],
        "cycle_1_unloaded_mb": [120, 120, 120],
        "cycle_2_loaded_mb": [1100, 1100, 1100],
        "cycle_2_unloaded_mb": [120, 120, 120],
    }
    validator._assert_memory_recovery(accepted)

    rejected = {
        **accepted,
        "cycle_1_unloaded_mb": [250, 250, 250],
        "cycle_2_unloaded_mb": [250, 250, 250],
    }
    with pytest.raises(AssertionError, match="less than 90%"):
        validator._assert_memory_recovery(rejected)


def test_failure_path_emits_strict_nonpassing_evidence() -> None:
    evidence = validator._failed_evidence(RuntimeError("local path must not leak"))

    assert evidence["schema_version"] == "1"
    assert evidence["passed"] is False
    assert "local path" not in str(evidence)
