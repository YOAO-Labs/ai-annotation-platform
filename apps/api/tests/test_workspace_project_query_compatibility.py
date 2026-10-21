"""Replay the frozen Project scenarios against the current owner, without recapture."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
import os
from pathlib import Path

import pytest

from app.config import settings
from tests import test_workspace_project_queries_legacy as scenarios
from tests.workspace_project_query_helpers import (
    api_project_query_binding,
    project_query_briefs_dsn,
    project_query_dsn,
    project_query_provider,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize("binding", ["borrowed", "dedicated", "http"])
async def test_owner_matches_frozen_public_samples_and_caller_boundaries(
    test_engine, app_module, monkeypatch, tmp_path, binding
):
    reference = json.loads(
        (
            Path(__file__).parent
            / "fixtures/workspace_project_queries_legacy/samples.json"
        ).read_text()
    )
    comparisons = []

    def compare(current):
        assert current["samples"] == reference["samples"]
        assert current["direct_read_probes"] == reference["direct_read_probes"]
        comparisons.append(len(current["samples"]))

    for name in (
        "workspace_project_query_url",
        "workspace_project_query_database_url",
        "workspace_project_query_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")
    # Reuse only the scenario harness. The original one-time capture and its
    # source guard stay untouched; this run neither creates nor replaces evidence.
    monkeypatch.setattr(scenarios, "_CAPTURE_PATH", tmp_path / "unused-current-run")
    monkeypatch.setattr(scenarios, "_source_metadata", lambda: {"current_owner": True})
    monkeypatch.setattr(scenarios, "_write_capture", compare)
    original_probe = scenarios._direct_read_probe

    async def borrowed_probe(**kwargs):
        # These two probes freeze the default caller transaction only; dedicated
        # providers intentionally cannot see another connection's pending work.
        async with api_project_query_binding(token="test-query"):
            return await original_probe(**kwargs)

    monkeypatch.setattr(scenarios, "_direct_read_probe", borrowed_probe)
    async with AsyncExitStack() as stack:
        token = "test-query"
        url = dsn = None
        if binding == "dedicated":
            dsn = project_query_dsn(test_engine)
        elif binding == "http":
            url = await stack.enter_async_context(
                project_query_provider(
                    project_query_dsn(test_engine),
                    token,
                    briefs_dsn=project_query_briefs_dsn(test_engine),
                )
            )
        await stack.enter_async_context(
            api_project_query_binding(
                url=url,
                dsn=dsn,
                token=token,
            )
        )
        await scenarios.test_capture_legacy_project_query_and_projection_samples(
            test_engine, app_module, monkeypatch
        )
    assert comparisons == [len(reference["samples"])]
