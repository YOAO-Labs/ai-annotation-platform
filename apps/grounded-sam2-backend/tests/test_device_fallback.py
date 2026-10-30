"""Grounded-SAM2 构建期 GPU→CPU fallback 的事务边界。"""

from __future__ import annotations

import asyncio

import pytest

import predictor as predictor_mod
import video_predictor as video_mod


def test_image_pair_rebuilds_together_before_latch(monkeypatch) -> None:
    events: list[str] = []

    monkeypatch.setattr(predictor_mod, "effective_device", lambda _configured: "cuda")
    monkeypatch.setattr(
        predictor_mod, "free_gpu_memory", lambda: events.append("cleanup")
    )
    monkeypatch.setattr(
        predictor_mod,
        "latch_cpu",
        lambda _reason: events.append("latch"),
    )

    def _sam(_self, device: str):
        events.append(f"sam:{device}")
        return f"sam-{device}"

    def _dino(_self, device: str):
        events.append(f"dino:{device}")
        if device == "cuda":
            raise RuntimeError("CUDA error: unknown error")
        return f"dino-{device}"

    monkeypatch.setattr(predictor_mod.GroundedSAM2Predictor, "_build_sam", _sam)
    monkeypatch.setattr(predictor_mod.GroundedSAM2Predictor, "_build_dino", _dino)

    predictor = predictor_mod.GroundedSAM2Predictor()

    assert predictor.device == "cpu"
    assert predictor._sam_predictor == "sam-cpu"
    assert predictor._dino_model == "dino-cpu"
    assert predictor.cleanup_uncertain is True
    assert events == [
        "sam:cuda",
        "dino:cuda",
        "cleanup",
        "sam:cpu",
        "dino:cpu",
        "latch",
    ]


def test_cpu_replacement_failure_does_not_commit_latch(monkeypatch) -> None:
    latched: list[str] = []
    monkeypatch.setattr(predictor_mod, "effective_device", lambda _configured: "cuda")
    monkeypatch.setattr(predictor_mod, "free_gpu_memory", lambda: None)
    monkeypatch.setattr(predictor_mod, "latch_cpu", latched.append)
    monkeypatch.setattr(
        predictor_mod.GroundedSAM2Predictor,
        "_build_sam",
        lambda _self, device: f"sam-{device}",
    )

    def _dino(_self, device: str):
        if device == "cuda":
            raise RuntimeError("CUDA error: unknown error")
        raise RuntimeError("CPU build failed")

    monkeypatch.setattr(predictor_mod.GroundedSAM2Predictor, "_build_dino", _dino)

    with pytest.raises(RuntimeError, match="CPU build failed") as exc_info:
        predictor_mod.GroundedSAM2Predictor()

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "CUDA error" in str(exc_info.value.__cause__)
    assert latched == []


def test_non_device_error_is_not_retried_or_latched(monkeypatch) -> None:
    devices: list[str] = []
    latched: list[str] = []
    monkeypatch.setattr(predictor_mod, "effective_device", lambda _configured: "cuda")
    monkeypatch.setattr(predictor_mod, "latch_cpu", latched.append)

    def _sam(_self, device: str):
        devices.append(device)
        raise FileNotFoundError("checkpoint missing")

    monkeypatch.setattr(predictor_mod.GroundedSAM2Predictor, "_build_sam", _sam)
    monkeypatch.setattr(
        predictor_mod.GroundedSAM2Predictor,
        "_build_dino",
        lambda _self, device: pytest.fail(f"unexpected dino build on {device}"),
    )

    with pytest.raises(FileNotFoundError, match="checkpoint missing"):
        predictor_mod.GroundedSAM2Predictor()

    assert devices == ["cuda"]
    assert latched == []


