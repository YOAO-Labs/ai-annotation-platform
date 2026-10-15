"""Framework subprocess. Network delivery and GPU leasing belong to its supervisor."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path

from .files import atomic_json, atomic_bytes, immutable_copy
from training_contract import verify_native
from training_contract.catalog import digest
from training_contract.validation import normalized


def record_native_configuration(spec, output, observed, runtime_options, evidence=None):
    if spec["frozen_config"]["adapter_revision"] != 2:
        return
    observed = normalized(observed)
    verification = verify_native(spec["frozen_config"], observed, runtime_options)
    atomic_json(
        output / "native-config.json",
        {
            "config_digest": digest(spec["frozen_config"]),
            "observed": observed,
            "runtime_options": runtime_options,
            "verification": verification,
            "evidence": evidence or {},
        },
    )


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


class UltralyticsCheckpoints:
    """Carry the native historical best alongside the full optimizer checkpoint."""

    def __init__(self, spec, output):
        self.spec, self.output = spec, output
        self.checkpoint = None
        self.best_bytes = None
        if spec.get("resume_path"):
            import torch

            full = torch.load(
                spec["resume_path"], map_location="cpu", weights_only=False
            )
            self.validate_identity(full)
            epoch = full.get("epoch", -1) + 1
            if (
                not full.get("optimizer")
                or not 0 < epoch < spec["parameters"]["epochs"]
            ):
                raise ValueError(
                    "A complete unfinished native optimizer checkpoint is required"
                )
            state = full.get("aap_resume")
            if state is not None:
                if not isinstance(state, dict):
                    raise ValueError("Historical best receipt is invalid")
                best = state.get("best_weights")
                if (
                    state.get("schema") != 1
                    or state.get("config_digest") != digest(spec["frozen_config"])
                    or not isinstance(best, bytes)
                    or not best
                    or hashlib.sha256(best).hexdigest() != state.get("best_sha256")
                ):
                    raise ValueError(
                        "Historical best weights do not match the frozen resume checkpoint"
                    )
                self.best_bytes = best
                selected = torch.load(
                    io.BytesIO(best), map_location="cpu", weights_only=False
                )
                self.validate_identity(selected)
                if selected["epoch"] > full["epoch"] or selected.get(
                    "train_metrics", {}
                ).get("fitness") != full.get("best_fitness"):
                    raise ValueError(
                        "Historical best fitness or epoch does not match the resume checkpoint"
                    )
            elif spec["frozen_config"]["adapter_revision"] == 2:
                raise ValueError(
                    "The versioned checkpoint lacks verified historical best weights"
                )
            self.checkpoint = immutable_copy(
                Path(spec["resume_path"]), output / f"checkpoint-{epoch:04d}.pt"
            )
            self.checkpoint.update(role="checkpoint", epoch=epoch)
            atomic_json(output / "checkpoint.json", self.checkpoint)

    def validate_identity(self, blob):
        model = blob.get("ema")
        names = getattr(model, "names", None)
        if isinstance(names, dict) and set(names) == set(range(len(names))):
            names = [names[index] for index in range(len(names))]
        if (
            blob.get("version") != "8.4.177"
            or blob.get("train_args", {}).get("task") != "detect"
            or names != self.spec["frozen_config"]["classes"]
            or type(blob.get("epoch")) is not int
            or blob["epoch"] < 0
            or type(blob.get("best_fitness")) not in {int, float}
            or not math.isfinite(blob["best_fitness"])
        ):
            raise ValueError(
                "Native checkpoint version, task, class or epoch identity mismatch"
            )

    def started(self, trainer):
        if self.best_bytes is not None:
            atomic_bytes(Path(trainer.best), self.best_bytes)

    def saved(self, trainer):
        import torch

        previous = self.checkpoint
        epoch = trainer.epoch + 1
        full = torch.load(trainer.last, map_location="cpu", weights_only=False)
        self.validate_identity(full)
        if full.get("epoch") != trainer.epoch or not full.get("optimizer"):
            raise RuntimeError("Full training checkpoint was not saved")
        best = Path(trainer.best)
        if best.is_file():
            best_bytes = best.read_bytes()
            selected = torch.load(
                io.BytesIO(best_bytes), map_location="cpu", weights_only=False
            )
            self.validate_identity(selected)
            if selected["epoch"] > full["epoch"] or selected.get(
                "train_metrics", {}
            ).get("fitness") != full.get("best_fitness"):
                raise ValueError(
                    "Native best weights do not match the checkpoint selection"
                )
            full["aap_resume"] = {
                "schema": 1,
                "config_digest": digest(self.spec["frozen_config"]),
                "best_weights": best_bytes,
                "best_sha256": hashlib.sha256(best_bytes).hexdigest(),
            }
        staging = self.output / "checkpoint-staging.pt"
        with staging.open("wb") as stream:
            torch.save(full, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self.checkpoint = immutable_copy(
            staging, self.output / f"checkpoint-{epoch:04d}.pt"
        )
        staging.unlink()
        self.checkpoint.update(role="checkpoint", epoch=epoch)
        atomic_json(self.output / "checkpoint.json", self.checkpoint)
        if previous and previous["name"] != self.checkpoint["name"]:
            (self.output / previous["name"]).unlink(missing_ok=True)


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

    checkpoints = UltralyticsCheckpoints(spec, output)

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

    def started(trainer):
        checkpoints.started(trainer)
        record_native_configuration(
            spec,
            output,
            vars(trainer.args),
            {
                "data": spec["data_yaml"],
                "model": spec.get("resume_path") or spec["base_path"],
                "project": str(output),
                "name": "framework",
                "exist_ok": False,
                "resume": spec.get("resume_path") or False,
            },
            {
                "optimizer": type(trainer.optimizer).__name__,
                "optimizer_groups": [
                    {
                        key: normalized(value)
                        for key, value in group.items()
                        if key != "params"
                    }
                    for group in trainer.optimizer.param_groups
                ],
                "frozen_parameters": [
                    name
                    for name, parameter in trainer.model.named_parameters()
                    if not parameter.requires_grad
                ],
            },
        )
        emit(output, "training")

    model.add_callback("on_train_start", started)
    # Upstream resets its OOM counter after on_train_start. Set it at epoch start
    # so an OOM propagates instead of silently changing this fixed batch size.
    model.add_callback(
        "on_train_epoch_start", lambda trainer: setattr(trainer, "_oom_retries", 3)
    )
    model.add_callback("on_model_save", checkpoints.saved)
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
    if spec["frozen_config"]["adapter_revision"] == 2:
        arguments = {
            **spec["frozen_config"]["configuration"]["effective_values"],
            "data": spec["data_yaml"],
            "project": str(output),
            "name": "framework",
            "exist_ok": False,
            "resume": spec.get("resume_path") or False,
        }
    result = model.train(trainer=FixedBatchTrainer, **arguments)
    best = output / "framework/weights/best.pt"
    if not best.is_file() or checkpoints.checkpoint is None:
        raise RuntimeError(
            "Training did not produce required weights and checkpoint; legacy checkpoints may lack historical best weights"
        )
    weights = immutable_copy(best, output / "best.pt")
    weights["role"] = "weights"
    atomic_json(output / "metrics.json", ultralytics_report(result, spec))
    atomic_json(
        output / "result.json",
        {"weights": weights, "checkpoint": checkpoints.checkpoint},
    )


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
