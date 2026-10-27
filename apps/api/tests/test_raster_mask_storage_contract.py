"""Storage failures must retain retry semantics through existing writer boundaries."""

from unittest.mock import AsyncMock

import pytest

from app.services import raster_mask_objects, raster_mask_storage
from app.services.content_snapshot import ContentError
from app.services.raster_object_storage import RasterObjectStorageError


@pytest.mark.parametrize(
    "failure,status,reason",
    [
        (RasterObjectStorageError("missing"), 409, "raster_object_storage_missing"),
        (
            RasterObjectStorageError("unavailable"),
            503,
            "raster_object_storage_unavailable",
        ),
        (
            ContentError("storage_maintenance_busy", 409),
            409,
            "storage_maintenance_busy",
        ),
    ],
)
@pytest.mark.parametrize("boundary", ["store", "verify", "bind"])
async def test_storage_failures_preserve_typed_writer_contract(
    monkeypatch, failure, status, reason, boundary
):
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}
    reference = raster_mask_storage.build_rle_reference(rle)
    if boundary == "store":
        monkeypatch.setattr(
            raster_mask_objects, "store", AsyncMock(side_effect=failure)
        )
        action = raster_mask_storage.store_coco_rle(rle)
    elif boundary == "verify":
        monkeypatch.setattr(
            raster_mask_storage, "load_coco_rle", AsyncMock(side_effect=failure)
        )
        action = raster_mask_storage._verify_mask_reference_content(
            reference,
            key=reference["object_key"],
            value=reference,
            require_raster_foreground=False,
        )
    else:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        result = MagicMock()
        result.scalar_one.return_value = True
        db = SimpleNamespace(execute=AsyncMock(return_value=result))
        monkeypatch.setattr(
            raster_mask_storage, "load_coco_rle", AsyncMock(return_value=rle)
        )
        monkeypatch.setattr(
            raster_mask_objects, "bind_publications", AsyncMock(side_effect=failure)
        )
        action = raster_mask_storage.lock_raster_mask_references(db, reference)
    with pytest.raises(raster_mask_storage.RasterMaskContractError) as caught:
        await action
    assert caught.value.status_code == status
    assert caught.value.detail["reason"] == reason
    assert caught.value.detail["retryable"] is True
    assert caught.value.__cause__ is failure
