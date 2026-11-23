"""Frozen Search and accepted-write projections through the borrowed API owner."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1 import projects as projects_api
from tests import test_workspace_project_query_consumers_legacy as scenarios
from tests.workspace_project_query_helpers import expected_project_sample

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _comparable(sample):
    response = sample["response"]
    body = json.loads(json.dumps(response["body"]))
    if isinstance(body, dict) and "projects" in body and "tasks" in body:
        groups = ["projects", "tasks"]
        if sample["request"]["actor"]["role"] != "super_admin":
            groups.append("members")
        for group in groups:
            body[group].sort(key=lambda item: item["id"])
    return {
        "label": sample["label"],
        "request": sample["request"],
        "response": {
            "status": response["status"],
            "headers": response["headers"],
            "body": body,
        },
        "commit_count": sample["activity"]["commit_count"],
    }


async def test_current_owner_matches_frozen_consumers_and_postcommit_authority(
    test_engine, app_module, monkeypatch, tmp_path
):
    reference = json.loads(
        (
            Path(__file__).parent
            / "fixtures/workspace_project_queries_legacy/consumers.json"
        ).read_text()
    )
    comparisons = []

    def compare(current):
        assert [_comparable(item) for item in current["samples"]] == [
            _comparable(expected_project_sample(item)) for item in reference["samples"]
        ]
        comparisons.append(len(current["samples"]))

    execute = projects_api.execute_project_query

    async def forward(db, _project, *, current_request):
        return await execute(db, **current_request)

    async def observed_execute(db, **kwargs):
        if kwargs["operation"] == "get_project_projection":
            return await projects_api._serialize_project(
                db, SimpleNamespace(id=kwargs["target_id"]), current_request=kwargs
            )
        return await execute(db, **kwargs)

    monkeypatch.setattr(projects_api, "_serialize_project", forward, raising=False)
    monkeypatch.setattr(projects_api, "execute_project_query", observed_execute)
    monkeypatch.setattr(scenarios, "_CAPTURE_PATH", tmp_path / "unused-current-run")
    monkeypatch.setattr(scenarios, "_source_metadata", lambda: {"current_owner": True})
    monkeypatch.setattr(scenarios, "_write_capture", compare)
    await scenarios.test_capture_legacy_project_query_consumers_and_postcommit_projections(
        test_engine, app_module, monkeypatch
    )
    assert comparisons == [15]
