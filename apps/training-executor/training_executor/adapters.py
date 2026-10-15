"""Framework contracts, selected explicitly before the training subprocess runs."""

from __future__ import annotations
import json
import math
from pathlib import Path, PurePosixPath
import yaml
from .files import digest_file
from training_contract import ADAPTERS, verify_frozen
from training_contract.dataset import (
    SEGMENTATION,
    config_task,
    data_contract,
    segmentation_labels,
)
from training_contract.observability import (
    InputMismatchError,
    NativeConfigurationMismatch,
)


def adapter_for(config):
    adapter = ADAPTERS.get(config["framework"])
    if (
        not adapter
        or config["framework_version"] != adapter["version"]
        or config["adapter_revision"] not in {1, 2}
    ):
        raise ValueError("Unsupported frozen training adapter identity")
    if config["adapter_revision"] == 2:
        verify_frozen(config)
    elif "configuration" in config:
        raise ValueError("Legacy adapter cannot interpret v2 parameters")
    return adapter


def validate_dataset(spec):
    """No implicit negatives, downloads, escaping paths or class guessing."""
    root = Path(spec["data_yaml"]).resolve().parent
    config = yaml.safe_load(Path(spec["data_yaml"]).read_text())
    classes = spec["frozen_config"]["classes"]
    if (
        set(config) != {"train", "val", "names"}
        or config["train"] != "images/train"
        or config["val"] != "images/val"
    ):
        raise ValueError("Unsupported frozen dataset paths or download configuration")
    names = config["names"]
    if isinstance(names, dict):
        names = {int(key): value for key, value in names.items()}
        if set(names) != set(range(len(names))):
            raise ValueError("Invalid dataset class indexes")
        ordered = [names[i] for i in range(len(names))]
    else:
        ordered = names
    manifest = json.loads((root / "manifest.json").read_text())
    contract = data_contract(manifest)
    frozen = spec["frozen_config"]
    segmentation = contract["task"] == SEGMENTATION
    if (
        contract["task"] != config_task(frozen)
        or segmentation
        and (
            frozen.get("adapter_revision") != 2
            or frozen.get("data_contract") != contract
        )
    ):
        raise ValueError("Frozen dataset and native task identity mismatch")
    if (
        ordered != classes
        or manifest["classes"] != classes
        or json.loads((root / "classes.json").read_text()) != classes
    ):
        raise ValueError("Frozen dataset class order mismatch")
    seen, splits = set(), set()
    for sample in manifest["samples"]:
        split = sample["split"]
        splits.add(split)
        paths = [sample["image_path"], sample["label_path"]]
        for index, raw in enumerate(paths):
            path = PurePosixPath(raw)
            if (
                path.is_absolute()
                or ".." in path.parts
                or len(path.parts) != 3
                or path.parts[:2] != (("images" if index == 0 else "labels"), split)
                or raw in seen
            ):
                raise ValueError("Invalid frozen sample path")
            seen.add(raw)
        image, label = (root / p for p in paths)
        if (
            image.stem != label.stem
            or not image.is_file()
            or not label.is_file()
            or digest_file(image) != sample["sha256"]
            or digest_file(label) != sample["label_sha256"]
        ):
            raise ValueError("Frozen image or explicit label file missing or changed")
        text = label.read_text()
        if segmentation:
            lines = text.splitlines()
            mapping = sample.get("instances")
            if not isinstance(mapping, list) or len(mapping) != len(lines):
                raise ValueError("Frozen segmentation instance receipt missing")
            annotations = []
            for line, source in zip(lines, mapping, strict=True):
                values = line.split()
                if len(values) < 7 or len(values) % 2 != 1:
                    raise ValueError("Invalid segmentation label")
                index = int(values[0])
                if not 0 <= index < len(classes):
                    raise ValueError("Invalid segmentation class index")
                coords = list(map(float, values[1:]))
                annotations.append(
                    {
                        "id": source["annotation_id"],
                        "version": source["annotation_version"],
                        "class_name": classes[index],
                        "tool_unit_id": "region",
                        "geometry": {
                            "type": "polygon",
                            "points": [
                                coords[i : i + 2] for i in range(0, len(coords), 2)
                            ],
                        },
                    }
                )
            canonical, receipt = segmentation_labels(annotations, classes)
            if canonical != text or receipt != mapping:
                raise ValueError(
                    "Frozen segmentation label or instance receipt changed"
                )
            continue
        for line in text.splitlines():
            values = line.split()
            if len(values) != 5:
                raise ValueError("Invalid detection label")
            index = int(values[0])
            x, y, w, h = map(float, values[1:])
            if (
                not 0 <= index < len(classes)
                or not all(math.isfinite(v) for v in (x, y, w, h))
                or min(w, h) <= 0
                or x - w / 2 < -1e-8
                or y - h / 2 < -1e-8
                or x + w / 2 > 1 + 1e-8
                or y + h / 2 > 1 + 1e-8
            ):
                raise ValueError("Invalid normalized detection label")
    if splits != {"train", "val"}:
        raise ValueError("Frozen training and validation partitions required")


def train(spec, output, *, progress=None):
    try:
        adapter_for(spec["frozen_config"])
    except ValueError as exc:
        raise NativeConfigurationMismatch(str(exc)) from exc
    try:
        validate_dataset(spec)
    except (ValueError, KeyError, OSError) as exc:
        raise InputMismatchError(str(exc)) from exc
    if spec["framework"] == "ultralytics":
        from .train import ultralytics_train

        ultralytics_train(spec, output, progress=progress)
    elif spec["framework"] == "libreyolo":
        from .libreyolo_adapter import libreyolo_train

        libreyolo_train(spec, output, progress=progress)
