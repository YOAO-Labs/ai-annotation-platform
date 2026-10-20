"""Validate bounded portable instance masks before linking content-addressed objects."""

import json
import math

from training_contract.model_return import (
    SEGMENTATION_OUTPUT_CONTRACT,
    SEGMENTATION_RUNTIME,
)

from app.schemas._jsonb_types import RasterMaskGeometry
from app.services.raster_mask_storage import (
    resolve_mask_reference_objects,
    store_mask_reference_objects,
)
from app.utils.raster_mask_rle import coco_rle_area

MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_MASK_PIXELS = 16_777_216
MAX_MASK_ELEMENTS = 128_000_000
MAX_INSTANCES = 300


def validate_results(results, ref):
    identity = {
        key: ref[key] for key in ("model_version_id", "sha256", "classes_digest")
    }
    for result in results:
        meta = result.meta or {}
        segmentation = meta.get("segmentation")
        if meta.get("trained_model") != identity or not isinstance(segmentation, dict):
            raise ValueError("Segmentation model identity is missing or mismatched")
        expected = {
            "output_contract": SEGMENTATION_OUTPUT_CONTRACT,
            "decoder": SEGMENTATION_RUNTIME["decoder"],
            "mask_processing": SEGMENTATION_RUNTIME["mask_processing"],
            "runtime_qualification": SEGMENTATION_RUNTIME,
        }
        if any(segmentation.get(key) != value for key, value in expected.items()):
            raise ValueError("Unqualified segmentation decoder or runtime")
        dimensions = segmentation.get("dimensions")
        if (
            not isinstance(dimensions, list)
            or len(dimensions) != 2
            or any(type(n) is not int or not 0 < n <= 8192 for n in dimensions)
            or dimensions[0] * dimensions[1] > MAX_MASK_PIXELS
            or len(result.result) > MAX_INSTANCES
            or dimensions[0] * dimensions[1] * len(result.result) > MAX_MASK_ELEMENTS
        ):
            raise ValueError("Segmentation dimensions or instance budget exceeded")
        objects = meta.get("mask_objects")
        if not isinstance(objects, dict) or len(objects) > len(result.result):
            raise ValueError("Invalid segmentation mask object envelope")
        if (
            len(json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode())
            > MAX_METADATA_BYTES
        ):
            raise ValueError("Segmentation metadata exceeds the byte budget")
        mapped, referenced = [], set()
        for instance_index, item in enumerate(result.result):
            native = item.get("native_class_index")
            score = item.get("score")
            if (
                type(native) is not int
                or not 0 <= native < len(ref["classes"])
                or item.get("class_name") != ref["classes"][native]
                or item.get("tool_unit_id") != "region"
                or type(item.get("instance_index")) is not int
                or item["instance_index"] != instance_index
                or type(score) not in {int, float}
                or not math.isfinite(score)
                or not 0 <= score <= 1
                or item.get("confidence") != score
            ):
                raise ValueError(
                    "Segmentation class, instance or confidence is misaligned"
                )
            geometry = RasterMaskGeometry.model_validate(
                item.get("geometry")
            ).model_dump(mode="json", exclude_none=True)
            if geometry["mask"]["size"] != dimensions:
                raise ValueError("Segmentation mask size differs from the source")
            referenced.add(geometry["mask"]["sha256"])
            mapped.append(
                {**item, "class_name": ref["bindings"][native], "geometry": geometry}
            )
        if set(objects) != referenced:
            raise ValueError("Missing or unreferenced segmentation mask objects")
        resolved = resolve_mask_reference_objects(mapped, objects)
        if any(coco_rle_area(rle) <= 0 for _, rle in resolved):
            raise ValueError("Segmentation instances must have foreground pixels")
        result.result = mapped
        result.model_version = f"trained:{ref['model_version_id']}"


async def materialize_masks(db, result, meta, *, task_id=None):
    """Caller owns the task row lock before taking content locks for predictions."""
    resolved = resolve_mask_reference_objects(result, meta["mask_objects"])
    await store_mask_reference_objects(db, result, resolved, task_id=task_id)
