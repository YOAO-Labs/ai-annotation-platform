"""Isolated, bounded observation of the pinned native training image pipeline."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import inspect
import json
import os
import random
import resource
import signal
from pathlib import Path

from training_contract.catalog import digest
from training_contract.preview import (
    LIMITS,
    POLICY,
    POLICY_DIGEST,
    closed_mosaic,
    input_digest,
    native_values,
    validate_request,
)

from .files import atomic_json, digest_file


def enforce_limits():
    resource.setrlimit(resource.RLIMIT_AS, (LIMITS["address_bytes"],) * 2)
    resource.setrlimit(
        resource.RLIMIT_CPU, (LIMITS["cpu_seconds"], LIMITS["cpu_hard_seconds"])
    )
    signal.alarm(LIMITS["child_seconds"])
    allowed = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {min(allowed)})
    if os.environ.get("CUDA_VISIBLE_DEVICES") != "":
        raise ValueError("A preview child must hide CUDA before framework imports")


class NativeTrace:
    """Observe native masks and inputs; wrappers never change arrays or RNG."""

    def __init__(self):
        self.reads = []
        self.operations = []

    @contextlib.contextmanager
    def observe(self):
        import numpy as np
        from ultralytics.data.augment import (
            Albumentations,
            CutMix,
            MixUp,
            Mosaic,
            RandomPerspective,
        )
        from ultralytics.utils.instance import Instances

        originals = []

        def patch(owner, key, wrapper, *, static=False):
            original = inspect.getattr_static(owner, key)
            originals.append((owner, key, original))
            callable_original = getattr(owner, key)
            replacement = wrapper(callable_original)
            setattr(owner, key, staticmethod(replacement) if static else replacement)

        def clip(original):
            def call(instance, *args, **kwargs):
                before = instance.bboxes.copy()
                result = original(instance, *args, **kwargs)
                changed = int(np.any(before != instance.bboxes, axis=1).sum())
                self.operations.append(
                    {"step": "clip", "clipped": changed, "filtered": 0}
                )
                return result

            return call

        def zero_area(original):
            def call(instance, *args, **kwargs):
                mask = original(instance, *args, **kwargs)
                self.operations.append(
                    {"step": "zero_area", "clipped": 0, "filtered": int((~mask).sum())}
                )
                return mask

            return call

        def candidates(original):
            def call(*args, **kwargs):
                mask = original(*args, **kwargs)
                self.operations.append(
                    {
                        "step": "perspective_candidates",
                        "clipped": 0,
                        "filtered": int((~mask).sum()),
                    }
                )
                return mask

            return call

        def mosaic(original):
            def call(transform, labels):
                sources = [
                    source for label in labels for source in label["_aap_sources"]
                ]
                result = original(transform, labels)
                result["_aap_sources"] = sources
                self.operations.append({"step": "mosaic", "clipped": 0, "filtered": 0})
                return result

            return call

        def mix(original, step):
            def call(transform, labels, params=None):
                sources = list(labels["_aap_sources"])
                other = labels["mix_labels"][0]
                applied = not (params or {}).get("skip", False)
                filtered = (
                    len(other["cls"]) - len(params["indexes2"])
                    if applied and step == "cutmix"
                    else 0
                )
                if applied:
                    sources += other["_aap_sources"]
                result = original(transform, labels, params)
                result["_aap_sources"] = sources
                self.operations.append(
                    {
                        "step": step if applied else step + "_skipped",
                        "clipped": 0,
                        "filtered": filtered,
                    }
                )
                return result

            return call

        def albumentations(original):
            def call(transform, labels):
                before = len(labels["cls"])
                result = original(transform, labels)
                self.operations.append(
                    {
                        "step": "albumentations",
                        "clipped": 0,
                        "filtered": before - len(result["cls"]),
                    }
                )
                return result

            return call

        patch(Instances, "clip", clip)
        patch(Instances, "remove_zero_area_boxes", zero_area)
        patch(RandomPerspective, "box_candidates", candidates, static=True)
        patch(Mosaic, "_cat_labels", mosaic)
        patch(MixUp, "apply_instances", lambda original: mix(original, "mixup"))
        patch(CutMix, "apply_instances", lambda original: mix(original, "cutmix"))
        patch(Albumentations, "__call__", albumentations)
        try:
            yield
        finally:
            for owner, key, original in reversed(originals):
                setattr(owner, key, original)


def verify_pool(spec, directory):
    """No archive extraction, mutable source lookup or implicit negative labels."""
    from PIL import Image, ImageOps

    sources = spec["sources"]
    if not 1 <= len(sources) <= LIMITS["sources"]:
        raise ValueError("Invalid native source pool")
    total_bytes = total_pixels = total_boxes = 0
    for source in sources:
        image = directory / source["image_path"]
        label = directory / source["label_path"]
        for path in (image, label):
            if (
                not path.resolve().is_relative_to(directory.resolve())
                or not path.is_file()
            ):
                raise ValueError("Missing or escaping preview input")
        if (
            image.stat().st_size != source["size_bytes"]
            or image.stat().st_size > LIMITS["source_bytes"]
            or digest_file(image) != source["sha256"]
            or digest_file(label) != source["label_sha256"]
            or label.read_text() != source["labels"]
        ):
            raise ValueError("Preview source bytes differ from frozen inputs")
        with Image.open(image) as opened, ImageOps.exif_transpose(opened) as decoded:
            width, height = decoded.size
            if (width, height) != (source["width"], source["height"]):
                raise ValueError("Preview source dimensions differ")
            pixels = width * height
            if pixels > LIMITS["source_pixels"]:
                raise ValueError("Preview source pixel bound exceeded")
            decoded.load()
        labels = label.read_text().splitlines()
        if len(labels) > LIMITS["source_boxes"]:
            raise ValueError("Preview source object bound exceeded")
        total_bytes += image.stat().st_size
        total_pixels += pixels
        total_boxes += len(labels)
    if (
        total_bytes > LIMITS["pool_bytes"]
        or total_pixels > LIMITS["pool_pixels"]
        or total_boxes > LIMITS["pool_boxes"]
    ):
        raise ValueError("Preview pool bound exceeded")


def native_preview(spec, directory, *, traced=True):
    import cv2
    import numpy as np
    import torch
    import ultralytics
    from PIL import Image
    from ultralytics import YOLO
    from ultralytics.cfg import get_cfg
    from ultralytics.data.build import build_yolo_dataset
    from ultralytics.models.yolo.detect import DetectionTrainer
    from ultralytics.utils import DEFAULT_CFG

    config = spec["config"]
    validate_request(config, spec["seed"], spec["epoch"], spec["count"])
    if (
        spec["policy_digest"] != POLICY_DIGEST
        or ultralytics.__version__ != config["framework_version"]
        or digest(config) != spec["config_digest"]
        or input_digest(spec) != spec["input_digest"]
        or spec["native_values"] != native_values(config)
        or digest_file(Path(spec["base_path"])) != config["base_model"]["sha256"]
    ):
        raise ValueError("Unqualified preview runtime or frozen configuration")
    verify_pool(spec, directory)
    cv2.setNumThreads(1)
    torch.set_num_threads(1)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    random.seed(spec["seed"])
    np.random.seed(spec["seed"])
    torch.manual_seed(spec["seed"])
    model = YOLO(spec["base_path"], task="detect")
    stride = max(int(model.model.stride.max()), 32)
    args = get_cfg(DEFAULT_CFG, native_values(config))
    observed = {key: getattr(args, key) for key in native_values(config)}
    if observed != native_values(config):
        raise ValueError("Native preview parameter observation mismatch")
    classes = config["classes"]
    dataset = build_yolo_dataset(
        args,
        str(directory / "images/train"),
        batch=config["parameters"]["batch"],
        data={"names": dict(enumerate(classes)), "nc": len(classes)},
        stride=stride,
    )
    if len(dataset) != len(spec["sources"]):
        raise ValueError("Native validation removed a frozen pool member")
    by_path = {
        str((directory / source["image_path"]).resolve()): source
        for source in spec["sources"]
    }
    trace = NativeTrace()
    original_get = dataset.get_image_and_label

    def get_label(index):
        result = original_get(index)
        source = by_path[str(Path(result["im_file"]).resolve())]
        result["_aap_sources"] = [source["task_id"]]
        trace.reads.append(source["task_id"])
        trace.operations.append(
            {
                "step": "native_label_scan",
                "clipped": 0,
                "filtered": len(source["labels"].splitlines()) - len(result["cls"]),
            }
        )
        return result

    dataset.get_image_and_label = get_label
    if closed_mosaic(config, spec["epoch"]):
        dataset.close_mosaic(args)
    # Reproduce a declared, deterministic native buffer state without RNG calls.
    for index in range(len(dataset)):
        dataset.load_image(index)
    trainer = object.__new__(DetectionTrainer)
    trainer.args, trainer.device, trainer.stride = args, torch.device("cpu"), stride
    outputs = []
    with trace.observe() if traced else contextlib.nullcontext():
        for index in range(spec["count"]):
            trace.reads, trace.operations = [], []
            result = dataset[index % len(dataset)]
            sources = result["_aap_sources"]
            batch = trainer.preprocess_batch(dataset.collate_fn([result]))
            image = (
                (batch["img"][0].clamp(0, 1) * 255)
                .round()
                .byte()
                .permute(1, 2, 0)
                .numpy()
            )
            height, width = image.shape[:2]
            boxes = batch["bboxes"].tolist()
            indexes = batch["cls"].view(-1).tolist()
            if (
                max(width, height) > LIMITS["output_edge"]
                or len(boxes) > LIMITS["output_boxes"]
                or not np.isfinite(np.asarray(boxes)).all()
                or any(
                    type(v) not in (int, float)
                    or int(v) != v
                    or not 0 <= v < len(classes)
                    for v in indexes
                )
            ):
                raise ValueError("Native output violates preview bounds")
            name = f"preview-{index}.png"
            Image.fromarray(image).save(
                directory / name, format="PNG", compress_level=3
            )
            if (directory / name).stat().st_size > LIMITS["output_bytes"]:
                raise ValueError("Native preview image exceeds byte bound")
            outputs.append(
                {
                    "index": index,
                    "image": name,
                    "sha256": digest_file(directory / name),
                    "width": width,
                    "height": height,
                    "boxes": [
                        {"class_index": int(c), "xywh": box}
                        for c, box in zip(indexes, boxes, strict=True)
                    ],
                    "source_draws": sources,
                    "sources_read": trace.reads,
                    "operations": trace.operations,
                    "filtered": sum(item["filtered"] for item in trace.operations),
                    "clipped": sum(item["clipped"] for item in trace.operations),
                }
            )
    if torch.cuda.is_initialized():
        raise ValueError("Native preview unexpectedly initialized CUDA")
    if digest_file(Path(spec["base_path"])) != config["base_model"]["sha256"]:
        raise ValueError("Preview changed base weights")
    return {
        "revision": 1,
        "preview_id": spec["preview_id"],
        "input_digest": spec["input_digest"],
        "config_digest": spec["config_digest"],
        "policy": POLICY,
        "policy_digest": POLICY_DIGEST,
        "seed": spec["seed"],
        "epoch": spec["epoch"],
        "mosaic_closed": closed_mosaic(config, spec["epoch"]),
        "native_parameters": observed,
        "transform_parameters": {
            key: getattr(args, key)
            for key in ("mosaic", "mixup", "cutmix", "copy_paste")
        },
        "source_pool": spec["sources"],
        "pool_summary": spec["pool_summary"],
        "outputs": outputs,
        "runtime": {
            "framework": "ultralytics",
            "framework_version": ultralytics.__version__,
            "image_digest": config["image_digest"],
            "adapter_revision": config["adapter_revision"],
            "pipeline_sha256": digest(
                {
                    str(
                        path.relative_to(Path(ultralytics.__file__).parent)
                    ): digest_file(path)
                    for path in sorted(Path(ultralytics.__file__).parent.rglob("*.py"))
                }
            ),
            "torch": torch.__version__,
            "numpy": np.__version__,
            "opencv": cv2.__version__,
            "pillow": importlib.metadata.version("pillow"),
            "stride": stride,
            "cuda_initialized": False,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    body = parser.parse_args()
    enforce_limits()
    if body.spec.stat().st_size > LIMITS["report_bytes"]:
        raise ValueError("Preview spec exceeds bound")
    spec = json.loads(body.spec.read_text())
    report = native_preview(spec, body.spec.parent)
    atomic_json(body.spec.parent / "report.json", report)
    if (body.spec.parent / "report.json").stat().st_size > LIMITS["report_bytes"]:
        raise ValueError("Preview report exceeds bound")


if __name__ == "__main__":
    main()
