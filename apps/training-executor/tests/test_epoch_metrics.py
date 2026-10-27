"""Epoch telemetry preserves native training and validation loss identities."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from training_executor.train import (
    UltralyticsTrainingEvaluation,
    ultralytics_epoch_metrics,
    ultralytics_model_path,
)


@pytest.mark.parametrize(
    "losses",
    [
        {"box_loss": 1.2, "cls_loss": 0.0, "dfl_loss": 0.8},
        {
            "box_loss": 1.2,
            "cls_loss": 0.0,
            "dfl_loss": 0.8,
            "seg_loss": 2.3,
            "sem_loss": 0.0,
        },
    ],
)
def test_epoch_metrics_include_native_training_losses_without_replacing_validation(
    losses,
):
    validation = {"val/box_loss": 1.9, "metrics/mAP50-95(M)": 0.3}
    label = Mock(return_value={f"train/{key}": value for key, value in losses.items()})
    trainer = SimpleNamespace(metrics=validation, tloss=losses, label_loss_items=label)

    assert ultralytics_epoch_metrics(trainer) == {
        **validation,
        **{f"train/{key}": value for key, value in losses.items()},
    }
    label.assert_called_once_with(losses, prefix="train")
    assert validation == {"val/box_loss": 1.9, "metrics/mAP50-95(M)": 0.3}


def test_missing_training_losses_are_not_inferred_from_validation():
    trainer = SimpleNamespace(
        metrics={"val/seg_loss": 2.0}, tloss=None, label_loss_items=Mock()
    )

    assert ultralytics_epoch_metrics(trainer) == {"val/seg_loss": 2.0}
    trainer.label_loss_items.assert_not_called()


def test_epoch_metrics_only_emit_finite_native_numbers():
    trainer = SimpleNamespace(
        metrics=None,
        tloss={"box_loss": 0.0, "seg_loss": float("nan")},
        label_loss_items=Mock(
            return_value={"train/box_loss": 0.0, "train/seg_loss": float("nan")}
        ),
    )

    assert ultralytics_epoch_metrics(trainer) == {"train/box_loss": 0.0}


def test_training_evaluation_uses_independent_validator_and_unaugmented_train_split(
    tmp_path,
):
    instances = []

    class Validator:
        def __init__(self, *, args, save_dir):
            self.args, self.save_dir = args, save_dir
            self.get_dataloader = Mock(return_value="frozen-training-loader")
            self.calls = []
            instances.append(self)

        def __call__(self, *, trainer):
            self.calls.append(trainer)
            return {
                "metrics/precision(B)": 0.8,
                "metrics/recall(M)": 0.6,
                "metrics/mAP50-95(M)": 0.5,
                "val/box_loss": 2.0,
                "fitness": 0.5,
            }

    validation_args = SimpleNamespace(
        split="val", augment=False, plots=True, save_json=True, save_txt=True, workers=2
    )
    original = Validator(args=validation_args, save_dir=tmp_path / "validation")
    original.dataloader = SimpleNamespace(dataset=SimpleNamespace(rect=True))
    trainer = SimpleNamespace(
        validator=original,
        data={"train": "frozen/train", "val": "frozen/val"},
        device="device",
        stride=32,
        model=SimpleNamespace(names={0: "car"}),
        batch_size=2,
        metrics={"metrics/mAP50-95(M)": 0.2},
        best_fitness=0.2,
    )
    evaluation = UltralyticsTrainingEvaluation(tmp_path)
    for _ in range(2):
        assert evaluation.evaluate(trainer) == {
            "train/metrics/precision(B)": 0.8,
            "train/metrics/recall(M)": 0.6,
            "train/metrics/mAP50-95(M)": 0.5,
        }

    separate = instances[1]
    assert len(instances) == 2
    separate.get_dataloader.assert_called_once_with("frozen/train", 2)
    assert separate.dataloader == "frozen-training-loader"
    assert separate.args.split == "train"
    assert separate.args.rect
    assert not separate.args.augment
    assert not separate.args.plots
    assert not separate.args.save_json
    assert not separate.args.save_txt
    assert separate.args.workers == 0
    assert len(separate.calls) == 2
    assert original.calls == []
    assert original.args.split == "val" and original.args.plots
    assert trainer.metrics == {"metrics/mAP50-95(M)": 0.2}
    assert trainer.best_fitness == 0.2


@pytest.mark.parametrize("base", ["yolo11n", "yolo11n-seg"])
def test_scratch_starts_from_architecture_and_resume_keeps_checkpoint(base):
    spec = {
        "base_path": "/models/pretrained.pt",
        "frozen_config": {
            "base_model": {"id": base},
            "configuration": {"effective_values": {"pretrained": False}},
        },
    }
    assert ultralytics_model_path(spec) == base + ".yaml"
    assert (
        ultralytics_model_path({**spec, "resume_path": "/state/checkpoint.pt"})
        == "/state/checkpoint.pt"
    )
    with pytest.raises(ValueError, match="project weights"):
        ultralytics_model_path(
            {
                **spec,
                "frozen_config": {
                    **spec["frozen_config"],
                    "initialization": {"kind": "trained_model"},
                },
            }
        )


def test_default_and_project_initialization_keep_existing_weight_paths():
    spec = {"base_path": "/models/base.pt", "frozen_config": {}}
    assert ultralytics_model_path(spec) == "/models/base.pt"
    assert (
        ultralytics_model_path({**spec, "initialization_path": "/state/project.pt"})
        == "/state/project.pt"
    )
