"""Strict, deterministic image detection dataset packaging."""

from __future__ import annotations

import hashlib
import json
import math
import zipfile
from copy import deepcopy
from pathlib import Path
from aap_task_contract import normalize_task
from app.services import storage_access
from training_contract.dataset import (
    DETECTION_CONTRACT,
    DatasetContractError,
    LEGACY_SEGMENTATION_CONTRACT,
    SEGMENTATION,
    SEGMENTATION_CONTRACT,
    data_contract,
    segmentation_labels,
    INSTANCE_RLE_CONTRACT,
    INSTANCE_RLE_LIMITS,
    is_instance_rle,
    instance_rle_budget,
    instance_rle_evidence,
    polygon_area,
)
from training_contract.lineage import validate_source_lineage


class TrainingDatasetError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def contract_of(record):
    try:
        validate_source_lineage(record)
        return data_contract(record)
    except DatasetContractError as exc:
        raise TrainingDatasetError(exc.code, str(exc)) from exc


def native_labels(annotations, classes, contract):
    if normalize_task(contract["task"]) == SEGMENTATION:
        try:
            return segmentation_labels(annotations, classes)
        except DatasetContractError as exc:
            raise TrainingDatasetError(exc.code, str(exc)) from exc
    return detection_labels(annotations, classes), None


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


def attach_asset_sources(snapshot: dict, manifest: dict) -> None:
    """Add an explicit source contract only for newly frozen reference inputs."""
    if not any(
        key in snapshot for key in ("asset_source_contract", "source_lineage_contract")
    ):
        return
    for key in ("asset_source_contract", "source_lineage_contract"):
        if key in snapshot:
            manifest[key] = deepcopy(snapshot[key])
    sources = {s["task_id"]: s for s in snapshot["samples"]}
    for member in manifest["samples"]:
        source = sources[member["task_id"]]
        if source.get("asset_reference"):
            member["asset_reference"] = deepcopy(source["asset_reference"])
        if "source_lineage" in source:
            member["source_lineage"] = deepcopy(source["source_lineage"])
    validate_source_lineage(manifest)


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
    contract = contract_of(snapshot)
    if is_instance_rle(contract):
        try:
            return build_instance_rle_package(
                snapshot, media, output, parent_manifest=parent_manifest
            )
        except DatasetContractError as exc:
            raise TrainingDatasetError(exc.code, str(exc)) from exc
    segmentation = normalize_task(contract["task"]) == SEGMENTATION
    if parent_manifest is not None and normalize_task(
        contract_of(parent_manifest)["task"]
    ) != normalize_task(contract["task"]):
        raise TrainingDatasetError(
            "task_mismatch", "父数据版本与当前训练任务或几何身份不同"
        )
    samples = []
    for item in media:
        sample = by_task[item["task_id"]]
        labels, instances = native_labels(sample["annotations"], classes, contract)
        samples.append(
            {
                **item,
                "scene_id": sample.get("scene_id"),
                "labels": labels,
                **({"instances": instances} if segmentation else {}),
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
    with storage_access.closing_registered(
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1)
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
                    **({"instances": sample["instances"]} if segmentation else {}),
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
        if contract["schema"] == 3:
            manifest.update(contract)
            manifest["annotation_scope"] = snapshot["annotation_scope"]
            manifest["source"] = snapshot["source"]
            manifest["summary"]["objects"] = sum(
                len(s["labels"].splitlines()) for s in samples
            )
            manifest["summary"]["classes"] = len(classes)
        elif segmentation:
            manifest.update(**contract, format="ultralytics-segmentation")
        attach_asset_sources(snapshot, manifest)
        manifest["split_history"] = split_history(manifest)
        manifest["semantic_partitions"] = semantic_partitions(manifest)
        manifest["validation_digest"] = content_digest(
            {
                "classes": classes,
                "samples": [s for s in manifest_samples if s["split"] == "val"],
                **(
                    {"data_contract": contract}
                    if segmentation or contract["schema"] == 3
                    else {}
                ),
            }
        )
        manifest["content_digest"] = content_digest(manifest)
        package.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        )
    return manifest


def semantic_partition_aliases(manifest: dict) -> list[dict]:
    """Compare qualified equivalent contracts without changing frozen evidence."""
    contract = contract_of(manifest)
    contracts = (
        [INSTANCE_RLE_CONTRACT]
        if is_instance_rle(contract)
        else [LEGACY_SEGMENTATION_CONTRACT, SEGMENTATION_CONTRACT]
        if normalize_task(contract["task"]) == SEGMENTATION
        else [{"schema": 1}, DETECTION_CONTRACT]
    )
    return [
        semantic_partitions(
            {"classes": manifest["classes"], "samples": manifest["samples"], **value}
        )
        for value in contracts
    ]


def semantic_partitions(manifest: dict) -> dict:
    """Identify content without database IDs, ordering, paths or actor noise."""
    classes = manifest["classes"]
    contract = contract_of(manifest)
    identity = (
        {"data_contract": contract}
        if normalize_task(contract["task"]) == SEGMENTATION or contract["schema"] == 3
        else {}
    )
    partitions = {"train": {}, "val": {}}
    for sample in manifest["samples"]:
        values = partitions[sample["split"]]
        sha, label = (
            sample["sha256"],
            sample.get("semantic_label_sha256", sample["label_sha256"]),
        )
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
            {"classes": classes, "samples": partitions["train"], **identity}
        ),
        "validation_digest": content_digest(
            {"classes": classes, "samples": partitions["val"], **identity}
        ),
    }


