"""Replay frozen public directory samples against each current owner binding."""

from __future__ import annotations

from contextlib import AsyncExitStack
import json
import os
from pathlib import Path

import pytest

from app.config import settings
from tests.workspace_directory_query_helpers import (
    api_directory_query_binding,
    capture_directory_scenario,
    directory_query_dsn,
    directory_query_provider,
)
from tests.workspace_project_query_helpers import project_query_briefs_dsn

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
        assert {field: current[mode][field] for field in _PROBE_FIELDS} == {
            field: reference[mode][field] for field in _PROBE_FIELDS
        }


@pytest.mark.parametrize("binding", ["borrowed", "dedicated", "http"])
async def test_current_directory_owner_matches_frozen_samples_and_caller_probes(
    test_engine, app_module, monkeypatch, binding
):
    reference = json.loads(_SAMPLE_PATH.read_text(encoding="utf-8"))
    assert len(reference["samples"]) == 13
    member_ids_by_user_id = {
        row["user_id"]: row["id"] for row in reference["samples"][0]["response"]["body"]
    }
    assert len(member_ids_by_user_id) == 6

    for name in (
        "workspace_directory_query_url",
        "workspace_directory_query_database_url",
        "workspace_directory_query_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")

    async with AsyncExitStack() as stack:
        token = "directory-query-control"
        url = dsn = None
        if binding == "dedicated":
            dsn = directory_query_dsn(test_engine)
        elif binding == "http":
            briefs_dsn = project_query_briefs_dsn(test_engine)
            url = await stack.enter_async_context(
                directory_query_provider(
                    directory_query_dsn(test_engine), token, briefs_dsn
                )
            )
        await stack.enter_async_context(
            api_directory_query_binding(url=url, dsn=dsn, token=token)
        )
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
