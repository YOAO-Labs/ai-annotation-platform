"""Framework subprocess. Network delivery and GPU leasing belong to its supervisor."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sys
from pathlib import Path

from .files import atomic_json, atomic_bytes, immutable_copy
from training_contract import verify_native
from training_contract.catalog import digest
from training_contract.validation import native_values, normalized
from training_contract.dataset import SEGMENTATION, config_task, is_instance_rle
from training_contract.observability import (
    InputMismatchError,
    NativeConfigurationMismatch,
    NativeRuntimeMismatch,
    failure_receipt,
)
from .observability import NativeProgress
from .native_identity import validate_architecture


def record_native_configuration(spec, output, observed, runtime_options, evidence=None):
    if spec["frozen_config"]["adapter_revision"] != 2:
        return
    observed = normalized(observed)
    try:
        verification = verify_native(spec["frozen_config"], observed, runtime_options)
    except ValueError as exc:
        raise NativeConfigurationMismatch(str(exc)) from exc
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


def ultralytics_epoch_metrics(trainer):
    """Include the native epoch-averaged training losses beside validation metrics."""
    metrics = metrics_of(trainer.metrics or {})
    if trainer.tloss is not None:
        metrics.update(
            metrics_of(trainer.label_loss_items(trainer.tloss, prefix="train"))
        )
    return metrics


def ultralytics_model_path(spec):
    config = spec["frozen_config"]
    pretrained = (
        config.get("configuration", {})
        .get("effective_values", {})
        .get("pretrained", True)
    )
    if not pretrained and config.get("initialization"):
        raise InputMismatchError("Random initialization cannot load project weights")
    if spec.get("resume_path"):
        return spec["resume_path"]
    if not pretrained:
        return config["base_model"]["id"] + ".yaml"
    return spec.get("initialization_path") or spec["base_path"]


class UltralyticsTrainingEvaluation:
    """Evaluate frozen training members with the validation recipe, without augmentation."""

    def __init__(self, output, membership=None):
        self.output = output
        self.membership = membership
        self.validator = None

    def __call__(self, trainer):
        import random

        import numpy as np
        import torch

        # Evaluation must not advance the optimizer's augmentation/random stream.
        python_state, numpy_state = random.getstate(), np.random.get_state()
        devices = [trainer.device.index] if trainer.device.type == "cuda" else []
        try:
            with torch.random.fork_rng(devices=devices):
                return self.evaluate(trainer)
        finally:
            random.setstate(python_state)
            np.random.set_state(numpy_state)

    def evaluate(self, trainer):
        from copy import copy

        if self.validator is None:
            args = copy(trainer.validator.args)
            args.split = "train"
            args.rect = trainer.validator.dataloader.dataset.rect
            args.augment = args.plots = args.save_json = args.save_txt = False
            args.workers = 0
            validator = type(trainer.validator)(
                save_dir=self.output / "training-evaluation", args=args
            )
            validator.data, validator.device = trainer.data, trainer.device
            validator.stride, validator.names = trainer.stride, trainer.model.names
            validator.dataloader = validator.get_dataloader(
                trainer.data["train"], trainer.batch_size
            )
            if self.membership is not None:
                self.membership.observe("train", validator.dataloader.dataset.im_files)
            self.validator = validator
        # A separate validator preserves validation fitness, best selection and reports.
        return {
            f"train/{key}": value
            for key, value in metrics_of(self.validator(trainer=trainer)).items()
            if key.startswith("metrics/")
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
                raise InputMismatchError(
                    "A complete unfinished native optimizer checkpoint is required"
                )
            state = full.get("aap_resume")
            if state is not None:
                if not isinstance(state, dict):
                    raise InputMismatchError("Historical best receipt is invalid")
                best = state.get("best_weights")
                if (
                    state.get("schema") != 1
                    or state.get("config_digest") != digest(spec["frozen_config"])
                    or not isinstance(best, bytes)
                    or not best
                    or hashlib.sha256(best).hexdigest() != state.get("best_sha256")
                ):
                    raise InputMismatchError(
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
                    raise InputMismatchError(
                        "Historical best fitness or epoch does not match the resume checkpoint"
                    )
            elif spec["frozen_config"]["adapter_revision"] == 2:
                raise InputMismatchError(
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
            blob.get("version") != self.spec["frozen_config"]["framework_version"]
            or blob.get("train_args", {}).get("task")
            != (
                "segment"
                if config_task(self.spec["frozen_config"]) == SEGMENTATION
                else "detect"
            )
            or names != self.spec["frozen_config"]["classes"]
            or type(blob.get("epoch")) is not int
            or blob["epoch"] < 0
            or type(blob.get("best_fitness")) not in {int, float}
            or not math.isfinite(blob["best_fitness"])
        ):
            raise InputMismatchError(
                "Native checkpoint version, task, class or epoch identity mismatch"
            )
        if (
            self.spec["frozen_config"]["framework_version"] == "8.4.183"
            or config_task(self.spec["frozen_config"]) == SEGMENTATION
        ):
            try:
                validate_architecture(
                    model,
                    self.spec["frozen_config"]["base_model"]["id"],
                    classes=len(names),
                )
            except (ValueError, AttributeError, KeyError, TypeError) as exc:
                raise InputMismatchError("Unqualified checkpoint architecture") from exc

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


def ultralytics_report(result, spec, comparison=None):
    classes = spec["frozen_config"]["classes"]
    segmentation = config_task(spec["frozen_config"]) == SEGMENTATION
    fields = ("precision", "recall", "mAP50", "mAP50-95")
    if segmentation:
        fields = tuple(f"{key}({kind})" for kind in ("B", "M") for key in fields)
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
                        fields,
                        values,
                        strict=True,
                    )
                ),
            }
        )
    report = {
        "summary": metrics_of(result.results_dict),
        "per_class": per_class,
        "validation_digest": spec["frozen_config"]["validation_digest"],
        "evaluation": {
            "framework": "ultralytics",
            "version": spec["frozen_config"]["framework_version"],
            "task": "instance-segmentation" if segmentation else "detection",
            "split": "val",
            "imgsz": spec["parameters"]["imgsz"],
            "independent_test_set": False,
        },
    }
    if comparison is not None:
        report["comparison"] = comparison
    return report


def ultralytics_train(spec: dict, output: Path, progress=None, membership=None):
    from .input_membership import membership_for

    membership = membership_for(spec, output, membership)
    progress = progress or NativeProgress(spec, output)
    # The framework checks the parent before creating its config subdirectory.
    # Materialize our writable state directory before importing it.
    config_directory = Path(
        os.environ.setdefault("YOLO_CONFIG_DIR", str(output / "ultralytics"))
    )
    config_directory.mkdir(parents=True, exist_ok=True)
    import ultralytics
    from ultralytics import YOLO, settings
    from ultralytics.models.yolo.detect import DetectionTrainer
    from ultralytics.models.yolo.segment import SegmentationTrainer
    from ultralytics.utils import USER_CONFIG_DIR

    if ultralytics.__version__ != spec["frozen_config"]["framework_version"]:
        raise NativeRuntimeMismatch("Unexpected Ultralytics runtime")
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

    segmentation = config_task(spec["frozen_config"]) == SEGMENTATION
    native_task = "segment" if segmentation else "detect"
    raster = is_instance_rle(spec["frozen_config"].get("data_contract", {}))
    trainer_class = SegmentationTrainer if segmentation else DetectionTrainer
    if raster:
        from .instance_rle import InstanceRLETrainer, consumer_evidence

        trainer_class = InstanceRLETrainer
        atomic_json(output / "rle-consumer.json", consumer_evidence())

    class FixedBatchTrainer(trainer_class):
        def preprocess_batch(self, batch):
            result = super().preprocess_batch(batch)
            self._platform_batch_images = int(result["img"].shape[0])
            return result

        def final_eval(self):
            self._platform_final_eval = True
            progress.phase("evaluating")
            emit(output, "evaluating")
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
    evaluate_train = (
        spec["frozen_config"]
        .get("configuration", {})
        .get("effective_values", {})
        .get("evaluate_train", False)
    )
    training_evaluation = (
        UltralyticsTrainingEvaluation(output, membership) if evaluate_train else None
    )

    def epoch_end(trainer):
        # The pinned final_eval temporarily increments epoch before invoking this
        # callback. Its final best-model metrics are written separately below.
        if (
            not getattr(trainer, "_platform_final_eval", False)
            and 0 <= trainer.epoch < trainer.epochs
        ):
            metrics = ultralytics_epoch_metrics(trainer)
            if training_evaluation is not None:
                progress.phase("evaluating")
                try:
                    metrics.update(training_evaluation(trainer))
                finally:
                    progress.phase("training")
            emit(
                output,
                "epoch",
                epoch=trainer.epoch + 1,
                metrics=metrics,
            )
            progress.epoch(trainer.epoch + 1)

    from .initialization import (
        validate_input as validate_initialization_input,
        prepare as prepare_initialization,
        observation as initialization_observation,
    )

    model_path = ultralytics_model_path(spec)
    validate_initialization_input(spec)
    model = YOLO(model_path, task=native_task)
    if model.task != native_task:
        raise InputMismatchError("Loaded weights do not match the frozen native task")
    try:
        validate_architecture(model.model, spec["frozen_config"]["base_model"]["id"])
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        raise InputMismatchError("Unqualified native base architecture") from exc
    initialization_baseline = prepare_initialization(spec, model)

    def started(trainer):
        if membership is not None:
            membership.observe("train", trainer.train_loader.dataset.im_files)
            membership.observe("val", trainer.test_loader.dataset.im_files)
        checkpoints.started(trainer)
        record_native_configuration(
            spec,
            output,
            vars(trainer.args),
            {
                "data": spec["data_yaml"],
                "model": model_path,
                "project": str(output),
                "name": "framework",
                "exist_ok": False,
                "resume": spec.get("resume_path") or False,
            },
            {
                **(
                    {
                        "initialization": initialization_observation(
                            spec, initialization_baseline, trainer
                        )
                    }
                    if initialization_baseline
                    else {}
                ),
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
        progress.started(closing_epochs=trainer.args.close_mosaic)

    model.add_callback("on_train_start", started)
    model.add_callback(
        "on_train_batch_end",
        lambda trainer: progress.batch(
            getattr(trainer, "_platform_batch_images", None)
        ),
    )

    def validating(validator):
        if membership is not None:
            membership.observe("val", validator.dataloader.dataset.im_files)
        progress.phase("evaluating")

    model.add_callback("on_val_start", validating)
    model.add_callback("on_val_end", lambda validator: progress.phase("training"))
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
            **native_values(spec["frozen_config"]["configuration"]),
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
    from .evaluation import ultralytics_descriptor

    report = ultralytics_report(result, spec)
    if raster:
        report["mask_consumer"] = consumer_evidence()
        report["mask_resolution"] = {
            split: json.loads(
                (output / f"framework/rle-{split}-preflight.json").read_text()
            )
            for split in ("train", "val")
        }
    report["comparison"] = ultralytics_descriptor(
        spec, weights, model.trainer.validator, report["summary"]
    )
    if membership is not None:
        membership.complete()
    atomic_json(output / "metrics.json", report)
    atomic_json(
        output / "result.json",
        {"weights": weights, "checkpoint": checkpoints.checkpoint},
    )


def native_failure(spec, progress, exc):
    torch = sys.modules.get("torch")
    code = (
        "cuda_oom"
        if torch is not None and isinstance(exc, torch.cuda.OutOfMemoryError)
        else "input_mismatch"
        if isinstance(exc, InputMismatchError)
        else "runtime_mismatch"
        if isinstance(exc, NativeRuntimeMismatch)
        else "config_mismatch"
        if isinstance(exc, NativeConfigurationMismatch)
        else "native_exception"
    )
    return failure_receipt(
        code,
        progress.phase_name,
        digest(spec["frozen_config"]),
        str(exc),
        exception_type=type(exc).__name__,
        source="native",
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

    progress = NativeProgress(spec, output)
    progress.write(force=True)
    try:
        train(spec, output, progress=progress)
    except Exception as exc:
        try:
            atomic_json(output / "failure.json", native_failure(spec, progress, exc))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
