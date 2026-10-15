"""Strict, deterministic image detection dataset packaging."""

from __future__ import annotations

import hashlib
import json
import math
import zipfile
from pathlib import Path


class TrainingDatasetError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def content_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def detection_labels(annotations: list[dict], classes: list[str]) -> str:
    """Reject unsupported shapes and unknown classes instead of dropping them."""
    lines = []
    for annotation in annotations:
        geometry = annotation["geometry"]
        if (
            annotation.get("tool_unit_id") != "bbox"
            or geometry.get("type") != "bbox"
            or annotation["class_name"] not in classes
        ):
            raise TrainingDatasetError(
                "unsupported_annotation", "标注类型或类别不属于冻结的检测类别表"
            )
        values = [geometry.get(key) for key in ("x", "y", "w", "h")]
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in values
        ):
            raise TrainingDatasetError("invalid_bbox", "矩形框必须包含有限的数值坐标")
        x, y, w, h = values
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1 + 1e-9 or y + h > 1 + 1e-9:
            raise TrainingDatasetError(
                "invalid_bbox", "矩形框必须完整位于归一化图像范围内"
            )
        lines.append(
            f"{classes.index(annotation['class_name'])} {x + w / 2:.8f} {y + h / 2:.8f} {w:.8f} {h:.8f}"
        )
    return "\n".join(sorted(lines)) + ("\n" if lines else "")


def split_history(manifest: dict | None) -> dict[str, str]:
    history = dict((manifest or {}).get("split_history", {}))
    for sample in (manifest or {}).get("samples", []):
        for identity in sample_identities(sample):
            if identity in history and history[identity] != sample["split"]:
                raise TrainingDatasetError(
                    "inherited_split_conflict", "历史素材或场景存在相互冲突的分区"
                )
            history[identity] = sample["split"]
    return history


def sample_identities(sample: dict) -> list[str]:
    identities = [f"task:{sample['task_id']}", f"content:{sample['sha256']}"]
    if sample.get("scene_id"):
        identities.append(f"scene:{sample['scene_id']}")
    return identities


def assign_splits(
    samples: list[dict],
    *,
    seed: int,
    validation_fraction: float,
    parent_manifest: dict | None = None,
) -> dict[str, str]:
    """Keep scene/content connected groups together and preserve ancestry."""
    roots = {sample["task_id"]: sample["task_id"] for sample in samples}

    def root(key):
        while roots[key] != key:
            roots[key] = roots[roots[key]]
            key = roots[key]
        return key

    seen = {}
    label_signatures = {}
    for sample in samples:
        task_id = sample["task_id"]
        signature = sample["labels"]
        sha = sample["sha256"]
        if sha in label_signatures and label_signatures[sha] != signature:
            raise TrainingDatasetError(
                "duplicate_labels_conflict", "相同图片存在相互冲突的标注"
            )
        label_signatures[sha] = signature
        identities = [f"content:{sha}"]
        if sample.get("scene_id"):
            identities.append(f"scene:{sample['scene_id']}")
        for identity in identities:
            if identity in seen:
                roots[root(task_id)] = root(seen[identity])
            seen[identity] = task_id
    groups: dict[str, list[dict]] = {}
    for sample in samples:
        groups.setdefault(root(sample["task_id"]), []).append(sample)
    if len(groups) < 2:
        raise TrainingDatasetError(
            "insufficient_groups", "至少需要两组互不重复的图片才能分开训练与验证"
        )
    history = split_history(parent_manifest)
    assignments = {}
    unassigned = []
    for key, group in groups.items():
        inherited = set()
        for sample in group:
            inherited.update(
                history[identity]
                for identity in sample_identities(sample)
                if identity in history
            )
        if len(inherited) > 1:
            raise TrainingDatasetError(
                "inherited_split_conflict",
                "重复素材或同一场景合并后跨越历史训练与验证分区",
            )
        if inherited:
            assignments[key] = inherited.pop()
        else:
            identity = ":".join(sorted(s["sha256"] for s in group))
            unassigned.append(
                (hashlib.sha256(f"{seed}:{identity}".encode()).hexdigest(), key)
            )
    desired_validation = max(
        1, min(len(groups) - 1, round(len(groups) * validation_fraction))
    )
    missing_validation = max(
        0, desired_validation - list(assignments.values()).count("val")
    )
    for index, (_, key) in enumerate(sorted(unassigned)):
        assignments[key] = "val" if index < missing_validation else "train"
    if set(assignments.values()) != {"train", "val"}:
        raise TrainingDatasetError(
            "empty_split", "继承历史分区后训练或验证为空，请选择更完整的数据范围"
        )
    return {s["task_id"]: assignments[root(s["task_id"])] for s in samples}


