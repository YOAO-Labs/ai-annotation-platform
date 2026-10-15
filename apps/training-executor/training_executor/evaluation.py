"""Observed final-evaluator identity, separate from legacy report fields."""

import hashlib
import inspect
from importlib.metadata import version
from pathlib import Path

from training_contract.catalog import digest
from training_contract.validation import normalized


def descriptor(spec, weights, implementation, source_files, options, metrics):
    config = spec["frozen_config"]
    return {
        "revision": 1,
        "config_digest": digest(config),
        "candidate": {
            "weights_sha256": weights["sha256"],
            "selection": "native_best",
            "best_epoch": None,
        },
        "benchmark": {
            "validation_digest": config["validation_digest"],
            "classes": config["classes"],
            "class_space": "frozen_native",
        },
        "evaluator": {
            "framework": config["framework"],
            "version": config["framework_version"],
            "task": "detection",
            "implementation": implementation,
            "source_sha256": digest(
                [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in source_files]
            ),
            "options": normalized(options),
        },
        "semantics": {
            key.removesuffix("(B)"): dict(
                unit="fraction",
                direction="higher",
                minimum=0,
                maximum=1,
                unavailable_below=0,
            )
            for key in metrics
            if key.startswith("metrics/")
        },
    }


def ultralytics_descriptor(spec, weights, validator, metrics):
    from ultralytics.engine import validator as engine
    from ultralytics.utils import metrics as native_metrics, ops
    from ultralytics.data import augment, dataset

    args = vars(validator.args)
    fields = (
        "split",
        "imgsz",
        "conf",
        "iou",
        "max_det",
        "augment",
        "single_cls",
        "classes",
        "agnostic_nms",
        "dnn",
        "compile",
        "nms",
        "quantize",
        "channels_last",
        "save_json",
        "fraction",
    )
    options = {key: args[key] for key in fields}
    options.update(
        half=args["quantize"] == 16,
        device=str(validator.device),
        batch=validator.dataloader.batch_size,
        workers=validator.dataloader.num_workers,
        rect=validator.dataloader.dataset.rect,
        imgsz=validator.dataloader.dataset.imgsz,
        torch_version=version("torch"),
        torchvision_version=version("torchvision"),
    )
    return descriptor(
        spec,
        weights,
        type(validator).__module__ + "." + type(validator).__name__,
        [
            inspect.getfile(type(validator)),
            engine.__file__,
            native_metrics.__file__,
            ops.__file__,
            augment.__file__,
            dataset.__file__,
        ],
        options,
        metrics,
    )


def libreyolo_descriptor(spec, weights, backend, options, metrics):
    import libreyolo
    from pycocotools import cocoeval, coco, mask

    # Native validation skips COCOeval when no predictions were accumulated.
    # An absent backend is valid evidence of an incomplete evaluator identity.
    if backend is None:
        return None
    if not isinstance(backend, str) or not (
        backend == "pycocotools" or backend.startswith("pycocotools ")
    ):
        raise ValueError("Unexpected final evaluator backend")
    root = Path(libreyolo.__file__).parent
    options = {
        key: value
        for key, value in options.items()
        if key not in {"data", "project", "name", "exist_ok"}
    }
    options.update(
        backend=backend,
        pycocotools_version=version("pycocotools"),
        torch_version=version("torch"),
        torchvision_version=version("torchvision"),
    )
    sources = sorted(root.rglob("*.py")) + [
        Path(cocoeval.__file__),
        Path(coco.__file__),
        Path(mask.__file__),
    ]
    return descriptor(
        spec, weights, "libreyolo.val/pycocotools.COCOeval", sources, options, metrics
    )