def test_initial_cpu_selection_builds_each_component_once(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(predictor_mod, "effective_device", lambda _configured: "cpu")
    monkeypatch.setattr(
        predictor_mod.GroundedSAM2Predictor,
        "_build_sam",
        lambda _self, device: events.append(f"sam:{device}") or "sam",
    )
    monkeypatch.setattr(
        predictor_mod.GroundedSAM2Predictor,
        "_build_dino",
        lambda _self, device: events.append(f"dino:{device}") or "dino",
    )

    predictor = predictor_mod.GroundedSAM2Predictor()

    assert events == ["sam:cpu", "dino:cpu"]
    assert predictor.cleanup_uncertain is False


def test_video_replacement_commits_latch_only_after_cpu_build(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(video_mod, "effective_device", lambda _configured: "cuda")
    monkeypatch.setattr(video_mod, "free_gpu_memory", lambda: events.append("cleanup"))
    monkeypatch.setattr(video_mod, "latch_cpu", lambda _reason: events.append("latch"))

    def _build(_self, device: str):
        events.append(f"build:{device}")
        if device == "cuda":
            raise RuntimeError("CUDA error: unknown error")
        return "cpu-predictor"

    monkeypatch.setattr(video_mod.SAM2VideoTracker, "_build_for_device", _build)

    tracker = video_mod.SAM2VideoTracker()

    assert tracker.device == "cpu"
    assert tracker._predictor == "cpu-predictor"
    assert tracker.cleanup_uncertain is True
    assert events == ["build:cuda", "cleanup", "build:cpu", "latch"]


def test_health_survives_cuda_runtime_failure(monkeypatch) -> None:
    import main

    def _broken_cuda_check():
        raise RuntimeError("CUDA error: unknown error")

    monkeypatch.setattr(main.torch.cuda, "is_available", _broken_cuda_check)
    monkeypatch.setattr(
        main,
        "sample_perfhud",
        lambda: {
            "gpu_utilization_percent": None,
            "gpu_temperature_celsius": None,
            "gpu_power_watts": None,
            "container_cpu_percent": 0.0,
            "container_memory_percent": 0.0,
        },
    )

    payload = asyncio.run(main.health())

    assert payload["ok"] is True
    assert payload["gpu"] is False
    assert payload["gpu_info"] is None


def test_managed_cleanup_propagates_cuda_runtime_failure(monkeypatch) -> None:
    import main

    monkeypatch.setattr(main.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(main.torch.cuda, "is_initialized", lambda: True)
    monkeypatch.setattr(main.torch.cuda, "synchronize", lambda: None)
    monkeypatch.setattr(
        main.torch._C,
        "_cuda_clearCublasWorkspaces",
        lambda: None,
        raising=False,
    )

    def _broken_empty_cache() -> None:
        raise RuntimeError("CUDA allocator unavailable")

    monkeypatch.setattr(main.torch.cuda, "empty_cache", _broken_empty_cache)

    with pytest.raises(RuntimeError, match="allocator unavailable"):
        main._strict_free_gpu_memory()


def test_managed_cleanup_releases_cublas_workspace(monkeypatch) -> None:
    import main

    events: list[str] = []
    monkeypatch.setattr(main.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(main.torch.cuda, "is_initialized", lambda: True)
    monkeypatch.setattr(
        main.torch.cuda,
        "synchronize",
        lambda: events.append("synchronize"),
    )
    monkeypatch.setattr(
        main.torch._C,
        "_cuda_clearCublasWorkspaces",
        lambda: events.append("clear-cublas"),
        raising=False,
    )
    monkeypatch.setattr(
        main.torch.cuda,
        "empty_cache",
        lambda: events.append("empty-cache"),
    )
    monkeypatch.setattr(
        main.torch.cuda,
        "ipc_collect",
        lambda: events.append("ipc-collect"),
    )

    main._strict_free_gpu_memory()

    assert events == [
        "synchronize",
        "clear-cublas",
        "empty-cache",
        "ipc-collect",
    ]


def test_managed_cleanup_does_not_initialize_unused_cuda(monkeypatch) -> None:
    import main

    events: list[str] = []
    monkeypatch.setattr(main.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(main.torch.cuda, "is_initialized", lambda: False)
    monkeypatch.setattr(
        main.torch.cuda,
        "synchronize",
        lambda: events.append("synchronize"),
    )

    main._strict_free_gpu_memory()

    assert events == []


def test_health_keeps_process_latch_separate_from_pool_devices(monkeypatch) -> None:
    import main

    monkeypatch.setattr(main, "effective_device_value", lambda: "cpu")
    monkeypatch.setattr(
        main,
        "sample_perfhud",
        lambda: {
            "gpu_utilization_percent": None,
            "gpu_temperature_celsius": None,
            "gpu_power_watts": None,
            "container_cpu_percent": 0.0,
            "container_memory_percent": 0.0,
        },
    )

    payload = asyncio.run(main.health())

    assert payload["compute"]["effective_device"] == "cpu"
    assert payload["compute"]["pool_devices"] == {"image": None, "video": None}
