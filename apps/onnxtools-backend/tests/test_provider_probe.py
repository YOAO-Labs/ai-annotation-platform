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


@pytest.mark.parametrize("device", ["cpu", "cuda:0"])
@pytest.mark.asyncio
async def test_plugin_profile_reports_factory_topology_and_enforces_device(
    monkeypatch, tmp_path, device
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from aap_protocol_v2.lifecycle import encode_ed25519_public_key

    registry = "bf7fcc5e-3be5-4ab0-a1d8-d009b4b2baaa"
    gpu_uuid = "GPU-78f02e07-08db-42cc-8758-68942a54443f"
    monkeypatch.setenv("AAP_PLUGIN_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("AAP_PLUGIN_REGISTRY_ID", registry)
    monkeypatch.setenv("AAP_ROUTE_REGISTRY_ID", registry)
    monkeypatch.setenv("AAP_PLUGIN_DEVICE", device)
    monkeypatch.setenv("AAP_PLUGIN_ID", "test")
    monkeypatch.setenv("AAP_PLUGIN_VERSION", "test")
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "none")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    monkeypatch.delenv("AAP_PLUGIN_GPU_RESOURCE_ID", raising=False)
    monkeypatch.delenv("AAP_PLUGIN_GPU_UUID", raising=False)
    monkeypatch.delenv("AAP_PLUGIN_QUALIFICATION_FILE", raising=False)
    monkeypatch.setattr(backend_main, "MANAGED_LIFECYCLE_VERIFIED", True)

    def unexpected_probe(*_args, **_kwargs):
        pytest.fail("plugin startup must not construct an untracked ORT session")

    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            __version__="test",
            get_available_providers=lambda: [
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            ],
            InferenceSession=unexpected_probe,
        ),
    )
    monkeypatch.setattr(
        backend_main,
        "_make_detector",
        lambda: SimpleNamespace(_onnx_session=_FakeSession(["CPUExecutionProvider"])),
    )
    for name in (
        "_handle_pool",
        "_predictor",
        "_gpu_lifecycle",
        "_idle_task",
        "_provider_preference",
    ):
        monkeypatch.setattr(backend_main, name, getattr(backend_main, name))
    for name in (
        "plugin_mode",
        "strict_cuda",
        "gpu_facts",
        "runtime_versions",
        "lifecycle_verified",
    ):
        monkeypatch.setattr(backend_main.app.state, name, None, raising=False)

    if device == "cuda:0":
        key = Ed25519PrivateKey.generate()
        keyring = tmp_path / "keyring.json"
        keyring.write_text(
            '{"test": "' + encode_ed25519_public_key(key.public_key()) + '"}'
        )
        monkeypatch.setenv("AAP_PLUGIN_GPU_KEYRING_FILE", str(keyring))
        monkeypatch.setenv("AAP_PLUGIN_GPU_RESOURCE_ID", "test-resource")
        monkeypatch.setenv("AAP_PLUGIN_GPU_UUID", gpu_uuid)
        monkeypatch.setenv("CUDA_VERSION", "12.8.1")
        monkeypatch.setattr(
            backend_main, "observed_cuda_runtime_version", lambda: "12.8.0"
        )
        monkeypatch.setattr(
            backend_main,
            "observed_single_nvidia_gpu",
            lambda _uuid: {
                "device_uuid": gpu_uuid,
                "driver_version": "test-driver",
                "total_memory_mb": 1000,
            },
        )

    async with backend_main.lifespan(backend_main.app):
        health = await backend_main.health()
        setup = backend_main.setup()
        counts = {"pipeline": 2, "detector": 1, "va": 1}
        assert "managed_lifecycle" not in setup
        assert health["residency"]["evictable"] is False
        assert health["compute"]["configured_device"] == device
        assert health["compute"]["cpu_fallback_supported"] is (device == "cpu")
        assert (
            health["ort_session_counts"]
            == setup["runtime_info"]["ort_session_counts"]
            == counts
        )
        if device == "cuda:0":
            with pytest.raises(backend_main.HandleDeviceError):
                async with backend_main._handle_pool.borrow("detector"):
                    pytest.fail("CPU session must not be published in a CUDA plugin")
            assert (await backend_main._handle_pool.snapshot())["current_size"] == 0
        else:
            async with backend_main._handle_pool.borrow("detector"):
                pass
            assert (await backend_main.health())["compute"]["effective_device"] == "cpu"


@pytest.mark.asyncio
async def test_cuda_plugin_fails_closed_when_ort_has_no_cuda_provider(monkeypatch):
    monkeypatch.setenv("AAP_PLUGIN_TOKEN_FILE", "/test/token")
    monkeypatch.setenv("AAP_PLUGIN_REGISTRY_ID", "registry")
    monkeypatch.setenv("AAP_ROUTE_REGISTRY_ID", "registry")
    monkeypatch.setenv("AAP_PLUGIN_DEVICE", "cuda:0")
    monkeypatch.setenv("AAP_PLUGIN_GPU_RESOURCE_ID", "resource")
    monkeypatch.setenv("AAP_PLUGIN_GPU_UUID", "GPU-test")
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "none")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            __version__="test", get_available_providers=lambda: ["CPUExecutionProvider"]
        ),
    )
    with pytest.raises(RuntimeError, match="requires the ONNX Runtime CUDA provider"):
        async with backend_main.lifespan(backend_main.app):
            pytest.fail("a CPU-only ORT must not start a CUDA plugin")
