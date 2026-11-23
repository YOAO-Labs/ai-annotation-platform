"""Replay frozen Project HTTP scenarios through the borrowed API owner."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests import test_workspace_project_queries_legacy as scenarios
from tests.workspace_project_query_helpers import expected_project_sample

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def test_owner_matches_frozen_public_samples_and_caller_boundaries(
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
        assert current["samples"] == [
            expected_project_sample(sample) for sample in reference["samples"]
        ]
        assert current["direct_read_probes"] == reference["direct_read_probes"]
        comparisons.append(len(current["samples"]))

    monkeypatch.setattr(scenarios, "_CAPTURE_PATH", tmp_path / "unused-current-run")
    monkeypatch.setattr(scenarios, "_source_metadata", lambda: {"current_owner": True})
    monkeypatch.setattr(scenarios, "_write_capture", compare)
    await scenarios.test_capture_legacy_project_query_and_projection_samples(
        test_engine, app_module, monkeypatch
    )
    assert comparisons == [len(reference["samples"])]
