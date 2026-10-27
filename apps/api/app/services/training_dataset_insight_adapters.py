"""Qualified frozen insight adapters; dispatch never follows task alone."""

from __future__ import annotations

from dataclasses import dataclass
import math

from aap_task_contract import normalize_task
from training_contract.dataset import (
    DETECTION_CONTRACT,
    SEGMENTATION_CONTRACT,
    INSTANCE_RLE_CONTRACT,
    is_instance_rle,
    polygon_area,
)

from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    contract_of,
    native_labels,
    split_history,
)

OPERATIONS = (
    "create_version",
    "qualified_training",
    "overview_statistics",
    "task_statistics",
    "sample_preview",
    "semantic_comparison",
)
PROFILE_FIELDS = ("task", "media_type", "format", "geometry", "bridge")


def contract_profile(record: dict) -> dict[str, str]:
    """Read accepted legacy equivalents without mutating their stored identity."""
    contract = contract_of(record)
    task = normalize_task(contract["task"])
    canonical = (
        INSTANCE_RLE_CONTRACT
        if is_instance_rle(contract)
        else DETECTION_CONTRACT
        if task == "detect"
        else SEGMENTATION_CONTRACT
    )
    # Historical schemas omitted media. An explicit different output medium is
    # not an image adapter merely because the legacy task spelling is familiar.
    if record.get("media_type", "image") != "image":
        raise TrainingDatasetError(
            "unsupported_insight_contract", "该媒体没有经过资格验证的冻结分析适配器"
        )
    return {key: canonical[key] for key in PROFILE_FIELDS}


def unsupported_capabilities(reason: str) -> dict:
    return {key: {"supported": False, "reason": reason} for key in OPERATIONS}


@dataclass(frozen=True)
class FrozenInsightAdapter:
    id: str
    revision: int
    profile: tuple[str, ...]

    @property
    def task(self) -> str:
        return self.profile[0]

    def capabilities(self, record: dict | None = None) -> dict:
        # These are data-contract qualifications, not claims that preview bytes
        # are currently retained or that every trainer/weight supports the task.
        result = {key: {"supported": True, "reason": None} for key in OPERATIONS}
        source = (record or {}).get("source") or {}
        if not isinstance(source, dict) or source.get("media_type", "image") != "image":
            for key in ("create_version", "qualified_training"):
                result[key] = {
                    "supported": False,
                    "reason": "当前仅资格验证图片项目物化；该来源媒体需要独立资格验证",
                }
        return result

    def labels(self, annotations: list, classes: list, contract: dict) -> tuple:
        text, instances = native_labels(annotations, classes, contract)
        labels = []
        for number, line in enumerate(text.splitlines()):
            values = line.split()
            index = int(values[0])
            label = {"class_index": index, "class_name": classes[index]}
            if self.task == "segment":
                coords = list(map(float, values[1:]))
                points = [coords[i : i + 2] for i in range(0, len(coords), 2)]
                label.update(
                    type="polygon",
                    points=points,
                    area=polygon_area(points),
                    annotation_id=instances[number]["annotation_id"],
                    annotation_version=instances[number]["annotation_version"],
                )
            else:
                x, y, width, height = map(float, values[1:])
                label.update(
                    type="bbox", x_center=x, y_center=y, width=width, height=height
                )
            labels.append(label)
        return text, instances, labels

    def mask_labels(self, annotations, classes, member):
        mapping = member.get("instances")
        if not isinstance(mapping, list) or len(mapping) != len(annotations):
            raise TrainingDatasetError(
                "frozen_instances_mismatch", "原生掩码来源实例不完整"
            )
        labels = []
        for annotation, instance in zip(
            sorted(annotations, key=lambda item: item["id"]), mapping, strict=True
        ):
            geometry = annotation["geometry"]
            if (
                instance["annotation_id"] != annotation["id"]
                or instance["annotation_version"] != annotation["version"]
                or instance["class_index"] != classes.index(annotation["class_name"])
                or instance["source_geometry"] != geometry["type"]
                or geometry["type"] == "raster_mask"
                and instance["source_object_sha256"] != geometry["mask"]["sha256"]
                or geometry["type"] == "polygon"
                and instance["source_polygon"] != geometry["points"]
            ):
                raise TrainingDatasetError(
                    "frozen_instances_mismatch", "掩码来源版本与训练包不一致"
                )
            height, width = instance["size"]
            labels.append(
                {
                    "type": "instance_mask",
                    "class_index": instance["class_index"],
                    "class_name": classes[instance["class_index"]],
                    "annotation_id": instance["annotation_id"],
                    "annotation_version": instance["annotation_version"],
                    "area": instance["area_pixels"] / (width * height),
                    **{
                        key: instance[key]
                        for key in ("area_pixels", "bbox_xyxy", "pixel_sha256", "size")
                    },
                }
            )
        return mapping, labels

    def semantic_fingerprint(self, labels: list[dict]) -> str:
        if self.profile[-1] == INSTANCE_RLE_CONTRACT["bridge"]:
            return content_digest(
                {
                    "data_contract": dict(
                        zip(PROFILE_FIELDS, self.profile, strict=True)
                    ),
                    "labels": sorted(
                        (label["class_name"], label["pixel_sha256"]) for label in labels
                    ),
                }
            )
        if self.task == "segment":
            return content_digest(
                {
                    "data_contract": dict(
                        zip(PROFILE_FIELDS, self.profile, strict=True)
                    ),
                    "labels": sorted(
                        (label["class_name"], label["points"]) for label in labels
                    ),
                }
            )
        # Native eight-decimal coordinates and multiplicity retain the existing
        # detection semantic identity. Provenance IDs/class positions are absent.
        return content_digest(
            sorted(
                (
                    label["class_name"],
                    label["x_center"],
                    label["y_center"],
                    label["width"],
                    label["height"],
                )
                for label in labels
            )
        )

    def statistics(self, members: list[dict]) -> dict:
        if self.profile[-1] == INSTANCE_RLE_CONTRACT["bridge"]:
            return {
                "task": "segment",
                "schema_version": 1,
                "geometry": "instance-mask",
                "known_instances": sum(row["instances"] or 0 for row in members),
                "polygon_area_sum": 0,
                "mask_area_pixels_sum": sum(
                    label["area_pixels"] for row in members for label in row["labels"]
                ),
                "normalized_mask_area_sum": sum(
                    label["area"] for row in members for label in row["labels"]
                ),
            }
        if self.task == "segment":
            return {
                "task": "segment",
                "schema_version": 1,
                "geometry": "normalized-simple-polygon",
                "known_instances": sum(row["instances"] or 0 for row in members),
                "polygon_area_sum": sum(
                    label["area"] for row in members for label in row["labels"]
                ),
            }
        return detection_statistics(members)

    def geometry_unknown_members(self, members: list[dict]) -> int:
        return sum(
            row["instances"] is None
            or self.task == "detect"
            and any(not valid_box(label) for label in row["labels"])
            for row in members
        )

    def assert_comparable(self, other: FrozenInsightAdapter) -> None:
        if self.task != other.task:
            raise TrainingDatasetError(
                "task_mismatch",
                "不同训练任务的数据版本不能作为同类数据比较",
                status_code=422,
            )
        if self.profile != other.profile:
            raise TrainingDatasetError(
                "data_contract_mismatch",
                "数据版本的媒体、几何或桥接契约不兼容",
                status_code=422,
            )


