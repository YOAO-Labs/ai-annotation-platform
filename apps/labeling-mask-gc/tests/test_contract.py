from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from aap_assets_mask_content.gc_contract import (
    DeleteOperationStatus,
    GcCandidate,
    GcScanPage,
)
from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc_runtime.database import (
    prepare_native_call,
    validate_native_result,
)

KEY = "raster-masks/sha256/a0/a0/" + "a0a0" + "a" * 60 + ".json"
OTHER_KEY = "raster-masks/sha256/b0/b0/" + "b0b0" + "b" * 60 + ".json"
STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _request_for(name):
    if name == "read_mask_write_epochs":
        return c.ReadMaskWriteEpochsRequest(object_keys=[KEY])
    if name == "begin_mask_content_write":
        return c.BeginMaskContentWriteRequest(object_keys=[KEY])
    if name == "lock_mask_write_epochs":
        return c.LockMaskWriteEpochsRequest(items=[{"object_key": KEY, "epoch": 0}])
    if name == "register_tracker_running_mask_root":
        return c.RegisterTrackerRunningMaskRootRequest(
            job_id=UUID(int=1), task_id=UUID(int=2), object_key=KEY
        )
    if name == "claim_mask_gc_sweep":
        return c.ClaimMaskGcSweepRequest(worker_id=UUID(int=1))
    if name == "stage_mask_gc_page":
        return c.StageMaskGcPageRequest(
            sweep_id=UUID(int=1),
            generation=1,
            lease_token=UUID(int=2),
            after_key=None,
            page=GcScanPage(items=(), after_key=KEY, complete=True),
        )
    if name == "claim_mask_gc_deletes":
        return c.ClaimMaskGcDeletesRequest(worker_id=UUID(int=1))
    if name == "finish_mask_gc_delete":
        status = DeleteOperationStatus(
            operation_id=UUID(int=1),
            request=None,
            state="not_found",
            outcome=None,
        )
        return c.FinishMaskGcDeleteRequest(
            operation_id=UUID(int=1),
            generation=1,
            lease_token=UUID(int=2),
            receipt=status,
            error_code=None,
        )
    if name == "read_mask_gc_progress":
        return c.ReadMaskGcProgressRequest()
    raise AssertionError(name)


def test_native_operation_allowlist_is_one_jsonb_argument():
    assert set(c.OPERATIONS) == {
        "read_mask_write_epochs",
        "begin_mask_content_write",
        "lock_mask_write_epochs",
        "register_tracker_running_mask_root",
        "claim_mask_gc_sweep",
        "stage_mask_gc_page",
        "claim_mask_gc_deletes",
        "finish_mask_gc_delete",
        "read_mask_gc_progress",
    }
    for name, operation in c.OPERATIONS.items():
        assert operation.arguments == ("payload",)
        assert operation.sql_types == ("jsonb",)
        statement, arguments = prepare_native_call(name, _request_for(name))
        assert statement == f"SELECT aap_labeling_v1.{name}($1::jsonb)"
        assert len(arguments) == 1


def test_running_tracker_root_is_labeling_only_and_echoes_exact_owner_scope():
    request = c.RegisterTrackerRunningMaskRootRequest(
        job_id=UUID(int=1), task_id=UUID(int=2), object_key=KEY
    )
    assert c.OPERATIONS["register_tracker_running_mask_root"].role == "labeling"
    with pytest.raises(PermissionError):
        prepare_native_call(
            "register_tracker_running_mask_root",
            request,
            allowed_roles=("cleanup",),
        )

    reply = {
        "schema": c.SCHEMA,
        "ok": True,
        "data": {
            "job_id": str(request.job_id),
            "task_id": str(request.task_id),
            "object_key": KEY,
            "registered": True,
        },
    }
    result = validate_native_result(
        "register_tracker_running_mask_root", request, json.dumps(reply)
    )
    assert result.job_id == request.job_id
    assert result.task_id == request.task_id
    assert result.object_key == KEY

    reply["data"]["task_id"] = str(UUID(int=3))
    with pytest.raises(c.MaskGcFailure) as error:
        validate_native_result(
            "register_tracker_running_mask_root", request, json.dumps(reply)
        )
    assert error.value.code == "invalid_response"


