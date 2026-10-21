"""Replay the frozen Project scenarios against the current owner, without recapture."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.config import settings
from tests import test_workspace_project_queries_legacy as scenarios

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def test_borrowed_owner_matches_frozen_public_samples_and_caller_boundaries(
    test_engine, app_module, monkeypatch, tmp_path
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
    await scenarios.test_capture_legacy_project_query_and_projection_samples(
        test_engine, app_module, monkeypatch
    )
    assert comparisons == [len(reference["samples"])]
