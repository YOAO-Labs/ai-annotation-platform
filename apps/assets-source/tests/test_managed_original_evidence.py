"""Observed current-head evidence for owner verification payloads."""

from __future__ import annotations

import pytest

from yoao_assets_source.managed_original_contract import (
    ManagedOriginalFailureEvidence,
    observed_current_head_match,
)


def test_current_head_flag_requires_an_observed_version_pinned_match():
    assert (
        observed_current_head_match(
            {"version_id": "v1", "observation": {"version_id": "v1", "size": 3}},
            size_bytes=3,
        )
        is True
    )
    assert (
        observed_current_head_match(
            {"version_id": "v1", "observation": {"version_id": "v2", "size": 3}},
            size_bytes=3,
        )
        is False
    )
    assert (
        observed_current_head_match(
            {"version_id": "v1", "observation": {"version_id": "v1", "size": 4}},
            size_bytes=3,
        )
        is False
    )
    assert (
        observed_current_head_match(
            {"version_id": None, "observation": {"version_id": None, "size": 3}},
            size_bytes=3,
        )
        is None
    )
    assert observed_current_head_match({"version_id": "v1"}, size_bytes=3) is None
    assert observed_current_head_match(None, size_bytes=3) is None


def test_unknown_physical_work_is_not_reclassified_by_a_later_precondition_error():
    from botocore.exceptions import ClientError
    from yoao_assets_source.managed_original import _failure

    error = ClientError(
        {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "GetObject",
    )
    error.storage_operation_unsettled = True
    assert _failure(error).outcome == "unknown"


@pytest.mark.parametrize("receipt", [{}, {"etag": "observed"}])
def test_no_write_proof_rejects_any_observed_receipt(receipt):
    with pytest.raises(ValueError, match="observed write receipt contradicts"):
        ManagedOriginalFailureEvidence.build(
            physical_settled=True,
            physical_outcome="not_written",
            stage="write",
            receipt=receipt,
        )


def test_undrained_observed_write_retains_its_receipt_as_unknown():
    receipt = {"etag": "observed", "version_id": "saved-version"}
    evidence = ManagedOriginalFailureEvidence.build(
        physical_settled=False,
        physical_outcome="unknown",
        stage="write",
        receipt=receipt,
    )
    assert evidence["receipt"] == receipt
    assert evidence["physical_settled"] is False
    assert evidence["physical_outcome"] == "unknown"
