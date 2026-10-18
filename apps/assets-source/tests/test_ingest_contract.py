"""Boundary invariants of the public synchronous ingest protocol."""

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_assets_source.ingest_contract import (
    INGEST_SCHEMA,
    SCAN_REQUEST_DIGEST,
    IngestBatchPlan,
    IngestContext,
    IngestDescriptor,
    IngestDeliveryAcknowledged,
    IngestFailure,
    IngestLayoutPage,
    IngestProgress,
    IngestReceipt,
    decode_ingest_reply,
    ingest_json_schema,
)


def _scope():
    return {"operation_id": uuid4(), "actor_id": uuid4(), "dataset_id": uuid4()}


def _receipt(kind="zip"):
    return IngestReceipt(
        **_scope(),
        kind=kind,
        request_digest=SCAN_REQUEST_DIGEST if kind == "scan" else "a" * 64,
        added=1,
        deduped=0,
        skipped=0,
        errors=[],
        error_count=0,
        total_in_zip=1 if kind == "zip" else None,
        linked_tasks=2,
        scene_inference_notes=[],
    )


def test_completed_receipt_replays_without_internal_fields_in_public_result():
    result = _receipt()
    descriptor = IngestDescriptor(
        kind="zip",
        request_digest=result.request_digest,
        total_in_zip=1,
        planned_entries=1,
    )
    context = IngestContext(
        operation_id=result.operation_id,
        actor_id=result.actor_id,
        dataset_id=result.dataset_id,
        descriptor=descriptor,
        state="completed",
        dataset_name="old name",
        source_prefix="old name/",
        is_temporal=False,
        next_index=1,
        result=result,
    )
    envelope = {
        "schema": INGEST_SCHEMA,
        "ok": True,
        "data": context.model_dump(mode="json"),
    }
    decoded = decode_ingest_reply(json.dumps(envelope), IngestContext)
    assert decoded == context
    assert result.public_result() == {
        "added": 1,
        "deduped": 0,
        "skipped": 0,
        "errors": [],
        "total_in_zip": 1,
        "linked_tasks": 2,
        "scene_inference_notes": [],
    }


def test_scan_receipt_has_its_existing_response_and_no_scene_processing():
    result = _receipt("scan")
    assert result.public_result() == {"status": "ok", "new_items": 1, "linked_tasks": 2}
    value = result.model_dump()
    value["scene_inference_notes"] = ["not a scan responsibility"]
    with pytest.raises(ValidationError):
        IngestReceipt.model_validate(value)


@pytest.mark.parametrize(
    "envelope",
    [
        {"schema": INGEST_SCHEMA, "ok": 0, "error": {"code": "dataset_busy"}},
        {"schema": INGEST_SCHEMA, "ok": False, "error": None},
        {"schema": INGEST_SCHEMA, "ok": False, "error": {"code": []}},
        {"schema": INGEST_SCHEMA, "ok": False, "error": {"code": "not_published"}},
        {"schema": INGEST_SCHEMA, "ok": True, "data": None, "error": None},
        {
            "schema": INGEST_SCHEMA,
            "ok": False,
            "error": {"code": "dataset_busy"},
            "data": None,
        },
    ],
)
def test_malformed_envelopes_cannot_be_treated_as_success(envelope):
    with pytest.raises(IngestFailure, match="invalid_response"):
        decode_ingest_reply(envelope, IngestReceipt)


def test_archive_failure_preserves_bounded_public_detail():
    payload = {
        "schema": INGEST_SCHEMA,
        "ok": False,
        "error": {
            "code": "resource_budget_exceeded",
            "detail": {
                "reason": "resource_budget_exceeded",
                "message": "too many files",
                "budget": "max_files",
                "limit": 5000,
                "observed": 5001,
            },
        },
    }
    with pytest.raises(IngestFailure) as caught:
        decode_ingest_reply(payload, IngestReceipt)
    assert (
        caught.value.detail.model_dump(exclude_unset=True) == payload["error"]["detail"]
    )
    malformed = deepcopy(payload)
    malformed["error"]["detail"]["observed"] = True
    with pytest.raises(IngestFailure, match="invalid_response"):
        decode_ingest_reply(malformed, IngestReceipt)


def test_completed_context_cannot_embed_a_receipt_for_another_dataset():
    result = _receipt()
    context = {
        "operation_id": result.operation_id,
        "actor_id": result.actor_id,
        "dataset_id": uuid4(),
        "descriptor": {
            "kind": "zip",
            "request_digest": result.request_digest,
            "total_in_zip": 1,
            "planned_entries": 1,
        },
        "state": "completed",
        "dataset_name": "d",
        "source_prefix": "d/",
        "is_temporal": False,
        "next_index": 1,
        "result": result.model_dump(),
    }
    with pytest.raises(IngestFailure, match="invalid_response"):
        IngestContext.model_validate(context)


def test_batch_progress_cannot_leap_over_unstaged_or_wrong_scope_entries():
    scope = _scope()
    progress = dict(
        scope,
        staged=1,
        settled=2,
        added=2,
        deduped=0,
        skipped=0,
        error_count=0,
        linked_tasks=0,
    )
    with pytest.raises(ValidationError):
        IngestProgress.model_validate(progress)
    progress.update(staged=0, settled=0, added=0, actor_id=uuid4())
    with pytest.raises(IngestFailure, match="invalid_response"):
        IngestBatchPlan(**scope, batch_id=uuid4(), entries=[], progress=progress)


def test_layout_cursor_cannot_repeat_a_previous_page():
    scope = _scope()
    item = {
        "id": uuid4(),
        "file_name": "a.jpg",
        "file_path": "old/a.jpg",
        "file_type": "image",
        "scene_id": None,
        "frame_index": None,
        "logical_path": None,
    }
    page = dict(
        scope,
        snapshot_digest="b" * 64,
        scene_count=0,
        assigned_count=0,
        total_items=2,
        items=[item],
        next_item_id=uuid4(),
    )
    with pytest.raises(ValidationError):
        IngestLayoutPage.model_validate(page)


def test_published_schema_is_generated_from_the_same_wire_values():
    path = (
        Path(__file__).resolve().parents[1]
        / "src/aap_assets_source/ingest_contract.schema.json"
    )
    assert json.loads(path.read_text()) == ingest_json_schema()


@pytest.mark.parametrize("value", [1, 0, False, "true", None])
def test_acknowledgement_requires_an_actual_delivered_boolean(value):
    with pytest.raises(ValidationError):
        IngestDeliveryAcknowledged(id=uuid4(), lease_token=uuid4(), delivered=value)
