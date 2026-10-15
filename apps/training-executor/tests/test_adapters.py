"""Independent framework contracts, complete epochs and truthful metrics."""

import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import pytest
from training_executor.adapters import adapter_for, validate_dataset
from training_executor.files import digest_file
from training_executor.libreyolo_adapter import (
    CheckpointCallbacks,
    StrictResumeLogging,
    libreyolo_report,
    libreyolo_train,
    native_dataset_yaml,
    validate_checkpoint,
)


def spec(tmp_path):
    config = {
        "framework": "libreyolo",
        "framework_version": "1.6.0",
        "adapter_revision": 1,
        "classes": ["car", "person"],
        "validation_digest": "fixed-validation",
        "parameters": {
            "epochs": 3,
            "batch": 1,
            "imgsz": 128,
            "lr0": 0.01,
            "seed": 42,
            "patience": 3,
        },
    }
    return {
        "framework": "libreyolo",
        "parameters": config["parameters"],
        "frozen_config": config,
        "data_yaml": str(tmp_path / "data.yaml"),
        "base_path": "/models/LibreYOLO9t.pt",
        "resume_path": None,
    }


def native_checkpoint(spec, epoch=0, best_epoch=1):
    import torch
    from libreyolo.utils.serialization import wrap_libreyolo_checkpoint

    return wrap_libreyolo_checkpoint(
        {"weight": torch.ones(1)},
        model_family="yolo9",
        size="t",
        task="detect",
        nc=2,
        names=spec["frozen_config"]["classes"],
        imgsz=128,
        epoch=epoch,
        optimizer={"state": {}, "param_groups": [{}]},
        rng_state={"torch": torch.get_rng_state()},
        best_epoch=best_epoch,
        libreyolo_version="1.6.0",
    )


def epoch_event(output, epoch):
    return SimpleNamespace(
        epoch=epoch,
        save_dir=str(output / "framework"),
        val_metrics={
            "metrics/mAP50": 0.3,
            "metrics/precision": 0.2,
            "metrics/recall": 0.4,
        },
        train_loss=0.5,
    )


def test_adapter_requires_qualified_identity_and_does_not_enable_libre_registration(
    tmp_path,
):
    s = spec(tmp_path)
    assert adapter_for(s["frozen_config"])["model_registration"] is False
    s["frozen_config"]["framework_version"] = "other"
    with pytest.raises(ValueError, match="identity"):
        adapter_for(s["frozen_config"])


def test_metrics_use_native_map_and_ar_semantics_without_inventing_per_class(tmp_path):
    s = spec(tmp_path)
    raw = {
        "metrics/precision": 0.3,
        "metrics/recall": 0.6,
        "metrics/mAP50-95": 0.3,
        "metrics/mAP50": 0.5,
        "metrics/mAP75": 0.2,
        "metrics/AR100": 0.6,
    }
    report = libreyolo_report(raw, s, {"sha256": "fixed-weights"}, "pycocotools")
    assert report["summary"] == {
        k: raw[k]
        for k in ("metrics/mAP50-95", "metrics/mAP50", "metrics/mAP75", "metrics/AR100")
    }
    assert "metrics/precision" not in report["summary"] and report["native_raw"] == raw
    assert report["per_class"] == [] and report["per_class_available"] is False
    assert report["evaluation"]["weights_sha256"] == "fixed-weights"
    assert report["evaluation"]["evaluator"] == "pycocotools"
    with pytest.raises(ValueError, match="incomplete"):
        libreyolo_report({"metrics/precision": 0.3}, s, {"sha256": "x"}, None)


