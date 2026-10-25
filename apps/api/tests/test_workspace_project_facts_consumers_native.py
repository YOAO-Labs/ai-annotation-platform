"""Restricted-consumer coverage for native Project facts and ORM callers."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import uuid

import httpx
import pytest
from sqlalchemy import delete, event, text

from aap_workspace_contract.project_query_contract import PROJECT_ROW_COLUMNS
from aap_workspace_contract.read_relations import PROJECT_COUNTER_COLUMNS
from app.config import settings
from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.prediction import FailedPrediction
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.services.dataset_task_facts import ProjectTaskSourceRead
from app.services.labeling_assets import SqlAlchemyProjectTaskSourceReader
from tests.factory import create_batch, create_project, create_user
from tests.test_workspace_actor_scope_native import (
    _assert_private_denied,
    _consumer_world,
)
from tests.test_workspace_project_exclusive_native import _configure_api_auth
from tests.workspace_project_query_helpers import project_query_briefs_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned Workspace Project facts and actual API LOGIN",
    ),
]

_PEER = Path(__file__).parent / "fixtures/workspace_project_facts_peer.mjs"


def _configure_actual_api(test_engine, monkeypatch) -> None:
    _configure_api_auth(test_engine, monkeypatch)
    monkeypatch.setattr(settings, "identity_briefs_database_url_file", "")
    monkeypatch.setattr(
        settings,
        "identity_briefs_database_url",
        project_query_briefs_dsn(test_engine),
    )


def _install_read_only_api_db(app_module, monkeypatch, api) -> None:
    from app.deps import get_db

    async def request_db():
        async with api() as db:
            await _assert_private_denied(db)
            await db.rollback()
            await db.execute(text("SET TRANSACTION READ ONLY"))
            try:
                yield db
            finally:
                await db.rollback()

    monkeypatch.setitem(app_module.dependency_overrides, get_db, request_db)


def _headers(actor_id: uuid.UUID, role: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token(subject=str(actor_id), role=role)}"
    }


async def _add_foreign_project(owner, world: dict, *, name: str):
    async with owner.begin() as db:
        foreign_owner = await create_user(
            db,
            "project_admin",
            f"project-facts-{uuid.uuid4().hex}@example.test",
            "Foreign project owner",
        )
        project = await create_project(db, owner_id=foreign_owner.id, name=name)
    world["actor_ids"].append(foreign_owner.id)
    world["project_ids"].append(project.id)
    return foreign_owner, project


async def _seed_ready_run(db, *, project_id, creator_id, name: str) -> None:
    dataset = TrainingDatasetVersion(
        project_id=project_id,
        creator_id=creator_id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        snapshot_digest="b" * 64,
        name=f"{name} dataset",
        snapshot={},
        status="ready",
        manifest={"schema": 1, "classes": ["car"]},
        package_key=f"owned/{name}.zip",
        package_sha256="e" * 64,
    )
    db.add(dataset)
    await db.flush()
    db.add(
        TrainingRun(
            project_id=project_id,
            creator_id=creator_id,
            dataset_id=dataset.id,
            request_id=uuid.uuid4(),
            request_digest="c" * 64,
            name=name,
            status="succeeded",
            config={"framework": "ultralytics", "parameters": {"epochs": 2}},
            config_digest="d" * 64,
        )
    )


async def test_node_reads_workspace_and_labeling_project_facts_without_private_select(
    test_engine,
):
    async with _consumer_world(test_engine) as (_owner, _api, world, url):
        workspace_columns = [
            column
            for column in PROJECT_ROW_COLUMNS
            if column not in PROJECT_COUNTER_COLUMNS
        ]
        counter_columns = ["project_id", *sorted(PROJECT_COUNTER_COLUMNS)]
        assert len(workspace_columns) == 38
        assert len(counter_columns) == 6
        assert len(PROJECT_ROW_COLUMNS) == 43

        process = await asyncio.create_subprocess_exec(
            "node",
            str(_PEER),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(
                json.dumps(
                    {
                        "database_url": url.set(
                            drivername="postgresql"
                        ).render_as_string(hide_password=False),
                        "project_id": str(world["project_id"]),
                        "owner_id": str(world["owner_id"]),
                        "workspace_columns": workspace_columns,
                        "counter_columns": counter_columns,
                        "project_columns": list(PROJECT_ROW_COLUMNS),
                    }
                ).encode(),
            ),
            timeout=20,
        )
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout) == {
            "ok": True,
            "workspace_columns": 38,
            "counter_columns": 6,
            "project_columns": 43,
            "read_only": True,
            "same_transaction": True,
        }


async def test_training_project_and_run_discovery_keep_owner_scope(
    test_engine, app_module, monkeypatch
):
    _configure_actual_api(test_engine, monkeypatch)
    monkeypatch.setattr(settings, "training_enabled", False)
    async with _consumer_world(test_engine) as (owner, api, world, _url):
        _, foreign = await _add_foreign_project(
            owner, world, name="Foreign training project"
        )
        try:
            async with owner.begin() as db:
                await _seed_ready_run(
                    db,
                    project_id=world["project_id"],
                    creator_id=world["owner_id"],
                    name="owned-run",
                )
                await _seed_ready_run(
                    db,
                    project_id=foreign.id,
                    creator_id=foreign.owner_id,
                    name="foreign-run",
                )
            _install_read_only_api_db(app_module, monkeypatch, api)
            headers = _headers(world["owner_id"], "project_admin")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module), base_url="http://test"
            ) as client:
                projects = await client.get(
                    "/api/v1/training/projects", headers=headers
                )
                runs = await client.get(
                    "/api/v1/training/runs?state=all", headers=headers
                )
                foreign_runs = await client.get(
                    f"/api/v1/training/runs?state=all&project_id={foreign.id}",
                    headers=headers,
                )
            assert projects.status_code == 200, projects.text
            assert runs.status_code == 200, runs.text
            assert foreign_runs.status_code == 200, foreign_runs.text
            project_ids = {item["id"] for item in projects.json()["items"]}
            assert str(world["project_id"]) in project_ids
            assert str(foreign.id) not in project_ids
            assert runs.json()["total"] == 1
            assert runs.json()["items"][0]["run"]["name"] == "owned-run"
            assert foreign_runs.json()["total"] == 0
        finally:
            async with owner.begin() as db:
                await db.execute(
                    delete(TrainingRun).where(
                        TrainingRun.project_id.in_(world["project_ids"])
                    )
                )
                await db.execute(
                    delete(TrainingDatasetVersion).where(
                        TrainingDatasetVersion.project_id.in_(world["project_ids"])
                    )
                )


async def test_admin_overview_and_person_project_distribution_use_facts(
    test_engine, app_module, monkeypatch
):
    _configure_actual_api(test_engine, monkeypatch)
    async with _consumer_world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as db:
            superadmin = await create_user(
                db,
                "super_admin",
                f"project-facts-admin-{uuid.uuid4().hex}@example.test",
                "Project facts superadmin",
            )
            db.add(
                Annotation(
                    task_id=world["task_id"],
                    project_id=world["project_id"],
                    user_id=world["member_id"],
                    class_name="car",
                    geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                    is_active=True,
                )
            )
        world["actor_ids"].append(superadmin.id)
        try:
            _install_read_only_api_db(app_module, monkeypatch, api)
            headers = _headers(superadmin.id, "super_admin")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module), base_url="http://test"
            ) as client:
                overview = await client.get("/api/v1/dashboard/admin", headers=headers)
                detail = await client.get(
                    f"/api/v1/dashboard/admin/people/{world['member_id']}"
                    f"?project={world['project_id']}",
                    headers=headers,
                )
            assert overview.status_code == 200, overview.text
            assert overview.json()["total_projects"] >= 1
            assert detail.status_code == 200, detail.text
            distribution = detail.json()["project_distribution"]
            assert distribution == [
                {
                    "project_id": str(world["project_id"]),
                    "project_name": "E2E Project",
                    "count": 1,
                }
            ]
        finally:
            async with owner.begin() as db:
                await db.execute(
                    delete(Annotation).where(
                        Annotation.project_id == world["project_id"]
                    )
                )


async def test_preannotate_queue_returns_only_owned_nonempty_batch(
    test_engine, app_module, monkeypatch
):
    _configure_actual_api(test_engine, monkeypatch)
    async with _consumer_world(test_engine, role="reviewer") as (
        owner,
        api,
        world,
        _url,
    ):
        _, foreign = await _add_foreign_project(
            owner, world, name="Foreign preannotation project"
        )
        async with owner.begin() as db:
            owned_batch = await db.get(TaskBatch, world["batch_id"])
            owned_batch.status = "pre_annotated"
            owned_batch.name = "Owned ready batch"
            foreign_batch = await create_batch(
                db,
                project_id=foreign.id,
                name="Foreign ready batch",
                status="pre_annotated",
            )
            owned_batch_id = owned_batch.id
            foreign_batch_id = foreign_batch.id
        _install_read_only_api_db(app_module, monkeypatch, api)
        headers = _headers(world["owner_id"], "project_admin")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/admin/preannotate-queue", headers=headers
            )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["batch_id"] for item in items] == [str(owned_batch_id)]
        assert items[0]["batch_status"] == "pre_annotated"
        assert items[0]["project_id"] == str(world["project_id"])
        assert items[0]["project_name"] == "E2E Project"
        assert items[0]["total_tasks"] == 1
        assert str(foreign_batch_id) not in {item["batch_id"] for item in items}


async def test_failed_prediction_list_handles_missing_task_and_backend_in_owner_scope(
    test_engine, app_module, monkeypatch
):
    _configure_actual_api(test_engine, monkeypatch)
    async with _consumer_world(test_engine) as (owner, api, world, _url):
        _, foreign = await _add_foreign_project(
            owner, world, name="Foreign failed-prediction project"
        )
        async with owner.begin() as db:
            owned_failure = FailedPrediction(
                project_id=world["project_id"],
                task_id=None,
                ml_backend_id=None,
                error_type="EMPTY_RESULT",
                message="missing task and backend",
            )
            db.add_all(
                [
                    owned_failure,
                    FailedPrediction(
                        project_id=foreign.id,
                        task_id=None,
                        ml_backend_id=None,
                        error_type="FOREIGN_RESULT",
                        message="must remain hidden",
                    ),
                ]
            )
            await db.flush()
            failure_id = owned_failure.id
        try:
            _install_read_only_api_db(app_module, monkeypatch, api)
            headers = _headers(world["owner_id"], "project_admin")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/v1/admin/failed-predictions", headers=headers
                )
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["total"] == 1
            assert len(data["items"]) == 1
            item = data["items"][0]
            assert item["id"] == str(failure_id)
            assert item["task_id"] is None and item["task_display_id"] is None
            assert item["ml_backend_id"] is None and item["backend_name"] is None
            assert item["project_id"] == str(world["project_id"])
            assert item["project_name"] == "E2E Project"
        finally:
            async with owner.begin() as db:
                await db.execute(
                    delete(FailedPrediction).where(
                        FailedPrediction.project_id.in_(world["project_ids"])
                    )
                )


async def test_labeling_source_reader_stream_uses_facts_and_refreshes_task(
    test_engine, monkeypatch
):
    async with _consumer_world(test_engine) as (_owner, api, world, _url):
        async with api() as db:
            await _assert_private_denied(db)
            await db.rollback()
            await db.execute(text("SET TRANSACTION READ ONLY"))
            task = await db.get(Task, world["task_id"])
            persisted_status = task.status
            task.status = "stale_identity_map_value"
            db.autoflush = False
            statements = []
            stream_options = {}
            original_stream = db.stream

            async def capture_stream(statement, *args, **kwargs):
                stream_options.update(statement.get_execution_options())
                return await original_stream(statement, *args, **kwargs)

            monkeypatch.setattr(db, "stream", capture_stream)

            def capture(_connection, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
            try:
                facts = await SqlAlchemyProjectTaskSourceReader(
                    db
                ).read_project_task_source_facts(
                    ProjectTaskSourceRead(
                        project_id=world["project_id"],
                        tool_unit_id="bbox",
                        task_ids=(world["task_id"],),
                        limit=1,
                    )
                )
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
            assert len(statements) == 1
            assert "aap_workspace_v1.project_facts" in statements[0]
            assert "from projects" not in statements[0].lower()
            assert stream_options["populate_existing"] is True
            assert stream_options["yield_per"] == 128
            assert len(facts.tasks) == 1
            assert facts.tasks[0].task_id == world["task_id"]
            assert task.status == persisted_status
            await db.rollback()
