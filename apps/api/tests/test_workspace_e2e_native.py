"""Owner-process fixtures, restricted Workspace consumers and owner fault checks."""

from __future__ import annotations

import asyncio
from contextlib import closing
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
from tests import seed_owned_scenarios as owned_cases
from tests import seed_router_scenarios as seed_cases
from tests.identity_e2e_support import (
    committed_owner as committed_owner,
    fenced_http as fenced_http,
    fixture_and_fenced_http as fixture_and_fenced_http,
    fixture_routing_owner,
    owner_fault_http as owner_fault_http,
    scenario_rle_bindings as scenario_rle_bindings,
)
from tests.seed_owner_support import (
    _fixture_versions,
    _read_versions,
    OwnedSuite,
    fixture_original_reader,
    private_seed_owner,
)
from tests.test_training_dataset_references import (
    committed_rle_bindings as committed_rle_bindings,
)
from tests.test_workspace_consumer_profile_native import _assert_closed, _grants, _world

ROOT = Path(__file__).resolve().parents[3]
NAMESPACE = "wkse2e"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned disposable PostgreSQL",
    ),
]


@pytest.fixture
async def e2e_consumer(test_engine):
    async with _world(test_engine, apply=True) as (_, producer, role, _):
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION yoao_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid), "
                    f"yoao_assets_v1.assign_spatial_items(uuid,uuid,jsonb), "
                    f'yoao_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb) TO "{role}"'
                )
            )
        async with producer() as db:
            await _assert_closed(db)
        yield producer.kw["bind"]


@pytest.fixture
async def fixture_operator(test_engine):
    """A separate LOGIN for explicit Workspace fixture capability checks."""
    async with _world(test_engine, apply=True) as (_, producer, role, _):
        for profile in (
            "identity-e2e-fixture-grants.sql",
            "workspace-seed-operator-grants.sql",
            "workspace-e2e-fixture-grants.sql",
        ):
            result = await _grants(test_engine, ROOT / "infra/docker" / profile, role)
            assert result.returncode == 0, result.stderr
        yield producer.kw["bind"]


@pytest.fixture
async def fault_suite(test_engine, monkeypatch):
    """Keep in-process fault cases under a suite without native participants."""
    async with (
        fixture_routing_owner(test_engine, monkeypatch),
        private_seed_owner() as owner,
    ):
        suite = OwnedSuite(owner, uuid.uuid4())
        await owner.prepare_suite(suite.suite_id, "owned")
        failure = None
        try:
            yield suite
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if owner.failure is not None:
                if failure is None:
                    raise owner.failure
            else:
                try:
                    await suite.finish()
                    await owner.shutdown_authority(suite.suite_id)
                except BaseException as cleanup_failure:
                    if failure is not None:
                        raise BaseExceptionGroup(
                            "Fixture operation and suite cleanup failed",
                            [failure, cleanup_failure],
                        ) from None
                    raise


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


@pytest.mark.parametrize(
    "scenario",
    [
        seed_cases.project_roles_seed_builds_multi_project_employee_matrix,
        seed_cases.project_roles_seed_rebuilds_after_cleanup,
        seed_cases.seed_owned_rebuilds_with_singleton_pool,
        seed_cases.seed_cleanup_is_idempotent_and_preserves_non_e2e_data,
        seed_cases.seed_raster_mask_project_opt_in_and_content_fixtures,
        owned_cases.owned_cleanup_is_exact_idempotent_and_neighbour_safe,
        owned_cases.owned_rebuild_is_retry_safe,
        owned_cases.owned_filtering_fixture_is_namespaced_and_converges,
        owned_cases.owned_lidar_fixture_is_namespaced,
        owned_cases.owned_project_roles_fixture_is_namespaced,
    ],
    ids=lambda scenario: "test_" + scenario.__name__,
)
async def test_owner_fixture_scenarios_with_fenced_workspace_business_http(
    scenario,
    scenario_rle_bindings,
    fixture_and_fenced_http,
    committed_owner,
    record_property,
):
    values = {
        "httpx_client": fixture_and_fenced_http,
        "db_session": committed_owner,
    }
    arguments = inspect.signature(scenario).parameters
    assert set(arguments).issubset(values)
    await scenario(**{name: values[name] for name in arguments})
    record_property(
        "workspace_owner_fixture_fenced_business_case", "test_" + scenario.__name__
    )