def test_full_epoch_resume_retains_exact_historical_best_without_later_improvement(
    tmp_path,
):
    import torch

    s = spec(tmp_path)
    first = tmp_path / "first"
    (first / "framework/weights").mkdir(parents=True)
    initial = native_checkpoint(s)
    torch.save(initial, first / "framework/weights/last.pt")
    torch.save(initial, first / "framework/weights/best.pt")
    best = (first / "framework/weights/best.pt").read_bytes()
    callback = CheckpointCallbacks(s, first)
    callback.on_train_start(
        SimpleNamespace(save_dir=str(first / "framework"), start_epoch=1, task="detect")
    )
    callback.on_train_epoch_end(epoch_event(first, 1))
    committed = torch.load(first / callback.checkpoint["name"], weights_only=True)
    validate_checkpoint(committed, s, resume=True)
    assert committed["aap_resume"]["best_weights"] == best
    second = tmp_path / "second"
    second.mkdir()
    resumed = CheckpointCallbacks(s, second, committed)
    resumed.commit_resume(first / callback.checkpoint["name"])
    # Cancellation before the next epoch still has an identical resumable artifact.
    pointer = json.loads((second / "checkpoint.json").read_text())
    assert pointer["epoch"] == 1 and pointer["role"] == "checkpoint"
    assert (second / pointer["name"]).read_bytes() == (
        first / callback.checkpoint["name"]
    ).read_bytes()
    validate_checkpoint(
        torch.load(second / pointer["name"], weights_only=True), s, resume=True
    )
    resumed.on_train_start(
        SimpleNamespace(
            save_dir=str(second / "framework"), start_epoch=2, task="detect"
        )
    )
    torch.save(
        native_checkpoint(s, epoch=1, best_epoch=1),
        second / "framework/weights/last.pt",
    )
    resumed.on_train_epoch_end(epoch_event(second, 2))
    assert (
        digest_file(second / "framework/weights/best.pt")
        == hashlib.sha256(best).hexdigest()
    )
    saved = torch.load(second / resumed.checkpoint["name"], weights_only=True)
    assert saved["aap_resume"]["best_weights"] == best and saved["epoch"] == 1
    events = [
        json.loads(line) for line in (second / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["epoch"] == 2 and "metrics/precision" not in events[-1]["metrics"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("task", "segment"),
        ("names", {0: "person", 1: "car"}),
        ("libreyolo_version", "other"),
        ("optimizer", None),
    ],
)
def test_incompatible_or_incomplete_checkpoint_is_rejected(tmp_path, field, value):
    s = spec(tmp_path)
    blob = native_checkpoint(s)
    blob[field] = value
    with pytest.raises(ValueError):
        validate_checkpoint(blob, s, epoch=1)


def test_native_restore_warnings_fail_instead_of_silently_restarting_optimizer():
    record = logging.LogRecord(
        "libreyolo.training.trainer",
        logging.WARNING,
        "",
        0,
        "Could not load optimizer state: incompatible",
        (),
        None,
    )
    with pytest.raises(RuntimeError, match="restoration failed"):
        StrictResumeLogging().emit(record)


def dataset(tmp_path):
    s = spec(tmp_path)
    (tmp_path / "data.yaml").write_text(
        'train: images/train\nval: images/val\nnames: {"0": "car", "1": "person"}\n'
    )
    (tmp_path / "classes.json").write_text(json.dumps(["car", "person"]))
    samples = []
    for split, labels in [("train", ""), ("val", "0 0.5 0.5 0.2 0.2\n")]:
        image = tmp_path / f"images/{split}/sample.png"
        label = tmp_path / f"labels/{split}/sample.txt"
        image.parent.mkdir(parents=True)
        label.parent.mkdir(parents=True)
        image.write_bytes(split.encode())
        label.write_text(labels)
        samples.append(
            {
                "split": split,
                "image_path": str(image.relative_to(tmp_path)),
                "label_path": str(label.relative_to(tmp_path)),
                "sha256": digest_file(image),
                "label_sha256": digest_file(label),
            }
        )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"classes": ["car", "person"], "samples": samples})
    )
    return s


def test_explicit_negatives_are_valid_but_missing_labels_or_changed_class_order_fail(
    tmp_path,
):
    s = dataset(tmp_path)
    validate_dataset(s)
    label = tmp_path / "labels/train/sample.txt"
    label.unlink()
    with pytest.raises(ValueError, match="missing or changed"):
        validate_dataset(s)
    label.write_text("")
    (tmp_path / "classes.json").write_text(json.dumps(["person", "car"]))
    with pytest.raises(ValueError, match="class order"):
        validate_dataset(s)


def test_native_yaml_normalizes_class_keys_without_changing_frozen_dataset(tmp_path):
    import yaml

    s = dataset(tmp_path)
    validate_dataset(s)
    original = (tmp_path / "data.yaml").read_bytes()
    native = native_dataset_yaml(s, tmp_path)
    parsed = yaml.safe_load(Path(native["data_yaml"]).read_text())
    assert parsed["names"] == ["car", "person"]
    assert parsed["train"] == str(tmp_path / "images/train")
    assert parsed["val"] == str(tmp_path / "images/val")
    assert (tmp_path / "data.yaml").read_bytes() == original
    assert native["frozen_config"] == s["frozen_config"]


def test_libre_arguments_use_native_callbacks_and_evaluate_exact_selected_weights(
    tmp_path, monkeypatch
):
    import torch
    import libreyolo

    s = spec(tmp_path)
    calls = []

    class Model:
        task = "detect"
        names = {0: "car", 1: "person"}
        last_eval_backend = "pycocotools"

        def __init__(self, path, device):
            self.path = path
            calls.append(("load", path, device))

        def train(self, **kw):
            calls.append(("train", kw))
            weights = tmp_path / "framework/weights"
            weights.mkdir(parents=True)
            full = native_checkpoint(s)
            torch.save(full, weights / "last.pt")
            torch.save(full, weights / "best.pt")
            kw["callbacks"].on_train_start(
                SimpleNamespace(
                    save_dir=str(tmp_path / "framework"), start_epoch=1, task="detect"
                )
            )
            kw["callbacks"].on_train_epoch_end(epoch_event(tmp_path, 1))

        def val(self, **kw):
            calls.append(("val", kw))
            return {
                "metrics/mAP50-95": 0.2,
                "metrics/mAP50": 0.3,
                "metrics/mAP75": 0.1,
                "metrics/AR100": 0.4,
            }

    monkeypatch.setattr(libreyolo, "LibreYOLO", Model)
    libreyolo_train(s, tmp_path)
    arguments = next(v[1] for v in calls if v[0] == "train")
    assert arguments["device"] == "cuda:0" and arguments["save_plots"] is False
    assert arguments["eval_interval"] == 1 and arguments["loggers"] == []
    assert "deterministic" not in arguments and "save" not in arguments
    assert calls[-2] == ("load", str(tmp_path / "best.pt"), "cuda:0")
    evaluation = calls[-1][1]
    assert evaluation["split"] == "val" and evaluation["faster_coco_eval"] is False
    report = json.loads((tmp_path / "metrics.json").read_text())
    assert report["evaluation"]["weights_sha256"] == digest_file(tmp_path / "best.pt")
