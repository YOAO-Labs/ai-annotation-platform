"""Native architecture, checkpoint and semantic-target contracts for every scale."""

from types import SimpleNamespace
import gc
import json

import numpy as np
import pytest
import torch
from ultralytics.cfg import get_cfg
from ultralytics.nn.tasks import DetectionModel, SegmentationModel

from training_contract.models import models_for_framework
from training_contract.observability import InputMismatchError
from training_executor.instance_rle import InstanceRLEDataset, semantic_target
from training_executor.native_identity import validate_architecture
from training_executor.train import UltralyticsCheckpoints, ultralytics_epoch_metrics


@pytest.fixture(autouse=True)
def bounded_native_cpu():
    previous = torch.get_num_threads()
    torch.set_num_threads(2)
    yield
    torch.set_num_threads(previous)
    gc.collect()


@pytest.mark.parametrize(
    "model", models_for_framework("ultralytics"), ids=lambda item: item.id
)
def test_every_scale_has_exact_native_head_criterion_and_checkpoint_identity(
    tmp_path, model
):
    native_type = SegmentationModel if model.task == "segment" else DetectionModel
    native = native_type(model.yaml_filename, nc=2, verbose=False)
    native.names = {0: "car", 1: "person"}
    native.args = get_cfg()
    assert validate_architecture(native, model.id, classes=2) == model
    criterion = native.init_criterion()
    loss = criterion.one2one if model.end2end else criterion
    assert ("l1_loss" in loss.loss_names) is model.end2end
    assert ("dfl_loss" in loss.loss_names) is not model.end2end
    assert ("sem_loss" in loss.loss_names) is (model.task == "segment")
    config = {
        "framework": "ultralytics",
        "framework_version": "8.4.183",
        "adapter_revision": 2,
        "base_model": {"id": model.id},
        "classes": ["car", "person"],
        "task": model.task,
    }
    checkpoint = {
        "version": "8.4.183",
        "train_args": {"task": model.task},
        "ema": native,
        "epoch": 0,
        "best_fitness": 0.0,
    }
    callbacks = UltralyticsCheckpoints({"frozen_config": config}, tmp_path)
    callbacks.validate_identity(checkpoint)
    with pytest.raises(InputMismatchError, match="version"):
        callbacks.validate_identity({**checkpoint, "version": "8.4.177"})
    native.yaml["scale"] = "other"
    with pytest.raises(InputMismatchError, match="architecture"):
        callbacks.validate_identity(checkpoint)


def test_semantic_target_preserves_smallest_covering_instance_and_stable_ties():
    masks = np.zeros((4, 7, 9), dtype=np.uint8)
    masks[0, 1:6, 1:8] = 1
    masks[1, 2:5, 2:7] = 1
    masks[2] = masks[1]
    masks[3, 0, 0] = 1
    classes = [4, 2, 7, 0]
    result = semantic_target(masks, classes).numpy()
    assert result[1, 1] == 4 and result[3, 3] == 2
    assert result[0, 0] == result[0, 8] == 0
    areas = masks.sum(axis=(1, 2))
    key = np.where(masks.astype(bool), areas[:, None, None], areas.max() + 1)
    expected = np.asarray(classes)[key.argmin(axis=0)] * masks.any(axis=0)
    np.testing.assert_array_equal(result, expected)
    assert not semantic_target(np.zeros((0, 7, 9), dtype=np.uint8), []).any()


def test_semantic_budget_rejects_before_dense_targets_or_images_are_read(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"classes": list(map(str, range(100))), "samples": []})
    )
    args = get_cfg(overrides={"imgsz": 640, "nms": False})
    with pytest.raises(InputMismatchError, match="64M class pixels"):
        InstanceRLEDataset(tmp_path, args, "train", 2)
    args.nms = None
    assert len(InstanceRLEDataset(tmp_path, args, "train", 2)) == 0


def test_real_l1_and_semantic_metrics_are_not_relabelled_as_dfl():
    trainer = SimpleNamespace(
        metrics={"val/l1_loss": 0.3, "val/sem_loss": 0.4},
        tloss=object(),
        label_loss_items=lambda _, prefix: {
            f"{prefix}/l1_loss": 0.1,
            f"{prefix}/sem_loss": 0.2,
        },
    )
    metrics = ultralytics_epoch_metrics(trainer)
    assert metrics == {
        "val/l1_loss": 0.3,
        "val/sem_loss": 0.4,
        "train/l1_loss": 0.1,
        "train/sem_loss": 0.2,
    }
    assert not any("dfl" in key for key in metrics)


def test_exact_tensor_shapes_and_module_graph_are_checked_without_advancing_rng():
    native = SegmentationModel("yolo26n-seg.yaml", nc=3, verbose=False)
    rng = torch.get_rng_state().clone()
    validate_architecture(native, "yolo26n-seg", classes=3)
    assert torch.equal(torch.get_rng_state(), rng)
    native.model[0].conv.weight = torch.nn.Parameter(native.model[0].conv.weight[:2])
    with pytest.raises(ValueError, match="Unqualified"):
        validate_architecture(native, "yolo26n-seg", classes=3)


@pytest.mark.parametrize("family", ["yolov8", "yolo11", "yolo26"])
@pytest.mark.parametrize("segmentation", [False, True])
def test_coco_head_transfer_preserves_an_exact_supported_native_layout(
    family, segmentation
):
    base = family + "n" + ("-seg" if segmentation else "")
    native_type = SegmentationModel if segmentation else DetectionModel
    source = native_type(base + ".yaml", nc=80, verbose=False)
    target = native_type(base + ".yaml", nc=2, verbose=False)
    target.load(source, verbose=False)
    rng = torch.get_rng_state().clone()
    validate_architecture(target, base, classes=2)
    assert torch.equal(torch.get_rng_state(), rng)
    target.model[0].conv.weight = torch.nn.Parameter(target.model[0].conv.weight[:2])
    with pytest.raises(ValueError, match="Unqualified"):
        validate_architecture(target, base, classes=2)