@pytest.mark.parametrize("autoflush", [True, False])
@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
async def test_fixture_writes_keep_cache_pending_work_and_transaction(
    fixture_operator, test_engine, autoflush, commit
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
            fixture_operator, expire_on_commit=False, autoflush=autoflush
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
                        "SELECT id FROM yoao_workspace_v1.project_member_facts WHERE id=:id"
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


async def test_owner_unretired_video_blocks_fixture_cleanup_before_any_delete(
    owner_fault_http,
    fault_suite,
    test_engine,
    fixture_operator,
    monkeypatch,
):
    from app.api.v1 import _test_seed_attachments as attachments

    run = fault_suite.new_run(NAMESPACE)
    response = await owner_fault_http.post(
        "/api/v1/__test/seed/owned", json=run.payload()
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

            async def no_owned_object_delete(*args, **kwargs):
                pytest.fail("retirement refusal must precede owner object deletion")

            patch.setattr(attachments, "delete_objects", no_owned_object_delete)
            patch.setattr(
                routes, "_delete_fixture_video_objects", no_owned_object_delete
            )
            patch.setattr(routes, "_reclaim_owned_originals", no_owned_object_delete)
            response = await owner_fault_http.post(
                "/api/v1/__test/seed/owned-cleanup", json=run.payload()
            )
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "video_execution_not_retired"
        assert await _snapshot(test_engine) == before
        async with async_sessionmaker(fixture_operator)() as db:
            with pytest.raises(DBAPIError) as error:
                await db.scalar(
                    text(
                        "SELECT yoao_workspace_fixture_v1.delete_projects(CAST(:ids AS uuid[]))"
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
                        "SELECT count(*) FROM yoao_jobs_v1.video_deletion_clearances WHERE job_id=:id"
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
        response = await owner_fault_http.post(
            "/api/v1/__test/seed/owned-cleanup", json=run.payload()
        )
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("kind", ["project", "template"])
async def test_owner_cleanup_detects_renamed_project_and_template_residuals(
    owner_fault_http, fault_suite, test_engine, monkeypatch, kind
):
    from app.db.models.project import Project
    from app.db.models.project_template import ProjectTemplate

    run = fault_suite.new_run(NAMESPACE)
    built = await fault_suite.owner.post(
        "/api/v1/__test/seed/filtering", json=run.payload()
    )
    assert built.status_code == 200, built.text
    manifest = built.json()
    target_id = uuid.UUID(
        manifest["image"]["project_id"]
        if kind == "project"
        else manifest["operations"]["template_ids"][0]
    )
    model = Project if kind == "project" else ProjectTemplate
    keeper_row_id = uuid.uuid4()
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as db:
        target = await db.get(model, target_id)
        old_display = target.display_id
        target.display_id = f"RENAMED-{uuid.uuid4().hex[:12]}"
        target.name = "Renamed owned fixture"
        await db.flush()
        keeper = await create_user(
            db, "super_admin", f"residual-{uuid.uuid4()}@keeper.test", "Keeper"
        )
        keeper_id = keeper.id
        if kind == "project":
            db.add(
                Project(
                    id=keeper_row_id,
                    display_id=old_display,
                    name="Ordinary neighbor",
                    type_label="Image",
                    type_key="image-det",
                    owner_id=keeper_id,
                )
            )
        else:
            db.add(
                ProjectTemplate(
                    id=keeper_row_id,
                    display_id=old_display,
                    name="Ordinary neighbor",
                    type_label="Image",
                    type_key="image-det",
                    created_by=keeper_id,
                )
            )
    original = AsyncSession.execute
    operation = "delete_projects" if kind == "project" else "delete_templates"

    async def controlled_failure(self, statement, *args, **kwargs):
        if str(statement).startswith("SELECT yoao_workspace_fixture_v1." + operation):
            raise RuntimeError("controlled fixture deletion failure")
        return await original(self, statement, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "execute", controlled_failure)
            with pytest.raises(
                RuntimeError, match="controlled fixture deletion failure"
            ):
                await owner_fault_http.post(
                    "/api/v1/__test/seed/owned-cleanup", json=run.payload()
                )
        async with async_sessionmaker(test_engine)() as db:
            assert await db.get(model, target_id) is not None
            assert await db.get(model, keeper_row_id) is not None
        response = await owner_fault_http.post(
            "/api/v1/__test/seed/owned-cleanup", json=run.payload()
        )
        assert response.status_code == 200, response.text
        async with async_sessionmaker(test_engine)() as db:
            assert await db.get(model, target_id) is None
            assert await db.get(model, keeper_row_id) is not None
    finally:
        async with test_engine.begin() as db:
            table = "projects" if kind == "project" else "project_templates"
            await db.execute(
                text(f"DELETE FROM {table} WHERE id=:id"), {"id": keeper_row_id}
            )
            await db.execute(text("DELETE FROM users WHERE id=:id"), {"id": keeper_id})


async def test_owner_lidar_cleanup_propagates_transaction_abort_before_storage(
    owner_fault_http, fault_suite, test_engine, monkeypatch
):
    run = fault_suite.new_run(NAMESPACE)
    response = await owner_fault_http.post(
        "/api/v1/__test/seed/lidar", json=run.payload()
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

            async def no_reclaim(*args, **kwargs):
                pytest.fail("aborted cleanup reclaimed original storage")

            patch.setattr(routes, "_reclaim_owned_originals", no_reclaim)
            with pytest.raises(ControlledAbort):
                await owner_fault_http.post(
                    "/api/v1/__test/seed/owned-cleanup", json=run.payload()
                )
        assert await _snapshot(test_engine) == before
    finally:
        response = await owner_fault_http.post(
            "/api/v1/__test/seed/owned-cleanup", json=run.payload()
        )
        assert response.status_code == 200, response.text


async def test_node_fixed_fixture_capabilities_and_borrowed_rollback(
    fixture_operator, test_engine, record_property
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
                    "database_url": fixture_operator.url.set(
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
                text("SELECT 'yoao_workspace_fixture_v1.templates'::regclass::oid")
            )
            function_oids = [
                await owner.scalar(
                    text("SELECT CAST(:signature AS regprocedure)::oid"),
                    {"signature": "yoao_workspace_fixture_v1." + signature},
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
async def test_owner_final_cleanup_denial_rolls_back_records_and_allows_retry(
    owner_fault_http, fault_suite, test_engine, monkeypatch, state
):
    from botocore.exceptions import ClientError

    run = fault_suite.new_run(NAMESPACE)
    response = await owner_fault_http.post(
        "/api/v1/__test/seed/owned", json=run.payload()
    )
    assert response.status_code == 200, response.text
    project_id = uuid.UUID(response.json()["project_id"])
    job_id = uuid.uuid4()
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as db:
        task = (
            await db.execute(
                text(
                    "SELECT id,dataset_item_id FROM tasks WHERE project_id=:id ORDER BY id LIMIT 1"
                ),
                {"id": project_id},
            )
        ).one()
        db.add(
            VideoTrackerJob(
                id=job_id,
                task_id=task.id,
                dataset_item_id=task.dataset_item_id,
                status="queued",
                model_key="fixture",
                direction="forward",
                from_frame=0,
                to_frame=1,
                prompt={},
                event_channel="e2e-retirement",
            )
        )
    before = await _snapshot(test_engine)
    versions = await _fixture_versions(test_engine, run)
    with closing(fixture_original_reader()) as reader:
        before_versions = _read_versions(reader, versions)

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
    assert before_children["tasks"]
    original = AsyncSession.execute

    class ControlledDenial(Exception):
        sqlstate = state

    async def denied(self, statement, *args, **kwargs):
        if str(statement).startswith(
            "SELECT yoao_workspace_fixture_v1.delete_projects("
        ):
            raise IntegrityError(
                str(statement), {}, ControlledDenial("video_execution_not_retired")
            )
        return await original(self, statement, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "execute", denied)
            response = await owner_fault_http.post(
                "/api/v1/__test/seed/owned-cleanup", json=run.payload()
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
        with closing(fixture_original_reader()) as reader:
            assert _read_versions(reader, versions) == before_versions
        async with test_engine.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT status FROM video_tracker_jobs WHERE id=:id"),
                    {"id": job_id},
                )
                == "queued"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM yoao_jobs_v1.video_deletion_clearances WHERE job_id=:id"
                    ),
                    {"id": job_id},
                )
                == 0
            )
    finally:
        response = await owner_fault_http.post(
            "/api/v1/__test/seed/owned-cleanup", json=run.payload()
        )
        assert response.status_code == 200, response.text
        async with test_engine.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM projects WHERE id=:id"),
                    {"id": project_id},
                )
                == 0
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM video_tracker_jobs WHERE id=:id"),
                    {"id": job_id},
                )
                == 0
            )
        with closing(fixture_original_reader()) as reader:
            for row in versions:
                with pytest.raises(ClientError) as removed:
                    reader.head_object(
                        Bucket=row["bucket"],
                        Key=row["object_key"],
                        VersionId=row["version_id"],
                    )
                assert (
                    removed.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
                )
