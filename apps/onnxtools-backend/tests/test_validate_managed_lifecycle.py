from __future__ import annotations

import hashlib
from types import SimpleNamespace

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


def test_plugin_artifacts_use_controlled_data_directory(tmp_path, monkeypatch) -> None:
    _approved_fixtures(tmp_path, monkeypatch)
    models = tmp_path / "controlled" / "models"
    models.mkdir(parents=True)
    detector = models / "det.onnx"
    classifier = models / "va.onnx"
    detector.write_bytes(b"approved-detector")
    classifier.write_bytes(b"approved-classifier")
    monkeypatch.setenv("AAP_PLUGIN_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("AAP_PLUGIN_DATA_DIR", str(models.parent))
    monkeypatch.setenv("ONNXTOOLS_MODEL_DIR", str(tmp_path / "uncontrolled"))
    monkeypatch.setenv("ONNXTOOLS_DET_MODEL", detector.name)
    monkeypatch.setenv("ONNXTOOLS_VA_MODEL", classifier.name)
    monkeypatch.setenv("VALIDATION_DET_SHA256", _sha256(detector.read_bytes()))
    monkeypatch.setenv("VALIDATION_VA_SHA256", _sha256(classifier.read_bytes()))
    monkeypatch.setenv("VALIDATION_MODEL_APPROVAL_REF", "approval:test")

    artifacts = validator._validate_artifacts()
    assert artifacts["detector"]["path"] == str(detector)
    assert artifacts["vehicle_attribute"]["path"] == str(classifier)


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
                {
                    "result": [
                        {
                            "value": {
                                "x": x,
                                "y": 20,
                                "width": width,
                                "height": 40,
                                "rectanglelabels": ["car"],
                            }
                        }
                    ]
                }
            ]
        }

    validator._assert_boundary_result("vehicle-detect", payload(0, 30))
    with pytest.raises(AssertionError):
        validator._assert_boundary_result("vehicle-detect", payload(20, 30))
    with pytest.raises(AssertionError):
        validator._assert_boundary_result("vehicle-detect", payload(95, 10))
    not_car = payload(0, 30)
    not_car["results"][0]["result"][0]["value"]["rectanglelabels"] = ["plate"]
    with pytest.raises(AssertionError):
        validator._assert_boundary_result("vehicle-detect", not_car)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "fallback", "wrong_device", "missing"])
async def test_provider_evidence_checks_each_composite_session(invalid) -> None:
    from handle_pool import HANDLE_SESSION_COUNTS, HandlePool

    class Session:
        def __init__(self, bad):
            self.bad = bad

        def get_providers(self):
            return (
                ["CPUExecutionProvider"]
                if self.bad == "fallback"
                else ["CUDAExecutionProvider", "CPUExecutionProvider"]
            )

        def get_provider_options(self):
            return {
                "CUDAExecutionProvider": {
                    "device_id": "1" if self.bad == "wrong_device" else "0"
                }
            }

    def owner(bad=None):
        return SimpleNamespace(_onnx_session=None if bad == "missing" else Session(bad))

    pool = HandlePool(
        {
            "pipeline": lambda: SimpleNamespace(
                detector=owner(), va_classifier=owner(invalid)
            ),
            "detector": owner,
            "va": owner,
        },
        lambda _name, _handle: None,
    )
    main = SimpleNamespace(_handle_pool=pool)
    try:
        if invalid:
            with pytest.raises(AssertionError):
                await validator._provider_evidence(main, HANDLE_SESSION_COUNTS)
        else:
            evidence = await validator._provider_evidence(main, HANDLE_SESSION_COUNTS)
            assert {name: len(sessions) for name, sessions in evidence.items()} == {
                "pipeline": 2,
                "detector": 1,
                "va": 1,
            }
            assert all(
                session["device_id"] == "0"
                and session["providers"][0] == "CUDAExecutionProvider"
                for sessions in evidence.values()
                for session in sessions
            )
    finally:
        await pool.shutdown()


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
    evidence = validator._failed_evidence(
        RuntimeError("local path must not leak"), validator._initial_evidence()
    )

    assert evidence["schema_version"] == "1"
    assert evidence["passed"] is False
    assert "local path" not in str(evidence)


def test_failed_memory_gate_preserves_completed_cycle_evidence() -> None:
    from aap_lifecycle_contract import memory_cycle_evidence

    progress = validator._initial_evidence()
    progress["stage"] = "memory_recovery"
    progress["contract_checks"]["real_inference"] = True
    progress["deployment"]["pool_topology"] = {
        "registry_id": "actual-registry",
        "gpu_resource_id": "actual-resource",
        "pools": {"pipeline": {"cap": 1}},
    }
    for index in (1, 2):
        cycle = memory_cycle_evidence(
            cycle=index,
            generation=str(index),
            context_samples_mb=[18] * 5,
            loaded_samples_mb=[1439] * 3,
            unloaded_samples_mb=[303] * 5,
        )
        cycle["session_observations"] = {
            "pipeline": [["CUDAExecutionProvider", "CPUExecutionProvider"]]
        }
        progress["cycles"].append(cycle)

    evidence = validator._failed_evidence(AssertionError("private detail"), progress)
    assert evidence["passed"] is False
    assert len(evidence["cycles"]) == 2
    assert evidence["cycles"][0]["unloaded_samples_mb"] == [303] * 5
    assert evidence["cycles"][0]["working_set_recovery_ratio"] < 0.9
    assert evidence["contract_checks"]["real_inference"] is True
    assert evidence["deployment"]["pool_topology"]["registry_id"] == "actual-registry"
    assert "private detail" not in str(evidence)
    assert any(
        item["code"] == "memory_recovery_insufficient" for item in evidence["blockers"]
    )


@pytest.mark.parametrize(
    "failed", ["cudaSetDevice", "cudaFree", "cudaDeviceSynchronize"]
)
def test_context_initialization_fails_closed(monkeypatch, failed) -> None:
    class Call:
        def __init__(self, name):
            self.name = name

        def __call__(self, *_args):
            return 1 if self.name == failed else 0

    library = SimpleNamespace(
        cudaSetDevice=Call("cudaSetDevice"),
        cudaFree=Call("cudaFree"),
        cudaDeviceSynchronize=Call("cudaDeviceSynchronize"),
    )
    monkeypatch.setattr(validator.ctypes, "CDLL", lambda _name: library)
    with pytest.raises(RuntimeError, match=failed):
        validator._initialize_cuda_context()
