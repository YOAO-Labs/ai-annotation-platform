"""Detection companions for the existing bounded native qualification entry point."""

import asyncio
import hashlib
import json
from pathlib import Path
import resource
import sys
import uuid

from PIL import Image, ImageDraw
import torch

from training_contract.catalog import digest
from training_contract.models import model_spec
from training_executor.train import UltralyticsCheckpoints
from training_executor.native_identity import validate_architecture


class Interrupted(Exception):
    pass


def detection_fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    samples = []
    for split in ("train", "val"):
        for index in range(2):
            image = root / f"images/{split}/{index}.png"
            label = root / f"labels/{split}/{index}.txt"
            image.parent.mkdir(parents=True, exist_ok=True)
            label.parent.mkdir(parents=True, exist_ok=True)
            canvas = Image.new("RGB", (128, 128), "black")
            if index == 0:
                ImageDraw.Draw(canvas).rectangle((32, 32, 95, 95), fill="white")
            canvas.save(image)
            label.write_text("0 0.5 0.5 0.5 0.5\n" if index == 0 else "")
            samples.append(
                {
                    "split": split,
                    "image_path": str(image.relative_to(root)),
                    "label_path": str(label.relative_to(root)),
                    "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                    "label_sha256": hashlib.sha256(label.read_bytes()).hexdigest(),
                }
            )
    (root / "manifest.json").write_text(
        json.dumps({"schema": 1, "classes": ["shape"], "samples": samples})
    )
    (root / "classes.json").write_text('["shape"]')
    (root / "data.yaml").write_text(
        f"train: {root}/images/train\nval: {root}/images/val\nnames: [shape]\n"
    )
    return root / "data.yaml"


def qualify_managed_return(path, base_model, root):
    """Use the actual HTTP installer and native inspection, with task-owned storage."""
    backend = Path(__file__).resolve().parents[2] / "yolo-backend"
    sys.path.insert(0, str(backend))
    from managed_models import InstallRequest, ManagedModelStore
    from scripts.controlled_segmentation_validation import _weight_server
    from training_contract.model_return import libre_identity, ultralytics_identity

    framework = "libreyolo" if base_model.startswith("libreyolo") else "ultralytics"
    spec = model_spec(framework, base_model)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = (
        libre_identity(base_model)
        if framework == "libreyolo"
        else ultralytics_identity(base_model, framework_version="8.4.183")
    )
    expected = {
        "model_version_id": str(uuid.uuid4()),
        "sha256": sha,
        "classes": ["shape"],
        "classes_digest": digest(["shape"]),
        "framework": framework,
        "framework_version": "1.6.0" if framework == "libreyolo" else "8.4.183",
        "task": "segmentation" if spec.task == "segment" else "detection",
        **identity,
        **(
            {"artifact_size_bytes": path.stat().st_size}
            if framework == "libreyolo"
            else {}
        ),
    }
    store = ManagedModelStore(root / "managed")
    with _weight_server(path) as url:
        body = InstallRequest.model_validate(
            {**expected, "size_bytes": path.stat().st_size, "download_url": url}
        )
        installed = asyncio.run(store.install(body))
    assert installed == expected
    resolved, receipt = store.resolve(expected["model_version_id"], sha)
    assert resolved.is_file() and receipt == expected
    return {
        "http_install": True,
        "receipt_digest": digest(receipt),
        "weights_sha256": sha,
        "native_inspection": True,
        "device": "cpu",
    }


def qualify_detection(base, temporary, base_model):
    from ultralytics import YOLO, __version__
    from ultralytics.models.yolo.detect import DetectionTrainer

    spec_model = model_spec("ultralytics", base_model)
    root = Path(temporary)
    data = detection_fixture(root / "data")
    frozen = {
        "framework": "ultralytics",
        "framework_version": __version__,
        "adapter_revision": 2,
        "base_model": {"id": base_model},
        "classes": ["shape"],
        "task": "detect",
        "parameters": {"epochs": 3},
        "validation_digest": "qualification",
    }
    spec = {"frozen_config": frozen, "parameters": frozen["parameters"]}
    first, second = root / "first", root / "second"
    first.mkdir()
    second.mkdir()
    callbacks = UltralyticsCheckpoints(spec, first)
    native = YOLO(base, task="detect")
    validate_architecture(native.model, base_model)
    native.add_callback("on_train_start", callbacks.started)
    native.add_callback("on_model_save", callbacks.saved)

    def interrupt(trainer):
        if trainer.epoch == 0:
            raise Interrupted(
                "Qualification stopped after a durable optimizer checkpoint"
            )

    native.add_callback("on_fit_epoch_end", interrupt)
    options = dict(
        data=str(data),
        device="cpu",
        epochs=3,
        imgsz=128,
        batch=1,
        workers=0,
        amp=False,
        cache=False,
        deterministic=True,
        optimizer="SGD",
        mosaic=0,
        mixup=0,
        cutmix=0,
        copy_paste=0,
        close_mosaic=0,
        translate=0,
        scale=0,
        degrees=0,
        shear=0,
        perspective=0,
        flipud=0,
        rect=False,
        multi_scale=0,
        plots=False,
        save=True,
        exist_ok=False,
        project=str(first),
        name="framework",
        nms=False if spec_model.end2end else None,
    )
    try:
        native.train(**options)
    except Interrupted:
        pass
    assert callbacks.checkpoint["epoch"] == 1
    checkpoint = first / callbacks.checkpoint["name"]
    resumed_spec = {**spec, "resume_path": str(checkpoint)}
    resumed_callbacks = UltralyticsCheckpoints(resumed_spec, second)
    resumed = YOLO(str(checkpoint), task="detect")
    resumed.add_callback("on_train_start", resumed_callbacks.started)
    resumed.add_callback("on_model_save", resumed_callbacks.saved)

    class RelocatedTrainer(DetectionTrainer):
        def check_resume(self, overrides):
            super().check_resume(overrides)
            for field in ("project", "name", "data", "exist_ok"):
                setattr(self.args, field, overrides[field])
            self.args.save_dir = str(second / "framework")

    result = resumed.train(
        trainer=RelocatedTrainer,
        **{**options, "project": str(second), "resume": str(checkpoint)},
    )
    best = second / "framework/weights/best.pt"
    reloaded = YOLO(str(best))
    validate_architecture(reloaded.model, base_model, classes=1)
    assert reloaded.names == {0: "shape"} and resumed.trainer.start_epoch == 1
    assert resumed_callbacks.checkpoint["epoch"] == 3
    return {
        "base_model": base_model,
        "native_version": __version__,
        "device": "cpu",
        "initialization": "pretrained" if Path(base).suffix == ".pt" else "scratch",
        "cancel_checkpoint_epoch": 1,
        "resume_start_epoch": 1,
        "resume_completed_epoch": 3,
        "best_reloaded": True,
        "native_metrics": result.results_dict,
        "end2end": bool(resumed.trainer.validator.end2end),
        "nms": resumed.trainer.validator.args.nms,
        "managed_return": qualify_managed_return(best, base_model, root),
        "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "gpu_qualification": "requires platform-owned GPU execution",
    }


