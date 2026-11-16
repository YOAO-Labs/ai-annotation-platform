"""Focused Native pyramid retirement wire-contract edges."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from yoao_assets_images import contract as c


def _target(*, output_id, endpoint_url="https://cache.example", bucket="media-cache"):
    key_prefix = ""
    return {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(uuid4()),
        "config_revision": 1,
        "binding_generation": 2,
        "endpoint_url": endpoint_url,
        "bucket": bucket,
        "key_prefix": key_prefix,
        "prefix": key_prefix + f"image-pyramids/objects/{output_id}/",
        "storage_identity": "a" * 64,
    }


def _claim(*, output_id=None, lease_token=None, after=None, **target_overrides):
    output_id = output_id or uuid4()
    return c.NativePyramidRetirementClaim.model_validate(
        {
            "output_id": output_id,
            "lease_token": lease_token or uuid4(),
            "lease_expires_at": datetime.now(timezone.utc),
            "storage_ref": _target(output_id=output_id, **target_overrides),
            "after_operation_id": after,
        }
    )


def _member(*, operation_id=None, relative_key="manifest.json"):
    return c.NativePyramidSavedMember(
        operation_id=operation_id or uuid4(),
        relative_key=relative_key,
        sha256="b" * 64,
        size_bytes=12,
        etag='"etag-value"',
        version_id=None,
    )


def _page(*, output_id, lease_token, items, exhausted, after=None):
    return c.NativePyramidRetirementPage(
        output_id=output_id,
        lease_token=lease_token,
        page_id=uuid4(),
        lease_expires_at=datetime.now(timezone.utc),
        after_operation_id=after,
        items=items,
        exhausted=exhausted,
    )


def test_claim_uses_the_saved_target_without_a_configured_store():
    output_id = uuid4()
    claim = _claim(output_id=output_id)
    request = c.ClaimNativePyramidRetirementRequest(worker_id=uuid4())
    assert request.lease_seconds == 120
    result = c.ClaimedNativePyramidRetirement(claim=claim)
    c.require_scope(request, result)

    escaped = claim.model_copy(
        update={
            "storage_ref": c.NativePyramidSavedTarget.model_validate(
                _target(output_id=uuid4())
            )
        }
    )
    with pytest.raises(ValueError):
        c.require_scope(request, c.ClaimedNativePyramidRetirement(claim=escaped))

    wrong_prefix = _target(output_id=output_id)
    wrong_prefix["prefix"] = wrong_prefix["key_prefix"] + "image-pyramids/objects/x/"
    with pytest.raises(ValueError):
        c.require_scope(
            request,
            c.ClaimedNativePyramidRetirement(
                claim=claim.model_copy(
                    update={
                        "storage_ref": c.NativePyramidSavedTarget.model_validate(
                            wrong_prefix
                        )
                    }
                )
            ),
        )


def test_empty_claim_is_the_only_bounded_no_work_result():
    request = c.ClaimNativePyramidRetirementRequest(worker_id=uuid4())
    assert c.ClaimedNativePyramidRetirement(claim=None).claim is None
    c.require_scope(request, c.ClaimedNativePyramidRetirement(claim=None))


def test_page_cursor_and_terminal_page_are_exact():
    output_id, token = uuid4(), uuid4()
    first, second = sorted(
        [_member(), _member(relative_key="overview.webp")],
        key=lambda item: item.operation_id,
    )
    page = _page(
        output_id=output_id,
        lease_token=token,
        items=[first, second],
        exhausted=False,
        after=second.operation_id,
    )
    request = c.PrepareNativePyramidRetirementPageRequest(
        output_id=output_id, lease_token=token, page_size=100
    )
    c.require_scope(request, page)

    with pytest.raises(ValidationError):
        _page(output_id=output_id, lease_token=token, items=[], exhausted=False)
    with pytest.raises(ValidationError):
        _page(output_id=output_id, lease_token=token, items=[first], exhausted=True)
    with pytest.raises(ValidationError):
        _page(
            output_id=output_id,
            lease_token=token,
            items=[second, first],
            exhausted=False,
        )
    with pytest.raises(ValidationError):
        _page(
            output_id=output_id,
            lease_token=token,
            items=[first, first],
            exhausted=False,
        )
    with pytest.raises(ValidationError):
        _page(
            output_id=output_id,
            lease_token=token,
            items=[first, second],
            exhausted=False,
            after=first.operation_id,
        )
    with pytest.raises(ValidationError):
        _page(
            output_id=output_id,
            lease_token=token,
            items=[first],
            exhausted=False,
            after=None,
        )
    terminal = _page(
        output_id=output_id,
        lease_token=token,
        items=[],
        exhausted=True,
        after=second.operation_id,
    )
    c.require_scope(request, terminal)

    escaped = page.model_copy(update={"output_id": uuid4()})
    with pytest.raises(ValueError):
        c.require_scope(request, escaped)
    with pytest.raises(ValueError):
        c.require_scope(
            request.model_copy(update={"lease_token": uuid4()}), page
        )
    with pytest.raises(ValueError):
        c.require_scope(
            request.model_copy(update={"page_size": 1}), page
        )


def test_complete_request_binds_exactly_one_outcome_shape():
    base = {
        "output_id": uuid4(),
        "lease_token": uuid4(),
        "page_id": uuid4(),
    }
    member_id = uuid4()
    c.CompleteNativePyramidRetirementPageRequest(
        **base, outcome="members_absent", operation_ids=[member_id]
    )
    with pytest.raises(ValidationError):
        c.CompleteNativePyramidRetirementPageRequest(
            **base, outcome="members_absent", operation_ids=[]
        )
    with pytest.raises(ValidationError):
        c.CompleteNativePyramidRetirementPageRequest(
            **base, outcome="members_absent", operation_ids=[member_id, member_id]
        )
    for outcome in ("prefix_absent", "closed_without_ack"):
        c.CompleteNativePyramidRetirementPageRequest(
            **base, outcome=outcome, operation_ids=[]
        )
        with pytest.raises(ValidationError):
            c.CompleteNativePyramidRetirementPageRequest(
                **base, outcome=outcome, operation_ids=[member_id]
            )


def test_ack_cannot_claim_closure_without_absence_proof():
    output_id, cursor = uuid4(), uuid4()
    prefix_request = c.CompleteNativePyramidRetirementPageRequest(
        output_id=output_id,
        lease_token=uuid4(),
        page_id=uuid4(),
        outcome="prefix_absent",
        operation_ids=[],
    )
    closed = c.NativePyramidRetirementAck(
        output_id=output_id, after_operation_id=cursor, completed=True
    )
    c.require_scope(prefix_request, closed)
    with pytest.raises(ValueError):
        c.require_scope(
            prefix_request,
            c.NativePyramidRetirementAck(
                output_id=output_id, after_operation_id=cursor, completed=False
            ),
        )

    members_request = prefix_request.model_copy(
        update={"outcome": "members_absent", "operation_ids": [cursor]}
    )
    advanced = c.NativePyramidRetirementAck(
        output_id=output_id, after_operation_id=cursor, completed=False
    )
    c.require_scope(members_request, advanced)
    with pytest.raises(ValueError):
        c.require_scope(
            members_request,
            c.NativePyramidRetirementAck(
                output_id=output_id, after_operation_id=cursor, completed=True
            ),
        )
    with pytest.raises(ValueError):
        c.require_scope(
            members_request,
            c.NativePyramidRetirementAck(
                output_id=output_id, after_operation_id=None, completed=False
            ),
        )

    closure_request = prefix_request.model_copy(
        update={"outcome": "closed_without_ack"}
    )
    settled = c.NativePyramidRetirementAck(
        output_id=output_id, after_operation_id=cursor, completed=False
    )
    c.require_scope(closure_request, settled)
    with pytest.raises(ValueError):
        c.require_scope(
            closure_request,
            c.NativePyramidRetirementAck(
                output_id=output_id, after_operation_id=cursor, completed=True
            ),
        )


def test_retirement_operations_are_cleanup_only_and_typed():
    claim = c.OPERATIONS["claim_native_pyramid_retirement"]
    assert claim.role == "cleanup"
    assert claim.request is c.ClaimNativePyramidRetirementRequest
    assert claim.result is c.ClaimedNativePyramidRetirement
    assert claim.arguments == ("worker_id", "lease_seconds")
    assert claim.sql_types == ("uuid", "integer")

    prepare = c.OPERATIONS["prepare_native_pyramid_retirement_page"]
    assert prepare.role == "cleanup"
    assert prepare.request is c.PrepareNativePyramidRetirementPageRequest
    assert prepare.result is c.NativePyramidRetirementPage
    assert prepare.arguments == ("output_id", "lease_token", "page_size")
    assert prepare.sql_types == ("uuid", "uuid", "integer")

    complete = c.OPERATIONS["complete_native_pyramid_retirement_page"]
    assert complete.role == "cleanup"
    assert complete.request is c.CompleteNativePyramidRetirementPageRequest
    assert complete.result is c.NativePyramidRetirementAck
    assert complete.arguments == (
        "output_id",
        "lease_token",
        "page_id",
        "outcome",
        "operation_ids",
    )
    assert complete.sql_types == ("uuid", "uuid", "uuid", "text", "jsonb")


def test_summary_carries_the_selected_generation_source_snapshot():
    snapshot = "c" * 64
    generation = c.ImagePyramidGeneration(
        asset_id=uuid4(),
        status="failed",
        required=True,
        generation=2,
        building_generation=None,
        building_status=None,
        source_generation=7,
        source_snapshot=snapshot,
        width=None,
        height=None,
        format=None,
        manifest=None,
        manifest_key=None,
        manifest_digest=None,
        overview_key=None,
        overview_digest=None,
        tile_count=0,
        retained_bytes=0,
        native=False,
        native_output=None,
    )
    summary = generation.summary()
    assert summary.source_snapshot == snapshot

    unavailable = c.ImagePyramidGeneration(
        asset_id=None,
        status="not_available",
        required=False,
        generation=None,
        building_generation=None,
        building_status=None,
        width=None,
        height=None,
        format=None,
        manifest=None,
        manifest_key=None,
        manifest_digest=None,
        overview_key=None,
        overview_digest=None,
        tile_count=0,
        retained_bytes=0,
        native=False,
        native_output=None,
    )
    assert unavailable.summary().source_snapshot is None