def instance_rle_payload(sample, classes, width, height, *, load_rle=None):
    """Materialize only frozen sources, one instance at a time, never contours."""
    from app.services.mask_conversion import rasterize_region_geometry
    from app.services.raster_mask_storage import load_coco_rle_sync
    from training_contract.dataset import instance_mask_annotations

    annotations = sample["annotations"]
    instance_mask_annotations(annotations, classes, width, height)
    load_rle = load_rle or load_coco_rle_sync
    instances, total_bytes = [], 0
    for annotation in sorted(annotations, key=lambda a: a["id"]):
        geometry = annotation["geometry"]
        polygon = geometry["type"] == "polygon"
        if polygon:
            rle = rasterize_region_geometry(geometry, width, height)
        else:
            rle = load_rle(geometry["mask"])
        evidence = instance_rle_evidence(rle)
        if evidence["size"] != [height, width]:
            raise TrainingDatasetError("mask_dimensions", "冻结掩码与原图尺寸不一致")
        total_bytes += evidence["rle_bytes"]
        instance_rle_budget(width, height, len(annotations), total_bytes)
        instances.append(
            {
                "annotation_id": annotation["id"],
                "annotation_version": annotation["version"],
                "class_index": classes.index(annotation["class_name"]),
                "source_geometry": geometry["type"],
                "source_object_sha256": None if polygon else geometry["mask"]["sha256"],
                **(
                    {
                        "source_polygon": geometry["points"],
                        "rasterizer": "aap-pixel-center-even-odd-1",
                        "source_vector_area": polygon_area(
                            [tuple(p) for p in geometry["points"]]
                        ),
                        "raster_area_fraction": evidence["area_pixels"]
                        / (width * height),
                    }
                    if polygon
                    else {}
                ),
                **evidence,
                "rle": rle,
            }
        )
    return {"schema": 1, "size": [height, width], "instances": instances}


