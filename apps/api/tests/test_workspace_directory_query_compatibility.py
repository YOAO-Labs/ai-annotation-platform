"""Replay frozen public directory samples through the borrowed API owner."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.workspace_directory_query_helpers import capture_directory_scenario

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_SAMPLE_PATH = (
    Path(__file__).parent / "fixtures/workspace_directory_reads_legacy/samples.json"
)
_PROBE_FIELDS = (
    "no_autoflush",
    "pending_member_in_route_result",
    "pending_member_in_session_new_after_read",
    "caller_transaction_open_after_read",
    "visible_from_owner_before_caller_rollback",
    "pending_user_alias",
)


def _comparable(sample):
    response = sample["response"]
    return {
        "label": sample["label"],
        "request": sample["request"],
        "response": {
            "status": response["status"],
            "headers": response["headers"],
            "body": response["body"],
            "raw_body_byte_length": response["raw_body"]["byte_length"],
        },
    }


def _compare_caller_probes(current, reference):
    for mode in ("default", "no_autoflush"):
        expected_with_native_member_staged = {
            **{field: reference[mode][field] for field in _PROBE_FIELDS},
            "pending_member_in_route_result": True,
        }
        assert {
            field: current[mode][field] for field in _PROBE_FIELDS
        } == expected_with_native_member_staged


async def test_current_directory_owner_matches_frozen_samples_and_caller_probes(
    test_engine, app_module, monkeypatch
):
    reference = json.loads(_SAMPLE_PATH.read_text(encoding="utf-8"))
    assert len(reference["samples"]) == 13
    member_ids_by_user_id = {
        row["user_id"]: row["id"] for row in reference["samples"][0]["response"]["body"]
    }
    assert len(member_ids_by_user_id) == 6

    current = await capture_directory_scenario(
        test_engine,
        app_module,
        monkeypatch,
        member_ids_by_user_id=member_ids_by_user_id,
    )

    assert [_comparable(sample) for sample in current["samples"]] == [
        _comparable(sample) for sample in reference["samples"]
    ]
    _compare_caller_probes(
        current["observations"]["caller_autoflush"],
        reference["observations"]["caller_autoflush"],
    )
