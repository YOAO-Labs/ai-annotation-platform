"""Replay frozen temporal HTTP responses without replacing legacy evidence."""

from contextlib import AsyncExitStack
import json
import os
from pathlib import Path

import pytest

from tests import test_workspace_project_query_temporal_extremes_legacy as scenarios
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
async def test_current_owner_matches_frozen_temporal_http_responses(
    test_engine, app_module, monkeypatch, tmp_path, binding
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

    def public_reads(capture):
        return [
            {key: item[key] for key in public_fields}
            for item in capture["public_reads"]
        ]

    def compare(current):
        # SQL spelling and internal exception wrappers vary by binding. Public
        # status/content type/body, including successful infinity values, do not.
        assert public_reads(current) == public_reads(reference)
        comparisons.append(len(current["public_reads"]))

    monkeypatch.setattr(scenarios, "_CAPTURE_PATH", tmp_path / "unused-current-run")
    monkeypatch.setattr(scenarios, "_source_metadata", lambda: {"current_owner": True})
    monkeypatch.setattr(scenarios, "_write_capture", compare)
    async with AsyncExitStack() as stack:
        token = "test-temporal-query"
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
        await scenarios.test_capture_legacy_project_temporal_extremes(
            test_engine, app_module
        )
    assert comparisons == [8]
