"""Independent framework contracts, complete epochs and truthful metrics."""

import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import pytest
from training_executor.adapters import adapter_for, validate_dataset
from training_executor.files import digest_file
from training_executor.train import UltralyticsCheckpoints
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


def test_v2_native_callback_verifies_resolved_config_and_writes_durable_receipt(
    tmp_path,
):
    from training_contract import definition, resolve
    from training_contract.validation import CANONICAL

    s = spec(tmp_path)
    schema = definition("libreyolo")
    configuration = resolve(
        "libreyolo",
        "LibreYOLO9t",
        {
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": {
                "epochs": 3,
                "batch": 1,
                "imgsz": 128,
                "optimizer": "adamw",
                "no_aug_epochs": 0,
            },
        },
    )
    s["frozen_config"].update(
        adapter_revision=2,
        configuration=configuration,
        base_model={"id": "LibreYOLO9t"},
    )
    s["parameters"] = s["frozen_config"]["parameters"] = {
        key: configuration["effective_values"][key] for key in CANONICAL
    }
    event = SimpleNamespace(
        save_dir=str(tmp_path / "framework"),
        start_epoch=1,
        task="detect",
        config={
            **configuration["effective_values"],
            "data": s["data_yaml"],
            "project": str(tmp_path),
            "name": "framework",
            "exist_ok": False,
            "resume": False,
            "num_classes": 2,
            "size": "t",
            "data_dir": None,
        },
    )
    callbacks = CheckpointCallbacks(s, tmp_path)
    callbacks.on_train_start(event)
    receipt = json.loads((tmp_path / "native-config.json").read_text())
    assert receipt["verification"]["verified"]
    assert receipt["verification"]["schema_digest"] == schema["schema_digest"]
    assert receipt["observed"]["optimizer"] == "adamw"
    event.config["optimizer"] = "sgd"
    with pytest.raises(ValueError, match="optimizer"):
        callbacks.on_train_start(event)


def test_v2_frozen_adapter_rejects_tampered_identity_before_native_import(tmp_path):
    s = spec(tmp_path)
    s["frozen_config"].update(
        adapter_revision=2, configuration={"identity": {}, "user_values": {}}
    )
    with pytest.raises((KeyError, ValueError)):
        adapter_for(s["frozen_config"])


def test_ultralytics_resume_preserves_best_even_without_new_improvement_and_recancellation(
    tmp_path,
):
    import torch

    s = spec(tmp_path)
    s["framework"] = s["frozen_config"]["framework"] = "ultralytics"
    s["frozen_config"]["framework_version"] = "8.4.177"
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    native = {
        "version": "8.4.177",
        "train_args": {"task": "detect"},
        "ema": SimpleNamespace(names={0: "car", 1: "person"}),
        "train_metrics": {"fitness": 0.9},
    }
    last, best = first / "last.pt", first / "best.pt"
    torch.save(
        {
            **native,
            "epoch": 0,
            "optimizer": {"state": {}, "param_groups": [{}]},
            "best_fitness": 0.9,
        },
        last,
    )
    torch.save(
        {
            **native,
            "epoch": 0,
            "model": {"weight": torch.tensor([1.25])},
            "best_fitness": 0.9,
        },
        best,
    )
    historical = best.read_bytes()
    callbacks = UltralyticsCheckpoints(s, first)
    callbacks.saved(SimpleNamespace(epoch=0, last=last, best=best))
    resumed_spec = {**s, "resume_path": str(first / callbacks.checkpoint["name"])}
    resumed = UltralyticsCheckpoints(resumed_spec, second)
    committed = json.loads((second / "checkpoint.json").read_text())
    assert committed["epoch"] == 1
    assert (second / committed["name"]).read_bytes() == Path(
        resumed_spec["resume_path"]
    ).read_bytes()
    new_best = second / "framework/weights/best.pt"
    resumed.started(SimpleNamespace(best=new_best))
    assert new_best.read_bytes() == historical
    new_last = second / "last.pt"
    torch.save(
        {
            **native,
            "epoch": 1,
            "optimizer": {"state": {}, "param_groups": [{}]},
            "best_fitness": 0.9,
        },
        new_last,
    )
    resumed.saved(SimpleNamespace(epoch=1, last=new_last, best=new_best))
    full = torch.load(second / resumed.checkpoint["name"], weights_only=False)
    assert full["aap_resume"]["best_weights"] == historical
    assert full["aap_resume"]["best_sha256"] == hashlib.sha256(historical).hexdigest()


