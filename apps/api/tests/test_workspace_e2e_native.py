"""Replay guarded fixture consumers with Workspace private relations denied."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import _test_seed as routes
from app.db.models.dataset import Dataset
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.project_access import get_workspace_project
from app.services.seed_membership_data import SeedMembershipWrites
from app.services.workspace_e2e_fixtures import (
    create_fixture_project,
    create_fixture_templates,
)
from app.services.workspace_seed_data import register_seed_project_projection
from tests.factory import create_user
from tests import test_seed_owned as owned_cases
from tests import test_seed_router as seed_cases
from tests.test_identity_e2e_native import fenced_http as _fenced_http
from tests.test_identity_e2e_native import committed_owner as _committed_owner
from tests.test_workspace_consumer_profile_native import _assert_closed, _grants, _world

ROOT = Path(__file__).resolve().parents[3]
CAPTURE = os.getenv("AAP_WORKSPACE_E2E_CAPTURE") == "1"
ORACLE = ROOT / "apps/api/tests/fixtures/workspace_e2e_legacy.json"
NAMESPACE = "wkse2e"
fenced_http = _fenced_http
committed_owner = _committed_owner
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned disposable PostgreSQL",
    ),
]


@pytest.fixture
async def e2e_consumer(test_engine):
    async with _world(test_engine, apply=not CAPTURE) as (_, producer, role, _):
        profiles = ["identity-e2e-fixture-grants.sql"]
        if not CAPTURE:
            profiles += [
                "workspace-seed-operator-grants.sql",
                "workspace-e2e-fixture-grants.sql",
            ]
        for profile in profiles:
            result = await _grants(test_engine, ROOT / "infra/docker" / profile, role)
            assert result.returncode == 0, result.stderr
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION aap_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid), "
                    f"aap_assets_v1.assign_spatial_items(uuid,uuid,jsonb), "
                    f'aap_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb) TO "{role}"'
                )
            )
        if not CAPTURE:
            async with producer() as db:
                await _assert_closed(db)
        yield producer.kw["bind"]


async def _snapshot(engine):
    async with engine.connect() as db:
        projects = (
            await db.scalars(text("SELECT to_jsonb(p) FROM projects p ORDER BY name"))
        ).all()
        accounts = dict(
            (await db.execute(text("SELECT id::text,email FROM users"))).all()
        )
        names = {project["id"]: project["name"] for project in projects}
        members = (
            await db.scalars(
                text(
                    "SELECT to_jsonb(m) FROM project_members m ORDER BY project_id,user_id"
                )
            )
        ).all()
        templates = (
            await db.scalars(
                text("SELECT to_jsonb(t) FROM project_templates t ORDER BY name")
            )
        ).all()
    for project in projects:
        assert project.pop("created_at") and project.pop("updated_at")
        project.pop("id")
        project["owner_id"] = accounts[project["owner_id"]]
        project["ml_backend_pool_id"] = bool(project["ml_backend_pool_id"])
        project["display_id"] = re.sub(
            r"[a-f0-9]{6}$", "<random>", project["display_id"]
        )
    for member in members:
        member.pop("id")
        assert member.pop("assigned_at") and member.pop("updated_at")
        member["project_id"] = names[member["project_id"]]
        member["user_id"] = accounts[member["user_id"]]
        member["assigned_by"] = accounts[member["assigned_by"]]
    for template in templates:
        template.pop("id")
        assert template.pop("created_at") and template.pop("updated_at")
        template["created_by"] = accounts[template["created_by"]]
        template["source_project_id"] = names[template["source_project_id"]]
    return {
        "projects": projects,
        "members": sorted(members, key=lambda row: (row["project_id"], row["user_id"])),
        "templates": templates,
    }


@pytest.mark.parametrize("kind", ["owned", "project-roles", "filtering", "lidar"])
async def test_original_workspace_fixture_rows(
    fenced_http, test_engine, kind, record_property
):
    baseline = await _snapshot(test_engine)
    response = await fenced_http.post(
        "/api/v1/__test/seed/" + kind, json={"namespace": NAMESPACE}
    )
    assert response.status_code == 200, response.text
    try:
        state = await _snapshot(test_engine)
        for group, rows in baseline.items():
            assert all(row in state[group] for row in rows), group
            state[group] = [row for row in state[group] if row not in rows]
        if CAPTURE:
            directory = ROOT / ".worktree/workspace-e2e-baseline-cases"
            directory.mkdir(exist_ok=True)
            (directory / (kind + ".json")).write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n"
            )
        else:
            assert state == json.loads(ORACLE.read_text())["snapshots"][kind]
        record_property("workspace_e2e_original_rows", kind)
    finally:
        response = await fenced_http.post(
            "/api/v1/__test/seed/owned-cleanup", json={"namespace": NAMESPACE}
        )
        assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "scenario",
    [
        seed_cases.test_project_roles_seed_builds_multi_project_employee_matrix,
        seed_cases.test_project_roles_seed_is_idempotent,
        seed_cases.test_seed_reset_is_idempotent_with_singleton_pool,
        seed_cases.test_seed_cleanup_is_idempotent_and_preserves_non_e2e_data,
        seed_cases.test_seed_raster_mask_project_opt_in_and_content_fixtures,
        owned_cases.test_owned_cleanup_is_exact_idempotent_and_neighbour_safe,
        owned_cases.test_owned_rebuild_is_retry_safe,
        owned_cases.test_owned_filtering_fixture_is_namespaced_and_converges,
        owned_cases.test_legacy_shared_filtering_cleanup_reports_exact_failures,
        owned_cases.test_owned_lidar_fixture_is_namespaced,
        owned_cases.test_owned_project_roles_fixture_is_namespaced,
        owned_cases.test_owned_cleanup_exposes_residuals_on_partial_failure,
    ],
    ids=lambda scenario: scenario.__name__,
)
async def test_original_http_consumers_with_private_workspace_denied(
    scenario,
    fenced_http,
    committed_owner,
    monkeypatch,
    caplog,
    app_module,
    record_property,
):
    values = {
        "httpx_client": fenced_http,
        "db_session": committed_owner,
        "monkeypatch": monkeypatch,
        "caplog": caplog,
        "app_module": app_module,
    }
    arguments = inspect.signature(scenario).parameters
    assert set(arguments).issubset(values)
    await scenario(**{name: values[name] for name in arguments})
    record_property("workspace_e2e_original_http_case", scenario.__name__)


@pytest.mark.parametrize("autoflush", [True, False])
@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
async def test_fixture_writes_keep_cache_pending_work_and_transaction(
    e2e_consumer, test_engine, autoflush, commit
):
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as owner:
        actor = await create_user(
            owner, "super_admin", f"fixture-{uuid.uuid4()}@keeper.test", "Keeper"
        )
        actor_id = actor.id
    project_id = template_id = None
    pending_id = uuid.uuid4()
    try:
        async with async_sessionmaker(
            e2e_consumer, expire_on_commit=False, autoflush=autoflush
        )() as db:
            pending = Dataset(
                id=pending_id,
                display_id="D-" + uuid.uuid4().hex[:12],
                name="Pending fixture",
                data_type="image",
                created_by=actor_id,
            )
            db.add(pending)
            project = await create_fixture_project(
                db,
                owner_id=actor_id,
                name="Literal fixture",
                data_type="lidar",
                scene_mode=True,
                tool_bindings={"unknown": {"literal": [False, None, "雪"]}},
            )
            project_id = project.id
            assert pending in db and pending.id == pending_id
            assert await get_workspace_project(db, project_id=project_id) is project
            register_seed_project_projection(db, project_id)
            project.ai_enabled = True
            writes = SeedMembershipWrites(db)
            member = writes.add(
                project_id=project_id,
                user_id=actor_id,
                role="viewer",
                assigned_by=actor_id,
            )
            await writes.flush()
            assert (
                await db.scalar(
                    text(
                        "SELECT id FROM aap_workspace_v1.project_member_facts WHERE id=:id"
                    ),
                    {"id": member.id},
                )
                == member.id
            )
            records = await create_fixture_templates(
                db,
                [
                    {
                        "display_id": "TPL-" + uuid.uuid4().hex[:12],
                        "name": "Literal template",
                        "description": None,
                        "type_label": "LiDAR",
                        "type_key": "lidar",
                        "data_type": "lidar",
                        "tool_bindings": project.tool_bindings,
                        "label_config": {},
                        "scope": "private",
                        "created_by": actor_id,
                        "source_project_id": project_id,
                    }
                ],
            )
            template_id = records[0].id
            await (db.commit() if commit else db.rollback())
        async with test_engine.connect() as db:
            for table, identifier in (
                ("projects", project_id),
                ("project_templates", template_id),
                ("datasets", pending_id),
            ):
                assert (
                    bool(
                        await db.scalar(
                            text(f"SELECT count(*) FROM {table} WHERE id=:id"),
                            {"id": identifier},
                        )
                    )
                    is commit
                )
            if commit:
                row = (
                    await db.execute(
                        text(
                            "SELECT ai_enabled,scene_mode,tool_bindings FROM projects WHERE id=:id"
                        ),
                        {"id": project_id},
                    )
                ).one()
                assert row == (
                    True,
                    True,
                    {"unknown": {"literal": [False, None, "雪"]}},
                )
    finally:
        async with test_engine.begin() as db:
            await db.execute(
                text("DELETE FROM project_templates WHERE id=:id"), {"id": template_id}
            )
            await db.execute(
                text("DELETE FROM projects WHERE id=:id"), {"id": project_id}
            )
            await db.execute(
                text("DELETE FROM datasets WHERE id=:id"), {"id": pending_id}
            )
            await db.execute(text("DELETE FROM users WHERE id=:id"), {"id": actor_id})


async def test_unretired_video_blocks_fixture_cleanup_before_any_delete(
    fenced_http, test_engine, e2e_consumer, monkeypatch
):
    response = await fenced_http.post(
        "/api/v1/__test/seed/owned", json={"namespace": NAMESPACE}
    )
    assert response.status_code == 200, response.text
    project_id = uuid.UUID(response.json()["project_id"])
    job_id = uuid.uuid4()
    try:
        async with async_sessionmaker(
            test_engine, expire_on_commit=False
        ).begin() as db:
            task = (
                await db.execute(
                    text(
                        "SELECT id,dataset_item_id FROM tasks WHERE project_id=:id ORDER BY id LIMIT 1"
                    ),
                    {"id": project_id},
                )
            ).one()
            # Synthetic started history has no retirement evidence and must stay blocked.
            db.add(
                VideoTrackerJob(
                    id=job_id,
                    task_id=task.id,
                    dataset_item_id=task.dataset_item_id,
                    status="running",
                    started_at=datetime.now(UTC),
                    model_key="fixture",
                    direction="forward",
                    from_frame=0,
                    to_frame=1,
                    prompt={},
                    event_channel="e2e-retirement",
                )
            )
        before = await _snapshot(test_engine)
        async with test_engine.connect() as db:
            tasks = await db.scalar(
                text("SELECT count(*) FROM tasks WHERE project_id=:id"),
                {"id": project_id},
            )
        with monkeypatch.context() as patch:

            def no_object_delete(*args, **kwargs):
                pytest.fail("retirement refusal must precede storage deletion")

            patch.setattr(routes, "_delete_seed_object_prefix", no_object_delete)
            response = await fenced_http.post(
                "/api/v1/__test/seed/owned-cleanup", json={"namespace": NAMESPACE}
            )
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "video_execution_not_retired"
        assert await _snapshot(test_engine) == before
        async with async_sessionmaker(e2e_consumer)() as db:
            with pytest.raises(DBAPIError) as error:
                await db.scalar(
                    text(
                        "SELECT aap_workspace_fixture_v1.delete_projects(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [project_id]},
                )
            assert error.value.orig.sqlstate == "23514"
            await db.rollback()
        async with test_engine.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM tasks WHERE project_id=:id"),
                    {"id": project_id},
                )
                == tasks
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM aap_jobs_v1.video_deletion_clearances WHERE job_id=:id"
                    ),
                    {"id": job_id},
                )
                == 0
            )
    finally:
        async with test_engine.begin() as db:
            await db.execute(
                text("DELETE FROM video_tracker_jobs WHERE id=:id"), {"id": job_id}
            )
        response = await fenced_http.post(
            "/api/v1/__test/seed/owned-cleanup", json={"namespace": NAMESPACE}
        )
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("kind", ["project", "template"])
async def test_cleanup_detects_renamed_project_and_template_residuals(
    fenced_http, test_engine, monkeypatch, kind
):
    from app.db.models.project import Project
    from app.db.models.project_template import ProjectTemplate

    row_id = uuid.uuid4()
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as db:
        keeper = await create_user(
            db, "super_admin", f"residual-{uuid.uuid4()}@keeper.test", "Keeper"
        )
        keeper_id = keeper.id
        if kind == "project":
            db.add(
                Project(
                    id=row_id,
                    display_id=f"P-FI-I-{NAMESPACE}",
                    name="Renamed ordinary name",
                    type_label="Image",
                    type_key="image-det",
                    owner_id=keeper_id,
                )
            )
        else:
            db.add(
                ProjectTemplate(
                    id=row_id,
                    display_id=f"TPLFI-A-{NAMESPACE}",
                    name="Retained template",
                    type_label="Image",
                    type_key="image-det",
                    created_by=keeper_id,
                )
            )
    original = AsyncSession.execute
    operation = "delete_projects" if kind == "project" else "delete_templates"

    async def controlled_failure(self, statement, *args, **kwargs):
        if str(statement).startswith("SELECT aap_workspace_fixture_v1." + operation):
            raise RuntimeError("controlled fixture deletion failure")
        return await original(self, statement, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "execute", controlled_failure)
            response = await fenced_http.post(
                "/api/v1/__test/seed/owned-cleanup", json={"namespace": NAMESPACE}
            )
            assert response.status_code == 500, response.text
            assert response.json()["detail"]["residuals"][kind + "s"] == 1
        response = await fenced_http.post(
            "/api/v1/__test/seed/owned-cleanup", json={"namespace": NAMESPACE}
        )
        assert response.status_code == 200, response.text
    finally:
        async with test_engine.begin() as db:
            table = "projects" if kind == "project" else "project_templates"
            await db.execute(text(f"DELETE FROM {table} WHERE id=:id"), {"id": row_id})
            await db.execute(text("DELETE FROM users WHERE id=:id"), {"id": keeper_id})


async def test_lidar_cleanup_propagates_transaction_abort_before_storage(
    fenced_http, test_engine, monkeypatch
):
    response = await fenced_http.post(
        "/api/v1/__test/seed/lidar", json={"namespace": NAMESPACE}
    )
    assert response.status_code == 200, response.text
    before = await _snapshot(test_engine)
    original = AsyncSession.execute

    class ControlledAbort(RuntimeError):
        sqlstate = "40P01"

    async def controlled_abort(self, statement, *args, **kwargs):
        if str(statement) == str(routes._LABELING_FIXTURE_DELETE_SQL):
            raise ControlledAbort("controlled lidar cleanup abort")
        return await original(self, statement, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "execute", controlled_abort)
            patch.setattr(
                routes,
                "_delete_seed_object_prefix",
                lambda *a, **k: pytest.fail("aborted cleanup deleted storage"),
            )
            with pytest.raises(ControlledAbort):
                await fenced_http.post(
                    "/api/v1/__test/seed/lidar", json={"namespace": NAMESPACE}
                )
        assert await _snapshot(test_engine) == before
    finally:
        response = await fenced_http.post(
            "/api/v1/__test/seed/owned-cleanup", json={"namespace": NAMESPACE}
        )
        assert response.status_code == 200, response.text


async def test_node_fixed_fixture_capabilities_and_borrowed_rollback(
    e2e_consumer, test_engine, record_property
):
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as db:
        actor = await create_user(
            db, "super_admin", f"node-{uuid.uuid4()}@keeper.test", "Node owner"
        )
        actor_id = actor.id
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["node", str(ROOT / "apps/api/tests/fixtures/workspace_e2e_peer.mjs")],
            input=json.dumps(
                {
                    "database_url": e2e_consumer.url.set(
                        drivername="postgresql"
                    ).render_as_string(hide_password=False),
                    "owner_id": str(actor_id),
                }
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert value == {
            "ok": True,
            "fixed_functions": 5,
            "private_tables_denied": True,
            "rolled_back": True,
        }
        record_property("workspace_e2e_node_peer", value)
    finally:
        async with test_engine.begin() as db:
            await db.execute(text("DELETE FROM users WHERE id=:id"), {"id": actor_id})


async def test_fixture_capabilities_require_explicit_operator_profile(test_engine):
    async with _world(test_engine) as (_, producer, role, _):
        signatures = (
            "create_project(jsonb)",
            "create_templates(jsonb)",
            "guard_project_cleanup(uuid[])",
            "delete_projects(uuid[])",
            "delete_templates(boolean,text[])",
        )
        async with test_engine.connect() as owner:
            view_oid = await owner.scalar(
                text("SELECT 'aap_workspace_fixture_v1.templates'::regclass::oid")
            )
            function_oids = [
                await owner.scalar(
                    text("SELECT CAST(:signature AS regprocedure)::oid"),
                    {"signature": "aap_workspace_fixture_v1." + signature},
                )
                for signature in signatures
            ]

        async def allowed(expected):
            async with producer() as db:
                await _assert_closed(db)
                assert (
                    bool(
                        await db.scalar(
                            text(
                                "SELECT has_table_privilege(current_user,CAST(:oid AS oid),'SELECT')"
                            ),
                            {"oid": view_oid},
                        )
                    )
                    is expected
                )
                for oid in function_oids:
                    assert (
                        bool(
                            await db.scalar(
                                text(
                                    "SELECT has_function_privilege(current_user,CAST(:oid AS oid),'EXECUTE')"
                                ),
                                {"oid": oid},
                            )
                        )
                        is expected
                    )

        await allowed(False)
        result = await _grants(
            test_engine, ROOT / "infra/docker/workspace-e2e-fixture-grants.sql", role
        )
        assert result.returncode == 0, result.stderr
        await allowed(True)
        result = await _grants(
            test_engine, ROOT / "infra/docker/workspace-consumer-grants.sql", role
        )
        assert result.returncode == 0, result.stderr
        await allowed(False)


@pytest.mark.parametrize("state", ["23514", "55P03"])
async def test_final_cleanup_denial_rolls_back_children_and_stops_storage(
    fenced_http, test_engine, monkeypatch, state
):
    response = await fenced_http.post(
        "/api/v1/__test/seed/owned", json={"namespace": NAMESPACE}
    )
    assert response.status_code == 200, response.text
    before = await _snapshot(test_engine)
    project_id = uuid.UUID(response.json()["project_id"])

    async def children():
        async with test_engine.connect() as db:
            return {
                table: set(
                    (
                        await db.scalars(
                            text(f"SELECT id FROM {table} WHERE project_id=:id"),
                            {"id": project_id},
                        )
                    ).all()
                )
                for table in ("tasks", "predictions", "annotations")
            }

    before_children = await children()
    original = AsyncSession.execute

    class ControlledDenial(Exception):
        sqlstate = state

    async def denied(self, statement, *args, **kwargs):
        if str(statement).startswith(
            "SELECT aap_workspace_fixture_v1.delete_projects("
        ):
            raise IntegrityError(
                str(statement), {}, ControlledDenial("video_execution_not_retired")
            )
        return await original(self, statement, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "execute", denied)
            patch.setattr(
                routes,
                "_delete_seed_object_prefix",
                lambda *a, **k: pytest.fail("denied deletion continued to storage"),
            )
            response = await fenced_http.post(
                "/api/v1/__test/seed/owned-cleanup", json={"namespace": NAMESPACE}
            )
            assert response.status_code == 409, response.text
            expected = (
                {"code": "video_execution_not_retired"}
                if state == "23514"
                else {"reason": "project_busy"}
            )
            assert all(
                response.json()["detail"][key] == value
                for key, value in expected.items()
            )
        assert await _snapshot(test_engine) == before
        assert await children() == before_children
    finally:
        response = await fenced_http.post(
            "/api/v1/__test/seed/owned-cleanup", json={"namespace": NAMESPACE}
        )
        assert response.status_code == 200, response.text