def build_training_package(
    snapshot: dict,
    media: list[dict],
    output: Path,
    *,
    parent_manifest: dict | None = None,
) -> dict:
    classes = snapshot["classes"]
    by_task = {sample["task_id"]: sample for sample in snapshot["samples"]}
    if len(media) != len(by_task) or {m["task_id"] for m in media} != set(by_task):
        raise TrainingDatasetError("incomplete_media", "冻结的素材尚未完整物化")
    samples = []
    for item in media:
        sample = by_task[item["task_id"]]
        samples.append(
            {
                **item,
                "scene_id": sample.get("scene_id"),
                "labels": detection_labels(sample["annotations"], classes),
            }
        )
    splits = assign_splits(
        samples,
        seed=snapshot["seed"],
        validation_fraction=snapshot["validation_fraction"],
        parent_manifest=parent_manifest,
    )
    # Preserve every grouping identity before deduplicating automatic inputs.
    history_manifest = {
        "split_history": split_history(parent_manifest),
        "samples": [
            {**sample, "split": splits[sample["task_id"]]} for sample in samples
        ],
    }
    history = split_history(history_manifest)
    if snapshot.get("deduplicate"):
        representatives = {}
        for sample in sorted(samples, key=lambda s: s["task_id"]):
            representatives.setdefault(sample["sha256"], sample)
        samples = list(representatives.values())
    manifest_samples = []
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
    ) as package:
        for sample in sorted(samples, key=lambda s: s["task_id"]):
            task_id = sample["task_id"]
            split = splits[task_id]
            image_path = f"images/{split}/{task_id}{sample['extension']}"
            label_path = f"labels/{split}/{task_id}.txt"
            package.write(sample["path"], image_path)
            package.writestr(label_path, sample["labels"])
            manifest_samples.append(
                {
                    "task_id": task_id,
                    "scene_id": sample.get("scene_id"),
                    "sha256": sample["sha256"],
                    "split": split,
                    "image_path": image_path,
                    "label_path": label_path,
                    "label_sha256": hashlib.sha256(
                        sample["labels"].encode()
                    ).hexdigest(),
                    "width": sample["width"],
                    "height": sample["height"],
                    "size_bytes": sample["size_bytes"],
                }
            )
        # No `path` key: Ultralytics resolves these relative to this YAML file.
        yaml = (
            "train: images/train\nval: images/val\nnames: "
            + json.dumps(dict(enumerate(classes)), ensure_ascii=False)
            + "\n"
        )
        package.writestr("data.yaml", yaml)
        package.writestr("classes.json", json.dumps(classes, ensure_ascii=False))
        manifest = {
            "schema": 1,
            "format": "ultralytics-detection",
            "classes": classes,
            "source_digest": content_digest(snapshot),
            "samples": manifest_samples,
            "split_history": history,
            "summary": {
                "samples": len(samples),
                "train": sum(s["split"] == "train" for s in manifest_samples),
                "val": sum(s["split"] == "val" for s in manifest_samples),
                "negatives": sum(not s["labels"] for s in samples),
            },
        }
        manifest["split_history"] = split_history(manifest)
        manifest["semantic_partitions"] = semantic_partitions(manifest)
        manifest["validation_digest"] = content_digest(
            {
                "classes": classes,
                "samples": [s for s in manifest_samples if s["split"] == "val"],
            }
        )
        manifest["content_digest"] = content_digest(manifest)
        package.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        )
    return manifest


def semantic_partitions(manifest: dict) -> dict:
    """Identify content without database IDs, ordering, paths or actor noise."""
    classes = manifest["classes"]
    partitions = {"train": {}, "val": {}}
    for sample in manifest["samples"]:
        values = partitions[sample["split"]]
        sha, label = sample["sha256"], sample["label_sha256"]
        if sha in values and values[sha] != label:
            raise TrainingDatasetError(
                "duplicate_labels_conflict", "相同图片存在相互冲突的标注"
            )
        values[sha] = label
    return {
        "classes_digest": content_digest(classes),
        "train": partitions["train"],
        "val": partitions["val"],
        "train_digest": content_digest(
            {"classes": classes, "samples": partitions["train"]}
        ),
        "validation_digest": content_digest(
            {"classes": classes, "samples": partitions["val"]}
        ),
    }
