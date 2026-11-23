"""Replay the frozen Project-access contract through the borrowed API owner."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.workspace_project_access_query_helpers import (
    replay_workspace_project_access,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_FIXTURE = Path(__file__).parent / "fixtures/workspace_access_reads_legacy/samples.json"
_FIXTURE_SHA256 = "d5d579a4ac384bc04891d9d7378eebdf01a01397351c7aa2d33d23154270603d"


def _public_http_sample(sample: dict) -> dict:
    response = sample["response"]
    return {
        "label": sample["label"],
        "request": {
            "method": sample["request"]["method"],
            "path": sample["request"]["path"],
            "actor_alias": sample["request"]["actor"]["alias"],
        },
        "response": {
            "status": response["status"],
            "headers": response["headers"],
            "body": response["body"],
            "raw_body": {"byte_length": response["raw_body"]["byte_length"]},
        },
    }


def _resolver_probe(sample: dict) -> dict:
    fields = (
        "status",
        "access",
        "detail",
        "no_autoflush",
        "caller_transaction_open_after_read",
        "visible_from_owner_before_caller_rollback",
        "pending_user_id",
        "caller",
    )
    return {name: sample[name] for name in fields if name in sample}


async def test_current_project_access_owner_matches_frozen_public_samples(
    test_engine, app_module, monkeypatch
):
    fixture_bytes = _FIXTURE.read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == _FIXTURE_SHA256
    reference = json.loads(fixture_bytes)
    assert reference["capture_kind"] == "actual-pre-extraction-project-access-http"
    assert len(reference["samples"]) == 16

    current = await replay_workspace_project_access(
        test_engine, app_module, monkeypatch, reference["samples"]
    )

    expected_http = [_public_http_sample(sample) for sample in reference["samples"]]
    assert current["samples"] == expected_http

    expected_resolver = reference["observations"]["resolver_caller_autoflush"]
    assert current["resolver_caller_autoflush"] == {
        "caller": expected_resolver["caller"],
        "default": _resolver_probe(expected_resolver["default"]),
        "no_autoflush": _resolver_probe(expected_resolver["no_autoflush"]),
    }
