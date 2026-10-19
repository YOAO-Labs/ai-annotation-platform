from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from aap_assets_mask_content import codec
from aap_assets_mask_content.gc_contract import (
    DeleteOperationRequest,
    DeleteOperationStatus,
    GcCandidate,
    GcScanPage,
    GcScanRequest,
)

_KEY = codec.rle_object_key("a" * 64)
_WHEN = datetime(2026, 10, 19, tzinfo=timezone.utc)
_OPERATION_ID = UUID("2d639c8a-0fd8-4dcb-9f90-26cb8a752f3a")


def _candidate(key=_KEY):
    return GcCandidate(
        object_key=key,
        etag="opaque-etag-1",
        last_modified=_WHEN,
        byte_size=8,
    )


def _delete_request():
    return DeleteOperationRequest(
        operation_id=_OPERATION_ID,
        object_key=_KEY,
        etag="opaque-etag-1",
        last_modified=_WHEN,
    )


def test_scan_contract_accepts_physical_cursor_after_skipped_key():
    skipped = "raster-masks/sha256/aa/aa/legacy-file.data"

    page = GcScanPage(items=[_candidate()], after_key=skipped, complete=False)

    assert page.items == (_candidate(),)
    assert page.after_key == skipped
    assert GcScanRequest(after_key=skipped, limit=1).after_key == skipped


@pytest.mark.parametrize(
    "changes",
    [
        {"after_key": "other-prefix/key"},
        {"after_key": "raster-masks/sha256/" + "x" * 1020},
        {"after_key": None, "complete": False},
    ],
)
def test_scan_contract_rejects_unbounded_or_nonadvancing_cursors(changes):
    values = {"items": [], "after_key": None, "complete": True}
    values.update(changes)

    with pytest.raises(ValidationError):
        if "limit" in values:
            GcScanRequest.model_validate(values)
        else:
            GcScanPage.model_validate(values)


def test_scan_page_rejects_duplicate_or_out_of_order_candidates():
    second_key = codec.rle_object_key("b" * 64)

    with pytest.raises(ValidationError):
        GcScanPage(
            items=[_candidate(), _candidate()],
            after_key=_KEY,
            complete=True,
        )
    with pytest.raises(ValidationError):
        GcScanPage(
            items=[_candidate(second_key), _candidate()],
            after_key=second_key,
            complete=True,
        )


def test_status_state_and_operation_identity_are_closed():
    request = _delete_request()
    pending = DeleteOperationStatus(
        operation_id=_OPERATION_ID,
        request=request,
        state="indeterminate",
        outcome=None,
    )
    assert pending.request == request

    with pytest.raises(ValidationError):
        DeleteOperationStatus(
            operation_id=_OPERATION_ID,
            request=None,
            state="completed",
            outcome="deleted",
        )
    with pytest.raises(ValidationError):
        DeleteOperationStatus(
            operation_id=_OPERATION_ID,
            request=request,
            state="pending",
            outcome="deleted",
        )
    with pytest.raises(ValidationError):
        DeleteOperationStatus(
            operation_id=UUID("9e1d88d5-5994-4d28-88af-5e1cdd55ea6b"),
            request=request,
            state="completed",
            outcome="deleted",
        )
