"""Mask conversion and algebra keep storage failures outside input validation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import annotation_conversion, mask_mutation
from app.services.content_snapshot import ContentError
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    build_rle_reference,
)
from app.services.raster_object_storage import RasterObjectStorageError


RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 3]}
BOUNDARIES = (
    "conversion-store",
    "conversion-plan",
    "mutation-algebra",
    "mutation-overlap",
)


async def _exercise(monkeypatch, boundary, error):
    reference = build_rle_reference(RLE)
    source = SimpleNamespace(
        id=uuid.uuid4(), geometry={"type": "raster_mask", "mask": reference}
    )
    scope = SimpleNamespace(media="image", strict_non_overlap=True)
    if boundary.startswith("conversion"):
        service = annotation_conversion.AnnotationConversionService(SimpleNamespace())
        if boundary == "conversion-store":
            service._plan_rle_reference(RLE)
            monkeypatch.setattr(
                annotation_conversion, "store_coco_rle", AsyncMock(side_effect=error)
            )
            return await service._store_generated_rles({reference["object_key"]})
        monkeypatch.setattr(
            annotation_conversion, "load_coco_rle", AsyncMock(side_effect=error)
        )
        return await service._build_plan(
            [source],
            SimpleNamespace(target="bbox", scope="all", operation="replace"),
            width=3,
            height=2,
            media="image",
        )
    service = mask_mutation.MaskMutationService(SimpleNamespace())
    monkeypatch.setattr(mask_mutation, "load_coco_rle", AsyncMock(side_effect=error))
    if boundary == "mutation-algebra":
        return await service._validate_pixel_algebra(
            SimpleNamespace(scope=scope, operation="join_masks"), [source], {source.id}
        )
    return await service._assert_non_overlapping(
        scope, [source], {}, set(), [], {}, mask_mutation._AlgebraBudget()
    )


@pytest.mark.parametrize("boundary", BOUNDARIES)
@pytest.mark.parametrize(
    "kind,code,status",
    [
        ("owned", "raster_object_storage_unpinned", 409),
        ("owned", "raster_object_storage_unavailable", 503),
        ("gate", "storage_maintenance_busy", 409),
        ("gate", "storage_operation_unsettled", 503),
        ("contract", "mask_content_busy", 409),
    ],
)
async def test_mask_service_preserves_typed_storage_status_and_reason(
    monkeypatch, boundary, kind, code, status
):
    if kind == "contract":
        error = RasterMaskContractError(
            status_code=status,
            reason=code,
            message="Retry the mask operation",
            retryable=True,
        )
    else:
        error = (
            RasterObjectStorageError(code, status)
            if kind == "owned"
            else ContentError(code, status)
        )
        error.args = ("private provider credentials must not reach the response",)
    expected_type = (
        mask_mutation.MaskMutationError
        if boundary.startswith("mutation")
        else RasterMaskContractError
    )

    with pytest.raises(expected_type) as caught:
        await _exercise(monkeypatch, boundary, error)

    assert caught.value.status_code == status
    assert caught.value.detail["reason"] == code
    assert caught.value.detail["retryable"] is True
    assert "private provider" not in caught.value.detail["message"]
    if kind == "contract":
        assert caught.value.detail == error.detail


@pytest.mark.parametrize(
    "boundary,status,reason",
    [
        ("conversion-store", 503, "mask_storage_unavailable"),
        ("conversion-plan", 409, "source_mask_invalid"),
        ("mutation-algebra", 422, "invalid_geometry"),
        ("mutation-overlap", 422, "invalid_geometry"),
    ],
)
async def test_plain_validation_error_keeps_existing_service_contract(
    monkeypatch, boundary, status, reason
):
    expected_type = (
        mask_mutation.MaskMutationError
        if boundary.startswith("mutation")
        else annotation_conversion.AnnotationConversionError
    )
    with pytest.raises(expected_type) as caught:
        await _exercise(monkeypatch, boundary, ValueError("invalid mask input"))
    assert caught.value.status_code == status
    assert caught.value.detail["reason"] == reason