def test_ultralytics_resume_rejects_corrupt_best_bytes_and_config_identity(tmp_path):
    import io
    import torch
    from training_contract.catalog import digest

    s = spec(tmp_path)
    s["framework"] = s["frozen_config"]["framework"] = "ultralytics"
    s["frozen_config"]["framework_version"] = "8.4.177"
    resume = tmp_path / "resume.pt"
    blob = {
        "version": "8.4.177",
        "train_args": {"task": "detect"},
        "ema": SimpleNamespace(names={0: "car", 1: "person"}),
        "best_fitness": 0.9,
        "epoch": 0,
        "optimizer": {"state": {}, "param_groups": [{}]},
        "aap_resume": {
            "schema": 1,
            "config_digest": digest(s["frozen_config"]),
            "best_weights": b"original",
            "best_sha256": hashlib.sha256(b"original").hexdigest(),
        },
    }
    for field, value in [("best_weights", b"changed"), ("config_digest", "0" * 64)]:
        changed = {**blob, "aap_resume": {**blob["aap_resume"], field: value}}
        torch.save(changed, resume)
        with pytest.raises(ValueError, match="Historical best"):
            UltralyticsCheckpoints({**s, "resume_path": str(resume)}, tmp_path)

    selected = {
        **blob,
        "aap_resume": None,
        "train_metrics": {"fitness": 0.9},
    }
    for field, value in [
        ("version", "unexpected"),
        ("ema", SimpleNamespace(names={0: "different", 1: "person"})),
        ("epoch", 1),
        ("train_metrics", {"fitness": 0.8}),
    ]:
        stream = io.BytesIO()
        torch.save({**selected, field: value}, stream)
        raw = stream.getvalue()
        changed = {
            **blob,
            "aap_resume": {
                **blob["aap_resume"],
                "best_weights": raw,
                "best_sha256": hashlib.sha256(raw).hexdigest(),
            },
        }
        torch.save(changed, resume)
        with pytest.raises(ValueError):
            UltralyticsCheckpoints({**s, "resume_path": str(resume)}, tmp_path)


def test_ultralytics_new_or_tied_best_updates_receipt_without_mutating_native_files(
    tmp_path,
):
    import torch

    s = spec(tmp_path)
    s["framework"] = s["frozen_config"]["framework"] = "ultralytics"
    s["frozen_config"]["framework_version"] = "8.4.177"
    callbacks = UltralyticsCheckpoints(s, tmp_path)
    last, best = tmp_path / "last.pt", tmp_path / "best.pt"
    for epoch, fitness in enumerate([0.0, 0.9, 0.9]):
        native = {
            "version": "8.4.177",
            "train_args": {"task": "detect"},
            "ema": SimpleNamespace(names={0: "car", 1: "person"}),
            "epoch": epoch,
            "optimizer": {"state": {}, "param_groups": [{}]},
            "best_fitness": fitness,
            "train_metrics": {"fitness": fitness},
        }
        torch.save(native, last)
        torch.save(native, best)
        original_last, original_best = last.read_bytes(), best.read_bytes()
        callbacks.saved(SimpleNamespace(epoch=epoch, last=last, best=best))
        committed = tmp_path / callbacks.checkpoint["name"]
        full = torch.load(committed, weights_only=False)
        assert full["aap_resume"]["best_weights"] == original_best
        assert last.read_bytes() == original_last
        assert best.read_bytes() == original_best
        before_strip = committed.read_bytes()
        torch.save({**native, "epoch": -1, "optimizer": None}, best)
        assert committed.read_bytes() == before_strip


def test_ultralytics_legacy_checkpoint_without_receipt_is_not_reinterpreted_as_v2(
    tmp_path,
):
    import torch

    s = spec(tmp_path)
    s["framework"] = s["frozen_config"]["framework"] = "ultralytics"
    s["frozen_config"]["framework_version"] = "8.4.177"
    resume = tmp_path / "legacy.pt"
    torch.save(
        {
            "version": "8.4.177",
            "train_args": {"task": "detect"},
            "ema": SimpleNamespace(names={0: "car", 1: "person"}),
            "epoch": 0,
            "optimizer": {"state": {}, "param_groups": [{}]},
            "best_fitness": 0.0,
        },
        resume,
    )
    callbacks = UltralyticsCheckpoints({**s, "resume_path": str(resume)}, tmp_path)
    assert callbacks.best_bytes is None
    s["frozen_config"]["adapter_revision"] = 2
    with pytest.raises(ValueError, match="lacks verified historical best"):
        UltralyticsCheckpoints({**s, "resume_path": str(resume)}, tmp_path)


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


