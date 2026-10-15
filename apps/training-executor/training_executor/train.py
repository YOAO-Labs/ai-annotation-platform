"""Framework subprocess. Network delivery and GPU leasing belong to its supervisor."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

from .files import atomic_json, immutable_copy


def emit(output: Path, kind: str, **payload):
    events = output / "events.jsonl"
    with events.open("a") as stream:
        stream.write(json.dumps({"kind": kind, **payload}, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def metrics_of(raw):
    return {
        str(key): float(value)
        for key, value in raw.items()
        if isinstance(value, (float, int)) and math.isfinite(float(value))
    }


def ultralytics_report(result, spec):
    classes = spec["frozen_config"]["classes"]
    per_class = []
    for position, class_index in enumerate(result.box.ap_class_index):
        index = int(class_index)
        values = [float(value) for value in result.class_result(position)]
        if not 0 <= index < len(classes) or not all(
            math.isfinite(value) for value in values
        ):
            raise ValueError("Invalid class evaluation result")
        per_class.append(
            {
                "index": index,
                "name": classes[index],
                **dict(
                    zip(
                        ("precision", "recall", "mAP50", "mAP50-95"),
                        values,
                        strict=True,
                    )
                ),
            }
        )
    return {
        "summary": metrics_of(result.results_dict),
        "per_class": per_class,
        "validation_digest": spec["frozen_config"]["validation_digest"],
        "evaluation": {
            "framework": "ultralytics",
            "version": "8.4.177",
            "task": "detection",
            "split": "val",
            "imgsz": spec["parameters"]["imgsz"],
            "independent_test_set": False,
        },
    }


def ultralytics_train(spec: dict, output: Path):
    # The framework checks the parent before creating its config subdirectory.
    # Materialize our writable state directory before importing it.
    config_directory = Path(
        os.environ.setdefault("YOLO_CONFIG_DIR", str(output / "ultralytics"))
    )
    config_directory.mkdir(parents=True, exist_ok=True)
    import torch
    import ultralytics
    from ultralytics import YOLO, settings
    from ultralytics.models.yolo.detect import DetectionTrainer
    from ultralytics.utils import USER_CONFIG_DIR

    if ultralytics.__version__ != "8.4.177":
        raise RuntimeError("Unexpected Ultralytics runtime")
    settings.update(
        {"sync": False, "wandb": False, "mlflow": False, "tensorboard": False}
    )
    # Validation's font check must not download assets from the network.
    import shutil

    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for font in ("Arial.ttf", "Arial.Unicode.ttf"):
        if not (USER_CONFIG_DIR / font).exists():
            shutil.copyfile(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                USER_CONFIG_DIR / font,
            )

    class FixedBatchTrainer(DetectionTrainer):
        def final_eval(self):
            self._platform_final_eval = True
            try:
                return super().final_eval()
            finally:
                self._platform_final_eval = False

        def check_resume(self, overrides):
            super().check_resume(overrides)
            if self.resume:
                # Relocate the same immutable inputs and outputs into this attempt;
                # upstream restores the previous attempt's project/name/data paths.
                for field in ("project", "name", "data", "exist_ok"):
                    setattr(self.args, field, overrides[field])
                self.args.save_dir = str(output / "framework")

    checkpoint = None

    def saved(trainer):
        nonlocal checkpoint
        previous = checkpoint
        epoch = trainer.epoch + 1
        full = torch.load(trainer.last, map_location="cpu", weights_only=False)
        if full.get("epoch") != trainer.epoch or not full.get("optimizer"):
            raise RuntimeError("Full training checkpoint was not saved")
        checkpoint = immutable_copy(
            Path(trainer.last), output / f"checkpoint-{epoch:04d}.pt"
        )
        checkpoint.update(role="checkpoint", epoch=epoch)
        atomic_json(output / "checkpoint.json", checkpoint)
        if previous and previous["name"] != checkpoint["name"]:
            # Keep the latest committed full epoch, not one large copy per epoch.
            (output / previous["name"]).unlink(missing_ok=True)

    def epoch_end(trainer):
        # The pinned final_eval temporarily increments epoch before invoking this
        # callback. Its final best-model metrics are written separately below.
        if (
            not getattr(trainer, "_platform_final_eval", False)
            and 0 <= trainer.epoch < trainer.epochs
        ):
            emit(
                output,
                "epoch",
                epoch=trainer.epoch + 1,
                metrics=metrics_of(trainer.metrics or {}),
            )

    model = YOLO(spec.get("resume_path") or spec["base_path"], task="detect")
    model.add_callback("on_train_start", lambda trainer: emit(output, "training"))
    # Upstream resets its OOM counter after on_train_start. Set it at epoch start
    # so an OOM propagates instead of silently changing this fixed batch size.
    model.add_callback(
        "on_train_epoch_start", lambda trainer: setattr(trainer, "_oom_retries", 3)
    )
    model.add_callback("on_model_save", saved)
    model.add_callback("on_fit_epoch_end", epoch_end)
    arguments = {
        **spec["parameters"],
        "data": spec["data_yaml"],
        "device": 0,
        "workers": 2,
        "project": str(output),
        "name": "framework",
        "exist_ok": False,
        "cache": False,
        "save": True,
        "plots": False,
        "amp": False,
        "deterministic": True,
        "optimizer": "SGD",
        "resume": bool(spec.get("resume_path")),
    }
    result = model.train(trainer=FixedBatchTrainer, **arguments)
    best = output / "framework/weights/best.pt"
    if not best.is_file() or checkpoint is None:
        raise RuntimeError("Training did not produce required weights and checkpoint")
    weights = immutable_copy(best, output / "best.pt")
    weights["role"] = "weights"
    atomic_json(output / "metrics.json", ultralytics_report(result, spec))
    atomic_json(output / "result.json", {"weights": weights, "checkpoint": checkpoint})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    options = parser.parse_args()
    spec = json.loads(options.spec.read_text())
    output = options.spec.parent
    atomic_json(output / "config.json", spec["frozen_config"])
    emit(output, "preparing")
    from .adapters import train

    train(spec, output)


if __name__ == "__main__":
    main()