_ADAPTERS = {
    tuple(contract[key] for key in PROFILE_FIELDS): FrozenInsightAdapter(
        id=identifier,
        revision=1,
        profile=tuple(contract[key] for key in PROFILE_FIELDS),
    )
    for identifier, contract in (
        ("image-detection-bbox", DETECTION_CONTRACT),
        ("image-segmentation-simple-polygon", SEGMENTATION_CONTRACT),
        ("image-segmentation-instance-rle", INSTANCE_RLE_CONTRACT),
    )
}


def adapter_for_profile(profile: dict) -> FrozenInsightAdapter:
    adapter = _ADAPTERS.get(tuple(profile.get(key) for key in PROFILE_FIELDS))
    if adapter is None:
        raise TrainingDatasetError(
            "unsupported_insight_contract",
            "该任务、媒体与几何契约没有经过资格验证的分析适配器",
            status_code=422,
        )
    return adapter


def adapter_for(record: dict) -> FrozenInsightAdapter:
    return adapter_for_profile(contract_profile(record))


def adapter_cache_identity(record: dict) -> dict:
    try:
        adapter = adapter_for(record)
    except TrainingDatasetError as exc:
        return {"id": None, "revision": 1, "unavailable": exc.code}
    return {"id": adapter.id, "revision": adapter.revision}


