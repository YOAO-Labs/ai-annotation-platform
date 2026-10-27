"""Original-pixel mask identity and rejection at the API trust boundary."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest
from aap_inference_contract.execution import ModelTaskResult, thaw_json
from training_contract.model_return import SEGMENTATION_RUNTIME, segmentation_identity

from aap_inference.execution_context import build_predict_context
from app.services import trained_models
from app.services.raster_mask_storage import canonical_rle_bytes
from app.utils.raster_mask_rle import decode_coco_rle, encode_coco_rle


def example():
    # A ring with a separate foreground part remains one instance.
    pixels = [1, 1, 1, 0, 0, 1, 0, 1, 0, 0, 1, 1, 1, 0, 1]
    rle = encode_coco_rle(pixels, 5, 3)
    data = canonical_rle_bytes(rle)
    sha = hashlib.sha256(data).hexdigest()
    ref = {
        "task": "segmentation",
        "model_version_id": "candidate",
        "sha256": "a" * 64,
        "classes_digest": "b" * 64,
        "classes": ["native"],
        "bindings": ["项目实例"],
        **segmentation_identity("yolo11n-seg"),
    }
    result = SimpleNamespace(
        task_id="image",
        result=[
            {
                "tool_unit_id": "region",
                "class_name": "native",
                "native_class_index": 0,
                "instance_index": 0,
                "score": 0.6,
                "confidence": 0.6,
                "geometry": {
                    "type": "raster_mask",
                    "mask": {
                        "encoding": "coco_rle_ref",
                        "size": [3, 5],
                        "sha256": sha,
                        "object_key": f"raster-masks/sha256/{sha[:2]}/{sha[2:4]}/{sha}.json",
                        "bytes": len(data),
                        "runs": len(rle["counts"]),
                    },
                },
            }
        ],
        meta={
            "trained_model": {
                key: ref[key]
                for key in ("model_version_id", "sha256", "classes_digest")
            },
            "mask_objects": {sha: rle},
            "segmentation": {
                "output_contract": ref["output_contract"],
                "dimensions": [3, 5],
                "runtime_qualification": SEGMENTATION_RUNTIME,
                "mask_processing": SEGMENTATION_RUNTIME["mask_processing"],
                "decoder": SEGMENTATION_RUNTIME["decoder"],
            },
        },
    )
    return result, ref, pixels


def test_original_mask_pixels_and_region_binding_survive_validation():
    result, ref, pixels = example()
    original = deepcopy(result.meta["mask_objects"])
    result = trained_models.validate_and_map_results(
        (ModelTaskResult(**vars(result)),), ref
    )[0]
    assert result.result[0]["class_name"] == "项目实例"
    assert result.result[0]["geometry"]["type"] == "raster_mask"
    assert thaw_json(result.meta["mask_objects"]) == original
    assert list(decode_coco_rle(next(iter(original.values())))) == pixels
    assert result.model_version == "trained:candidate"
    context = build_predict_context(
        prompt=None,
        output_mode="mask",
        params={},
        model_id="trained:candidate",
        frozen_model_ref=ref,
    )
    assert context["task"] == trained_models.wire_context(ref)["task"] == "segment"
    with pytest.raises(ValueError):
        build_predict_context(
            prompt=None,
            output_mode="mask",
            params={},
            model_id="trained:candidate",
            task="detect",
            frozen_model_ref=ref,
        )


@pytest.mark.parametrize(
    "fault",
    [
        "identity",
        "class",
        "index",
        "confidence",
        "dimensions",
        "missing",
        "digest",
        "decoder",
        "extra",
        "polygon",
    ],
)
def test_reject_misaligned_or_unqualified_masks(fault):
    result, ref, _ = example()
    item = result.result[0]
    if fault == "identity":
        result.meta["trained_model"]["sha256"] = "c" * 64
    elif fault == "class":
        item["class_name"] = "other"
    elif fault == "index":
        item["instance_index"] = True
    elif fault == "confidence":
        item["score"] = float("nan")
    elif fault == "dimensions":
        result.meta["segmentation"]["dimensions"] = [5, 3]
    elif fault == "missing":
        result.meta["mask_objects"] = {}
    elif fault == "digest":
        next(iter(result.meta["mask_objects"].values()))["counts"] = [0, 15]
    elif fault == "decoder":
        result.meta["segmentation"]["decoder"] = "contours"
    elif fault == "extra":
        result.meta["mask_objects"]["c" * 64] = {}
    else:
        item["geometry"] = {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]}
    with pytest.raises(ValueError):
        trained_models.validate_and_map_results((ModelTaskResult(**vars(result)),), ref)


@pytest.mark.parametrize("trained", ["8.4.177", "8.4.183"])
def test_segmentation_metadata_uses_pinned_inference_not_training_runtime(trained):
    from training_contract.inference_runtime import inference_runtime
    from app.services.controlled_segmentation import runtime_for_reference

    result, ref, _pixels = example()
    ref.update(framework="ultralytics", framework_version=trained)
    ref["inference_runtime"] = inference_runtime(ref, "8.4.183")
    actual = ref["inference_runtime"]["qualification"]
    result.meta["inference_runtime"] = ref["inference_runtime"]
    result.meta["segmentation"].update(
        runtime_qualification=actual,
        decoder=actual["decoder"],
        mask_processing=actual["mask_processing"],
    )
    assert runtime_for_reference(ref, "8.4.183") == actual
    trained_models.validate_and_map_results((ModelTaskResult(**vars(result)),), ref)
    with pytest.raises(ValueError):
        runtime_for_reference(ref, "8.4.177")