def test_adapter_requires_qualified_framework_identity(
    tmp_path,
):
    s = spec(tmp_path)
    assert adapter_for(s["frozen_config"])["model_registration"] is True
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
        SimpleNamespace(
            save_dir=str(first / "framework"),
            start_epoch=1,
            task="detect",
            config={"no_aug_epochs": 10},
        )
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
            save_dir=str(second / "framework"),
            start_epoch=2,
            task="detect",
            config={"no_aug_epochs": 10},
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


def test_libre_resume_rejects_checkpoint_from_another_model_scale(tmp_path):
    s = spec(tmp_path)
    s["frozen_config"]["base_model"] = {"id": "libreyolo9s"}
    with pytest.raises(ValueError, match="identity"):
        validate_checkpoint(native_checkpoint(s), s, epoch=1)


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
        json.dumps({"schema": 1, "classes": ["car", "person"], "samples": samples})
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


def segmentation_dataset(tmp_path):
    from training_contract import definition, resolve
    from training_contract.dataset import SEGMENTATION_CONTRACT, segmentation_labels
    from training_contract.validation import CANONICAL

    s = dataset(tmp_path)
    schema = definition("ultralytics", "yolo11n-seg", version="8.4.177")
    configuration = resolve(
        "ultralytics",
        "yolo11n-seg",
        {
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": {"epochs": 3, "batch": 1, "imgsz": 128},
        },
    )
    s["framework"] = "ultralytics"
    s["frozen_config"].update(
        framework="ultralytics",
        framework_version="8.4.177",
        adapter_revision=2,
        base_model={"id": "yolo11n-seg"},
        configuration=configuration,
        data_contract=SEGMENTATION_CONTRACT,
    )
    s["parameters"] = s["frozen_config"]["parameters"] = {
        key: configuration["effective_values"][key] for key in CANONICAL
    }
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(SEGMENTATION_CONTRACT)
    for sample in manifest["samples"]:
        annotations = (
            []
            if sample["split"] == "train"
            else [
                {
                    "id": "source-instance",
                    "version": 2,
                    "tool_unit_id": "region",
                    "class_name": "car",
                    "geometry": {
                        "type": "polygon",
                        "points": [[0.1, 0.1], [0.8, 0.1], [0.6, 0.7], [0.1, 0.8]],
                    },
                }
            ]
        )
        text, instances = segmentation_labels(
            annotations, s["frozen_config"]["classes"]
        )
        label = tmp_path / sample["label_path"]
        label.write_text(text)
        sample.update(instances=instances, label_sha256=digest_file(label))
    manifest_path.write_text(json.dumps(manifest))
    return s


