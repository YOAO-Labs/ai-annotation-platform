"""LibreYOLO's native detector contract, independent of Ultralytics callbacks."""

from __future__ import annotations
import hashlib
import json
import logging
import os
from pathlib import Path
from .files import atomic_json, atomic_bytes, digest_file, immutable_copy
from .train import emit, metrics_of, record_native_configuration
from .observability import NativeProgress
from training_contract.observability import InputMismatchError, NativeRuntimeMismatch

VERSION = "1.6.0"


def native_dataset_yaml(spec, output):
    import yaml

    source = Path(spec["data_yaml"]).resolve()
    # Native validation indexes names with integers; frozen JSON maps use strings.
    target = output / "libreyolo-data.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "train": str(source.parent / "images/train"),
                "val": str(source.parent / "images/val"),
                "names": spec["frozen_config"]["classes"],
            },
            allow_unicode=True,
        )
    )
    return {**spec, "data_yaml": str(target)}


def config_digest(spec):
    return hashlib.sha256(
        json.dumps(
            spec["frozen_config"],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def ordered_names(blob):
    names = blob["names"]
    if isinstance(names, dict):
        if set(names) != set(range(len(names))):
            raise ValueError("Invalid native class indexes")
        return [names[i] for i in range(len(names))]
    return names


def validate_checkpoint(blob, spec, *, epoch=None, resume=False):
    from libreyolo.utils.serialization import validate_checkpoint_metadata

    try:
        validate_checkpoint_metadata(blob, strict=True)
    except (TypeError, ValueError) as exc:
        raise InputMismatchError(str(exc)) from exc
    if (
        blob["task"] != "detect"
        or blob["model_family"] != "yolo9"
        or blob["libreyolo_version"] != VERSION
        or spec["frozen_config"].get("base_model") is not None
        and blob.get("size") != spec["frozen_config"]["base_model"]["id"][-1].lower()
        or ordered_names(blob) != spec["frozen_config"]["classes"]
    ):
        raise InputMismatchError(
            "Native checkpoint task, version or class identity mismatch"
        )
    if epoch is not None and (
        blob.get("epoch") != epoch - 1 or not blob.get("optimizer")
    ):
        raise InputMismatchError("Full native epoch and optimizer state required")
    if resume:
        state = blob.get("aap_resume", {})
        completed = blob.get("epoch", -1) + 1
        best = state.get("best_weights")
        if (
            state.get("schema") != 1
            or state.get("config_digest") != config_digest(spec)
            or not isinstance(best, bytes)
            or hashlib.sha256(best).hexdigest() != state.get("best_sha256")
            or not 0 < completed < spec["parameters"]["epochs"]
            or blob.get("fitness_source") == "callback"
            or not blob.get("optimizer")
            or not blob.get("rng_state")
        ):
            raise InputMismatchError(
                "Only a complete, verified platform resume checkpoint is supported"
            )
    return blob


class CheckpointCallbacks:
    def __init__(self, spec, output, resume=None, progress=None, membership=None):
        self.spec, self.output, self.resume = spec, output, resume
        self.progress = progress or NativeProgress(spec, output)
        self.checkpoint = None
        self.membership, self.trainer = membership, None

    def commit_resume(self, path):
        # A second cancellation before a new epoch must retain the verified input.
        epoch = self.resume["epoch"] + 1
        self.checkpoint = immutable_copy(
            Path(path), self.output / f"checkpoint-{epoch:04d}.pt"
        )
        self.checkpoint.update(role="checkpoint", epoch=epoch)
        atomic_json(self.output / "checkpoint.json", self.checkpoint)

    def on_train_start(self, event):
        if self.membership is not None:
            self.membership.observe(
                "train", self.trainer.train_loader.dataset.dataset.img_files
            )
        expected = (self.resume["epoch"] + 2) if self.resume else 1
        if (
            Path(event.save_dir).resolve() != (self.output / "framework").resolve()
            or event.start_epoch != expected
            or event.task != "detect"
        ):
            raise ValueError("Native resume directory or epoch mismatch")
        if self.resume:
            atomic_bytes(
                Path(event.save_dir) / "weights/best.pt",
                self.resume["aap_resume"]["best_weights"],
            )
        if self.spec["frozen_config"]["adapter_revision"] == 2:
            record_native_configuration(
                self.spec,
                self.output,
                dict(event.config),
                {
                    "data": self.spec["data_yaml"],
                    "project": str(self.output),
                    "name": "framework",
                    "exist_ok": False,
                    "resume": bool(self.resume),
                    "num_classes": len(self.spec["frozen_config"]["classes"]),
                    "size": self.spec["frozen_config"]["base_model"]["id"][-1].lower(),
                    "data_dir": None,
                },
            )
        emit(self.output, "training")
        self.progress.started(closing_epochs=dict(event.config).get("no_aug_epochs"))

    def on_train_epoch_end(self, event):
        import torch

        if self.membership is not None:
            if getattr(self.trainer, "_epoch_validator", None) is None:
                raise InputMismatchError(
                    "Native validation input observation is missing"
                )
            self.membership.observe(
                "val", self.trainer._epoch_validator.dataloader.dataset.img_files
            )
        directory = Path(event.save_dir) / "weights"
        full = validate_checkpoint(
            torch.load(directory / "last.pt", map_location="cpu", weights_only=True),
            self.spec,
            epoch=event.epoch,
        )
        best = directory / "best.pt"
        native_best = validate_checkpoint(
            torch.load(best, map_location="cpu", weights_only=True), self.spec
        )
        if native_best.get("epoch", -1) + 1 != full.get("best_epoch"):
            raise ValueError(
                "Historical best weights do not match checkpoint selection"
            )
        full["aap_resume"] = {
            "schema": 1,
            "config_digest": config_digest(self.spec),
            "best_weights": best.read_bytes(),
            "best_sha256": digest_file(best),
        }
        staging = self.output / "checkpoint-staging.pt"
        with staging.open("wb") as f:
            torch.save(full, f)
            f.flush()
            os.fsync(f.fileno())
        previous = self.checkpoint
        self.checkpoint = immutable_copy(
            staging, self.output / f"checkpoint-{event.epoch:04d}.pt"
        )
        staging.unlink()
        self.checkpoint.update(role="checkpoint", epoch=event.epoch)
        atomic_json(self.output / "checkpoint.json", self.checkpoint)
        if previous and previous["name"] != self.checkpoint["name"]:
            (self.output / previous["name"]).unlink(missing_ok=True)
        # Alias names in this framework are not conventional precision/recall.
        raw = metrics_of(event.val_metrics)
        metrics = {
            k: v for k, v in raw.items() if "precision" not in k and "recall" not in k
        }
        metrics["train/loss"] = float(event.train_loss)
        emit(self.output, "epoch", epoch=event.epoch, metrics=metrics)
        self.progress.epoch(
            event.epoch, raw_epoch_seconds=getattr(event, "epoch_seconds", None)
        )

    def on_train_end(self, event):
        pass

    def on_train_exception(self, event):
        pass


def evaluation_options(spec, output):
    return {
        "data": spec["data_yaml"],
        "split": "val",
        "batch": spec["parameters"]["batch"],
        "imgsz": spec["parameters"]["imgsz"],
        "conf": 0.001,
        "iou": 0.65,
        "max_det": 300,
        "eval_max_det": 100,
        "half": False,
        "workers": 2,
        "device": "cuda:0",
        "augment": False,
        "plots": False,
        "save_json": False,
        "faster_coco_eval": False,
        "allow_download_scripts": False,
        "project": str(output),
        "name": "evaluation",
        "exist_ok": False,
    }


def libreyolo_report(raw, spec, weights, backend):
    native = metrics_of(raw)
    keys = ("metrics/mAP50-95", "metrics/mAP50", "metrics/mAP75", "metrics/AR100")
    if any(k not in native for k in keys):
        raise ValueError("Native detection evaluation is incomplete")
    options = evaluation_options(spec, Path("."))
    for key in ("data", "project", "name", "exist_ok"):
        options.pop(key)
    return {
        "summary": {k: native[k] for k in keys},
        "native_raw": native,
        "per_class": [],
        "per_class_available": False,
        "native_alias_semantics": {
            "metrics/precision": "mAP50-95",
            "metrics/recall": "AR100",
        },
        "validation_digest": spec["frozen_config"]["validation_digest"],
        "evaluation": {
            "framework": "libreyolo",
            "version": VERSION,
            "task": "detection",
            "evaluator": backend,
            "weights_sha256": weights["sha256"],
            "parameters": options,
            "independent_test_set": False,
            "deterministic_algorithms": True,
            "bitwise_resume_guaranteed": False,
        },
    }


class StrictResumeLogging(logging.Handler):
    def emit(self, record):
        message = record.getMessage()
        if any(
            text in message
            for text in (
                "Could not load optimizer state",
                "Could not load EMA weights",
                "Could not restore RNG state",
                "Could not restore GradScaler state",
            )
        ):
            raise RuntimeError("Native checkpoint state restoration failed: " + message)


def libreyolo_train(spec, output, progress=None, membership=None):
    from .input_membership import membership_for

    membership = membership_for(spec, output, membership)
    progress = progress or NativeProgress(spec, output)
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    os.environ["LIBREYOLO_FASTER_COCO_EVAL"] = "0"
    import torch
    import cv2
    import libreyolo
    from libreyolo import LibreYOLO

    if libreyolo.__version__ != VERSION:
        raise NativeRuntimeMismatch("Unexpected LibreYOLO runtime")
    spec = native_dataset_yaml(spec, output)
    cv2.setNumThreads(1)
    torch.set_num_threads(4)
    torch.set_num_interop_threads(4)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.use_deterministic_algorithms(True)
    resume = None
    if spec.get("resume_path"):
        resume = validate_checkpoint(
            torch.load(spec["resume_path"], map_location="cpu", weights_only=True),
            spec,
            resume=True,
        )
        # Validate the exact best bytes before entering the native trusted loader.
        import io

        validate_checkpoint(
            torch.load(
                io.BytesIO(resume["aap_resume"]["best_weights"]),
                map_location="cpu",
                weights_only=True,
            ),
            spec,
        )
    callbacks = CheckpointCallbacks(
        spec, output, resume, progress=progress, membership=membership
    )
    if resume:
        callbacks.commit_resume(spec["resume_path"])
    model = LibreYOLO(spec.get("resume_path") or spec["base_path"], device="cuda:0")
    if model.task != "detect":
        raise ValueError("Controlled LibreYOLO base is not a detector")
    if membership is not None:
        native_trainer_class = model._trainer_class()

        class ObservedTrainer(native_trainer_class):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                callbacks.trainer = self

        model._trainer_class = lambda: ObservedTrainer
    logger = logging.getLogger("libreyolo.training.trainer")
    handler = StrictResumeLogging()
    logger.addHandler(handler)
    try:
        arguments = dict(
            **spec["parameters"],
            data=spec["data_yaml"],
            device="cuda:0",
            workers=2,
            project=str(output),
            name="framework",
            exist_ok=False,
            resume=spec.get("resume_path") or False,
            optimizer="SGD",
            amp=False,
            cache=False,
            eval_interval=1,
            save_period=0,
            save_plots=False,
            cuda_graph=False,
            compile=False,
            allow_download_scripts=False,
            callbacks=callbacks,
            loggers=[],
        )
        if spec["frozen_config"]["adapter_revision"] == 2:
            arguments = {
                **spec["frozen_config"]["configuration"]["effective_values"],
                "data": spec["data_yaml"],
                "project": str(output),
                "name": "framework",
                "exist_ok": False,
                "resume": spec.get("resume_path") or False,
                "callbacks": callbacks,
                "loggers": [],
                "cuda_graph": False,
                "compile": False,
            }
        model.train(**arguments)
    finally:
        logger.removeHandler(handler)
    best = output / "framework/weights/best.pt"
    if not best.is_file() or callbacks.checkpoint is None:
        raise RuntimeError(
            "Native training did not commit required weights and checkpoint"
        )
    weights = immutable_copy(best, output / "best.pt")
    weights["role"] = "weights"
    emit(output, "evaluating")
    progress.phase("evaluating")
    evaluated = LibreYOLO(str(output / "best.pt"), device="cuda:0")
    if ordered_names({"names": evaluated.names}) != spec["frozen_config"]["classes"]:
        raise ValueError("Trained native class order mismatch")
    if membership is not None:
        from libreyolo.validation.detection_validator import DetectionValidator

        class ObservedValidator(DetectionValidator):
            def _setup_dataloader(self):
                loader = super()._setup_dataloader()
                membership.observe("val", loader.dataset.img_files)
                return loader

        evaluated.validator_class = ObservedValidator
    raw = evaluated.val(**evaluation_options(spec, output))
    from .evaluation import libreyolo_descriptor

    report = libreyolo_report(
        raw, spec, weights, getattr(evaluated, "last_eval_backend", None)
    )
    comparison = libreyolo_descriptor(
        spec,
        weights,
        getattr(evaluated, "last_eval_backend", None),
        evaluation_options(spec, output),
        report["summary"],
    )
    if comparison is not None:
        report["comparison"] = comparison
    else:
        report["comparison_unavailable_reason"] = "evaluator_not_recorded"
    if membership is not None:
        membership.complete()
    atomic_json(
        output / "metrics.json",
        report,
    )
    atomic_json(
        output / "result.json", {"weights": weights, "checkpoint": callbacks.checkpoint}
    )
