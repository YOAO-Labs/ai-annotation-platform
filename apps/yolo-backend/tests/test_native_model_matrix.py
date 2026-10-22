"""Explicit CPU checks for native managed multi-family installation and prediction."""

import gc
import os
from types import SimpleNamespace
import uuid

import pytest

if os.environ.get("AAP_NATIVE_SEGMENTATION") != "1":
    pytest.skip("Exact native CPU qualification is explicit", allow_module_level=True)

import torch
from PIL import Image
from ultralytics import YOLO

from managed_models import InstallRequest, ManagedModelStore, digest
from predictor import YoloPredictor
from sample_evaluations import (
    model_options,
    qualification_profile,
    verify_inference_branch,
)
from schemas import Context
from training_contract.model_return import ultralytics_identity
from training_contract.models import models_for_framework


@pytest.fixture(autouse=True)
def bounded_native_cpu():
    torch.set_num_threads(2)
    yield
    gc.collect()


def expected(model, names):
    return {
        "model_version_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "classes": names,
        "classes_digest": digest(names),
        "framework": "ultralytics",
        "framework_version": "8.4.183",
        "task": "segmentation" if model.task == "segment" else "detection",
        **ultralytics_identity(model.id, framework_version="8.4.183"),
    }


@pytest.mark.parametrize(
    "model", models_for_framework("ultralytics"), ids=lambda item: item.id
)
def test_every_family_and_scale_reloads_only_its_exact_native_architecture(
    tmp_path, model
):
    source = YOLO(model.yaml_filename)
    names = [source.names[index] for index in range(len(source.names))]
    path = tmp_path / "controlled.pt"
    source.save(path)
    ref = expected(model, names)
    InstallRequest.model_validate(
        {
            **ref,
            "size_bytes": path.stat().st_size,
            "download_url": "https://storage.invalid/immutable.pt",
        }
    )
    del source
    gc.collect()
    loaded = ManagedModelStore.inspect(path, ref)
    assert type(loaded.model.model[-1]).__name__ == model.head
    assert (
        getattr(loaded.model.model[-1], "one2one_cv2", None) is not None
    ) == model.end2end
    other_scale = "s" if model.scale == "n" else "n"
    other_id = f"{model.family}{other_scale}{'-seg' if model.task == 'segment' else ''}"
    wrong = {**ref, **ultralytics_identity(other_id, framework_version="8.4.183")}
    with pytest.raises(ValueError, match="family.*scale"):
        ManagedModelStore.inspect(path, wrong)
    path.unlink()


@pytest.mark.parametrize(
    "base_model",
    ["yolov8n", "yolo11n", "yolo26n", "yolov8n-seg", "yolo11n-seg", "yolo26n-seg"],
)
def test_controlled_prediction_and_sample_profiles_observe_the_expected_native_branch(
    tmp_path, monkeypatch, base_model
):
    from training_contract.models import model_spec

    spec = model_spec("ultralytics", base_model)
    model = YOLO(spec.yaml_filename)
    ref = expected(spec, [model.names[index] for index in range(len(model.names))])
    ctx = Context(
        task=spec.task,
        model_id=f"trained:{ref['model_version_id']}",
        frozen_model_ref={
            key: ref[key] for key in ("model_version_id", "sha256", "classes_digest")
        },
        params={"conf": 0.99, "max_det": 3},
    )
    ctx._managed_receipt = ref
    monkeypatch.setattr("predictor.fetch_image", lambda _: Image.new("RGB", (160, 96)))
    YoloPredictor._predict_closed_sync(model, "fixture", ctx)
    assert bool(model.predictor.model.end2end) == spec.end2end
    assert model.predictor.args.nms is (False if spec.end2end else None)
    profile = qualification_profile("cpu", uuid.uuid4(), base_model=base_model)
    assert profile["options"] == model_options(base_model)
    if spec.task == "segment":
        assert ctx._segmentation_meta["segmentation"]["dimensions"] == [96, 160]
    if spec.end2end:
        with pytest.raises(ValueError, match="inference branch"):
            verify_inference_branch(
                SimpleNamespace(
                    predictor=SimpleNamespace(model=SimpleNamespace(end2end=False))
                ),
                ref,
            )


@pytest.mark.parametrize("base_model", ["yolo26n", "yolo26n-seg"])
def test_qualified_native_install_rejects_nonfinite_tensors(tmp_path, base_model):
    from training_contract.models import model_spec

    model = YOLO(base_model + ".yaml")
    ref = expected(model_spec("ultralytics", base_model), list(model.names.values()))
    with torch.no_grad():
        model.model.model[0].conv.weight[0, 0, 0, 0] = float("nan")
    path = tmp_path / "nonfinite.pt"
    model.save(path)
    with pytest.raises(ValueError, match="non-finite"):
        ManagedModelStore.inspect(path, ref)
