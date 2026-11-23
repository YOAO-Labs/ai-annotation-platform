"""Replay frozen temporal HTTP responses without replacing legacy evidence."""

import json
import os
from pathlib import Path

import pytest

from tests import test_workspace_project_query_temporal_extremes_legacy as scenarios
from tests.workspace_project_query_helpers import expected_project_sample

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def test_current_owner_matches_frozen_temporal_http_responses(
    test_engine, app_module, monkeypatch, tmp_path
):
    reference = json.loads(
        (
            Path(__file__).parent
            / "fixtures/workspace_project_queries_legacy/temporal-extremes.json"
        ).read_text()
    )
    comparisons = []
    public_fields = (
        "case",
        "column",
        "expression",
        "endpoint",
        "method",
        "path",
        "actor_role",
        "response",
    )

    def public_reads(samples):
        return [{key: item[key] for key in public_fields} for item in samples]

    def compare(current):
        expected = [expected_project_sample(item) for item in reference["public_reads"]]
        assert public_reads(current["public_reads"]) == public_reads(expected)
        comparisons.append(len(current["public_reads"]))

    monkeypatch.setattr(scenarios, "_CAPTURE_PATH", tmp_path / "unused-current-run")
    monkeypatch.setattr(scenarios, "_source_metadata", lambda: {"current_owner": True})
    monkeypatch.setattr(scenarios, "_write_capture", compare)
    await scenarios.test_capture_legacy_project_temporal_extremes(
        test_engine, app_module
    )
    assert comparisons == [8]
