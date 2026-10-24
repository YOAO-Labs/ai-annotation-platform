"""Typed mask failures abort imports before committing their job failure receipt."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException, UploadFile
import pytest
from starlette.requests import Request

from app.api.v1 import annotations, predictions
from app.schemas.aap_json import AAPImportResult
from app.services import (
    annotations_import,
    async_job,
    async_job_notify,
    predictions_import,
    reference_access,
)
from app.services.raster_mask_storage import RasterMaskContractError


class _Transactions:
    def __init__(self):
        self.pending = []
        self.committed = []
        self.events = []

    async def commit(self):
        self.events.append("commit")
        self.committed.append(tuple(self.pending))
        self.pending.clear()

    async def rollback(self):
        self.events.append("rollback")
        self.pending.clear()


def _route(
    monkeypatch, name, error, *, file_count=1, fail_at=0, job=True, ledger_fails=False
):
    db = _Transactions()
    job_id = uuid.uuid4()
    project = SimpleNamespace(id=uuid.uuid4(), display_id="P-MASK-IMPORT")
    user = SimpleNamespace(id=uuid.uuid4())
    importer = AsyncMock()

    async def import_file(*args, **kwargs):
        index = importer.await_count - 1
        db.events.append("import")
        db.pending.extend((f"purge-{index}", f"insert-{index}"))
        if index == fail_at:
            raise error
        return AAPImportResult(imported=1)

    importer.side_effect = import_file

    async def mark_failed(*args, **kwargs):
        db.events.append("failed")
        db.pending.append("job-failed")
        if ledger_fails:
            raise RuntimeError("failure receipt unavailable")

    async def notify(*args, **kwargs):
        db.events.append("notify")

    monkeypatch.setattr(
        async_job, "create_job", AsyncMock(return_value=SimpleNamespace(id=job_id))
    )
    monkeypatch.setattr(async_job, "mark_running", AsyncMock())
    failed = AsyncMock(side_effect=mark_failed)
    monkeypatch.setattr(async_job, "mark_failed", failed)
    monkeypatch.setattr(async_job, "mark_complete", AsyncMock())
    monkeypatch.setattr(
        async_job_notify, "notify_job_terminal", AsyncMock(side_effect=notify)
    )
    monkeypatch.setattr(reference_access, "reject_reference_scope", AsyncMock())
    monkeypatch.setattr(annotations.AuditService, "log", AsyncMock())
    request = Request({"type": "http", "headers": []})
    files = [
        UploadFile(BytesIO(b"{}"), filename=f"{index}.json")
        for index in range(file_count)
    ]

    async def invoke():
        if name == "annotations":
            monkeypatch.setattr(
                annotations_import, "import_aap_json_annotations", importer
            )
            return await annotations.import_annotations(
                request=request,
                file=files[0],
                format="aap_json",
                dry_run=not job,
                overwrite=True,
                project=project,
                db=db,
                current_user=user,
            )
        monkeypatch.setattr(predictions_import, "import_" + name, importer)
        return await predictions.import_predictions(
            request=request,
            files=files,
            format=name,
            yolo_variant="det",
            dry_run=not job,
            model_version=None,
            overwrite_existing=True,
            image_width=None,
            image_height=None,
            project=project,
            db=db,
            current_user=user,
        )

    return SimpleNamespace(
        db=db, invoke=invoke, importer=importer, failed=failed, job_id=job_id
    )


@pytest.mark.parametrize("route", ["annotations", "aap_json", "coco"])
@pytest.mark.parametrize(
    "status,reason",
    [
        (409, "mask_content_busy"),
        (409, "storage_maintenance"),
        (409, "raster_object_storage_unpinned"),
        (503, "raster_object_storage_unavailable"),
    ],
)
async def test_typed_mask_failure_preserves_http_contract_and_rolls_back_import(
    monkeypatch, route, status, reason
):
    error = RasterMaskContractError(
        status_code=status,
        reason=reason,
        message="Retry the mask operation",
        retryable=True,
    )
    context = _route(monkeypatch, route, error)

    with pytest.raises(HTTPException) as caught:
        await context.invoke()

    assert caught.value.status_code == status
    assert caught.value.detail == error.detail
    assert context.db.events == [
        "commit",
        "import",
        "rollback",
        "failed",
        "notify",
        "commit",
    ]
    assert context.db.committed == [(), ("job-failed",)]
    assert context.db.pending == []
    context.failed.assert_awaited_once_with(
        context.db, context.job_id, error=str(error)
    )


@pytest.mark.parametrize("format", ["aap_json", "coco"])
@pytest.mark.parametrize("fail_at", [0, 1])
async def test_multifile_mask_failure_stops_later_files_and_rolls_back_earlier_writes(
    monkeypatch, format, fail_at
):
    error = RasterMaskContractError(
        status_code=503,
        reason="raster_object_storage_unavailable",
        message="Storage is unavailable",
        retryable=True,
    )
    context = _route(monkeypatch, format, error, file_count=3, fail_at=fail_at)

    with pytest.raises(HTTPException) as caught:
        await context.invoke()

    assert caught.value.status_code == 503
    assert context.importer.await_count == fail_at + 1
    assert context.db.committed == [(), ("job-failed",)]
    assert context.db.events[-4:] == ["rollback", "failed", "notify", "commit"]


@pytest.mark.parametrize("route", ["annotations", "aap_json", "coco"])
@pytest.mark.parametrize("ledger_fails", [False, True])
async def test_typed_failure_survives_missing_job_or_failed_failure_receipt(
    monkeypatch, route, ledger_fails
):
    error = RasterMaskContractError(
        status_code=409,
        reason="storage_maintenance_changed",
        message="Retry after maintenance",
        retryable=True,
    )
    context = _route(
        monkeypatch, route, error, job=ledger_fails, ledger_fails=ledger_fails
    )

    with pytest.raises(HTTPException) as caught:
        await context.invoke()

    assert caught.value.status_code == 409
    assert caught.value.detail == error.detail
    assert context.db.pending == []
    assert context.db.committed == ([()] if ledger_fails else [])
    if ledger_fails:
        assert context.db.events[-3:] == ["rollback", "failed", "rollback"]
    else:
        context.failed.assert_not_awaited()
        assert context.db.events == ["import", "rollback"]


@pytest.mark.parametrize("route", ["annotations", "aap_json", "coco"])
async def test_plain_input_value_error_still_returns_422(monkeypatch, route):
    context = _route(monkeypatch, route, ValueError("invalid input"), job=False)
    with pytest.raises(HTTPException) as caught:
        await context.invoke()
    assert caught.value.status_code == 422
    assert caught.value.detail == "invalid input"


@pytest.mark.parametrize("format", ["aap_json", "coco"])
async def test_multifile_plain_input_error_remains_a_skipped_file(monkeypatch, format):
    context = _route(
        monkeypatch, format, ValueError("invalid input"), file_count=3, job=False
    )
    result = await context.invoke()
    assert context.importer.await_count == 3
    assert result.imported == 2
    assert result.skipped == 1
    assert result.errors[0].reason == "invalid input"
    context.failed.assert_not_awaited()
