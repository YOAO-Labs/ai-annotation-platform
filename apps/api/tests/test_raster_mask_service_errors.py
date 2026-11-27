"""Mask conversion and algebra keep storage failures outside input validation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import annotation_conversion, mask_mutation
from app.services.content_snapshot import ContentError
from app.services.mask_mutation_contract import MaskMutationError
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    build_rle_reference,
)
from app.services.raster_mask_storage import store_coco_rle
from app.services.storage_access import failure_unsettled, mark_unsettled
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
    if isinstance(error, ValueError):
        monkeypatch.setattr(
            mask_mutation,
            "coco_rle_bbox_norm",
            lambda _rle: (_ for _ in ()).throw(error),
        )
        return await service._assert_non_overlapping(
            scope,
            [source],
            {},
            set(),
            [],
            {reference["object_key"]: RLE},
            mask_mutation._AlgebraBudget(),
        )
    from app.services import raster_mask_storage

    monkeypatch.setattr(
        raster_mask_storage,
        "read_mask_write_epochs",
        AsyncMock(return_value={reference["object_key"]: 1}),
    )
    monkeypatch.setattr(
        raster_mask_storage, "load_coco_rle", AsyncMock(side_effect=error)
    )
    try:
        return await raster_mask_storage.verify_mask_payload_content(
            [source.geometry], db=SimpleNamespace(), reference_value=[reference]
        )
    except RasterMaskContractError as exc:
        mask_mutation._raise_mask_mutation_contract_error(exc)


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
        MaskMutationError
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
        MaskMutationError
        if boundary.startswith("mutation")
        else annotation_conversion.AnnotationConversionError
    )
    with pytest.raises(expected_type) as caught:
        await _exercise(monkeypatch, boundary, ValueError("invalid mask input"))
    assert caught.value.status_code == status
    assert caught.value.detail["reason"] == reason


async def test_unsettled_owner_failure_remains_unsettled_at_store_boundary(monkeypatch):
    from app.services import raster_mask_objects

    failure = mark_unsettled(RasterObjectStorageError("unavailable"))
    monkeypatch.setattr(raster_mask_objects, "store", AsyncMock(side_effect=failure))
    with pytest.raises(RasterMaskContractError) as caught:
        await store_coco_rle(RLE)
    assert caught.value.status_code == 503
    assert failure_unsettled(caught.value)
    assert caught.value.__cause__ is failure


def test_raster_physical_close_preserves_original_failure_and_uncertainty(monkeypatch):
    from app.services import raster_object_storage as physical

    failure = RuntimeError("physical read failed")

    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            raise OSError("physical close failed")

    location_id = uuid.uuid4()
    target = SimpleNamespace(
        location_id=location_id,
        endpoint_url="http://localhost",
        bucket="fixture",
        key_prefix="",
        client=lambda **_kwargs: Context(),
    )
    owner = SimpleNamespace(
        bucket="fixture",
        ref={
            "location_id": str(location_id),
            "endpoint_url": "http://localhost",
            "key_prefix": "",
        },
    )
    monkeypatch.setattr(physical, "resolve", lambda _record: owner)

    def read_fails(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(physical, "read", read_fails)
    with pytest.raises(BaseExceptionGroup) as caught:
        physical.run_io(target, None, "read", object())
    assert caught.value.exceptions[0] is failure
    assert caught.value.exceptions[1].code == "raster_object_storage_unavailable"
    assert failure_unsettled(caught.value)
