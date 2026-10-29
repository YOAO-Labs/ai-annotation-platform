"""RapidOCR provider preference, fallback transaction, and ownership-chain tests."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import catalog
import predictor as predictor_module


class _FakeSession:
    def __init__(self, providers: list[str]) -> None:
        self.providers = providers

    def get_providers(self) -> list[str]:
        return list(self.providers)


def _component(providers: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        session=SimpleNamespace(session=_FakeSession(providers)),
    )


def _engine(
    det: list[str],
    cls: list[str] | None = None,
    rec: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        text_det=_component(det),
        text_cls=_component(det if cls is None else cls),
        text_rec=_component(det if rec is None else rec),
    )


def _resolved():
    return catalog.resolve(
        catalog.E2E_MODEL_ID,
        {"version": "v5", "size": "mobile", "lang": "universal"},
    )


def _install_ort(monkeypatch, *, available: list[str], device: str = "GPU"):
    module = types.ModuleType("onnxruntime")
    module.get_available_providers = lambda: list(available)
    module.get_device = lambda: device

    def forbidden_session(*_args, **_kwargs):
        raise AssertionError("soft startup check must not construct InferenceSession")

    module.InferenceSession = forbidden_session
    monkeypatch.setitem(sys.modules, "onnxruntime", module)
    return module


def test_soft_cuda_check_never_constructs_an_ort_session(monkeypatch) -> None:
    _install_ort(
        monkeypatch,
        available=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    assert predictor_module._soft_ort_cuda_use() is True  # noqa: SLF001


def test_soft_cuda_check_rejects_cpu_build_or_cpu_runtime(monkeypatch) -> None:
    _install_ort(monkeypatch, available=["CPUExecutionProvider"])
    assert predictor_module._soft_ort_cuda_use() is False  # noqa: SLF001


def test_soft_cuda_check_treats_provider_query_failure_as_unavailable(
    monkeypatch,
) -> None:
    module = _install_ort(
        monkeypatch,
        available=["CUDAExecutionProvider"],
    )

    def fail_query():
        raise RuntimeError("provider query failed")

    module.get_available_providers = fail_query
    assert predictor_module._soft_ort_cuda_use() is False  # noqa: SLF001

    _install_ort(
        monkeypatch,
        available=["CUDAExecutionProvider", "CPUExecutionProvider"],
        device="CPU",
    )
    assert predictor_module._soft_ort_cuda_use() is False  # noqa: SLF001


def test_construct_passes_all_three_models_versions_and_cuda_flag(monkeypatch) -> None:
    captured: dict = {}

    class FakeRapidOCR:
        def __init__(self, *, params):
            captured.update(params)

    monkeypatch.setattr(predictor_module, "RapidOCR", FakeRapidOCR)
    resolved = _resolved()

    predictor_module.RapidOCREngineFactory._construct(resolved, use_cuda=True)

    assert captured["Global.use_det"] is True
    assert captured["Global.use_cls"] is True
    assert captured["Global.use_rec"] is True
    assert captured["Det.model_path"] == resolved.det_path
    assert captured["Cls.model_path"] == resolved.cls_path
    assert captured["Rec.model_path"] == resolved.rec_path
    assert captured["Det.ocr_version"].value == resolved.det_meta[0]
    assert captured["Rec.ocr_version"].value == resolved.rec_meta[0]
    assert captured["EngineConfig.onnxruntime.use_cuda"] is True


def test_provider_inspection_reads_three_complete_private_chains() -> None:
    engine = _engine(
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        ["CPUExecutionProvider"],
        ["TensorrtExecutionProvider", "CUDAExecutionProvider"],
    )

    assert predictor_module.inspect_engine_providers(engine) == {
        "det": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "cls": ["CPUExecutionProvider"],
        "rec": ["TensorrtExecutionProvider", "CUDAExecutionProvider"],
    }

    engine.text_rec = SimpleNamespace()
    assert predictor_module.inspect_engine_providers(engine)["rec"] is None


def test_cuda_build_keeps_preference_when_all_business_sessions_use_cuda(
    monkeypatch,
) -> None:
    monkeypatch.setattr(predictor_module, "_soft_ort_cuda_use", lambda: True)
    factory = predictor_module.RapidOCREngineFactory()
    monkeypatch.setattr(
        factory,
        "_construct",
        lambda _resolved, *, use_cuda: _engine(
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
        ),
    )

    artifact = factory.build(_resolved())

    assert artifact.cleanup_uncertain is False
    assert factory.use_cuda is True


def test_silent_cpu_business_sessions_latch_future_builders_to_cpu(
    monkeypatch,
) -> None:
    monkeypatch.setattr(predictor_module, "_soft_ort_cuda_use", lambda: True)
    factory = predictor_module.RapidOCREngineFactory()
    calls: list[bool] = []

    def construct(_resolved, *, use_cuda: bool):
        calls.append(use_cuda)
        return _engine(["CPUExecutionProvider"])

    monkeypatch.setattr(factory, "_construct", construct)
    factory.build(_resolved())
    factory.build(_resolved())

    assert calls == [True, False]
    assert factory.use_cuda is False


def test_cuda_exception_commits_cpu_latch_only_after_cpu_replacement_succeeds(
    monkeypatch,
) -> None:
    monkeypatch.setattr(predictor_module, "_soft_ort_cuda_use", lambda: True)
    factory = predictor_module.RapidOCREngineFactory()
    calls: list[bool] = []

    def construct(_resolved, *, use_cuda: bool):
        calls.append(use_cuda)
        if use_cuda:
            raise RuntimeError("CUDA driver unavailable")
        return _engine(["CPUExecutionProvider"])

    monkeypatch.setattr(factory, "_construct", construct)
    artifact = factory.build(_resolved())

    assert artifact.engine.text_det.session.session.get_providers() == [
        "CPUExecutionProvider"
    ]
    assert artifact.cleanup_uncertain is True
    assert calls == [True, False]
    assert factory.use_cuda is False


def test_strict_cuda_never_retries_with_cpu_after_device_error(monkeypatch) -> None:
    monkeypatch.setattr(predictor_module, "_soft_ort_cuda_use", lambda: True)
    monkeypatch.setenv("RAPIDOCR_DEVICE", "gpu")
    factory = predictor_module.RapidOCREngineFactory(strict_cuda=True)
    attempts = []

    def construct(_resolved, *, use_cuda):
        attempts.append(use_cuda)
        raise RuntimeError("CUDA driver unavailable")

    monkeypatch.setattr(factory, "_construct", construct)
    with pytest.raises(predictor_module.RapidOCREngineBuildError):
        factory.build(_resolved())
    assert attempts == [True]


def test_strict_cuda_rejects_unavailable_provider_before_construction(
    monkeypatch,
) -> None:
    monkeypatch.setattr(predictor_module, "_soft_ort_cuda_use", lambda: False)
    monkeypatch.setenv("RAPIDOCR_DEVICE", "gpu")
    factory = predictor_module.RapidOCREngineFactory(strict_cuda=True)
    with pytest.raises(predictor_module.RapidOCREngineBuildError, match="unavailable"):
        factory.build(_resolved())


def test_non_device_failure_never_retries_cpu_or_latches(
    monkeypatch,
) -> None:
    monkeypatch.setattr(predictor_module, "_soft_ort_cuda_use", lambda: True)
    factory = predictor_module.RapidOCREngineFactory()
    calls: list[bool] = []

    def construct(_resolved, *, use_cuda: bool):
        calls.append(use_cuda)
        raise FileNotFoundError("model missing")

    monkeypatch.setattr(factory, "_construct", construct)
    with pytest.raises(
        predictor_module.RapidOCREngineBuildError,
        match="FileNotFoundError: model missing",
    ):
        factory.build(_resolved())

    assert calls == [True]
    assert factory.use_cuda is True


def test_device_error_with_failed_cpu_replacement_does_not_latch(
    monkeypatch,
) -> None:
    monkeypatch.setattr(predictor_module, "_soft_ort_cuda_use", lambda: True)
    factory = predictor_module.RapidOCREngineFactory()

    def construct(_resolved, *, use_cuda: bool):
        if use_cuda:
            raise RuntimeError("CUDA driver unavailable")
        raise FileNotFoundError("cpu model missing")

    monkeypatch.setattr(factory, "_construct", construct)
    with pytest.raises(
        predictor_module.RapidOCREngineBuildError,
        match="CUDA build failed.*CPU replacement failed",
    ):
        factory.build(_resolved())

    assert factory.use_cuda is True


def test_tensor_rt_primary_does_not_latch_future_builders_to_cpu(monkeypatch) -> None:
    monkeypatch.setattr(predictor_module, "_soft_ort_cuda_use", lambda: True)
    factory = predictor_module.RapidOCREngineFactory()
    monkeypatch.setattr(
        factory,
        "_construct",
        lambda _resolved, *, use_cuda: _engine(
            ["TensorrtExecutionProvider", "CUDAExecutionProvider"]
        ),
    )

    factory.build(_resolved())

    assert factory.use_cuda is True