def instance_rle_text(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def instance_rle_semantic(instances):
    return content_digest(
        sorted(
            (instance["class_index"], instance["pixel_sha256"])
            for instance in instances
        )
    )


def build_instance_rle_package(snapshot, media, output, *, parent_manifest=None):
    """Spool encoded labels instead of retaining the whole dataset in RAM."""
    from app.services.training_dataset_originals import temporary_directory

    with temporary_directory(prefix="aap-training-rle-") as temporary:
        return _build_instance_rle_package(
            snapshot, media, output, Path(temporary), parent_manifest=parent_manifest
        )


def _build_instance_rle_package(
    snapshot, media, output, labels_directory, *, parent_manifest=None
):
    """Keep a separate schema and consumer; cross-bridge ancestry is forbidden."""
    contract, classes = contract_of(snapshot), snapshot["classes"]
    if parent_manifest is not None and contract_of(parent_manifest) != contract:
        raise TrainingDatasetError(
            "data_contract_mismatch", "切换桥接必须独立核验历史分区，不能重随机划分祖先"
        )
    sources = {sample["task_id"]: sample for sample in snapshot["samples"]}
    if len(media) != len(sources) or {item["task_id"] for item in media} != set(
        sources
    ):
        raise TrainingDatasetError("incomplete_media", "冻结的素材尚未完整物化")
    samples = []
    for item in media:
        source = sources[item["task_id"]]
        payload_path = item.get("mask_path")
        payload = (
            json.loads(Path(payload_path).read_text())
            if payload_path
            else instance_rle_payload(source, classes, item["width"], item["height"])
        )
        text = instance_rle_text(payload)
        if len(text.encode()) > INSTANCE_RLE_LIMITS["max_sample_label_bytes"]:
            raise TrainingDatasetError(
                "mask_label_limit", "单样本含来源信息的掩码标签超过 32 MiB"
            )
        instances = [
            {key: value for key, value in instance.items() if key != "rle"}
            for instance in payload["instances"]
        ]
        label_file = labels_directory / f"{item['task_id']}.json"
        with storage_access.closing_registered(label_file.open("w")) as target:
            target.write(text)
        label_size = len(text.encode())
        label_sha256 = hashlib.sha256(text.encode()).hexdigest()
        del text, payload
        samples.append(
            {
                **item,
                "scene_id": source.get("scene_id"),
                "labels": instance_rle_semantic(instances),
                "label_file": label_file,
                "label_bytes": label_size,
                "label_sha256": label_sha256,
                "instances": instances,
            }
        )
    if (
        sum(item["size_bytes"] + item["label_bytes"] for item in samples)
        > INSTANCE_RLE_LIMITS["max_dataset_bytes"]
    ):
        raise TrainingDatasetError(
            "dataset_size_limit", "原图与掩码训练对象总量超过 20 GiB"
        )
    splits = assign_splits(
        samples,
        seed=snapshot["seed"],
        validation_fraction=snapshot["validation_fraction"],
        parent_manifest=parent_manifest,
    )
    history = split_history(
        {
            "split_history": split_history(parent_manifest),
            "samples": [{**item, "split": splits[item["task_id"]]} for item in samples],
        }
    )
    if snapshot.get("deduplicate"):
        representatives = {}
        for item in sorted(samples, key=lambda item: item["task_id"]):
            representatives.setdefault(item["sha256"], item)
        samples = list(representatives.values())
    members = []
    with storage_access.closing_registered(
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1)
    ) as package:
        for item in sorted(samples, key=lambda item: item["task_id"]):
            task_id, split = item["task_id"], splits[item["task_id"]]
            image_path = f"images/{split}/{task_id}{item['extension']}"
            label_path = f"labels/{split}/{task_id}.json"
            package.write(item["path"], image_path)
            package.write(item["label_file"], label_path)
            members.append(
                {
                    "task_id": task_id,
                    "scene_id": item.get("scene_id"),
                    "sha256": item["sha256"],
                    "split": split,
                    "image_path": image_path,
                    "label_path": label_path,
                    "label_sha256": item["label_sha256"],
                    "semantic_label_sha256": item["labels"],
                    "width": item["width"],
                    "height": item["height"],
                    "size_bytes": item["size_bytes"],
                    "instances": item["instances"],
                }
            )
        package.writestr(
            "data.yaml",
            "train: images/train\nval: images/val\nnames: "
            + json.dumps(dict(enumerate(classes)), ensure_ascii=False)
            + "\n",
        )
        package.writestr("classes.json", json.dumps(classes, ensure_ascii=False))
        manifest = {
            **contract,
            "classes": classes,
            "source_digest": content_digest(snapshot),
            "annotation_scope": snapshot["annotation_scope"],
            "source": snapshot["source"],
            "samples": members,
            "split_history": history,
            "mask_limits": dict(INSTANCE_RLE_LIMITS),
            "summary": {
                "samples": len(members),
                "train": sum(item["split"] == "train" for item in members),
                "val": sum(item["split"] == "val" for item in members),
                "negatives": sum(not item["instances"] for item in members),
                "objects": sum(len(item["instances"]) for item in members),
                "classes": len(classes),
            },
        }
        attach_asset_sources(snapshot, manifest)
        manifest["semantic_partitions"] = semantic_partitions(manifest)
        manifest["validation_digest"] = content_digest(
            {
                "classes": classes,
                "data_contract": contract,
                "samples": [item for item in members if item["split"] == "val"],
            }
        )
        manifest["content_digest"] = content_digest(manifest)
        package.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        )
    if output.stat().st_size > INSTANCE_RLE_LIMITS["max_package_bytes"]:
        raise TrainingDatasetError("package_size_limit", "训练压缩包超过 20 GiB")
    return manifest