def assert_parent_compatible(
    parent_snapshot: dict, parent_manifest: dict, proposed_contract: dict
) -> None:
    """Validate output qualification and the existing task/content/scene grouping."""
    parent = adapter_for(parent_manifest)
    proposed = adapter_for(proposed_contract)
    parent.assert_comparable(proposed)
    if adapter_for(parent_snapshot).profile != parent.profile:
        raise TrainingDatasetError(
            "parent_contract_mismatch", "父版本快照与清单的输出契约不一致"
        )
    source = parent_snapshot.get("source") or {}
    if not isinstance(source, dict) or source.get("media_type", "image") != "image":
        raise TrainingDatasetError(
            "parent_grouping_mismatch", "父版本的来源分组语义不支持当前图片任务继承"
        )
    grouping = parent_snapshot.get("split_grouping")
    if grouping is not None and grouping != "task-content-scene-1":
        raise TrainingDatasetError(
            "parent_grouping_mismatch", "父版本使用不同的来源分组契约"
        )
    # The bridge resolves all retained cumulative task/content/scene identities
    # and rejects cross-partition conflicts. Unknown namespaces must not silently
    # lose their constraints when a future source grouping is introduced.
    try:
        history = split_history(parent_manifest)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise TrainingDatasetError(
            "parent_grouping_mismatch", "父版本的素材或场景分组证据不完整"
        ) from exc
    for key, value in history.items():
        if (
            not isinstance(key, str)
            or not key.startswith(("task:", "content:", "scene:"))
            or not key.split(":", 1)[1]
            or value not in {"train", "val"}
        ):
            raise TrainingDatasetError(
                "parent_grouping_mismatch", "父版本的素材或场景分组证据不受支持"
            )


def _axis(edges: list[float], labels: list[str]) -> list[dict]:
    return [
        {
            "key": str(index),
            "label": label,
            "lower": edges[index],
            "upper": edges[index + 1] if index + 1 < len(edges) else None,
        }
        for index, label in enumerate(labels)
    ]


def _heatmap(axis: list[dict], unit: str, unknown: int) -> dict:
    return {
        "x_bins": axis,
        "y_bins": axis,
        "counts": [[0 for _ in axis] for _ in axis],
        "unit": unit,
        "population": 0,
        "unknown_members": unknown,
    }


def _add(heatmap: dict, x: int, y: int) -> None:
    heatmap["counts"][y][x] += 1
    heatmap["population"] += 1


def valid_box(label: dict) -> bool:
    return all(
        type(label.get(key)) in {int, float}
        and math.isfinite(label[key])
        and (0 <= label[key] <= 1 if key.endswith("center") else 0 < label[key] <= 1)
        for key in ("x_center", "y_center", "width", "height")
    )


def detection_statistics(members: list[dict]) -> dict:
    pixel_edges = [0, 256, 512, 1024, 2048, 4096]
    pixels = _axis(
        pixel_edges, ["<256", "256–511", "512–1023", "1024–2047", "2048–4095", "≥4096"]
    )
    normalized = [
        {
            "key": str(index),
            "label": f"{index / 20:.2f}–{(index + 1) / 20:.2f}",
            "lower": index / 20,
            "upper": (index + 1) / 20,
        }
        for index in range(20)
    ]
    unknown_labels = sum(row["instances"] is None for row in members)
    geometry_unknown = sum(
        row["instances"] is None or any(not valid_box(label) for label in row["labels"])
        for row in members
    )
    dimensions = _heatmap(
        pixels,
        "member_images",
        sum(not row["width"] or not row["height"] for row in members),
    )
    centers = _heatmap(normalized, "instances", geometry_unknown)
    boxes = _heatmap(normalized, "instances", geometry_unknown)
    density = [
        {"key": key, "label": label, "count": 0}
        for key, label in (
            ("0", "0"),
            ("1", "1"),
            ("2-5", "2–5"),
            ("6-10", "6–10"),
            ("11-20", "11–20"),
            ("21+", "21+"),
        )
    ]
    for row in members:
        if row["width"] and row["height"]:
            x, y = (
                sum(value >= edge for edge in pixel_edges[1:])
                for value in (row["width"], row["height"])
            )
            _add(dimensions, x, y)
        count = row["instances"]
        if count is None:
            continue
        density[
            0
            if count == 0
            else 1
            if count == 1
            else 2
            if count <= 5
            else 3
            if count <= 10
            else 4
            if count <= 20
            else 5
        ]["count"] += 1
        for label in row["labels"]:
            if not valid_box(label):
                continue
            _add(
                centers,
                min(19, int(label["x_center"] * 20)),
                min(19, int(label["y_center"] * 20)),
            )
            _add(
                boxes,
                min(19, int(label["width"] * 20)),
                min(19, int(label["height"] * 20)),
            )
    return {
        "task": "detect",
        "schema_version": 1,
        "image_dimensions": dimensions,
        "box_centers": centers,
        "box_dimensions": boxes,
        "objects_per_member": {
            "bins": density,
            "analyzed_members": len(members) - unknown_labels,
            "unknown_label_members": unknown_labels,
        },
    }
