"""SAM3 GPU-only 设备策略回归测试。"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

import predictor as image_module
import pvs_video_predictor as pvs_module
import video_predictor as multiplex_module
from aap_backend_runtime import DeviceUnavailableError


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.mark.parametrize(
    ("module", "predictor_type", "kwargs", "load_method"),
    [
        (image_module, image_module.SAM3Predictor, {}, "_load_model"),
        (
            multiplex_module,
            multiplex_module.SAM3MultiplexVideoTracker,
            {},
            "_load_predictor",
        ),
        (pvs_module, pvs_module.SAM3PVSVideoTracker, {}, "_load_predictor"),
    ],
)
def test_constructor_rejects_cpu_before_vendor_build(
    monkeypatch, module, predictor_type, kwargs, load_method
) -> None:
    def reject_gpu(_configured: str) -> str:
        raise DeviceUnavailableError("CUDA unavailable")

    monkeypatch.setattr(module, "require_gpu_device", reject_gpu)
    monkeypatch.setattr(
        predictor_type,
        load_method,
        lambda *_args, **_kwargs: pytest.fail("GPU guard must run before vendor build"),
    )

    with pytest.raises(DeviceUnavailableError, match="CUDA unavailable"):
        predictor_type(**kwargs)


def test_image_builder_device_failure_is_not_retried_on_cpu(
    monkeypatch, tmp_path
) -> None:
    calls: list[dict] = []
    cleanup_calls: list[None] = []

    def build(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("CUDA error: unknown error")

    fake_sam3 = types.ModuleType("sam3")
    fake_sam3.build_sam3_image_model = build
    monkeypatch.setitem(sys.modules, "sam3", fake_sam3)
    monkeypatch.setattr(
        image_module, "free_gpu_memory", lambda: cleanup_calls.append(None)
    )

    predictor = object.__new__(image_module.SAM3Predictor)
    predictor.checkpoint_dir = str(tmp_path)
    predictor.device = "cuda"

    with pytest.raises(DeviceUnavailableError, match="CPU fallback is not supported"):
        predictor._load_model()

    assert [call["device"] for call in calls] == ["cuda"]
    assert cleanup_calls == [None]


def test_multiplex_builder_device_failure_is_called_once(monkeypatch) -> None:
    calls: list[dict] = []
    cleanup_calls: list[None] = []

    def build(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("CUDA driver error")

    fake_builder = types.ModuleType("sam3.model_builder")
    fake_builder.build_sam3_multiplex_video_predictor = build
    monkeypatch.setitem(sys.modules, "sam3.model_builder", fake_builder)
    monkeypatch.setattr(multiplex_module.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        multiplex_module, "free_gpu_memory", lambda: cleanup_calls.append(None)
    )

    tracker = object.__new__(multiplex_module.SAM3MultiplexVideoTracker)
    tracker.device = "cuda"

    with pytest.raises(DeviceUnavailableError, match="CPU fallback is not supported"):
        tracker._load_predictor(use_fa3=False)

    assert len(calls) == 1
    assert cleanup_calls == [None]


def test_pvs_builder_device_failure_cleans_up_without_cpu_retry(monkeypatch) -> None:
    calls: list[dict] = []
    cleanup_calls: list[None] = []

    def build(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("No CUDA GPUs are available")

    fake_builder = types.ModuleType("sam3.model_builder")
    fake_builder.build_sam3_video_model = build
    monkeypatch.setitem(sys.modules, "sam3.model_builder", fake_builder)
    monkeypatch.setattr(pvs_module.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        pvs_module, "free_gpu_memory", lambda: cleanup_calls.append(None)
    )

    tracker = object.__new__(pvs_module.SAM3PVSVideoTracker)
    tracker.device = "cuda"

    with pytest.raises(DeviceUnavailableError, match="CPU fallback is not supported"):
        tracker._load_predictor()

    assert [call["device"] for call in calls] == ["cuda"]
    assert cleanup_calls == [None]


def test_pvs_builder_receives_cuda_device_without_retry(monkeypatch) -> None:
    calls: list[dict] = []
    vendor_tracker = SimpleNamespace()
    model = SimpleNamespace(
        tracker=vendor_tracker,
        detector=SimpleNamespace(backbone=object()),
    )

    def build(**kwargs):
        calls.append(kwargs)
        return model

    fake_builder = types.ModuleType("sam3.model_builder")
    fake_builder.build_sam3_video_model = build
    monkeypatch.setitem(sys.modules, "sam3.model_builder", fake_builder)
    monkeypatch.setattr(pvs_module.os.path, "isfile", lambda _path: True)

    tracker = object.__new__(pvs_module.SAM3PVSVideoTracker)
    tracker.device = "cuda"

    assert tracker._load_predictor() is vendor_tracker
    assert [call["device"] for call in calls] == ["cuda"]
    assert vendor_tracker.backbone is model.detector.backbone


def test_health_reports_unknown_until_a_cuda_pool_is_loaded(monkeypatch) -> None:
    import main as main_module  # noqa: PLC0415
    from gpu_lifecycle import Sam3GpuLifecycle  # noqa: PLC0415
    from aap_backend_runtime import BuildArtifact, ManagedLruPool  # noqa: PLC0415
    from pool_domain import Sam3Pools  # noqa: PLC0415

    monkeypatch.setattr(main_module.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(
        main_module,
        "sample_perfhud",
        lambda: {
            "container_cpu_percent": None,
            "container_memory_percent": None,
        },
    )

    async def scenario() -> None:
        def pool(device=None):
            return ManagedLruPool(
                1,
                lambda _key: BuildArtifact(
                    SimpleNamespace(device=device, active_sessions=0)
                ),
                str,
                lambda: None,
            )

        image = pool("cuda:0")
        multiplex = pool("cuda:0")
        pvs = pool("cuda:0")
        domain = Sam3Pools(image, multiplex, pvs)
        lifecycle = Sam3GpuLifecycle(domain, verify_keyring={})
        monkeypatch.setattr(main_module, "_gpu_lifecycle", lifecycle)

        compute = (await main_module.health())["compute"]
        assert compute == {
            "configured_device": "cuda",
            "effective_device": None,
            "cpu_fallback_supported": False,
        }

        await multiplex.warmup("sam3_video")
        assert (await main_module.health())["compute"]["effective_device"] == "cuda:0"
        await lifecycle.shutdown()

    _run(scenario())


@pytest.mark.parametrize(
    "model_key",
    [
        "sam3",
        "sam3_video",
        "sam3_video_interactive",
    ],
)
def test_gpu_unavailable_is_translated_to_protocol_503(
    model_key,
) -> None:
    import main as main_module  # noqa: PLC0415
    from aap_backend_runtime import ManagedLruPool  # noqa: PLC0415

    def fail_build(_key):
        raise DeviceUnavailableError("CUDA unavailable")

    class _Operation:
        def track_future(self, _future):
            pass

    async def scenario() -> None:
        pool = ManagedLruPool(1, fail_build, str, lambda: None)
        with pytest.raises(Exception) as exc_info:
            await main_module._warm_pool(pool, model_key, _Operation())

        error = exc_info.value
        assert error.status_code == 503
        assert error.headers == {"Retry-After": "30"}
        assert error.detail == {
            "error_code": "model_unavailable",
            "key": model_key,
            "reason": "CUDA unavailable",
        }
        await pool.shutdown()

    _run(scenario())