def test_segmentation_dataset_requires_matching_task_and_unchanged_instance_receipts(
    tmp_path,
):
    s = segmentation_dataset(tmp_path)
    adapter_for(s["frozen_config"])
    validate_dataset(s)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    positive = next(sample for sample in manifest["samples"] if sample["instances"])
    positive["instances"][0]["annotation_version"] = 0
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="实例"):
        validate_dataset(s)
    positive["instances"][0]["annotation_version"] = 2
    positive["instances"][0]["line_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="receipt changed"):
        validate_dataset(s)


def test_detection_cannot_consume_segmentation_or_claim_legacy_segmentation_identity(
    tmp_path,
):
    s = segmentation_dataset(tmp_path)
    s["frozen_config"]["configuration"]["identity"]["task"] = "image-detection"
    with pytest.raises(ValueError, match="task identity"):
        validate_dataset(s)
    s["frozen_config"]["configuration"]["identity"]["task"] = (
        "image-instance-segmentation"
    )
    s["frozen_config"]["adapter_revision"] = 1
    with pytest.raises(ValueError, match="task identity"):
        validate_dataset(s)


@pytest.mark.parametrize("backend", ["pycocotools", None])
@pytest.mark.parametrize("observe_inputs", [False, True, "missing_final"])
def test_libre_arguments_use_native_callbacks_and_evaluate_exact_selected_weights(
    tmp_path, monkeypatch, backend, observe_inputs
):
    import torch
    import libreyolo

    s = spec(tmp_path)
    membership = None
    paths = {
        split: [str(tmp_path / f"images/{split}/sample.png")]
        for split in ("train", "val")
    }
    if observe_inputs:
        import uuid
        from training_contract.input_membership import contract_for
        from training_executor.input_membership import InputMembership

        manifest = {
            "samples": [
                {
                    "task_id": str(uuid.uuid4()),
                    "split": split,
                    "image_path": f"images/{split}/sample.png",
                    "label_path": f"labels/{split}/sample.txt",
                    "sha256": "a" * 64,
                    "label_sha256": "b" * 64,
                }
                for split in ("train", "val")
            ]
        }
        s.update(run_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()))
        s["frozen_config"].update(
            dataset_id=str(uuid.uuid4()),
            dataset_sha256="c" * 64,
            input_contract=contract_for(manifest),
        )
        membership = InputMembership(s, tmp_path, manifest)
        from libreyolo.validation.detection_validator import DetectionValidator

        monkeypatch.setattr(
            DetectionValidator,
            "_setup_dataloader",
            lambda self: SimpleNamespace(
                dataset=SimpleNamespace(img_files=paths["val"])
            ),
        )

    class Trainer:
        def __init__(self):
            self.train_loader = SimpleNamespace(
                dataset=SimpleNamespace(
                    dataset=SimpleNamespace(img_files=paths["train"])
                )
            )
            self._epoch_validator = SimpleNamespace(
                dataloader=SimpleNamespace(
                    dataset=SimpleNamespace(img_files=paths["val"])
                )
            )

    calls = []
    monkeypatch.setattr(torch, "set_num_interop_threads", lambda _: None)

    class Model:
        task = "detect"
        names = {0: "car", 1: "person"}
        last_eval_backend = backend

        def __init__(self, path, device):
            self.path = path
            calls.append(("load", path, device))

        def _trainer_class(self):
            return Trainer

        def train(self, **kw):
            calls.append(("train", kw))
            if observe_inputs:
                self._trainer_class()()
            weights = tmp_path / "framework/weights"
            weights.mkdir(parents=True)
            full = native_checkpoint(s)
            torch.save(full, weights / "last.pt")
            torch.save(full, weights / "best.pt")
            kw["callbacks"].on_train_start(
                SimpleNamespace(
                    save_dir=str(tmp_path / "framework"),
                    start_epoch=1,
                    task="detect",
                    config={"no_aug_epochs": 10},
                )
            )
            kw["callbacks"].on_train_epoch_end(epoch_event(tmp_path, 1))

        def val(self, **kw):
            calls.append(("val", kw))
            if observe_inputs:
                if observe_inputs == "missing_final":
                    paths["val"] = []
                validator = object.__new__(self.validator_class)
                validator._setup_dataloader()
            return {
                "metrics/mAP50-95": 0.2 if backend else 0.0,
                "metrics/mAP50": 0.3 if backend else 0.0,
                "metrics/mAP75": 0.1 if backend else 0.0,
                "metrics/AR100": 0.4 if backend else 0.0,
            }

    monkeypatch.setattr(libreyolo, "LibreYOLO", Model)
    if observe_inputs == "missing_final":
        from training_contract.observability import InputMismatchError

        with pytest.raises(InputMismatchError, match="differ"):
            libreyolo_train(s, tmp_path, membership=membership)
        assert not (tmp_path / "result.json").exists()
        return
    libreyolo_train(s, tmp_path, membership=membership)
    if observe_inputs:
        proof = json.loads((tmp_path / "input-membership.json").read_text())
        assert proof["counts"] == {"train": 1, "val": 1}
        assert proof["attempt_id"] == s["attempt_id"]
    arguments = next(v[1] for v in calls if v[0] == "train")
    assert arguments["device"] == "cuda:0" and arguments["save_plots"] is False
    assert arguments["eval_interval"] == 1 and arguments["loggers"] == []
    assert "deterministic" not in arguments and "save" not in arguments
    assert calls[-2] == ("load", str(tmp_path / "best.pt"), "cuda:0")
    evaluation = calls[-1][1]
    assert evaluation["split"] == "val" and evaluation["faster_coco_eval"] is False
    report = json.loads((tmp_path / "metrics.json").read_text())
    assert report["evaluation"]["weights_sha256"] == digest_file(tmp_path / "best.pt")

    if backend is None:
        assert "comparison" not in report
        assert report["comparison_unavailable_reason"] == "evaluator_not_recorded"
        assert report["summary"]["metrics/mAP50-95"] == 0.0
        return
    comparison = report["comparison"]
    assert comparison["candidate"]["weights_sha256"] == digest_file(
        tmp_path / "best.pt"
    )
    assert comparison["benchmark"]["classes"] == ["car", "person"]
    assert comparison["evaluator"]["options"]["conf"] == 0.001
    assert comparison["evaluator"]["options"]["pycocotools_version"]
    assert comparison["semantics"]["metrics/AR100"]["unit"] == "fraction"
    assert "candidate" not in report["evaluation"]