def qualify_libre(base, temporary, base_model):
    from types import SimpleNamespace
    import libreyolo
    from libreyolo import LibreYOLO
    from libreyolo.models.yolo9.model import LibreYOLO9
    from training_executor.libreyolo_adapter import (
        CheckpointCallbacks,
        validate_checkpoint,
    )

    identity = model_spec("libreyolo", base_model)
    root = Path(temporary)
    data = detection_fixture(root / "data")
    frozen = {
        "framework": "libreyolo",
        "framework_version": libreyolo.__version__,
        "adapter_revision": 1,
        "base_model": {"id": base_model},
        "classes": ["shape"],
        "task": "detect",
        "parameters": {"epochs": 3},
        "validation_digest": "qualification",
    }
    spec = {"frozen_config": frozen, "parameters": frozen["parameters"]}
    first, second = root / "first", root / "second"
    first.mkdir()
    second.mkdir()
    progress = SimpleNamespace(started=lambda **_: None, epoch=lambda *_, **__: None)
    callbacks = CheckpointCallbacks(spec, first, progress=progress)

    class StopAfterCheckpoint:
        def on_train_epoch_end(self, event):
            if event.epoch == 1:
                raise Interrupted(
                    "Qualification stopped after a durable optimizer checkpoint"
                )

    pretrained = Path(base).is_file() and Path(base).suffix == ".pt"
    native = (
        LibreYOLO(base, device="cpu")
        if pretrained
        else LibreYOLO9(
            None, size=identity.scale, nb_classes=1, device="cpu", _scratch_init=True
        )
    )
    options = dict(
        data=str(data),
        device="cpu",
        epochs=3,
        imgsz=128,
        batch=1,
        workers=0,
        amp=False,
        cache=False,
        optimizer="sgd",
        seed=0,
        patience=10,
        pretrained=None if pretrained else False,
        mosaic=0,
        mixup=0,
        cutmix=0,
        no_aug_epochs=0,
        save_plots=False,
        plot_samples=0,
        eval_interval=1,
        save_period=0,
        faster_coco_eval=False,
        max_det=100,
        eval_max_det=100,
        project=str(first),
        name="framework",
        exist_ok=False,
        callbacks=[callbacks, StopAfterCheckpoint()],
    )
    try:
        native.train(**options)
    except Interrupted:
        pass
    assert callbacks.checkpoint["epoch"] == 1
    checkpoint = first / callbacks.checkpoint["name"]
    blob = validate_checkpoint(
        torch.load(checkpoint, map_location="cpu", weights_only=True), spec, resume=True
    )
    resumed_callbacks = CheckpointCallbacks(
        spec, second, resume=blob, progress=progress
    )
    resumed = LibreYOLO(str(checkpoint), device="cpu")
    result = resumed.train(
        **{
            **options,
            "project": str(second),
            "resume": str(checkpoint),
            "pretrained": None,
            "callbacks": [resumed_callbacks],
        }
    )
    assert resumed_callbacks.checkpoint["epoch"] == 3
    best = second / "framework/weights/best.pt"
    selected = validate_checkpoint(
        torch.load(best, map_location="cpu", weights_only=True), spec
    )
    assert selected["size"] == identity.scale
    reloaded = LibreYOLO(str(best), device="cpu")
    assert reloaded.names == {0: "shape"}
    return {
        "base_model": base_model,
        "native_version": libreyolo.__version__,
        "device": "cpu",
        "initialization": "pretrained" if pretrained else "native-internal-scratch",
        "cancel_checkpoint_epoch": 1,
        "resume_start_epoch": 1,
        "resume_completed_epoch": 3,
        "best_reloaded": True,
        "native_metrics": result,
        "managed_return": qualify_managed_return(best, base_model, root),
        "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "gpu_qualification": "requires platform-owned GPU execution",
    }
