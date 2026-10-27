"""Validate bounded portable instance masks before linking content-addressed objects."""

import json
import math
from dataclasses import replace

from aap_inference_contract.execution import ModelTaskResult, thaw_json

from training_contract.model_return import (
    SEGMENTATION_OUTPUT_CONTRACT,
    segmentation_identity,
    segmentation_runtime,
)

from app.schemas._jsonb_types import RasterMaskGeometry
from app.services.raster_mask_storage import (
    collect_mask_geometries,
    resolve_mask_reference_objects,
    store_mask_reference_objects,
)
from app.utils.raster_mask_rle import coco_rle_area

MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_MASK_PIXELS = 16_777_216
MAX_MASK_ELEMENTS = 128_000_000
MAX_INSTANCES = 300


def validate_portable_masks(result, meta):
    """Validate a complete, possibly enriched payload without remapping classes.

    Transport validates the original model response once. Pipeline enrichment can
    add vector shapes or remove instances, so persistence must validate the final
    object envelope without treating its positions as native instance indices.
    """
    objects = meta.get("mask_objects")
    geometries = collect_mask_geometries(result)
    if (
        not isinstance(objects, dict)
        or len(geometries) > MAX_INSTANCES
        or len(objects) > len(geometries)
        or any(geometry.get("type") != "raster_mask" for geometry in geometries)
    ):
        raise ValueError("Invalid portable image mask envelope")
    referenced = set()
    elements = 0
    for geometry in geometries:
        mask = RasterMaskGeometry.model_validate(geometry).mask
        height, width = mask.size
        pixels = height * width
        if pixels > MAX_MASK_PIXELS:
            raise ValueError("Segmentation dimensions or instance budget exceeded")
        elements += pixels
        referenced.add(mask.sha256)
    if elements > MAX_MASK_ELEMENTS:
        raise ValueError("Segmentation dimensions or instance budget exceeded")
    if set(objects) != referenced:
        raise ValueError("Missing or unreferenced segmentation mask objects")
    if (
        len(json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode())
        > MAX_METADATA_BYTES
    ):
        raise ValueError("Segmentation metadata exceeds the byte budget")
    resolved = resolve_mask_reference_objects(result, objects)
    if any(coco_rle_area(rle) <= 0 for _, rle in resolved):
        raise ValueError("Segmentation instances must have foreground pixels")
    return resolved


def runtime_for_reference(ref, framework_version=None):
    """Resolve only the frozen model/profile, never a response-provided version."""
    if "inference_runtime" in ref:
        from training_contract.inference_runtime import validate_inference_runtime

        identity = validate_inference_runtime(ref, ref["inference_runtime"])
        if framework_version is not None and identity["version"] != framework_version:
            raise ValueError(
                "Frozen segmentation inference and profile versions differ"
            )
        return identity["qualification"]
    runtime = ref.get("runtime_qualification")
    if (
        framework_version is not None
        and ref.get("framework_version") is not None
        and ref["framework_version"] != framework_version
    ):
        raise ValueError("Frozen segmentation model and profile versions differ")
    version = ref.get("framework_version") or framework_version
    if version is None and isinstance(runtime, dict):
        version = runtime.get("ultralytics")
    if version == "8.4.177":
        expected = segmentation_runtime(version)
    else:
        try:
            expected = segmentation_identity(
                ref["provenance"]["base_model"]["id"], version
            )["runtime_qualification"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Frozen segmentation model identity is unavailable"
            ) from exc
    if runtime != expected and not (runtime is None and version == "8.4.177"):
        raise ValueError("Unqualified frozen segmentation runtime")
    return expected


def validate_metadata(meta, ref):
    """Validate frozen identity/runtime and source dimensions without remapping shapes."""
    identity = {
        key: ref[key] for key in ("model_version_id", "sha256", "classes_digest")
    }
    runtime = runtime_for_reference(ref)
    segmentation = meta.get("segmentation")
    if meta.get("trained_model") != identity or not isinstance(segmentation, dict):
        raise ValueError("Segmentation model identity is missing or mismatched")
    expected = {
        "output_contract": SEGMENTATION_OUTPUT_CONTRACT,
        "decoder": runtime["decoder"],
        "mask_processing": runtime["mask_processing"],
        "runtime_qualification": runtime,
    }
    if any(segmentation.get(key) != value for key, value in expected.items()):
        raise ValueError("Unqualified segmentation decoder or runtime")
    dimensions = segmentation.get("dimensions")
    if (
        not isinstance(dimensions, list)
        or len(dimensions) != 2
        or any(type(n) is not int or not 0 < n <= 8192 for n in dimensions)
        or dimensions[0] * dimensions[1] > MAX_MASK_PIXELS
    ):
        raise ValueError("Segmentation dimensions or instance budget exceeded")
    return dimensions


def validate_results(results, ref):
    mapped_results = []
    for result in results:
        if not isinstance(result, ModelTaskResult):
            raise TypeError("Controlled model results must be immutable task results")
        meta = thaw_json(result.meta) or {}
        dimensions = validate_metadata(meta, ref)
        if (
            len(result.result) > MAX_INSTANCES
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
        mapped_results.append(
            replace(
                result,
                result=tuple(mapped),
                model_version=f"trained:{ref['model_version_id']}",
            )
        )
    return tuple(mapped_results)


async def materialize_masks(db, result, meta, *, task_id=None):
    """Caller owns the task row lock before taking content locks for predictions."""
    resolved = validate_portable_masks(result, meta)
    await store_mask_reference_objects(db, result, resolved, task_id=task_id)
