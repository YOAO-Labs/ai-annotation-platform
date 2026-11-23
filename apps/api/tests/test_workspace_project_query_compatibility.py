"""Replay frozen Project HTTP scenarios through the borrowed API owner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import pytest

from yoao_workspace.configuration_requests import ProjectCreate
from app.api.v1.projects import list_projects
from app.db.models.project import Project
from app.db.models.user import User
from app.services.identity_contracts import IdentityActorFacts
from app.services.workspace_configuration import execute_configuration
from tests import test_workspace_project_queries_legacy as scenarios
from tests.workspace_project_query_helpers import (
    expected_project_sample,
    query_with_pending_orm_work,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _borrowed_project_probe(
    *, admin_maker, runtime_maker, actor: User, no_autoflush: bool
):
    facts = IdentityActorFacts(
        id=actor.id,
        name=actor.name,
        email=actor.email,
        role=actor.role,
        is_active=True,
    )
    name = f"Uncommitted project query probe {uuid.uuid4()}"
    payload = ProjectCreate(
        name=name, type_label="Image label", type_key="image-det"
    ).model_dump(exclude_unset=True)
    async with runtime_maker() as db:

        async def read_projects():
            return await list_projects(
                search=name, type_key=None, data_type=None, db=db, user=facts
            )

        assert await read_projects() == []
        staged = await execute_configuration(
            db, operation="create_project", actor_id=actor.id, payload=payload
        )
        async with query_with_pending_orm_work(
            db, no_autoflush=no_autoflush
        ) as pending:
            rows = await read_projects()
        included = [row["id"] for row in rows] == [staged.resource_id]
        pending_in_new = pending in db.new
        transaction_open = db.in_transaction()
        async with admin_maker() as observer:
            committed = await observer.get(Project, staged.resource_id) is not None
        await db.rollback()
        assert await read_projects() == []
    async with admin_maker() as observer:
        assert await observer.get(Project, staged.resource_id) is None
    return {
        "no_autoflush": no_autoflush,
        "included_in_route_result": included,
        "pending_in_session_new_after_read": pending_in_new,
        "caller_transaction_open_after_read": transaction_open,
        "visible_from_owner_before_caller_rollback": committed,
    }


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
        expected_with_native_write_staged = [
            {**probe, "included_in_route_result": True}
            for probe in reference["direct_read_probes"]
        ]
        assert current["direct_read_probes"] == expected_with_native_write_staged
        comparisons.append(len(current["samples"]))

    monkeypatch.setattr(scenarios, "_CAPTURE_PATH", tmp_path / "unused-current-run")
    monkeypatch.setattr(scenarios, "_source_metadata", lambda: {"current_owner": True})
    monkeypatch.setattr(scenarios, "_write_capture", compare)
    await scenarios.test_capture_legacy_project_query_and_projection_samples(
        test_engine, app_module, monkeypatch, direct_read_probe=_borrowed_project_probe
    )
    assert comparisons == [len(reference["samples"])]
