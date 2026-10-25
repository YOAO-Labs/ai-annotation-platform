from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.observability.metrics import (
    RASTER_MASK_CONTENT_ERROR_REASONS,
    RASTER_MASK_CONTENT_ERRORS_TOTAL,
    RASTER_MASK_CONTENT_OPERATIONS_TOTAL,
)
from app.services.raster_mask_storage import (
    build_rle_reference,
    classify_raster_mask_content_error,
    load_coco_rle,
    lock_raster_mask_references,
    store_coco_rle,
)

RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}
EXPECTED_ERROR_REASONS = {
    "missing_object",
    "digest_mismatch",
    "size_mismatch",
    "run_mismatch",
    "byte_mismatch",
    "invalid_encoding",
    "invalid_payload",
    "storage_unavailable",
    "unknown",
}


def _counter_value(counter, **labels: str) -> float:
    return float(counter.labels(**labels)._value.get())


def _storage(*, get_error: Exception | None = None):
    storage = MagicMock()
    storage.bucket = "annotation-data"
    storage.verify_upload.return_value = None
    if get_error is not None:
        storage.client.get_object.side_effect = get_error
    return storage


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (ValueError("stored mask RLE object is missing"), "missing_object"),
        (ValueError("stored mask RLE digest mismatch"), "digest_mismatch"),
        (ValueError("stored mask RLE size mismatch"), "size_mismatch"),
        (ValueError("stored mask RLE run count mismatch"), "run_mismatch"),
        (ValueError("stored mask RLE byte count mismatch"), "byte_mismatch"),
        (ValueError("unsupported mask reference encoding"), "invalid_encoding"),
        (json.JSONDecodeError("bad", "{", 0), "invalid_payload"),
        (
            EndpointConnectionError(endpoint_url="http://minio:9000"),
            "storage_unavailable",
        ),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
def test_raster_mask_content_error_classification_is_fixed(exc, reason):
    assert RASTER_MASK_CONTENT_ERROR_REASONS == EXPECTED_ERROR_REASONS
    assert classify_raster_mask_content_error(exc) == reason


@pytest.mark.asyncio
async def test_content_operation_success_and_error_counter_deltas():
    store_before = _counter_value(
        RASTER_MASK_CONTENT_OPERATIONS_TOTAL,
        operation="store",
        outcome="success",
    )
    await store_coco_rle(RLE, storage=_storage())
    assert (
        _counter_value(
            RASTER_MASK_CONTENT_OPERATIONS_TOTAL,
            operation="store",
            outcome="success",
        )
        == store_before + 1
    )

    missing = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
        "GetObject",
    )
    storage = _storage(get_error=missing)
    load_before = _counter_value(
        RASTER_MASK_CONTENT_OPERATIONS_TOTAL,
        operation="load",
        outcome="error",
    )
    error_before = _counter_value(
        RASTER_MASK_CONTENT_ERRORS_TOTAL,
        operation="load",
        reason="missing_object",
    )

    with pytest.raises(ValueError, match="missing"):
        await load_coco_rle(build_rle_reference(RLE), storage=storage)

    assert (
        _counter_value(
            RASTER_MASK_CONTENT_OPERATIONS_TOTAL,
            operation="load",
            outcome="error",
        )
        == load_before + 1
    )
    assert (
        _counter_value(
            RASTER_MASK_CONTENT_ERRORS_TOTAL,
            operation="load",
            reason="missing_object",
        )
        == error_before + 1
    )


@pytest.mark.asyncio
async def test_verify_counts_once_without_nested_load(monkeypatch):
    import app.services.raster_mask_storage as storage_module

    reference = build_rle_reference(RLE)

    async def nested_observed_load(_reference):
        return await storage_module._observe_content_operation(
            "load",
            asyncio.sleep(0, result=RLE),
        )

    monkeypatch.setattr(storage_module, "load_coco_rle", nested_observed_load)
    db = SimpleNamespace(execute=AsyncMock())
    verify_before = _counter_value(
        RASTER_MASK_CONTENT_OPERATIONS_TOTAL,
        operation="verify",
        outcome="success",
    )
    load_before = _counter_value(
        RASTER_MASK_CONTENT_OPERATIONS_TOTAL,
        operation="load",
        outcome="success",
    )

    await lock_raster_mask_references(
        db,
        {"type": "raster_mask", "mask": reference},
        require_raster_foreground=True,
    )

    assert (
        _counter_value(
            RASTER_MASK_CONTENT_OPERATIONS_TOTAL,
            operation="verify",
            outcome="success",
        )
        == verify_before + 1
    )
    assert (
        _counter_value(
            RASTER_MASK_CONTENT_OPERATIONS_TOTAL,
            operation="load",
            outcome="success",
        )
        == load_before
    )


@pytest.mark.asyncio
async def test_active_geometry_gauge_uses_exact_db_counts(monkeypatch):
    import app.observability.raster_mask as raster_metrics

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one=lambda: 7),
                SimpleNamespace(scalar_one=lambda: 3),
            ]
        )
    )
    children = {
        "annotation": SimpleNamespace(set=MagicMock()),
        "prediction": SimpleNamespace(set=MagicMock()),
    }
    gauge = SimpleNamespace(labels=MagicMock(side_effect=lambda kind: children[kind]))
    monkeypatch.setattr(raster_metrics, "RASTER_MASK_ACTIVE_GEOMETRIES", gauge)

    counts = await raster_metrics.refresh_raster_mask_active_geometries(db)

    assert counts == {"annotation": 7, "prediction": 3}
    children["annotation"].set.assert_called_once_with(7)
    children["prediction"].set.assert_called_once_with(3)
    queries = [str(call.args[0]) for call in db.execute.await_args_list]
    assert "FROM aap_labeling_v1.annotation_rows" in queries[0]
    assert "FROM aap_labeling_v1.prediction_rows" in queries[1]
    assert "is_active IS TRUE" in queries[0]
    assert "jsonb_path_exists" in queries[1]
    assert '@.type == "raster_mask"' in queries[1]


@pytest.mark.asyncio
async def test_active_geometry_refresh_failures_are_best_effort(monkeypatch):
    import app.observability.raster_mask as raster_metrics

    failing_query_db = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("db unavailable")),
        rollback=AsyncMock(),
    )
    assert (
        await raster_metrics.refresh_raster_mask_active_geometries_safely(
            failing_query_db
        )
        is None
    )
    failing_query_db.rollback.assert_awaited_once()

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one=lambda: 1),
                SimpleNamespace(scalar_one=lambda: 2),
            ]
        )
    )
    gauge = SimpleNamespace(labels=MagicMock(side_effect=RuntimeError("metric down")))
    monkeypatch.setattr(raster_metrics, "RASTER_MASK_ACTIVE_GEOMETRIES", gauge)
    assert await raster_metrics.refresh_raster_mask_active_geometries(db) == {
        "annotation": 1,
        "prediction": 2,
    }
