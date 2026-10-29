"""ORT provider 探测与 /health 语义回归测试。"""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
import weakref
from types import SimpleNamespace

import pytest

# Provider 测试不触发图像解码；允许在未安装 OpenCV 的 API 虚拟环境里导入 main。
try:
    import cv2  # noqa: F401
except ImportError:
    sys.modules["cv2"] = types.ModuleType("cv2")

import main as backend_main


class _FakeSession:
    def __init__(self, providers):
        self.providers = list(providers)

    def get_providers(self):
        return list(self.providers)


def _install_ort(monkeypatch, *, available, actual=None, error=None):
    module = types.ModuleType("onnxruntime")
    module.get_available_providers = lambda: list(available)
    calls = []

    def inference_session(path, providers):
        calls.append((path, providers))
        if error is not None:
            raise error
        return _FakeSession(actual or [])

    module.InferenceSession = inference_session
    monkeypatch.setitem(sys.modules, "onnxruntime", module)
    return calls


def _reset_probe(monkeypatch) -> None:
    monkeypatch.setattr(backend_main, "_provider_preference", None)


def test_probe_uses_cpu_when_cuda_is_not_listed(monkeypatch):
    _reset_probe(monkeypatch)
    calls = _install_ort(
        monkeypatch,
        available=["CPUExecutionProvider"],
    )

    assert backend_main._probe_providers() == ["CPUExecutionProvider"]
    assert calls == []


def test_probe_detects_silent_cpu_fallback(monkeypatch):
    _reset_probe(monkeypatch)
    monkeypatch.setattr(backend_main.os.path, "exists", lambda _path: True)
    calls = _install_ort(
        monkeypatch,
        available=["CUDAExecutionProvider", "CPUExecutionProvider"],
        actual=["CPUExecutionProvider"],
    )

    assert backend_main._probe_providers() == ["CPUExecutionProvider"]
    assert len(calls) == 1


def test_probe_keeps_cuda_preference_only_after_cuda_session(monkeypatch):
    _reset_probe(monkeypatch)
    monkeypatch.setattr(backend_main.os.path, "exists", lambda _path: True)
    _install_ort(
        monkeypatch,
        available=["CUDAExecutionProvider", "CPUExecutionProvider"],
        actual=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    assert backend_main._probe_providers() == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_missing_model_keeps_preference_without_claiming_effective(monkeypatch):
    _reset_probe(monkeypatch)
    monkeypatch.setattr(backend_main.os.path, "exists", lambda _path: False)
    calls = _install_ort(
        monkeypatch,
        available=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    monkeypatch.setattr(backend_main, "_handle_pool", None)
    monkeypatch.setattr(backend_main, "_gpu_lifecycle", None)
    monkeypatch.setattr(
        backend_main,
        "_available_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    assert backend_main._probe_providers()[0] == "CUDAExecutionProvider"
    assert calls == []
    assert asyncio.run(backend_main.health())["compute"] == {
        "configured_device": "cuda",
        "effective_provider": None,
        "cpu_fallback_supported": True,
    }


def test_composite_routes_cpu_preference_to_both_business_sessions(monkeypatch):
    calls = []
    probe_calls = []

    def create_detector(**kwargs):
        calls.append(("detector", kwargs["providers"]))
        return SimpleNamespace(class_names={0: "car"})

    def create_va(_path, **kwargs):
        calls.append(("va", kwargs["providers"]))
        return object()

    class FakePipeline:
        def _resolve_class_names(self, _config):
            return [self.detector.class_names[0]]

    package = types.ModuleType("onnxtools")
    package.create_detector = create_detector
    package.VehicleAttributeORT = create_va
    pipeline_module = types.ModuleType("onnxtools.pipeline")
    pipeline_module.VehicleAttributePipeline = FakePipeline
    monkeypatch.setitem(sys.modules, "onnxtools", package)
    monkeypatch.setitem(sys.modules, "onnxtools.pipeline", pipeline_module)

    def probe_providers():
        probe_calls.append(True)
        return ["CPUExecutionProvider"]

    monkeypatch.setattr(backend_main, "_probe_providers", probe_providers)

    pipeline = backend_main._make_pipeline()

    assert calls == [
        ("detector", ["CPUExecutionProvider"]),
        ("va", ["CPUExecutionProvider"]),
    ]
    assert len(probe_calls) == 1
    assert pipeline.class_names == ["car"]


def test_partial_composite_failure_releases_constructed_detector(monkeypatch):
    references = []

    class Detector:
        pass

    def make_detector(_providers):
        detector = Detector()
        references.append(weakref.ref(detector))
        return detector

    def fail_classifier(_providers):
        raise RuntimeError("classifier construction failed")

    class FakePipeline:
        pass

    pipeline_module = types.ModuleType("onnxtools.pipeline")
    pipeline_module.VehicleAttributePipeline = FakePipeline
    monkeypatch.setitem(sys.modules, "onnxtools.pipeline", pipeline_module)
    monkeypatch.setattr(
        backend_main, "_probe_providers", lambda: ["CPUExecutionProvider"]
    )
    monkeypatch.setattr(backend_main, "_make_detector", make_detector)
    monkeypatch.setattr(backend_main, "_make_va_classifier", fail_classifier)
    with pytest.raises(RuntimeError, match="classifier construction failed"):
        backend_main._make_pipeline()
    assert references[0]() is None


def test_upstream_pipeline_shim_contract():
    onnxtools = pytest.importorskip("onnxtools")
    pipeline_module = pytest.importorskip("onnxtools.pipeline")

    detector_params = inspect.signature(onnxtools.create_detector).parameters.values()
    assert any(param.kind is inspect.Parameter.VAR_KEYWORD for param in detector_params)
    assert "providers" in inspect.signature(onnxtools.VehicleAttributeORT).parameters
    assert hasattr(pipeline_module.VehicleAttributePipeline, "_resolve_class_names")
    pipeline_module.VehicleAttributePipeline.__new__(
        pipeline_module.VehicleAttributePipeline
    )