def test_native_call_rejects_unlisted_function_and_wrong_request_type():
    with pytest.raises(TypeError):
        prepare_native_call("DROP TABLE annotations", c.ReadMaskGcProgressRequest())
    with pytest.raises(TypeError):
        prepare_native_call("read_mask_gc_progress", c.RunRequest())


def test_write_epoch_receipt_must_cover_exact_keys_and_final_epoch():
    request = c.ReadMaskWriteEpochsRequest(object_keys=[KEY, OTHER_KEY])
    reply = {
        "schema": c.SCHEMA,
        "ok": True,
        "data": {
            "items": [
                {"object_key": KEY, "epoch": 0},
                {"object_key": OTHER_KEY, "epoch": 4},
            ]
        },
    }
    result = validate_native_result(
        "read_mask_write_epochs", request, json.dumps(reply)
    )
    assert tuple(item.object_key for item in result.items) == (KEY, OTHER_KEY)

    missing = {**reply, "data": {"items": [{"object_key": KEY, "epoch": 0}]}}
    with pytest.raises(c.MaskGcFailure) as error:
        validate_native_result("read_mask_write_epochs", request, json.dumps(missing))
    assert error.value.code == "invalid_response"

    locked = c.LockMaskWriteEpochsRequest(items=[{"object_key": KEY, "epoch": 3}])
    stale = {
        "schema": c.SCHEMA,
        "ok": True,
        "data": {"items": [{"object_key": KEY, "epoch": 4}]},
    }
    with pytest.raises(c.MaskGcFailure):
        validate_native_result("lock_mask_write_epochs", locked, json.dumps(stale))


def test_closed_native_envelope_and_json_duplicate_keys():
    request = c.ReadMaskGcProgressRequest()
    foreign = {
        "schema": c.SCHEMA,
        "ok": True,
        "data": {
            "active_intents": 0,
            "indeterminate_intents": 0,
            "completed_intents": 0,
            "sweep": None,
            "provider_error": "must not be accepted",
        },
    }
    with pytest.raises(c.MaskGcFailure) as error:
        validate_native_result("read_mask_gc_progress", request, json.dumps(foreign))
    assert error.value.code == "invalid_response"

    duplicate = '{"schema":"aap-labeling-mask-gc/v1","ok":true,"data":{},"ok":true}'
    with pytest.raises(c.MaskGcFailure):
        validate_native_result("read_mask_gc_progress", request, duplicate)


def test_uncertainty_cannot_be_upgraded_to_completion_without_receipt():
    request = c.FinishMaskGcDeleteRequest(
        operation_id=UUID(int=1),
        generation=1,
        lease_token=UUID(int=2),
        receipt=None,
        error_code="peer_unavailable",
    )
    invented = {
        "schema": c.SCHEMA,
        "ok": True,
        "data": {
            "operation_id": str(UUID(int=1)),
            "state": "completed",
            "completed": True,
        },
    }
    with pytest.raises(c.MaskGcFailure) as error:
        validate_native_result("finish_mask_gc_delete", request, json.dumps(invented))
    assert error.value.code == "invalid_response"


def test_scan_page_is_consumed_from_byte_owner_contract():
    candidate = GcCandidate(
        object_key=KEY,
        etag="opaque-etag",
        last_modified=STAMP,
        byte_size=12,
    )
    page = GcScanPage(items=(candidate,), after_key=KEY, complete=True)
    stage = c.StageMaskGcPageRequest(
        sweep_id=UUID("00000000-0000-0000-0000-000000000001"),
        generation=1,
        lease_token=UUID("00000000-0000-0000-0000-000000000002"),
        after_key=None,
        page=page,
    )
    assert stage.page is page
    with pytest.raises(ValidationError):
        GcScanPage(items=(candidate, candidate), after_key=KEY, complete=True)


def test_delete_claim_batch_is_bounded_and_unique_by_key_and_operation():
    claim = c.DeleteClaim(
        operation_id=UUID(int=1),
        object_key=KEY,
        etag="opaque-etag",
        last_modified=STAMP,
        byte_size=1,
        epoch=2,
        generation=1,
        lease_token=UUID(int=2),
        lease_expires_at=STAMP,
    )
    with pytest.raises(ValidationError):
        c.ClaimMaskGcDeletesResult(items=(claim, claim))
