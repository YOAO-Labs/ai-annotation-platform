"""Actual restricted-caller checks for Workspace's adjacent project writes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.project_maintenance_contract import (
    PROTOCOL,
    ProjectMaintenanceUnavailable,
    decode_import_project_reference,
    decode_point_quality_config_write,
    project_maintenance_schema,
)
from app.application.point_cloud_quality_promotion import (
    promote_point_cloud_quality_evaluation,
)
from app.db.models.point_cloud_quality import PointCloudQualityEvaluation
from app.db.models.project import Project
from app.services.point_cloud_quality.config import (
    load_point_cloud_quality_config,
    point_cloud_quality_config_digest,
)
from tests.factory import create_project, create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres

ROOT = Path(__file__).resolve().parents[3]
_CREATE = "SELECT aap_workspace_v1.create_import_project($1,$2,$3,$4,$5::jsonb)"
_WRITE = "SELECT aap_workspace_v1.write_point_cloud_quality_config($1,$2::jsonb)"
_FIXED_TIME = datetime(2000, 1, 1, tzinfo=UTC)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier's owned PostgreSQL and scoped LOGIN",
    ),
]


def _dsn(test_engine, *, owner=False):
    _verify_postgres(test_engine.url)
    if owner:
        url = test_engine.url
    else:
        url = make_url(os.environ["AAP_MODULE_DATA_WORKSPACE_PROJECT_MAINTENANCE_URL"])
        assert url.username == "workspace_project_maintenance_client"
        assert (url.host, url.port, url.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
    assert url.database.endswith("_test")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def _world(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    project_ids = []
    async with maker.begin() as db:
        actor = await create_user(
            db, "super_admin", f"maintenance-{uuid.uuid4()}@example.test", "Maintainer"
        )
        actor_id = actor.id
        project = await create_project(db, owner_id=actor_id, type_key="lidar")
        project.data_type = "lidar"
        project.updated_at = _FIXED_TIME
        project_ids.append(project.id)
        project_id = project.id
        other = await create_project(db, owner_id=actor_id)
        other.point_cloud_quality_config = {"sentinel": True}
        other.updated_at = _FIXED_TIME
        project_ids.append(other.id)
    owner = client = None
    try:
        owner = await asyncpg.connect(_dsn(test_engine, owner=True), command_timeout=10)
        client = await asyncpg.connect(_dsn(test_engine), command_timeout=10)
        yield maker, owner, client, actor_id, project_id, project_ids
    finally:
        for connection in (client, owner):
            if connection is not None:
                await connection.close()
        async with maker.begin() as db:
            await db.execute(
                text("DELETE FROM projects WHERE id=ANY(CAST(:ids AS uuid[]))"),
                {"ids": project_ids},
            )
            await db.execute(text("DELETE FROM users WHERE id=:id"), {"id": actor_id})


async def test_descriptor_and_function_only_login_match_exact_capabilities(test_engine):
    expected = project_maintenance_schema()
    assert expected["$id"] == PROTOCOL
    for path in (
        ROOT
        / "apps/workspace/src/aap_workspace/project_maintenance_protocol.schema.json",
        ROOT / "apps/api/alembic/sql/0288_workspace_project_maintenance.schema.json",
    ):
        assert json.loads(path.read_text()) == expected
    async with _world(test_engine) as (_, owner, client, _, _, _):
        assert (
            await client.fetchval("SELECT current_user")
            == "workspace_project_maintenance_client"
        )
        actual = await client.fetch(
            "SELECT n.nspname||'.'||p.proname AS name, p.prosecdef, r.rolname AS owner "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_roles r ON r.oid=p.proowner WHERE n.nspname LIKE 'aap_%' "
            "AND has_function_privilege(current_user,p.oid,'EXECUTE') ORDER BY name"
        )
        assert [(r["name"], r["prosecdef"], r["owner"]) for r in actual] == [
            ("aap_workspace_v1.create_import_project", True, "workspace_code"),
            (
                "aap_workspace_v1.create_point_cloud_seed_project",
                True,
                "workspace_code",
            ),
            ("aap_workspace_v1.read_import_project", True, "workspace_code"),
            ("aap_workspace_v1.write_nuscenes_seed_defaults", True, "workspace_code"),
            (
                "aap_workspace_v1.write_point_cloud_quality_config",
                True,
                "workspace_code",
            ),
            ("aap_workspace_v1.write_seed_members", True, "workspace_code"),
        ]
        for query in (
            "SELECT id FROM public.projects",
            "UPDATE public.projects SET name=name",
            "INSERT INTO public.projects(id) VALUES(gen_random_uuid())",
            "SELECT id FROM public.users",
            "CREATE TEMP TABLE maintenance_denied(id integer)",
            "SELECT aap_workspace_configuration_v1.configuration_policy()",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await client.execute(query)
        for role in ("append_client", "asset_operator_client", "labeling_code"):
            assert not await owner.fetchval(
                "SELECT has_function_privilege($1,"
                "'aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)','EXECUTE')",
                role,
            )


async def test_import_defaults_match_legacy_orm_and_rollback_retains_reference(
    test_engine,
):
    async with _world(test_engine) as (maker, owner, client, actor_id, _, ids):
        display_id = "P-NU-" + uuid.uuid4().hex[:10]
        bindings = {"lidar_box_3d": {"enabled": True, "classes": ["车辆"]}}
        native_id = uuid.uuid4()
        ids.append(native_id)
        await client.execute("BEGIN")
        assert (
            decode_import_project_reference(
                await client.fetchval(
                    "SELECT aap_workspace_v1.read_import_project($1)", display_id
                ),
                display_id=display_id,
            )
            is None
        )
        assert (
            decode_import_project_reference(
                await client.fetchval(
                    _CREATE,
                    native_id,
                    display_id,
                    "nuScenes 项目",
                    actor_id,
                    json.dumps(bindings),
                ),
                display_id=display_id,
                expected_project_id=native_id,
            )
            == native_id
        )
        assert (
            await owner.fetchval("SELECT count(*) FROM projects WHERE id=$1", native_id)
            == 0
        )
        await client.execute("ROLLBACK")
        assert (
            decode_import_project_reference(
                await client.fetchval(
                    "SELECT aap_workspace_v1.read_import_project($1)", display_id
                ),
                display_id=display_id,
            )
            is None
        )
        await client.fetchval(
            _CREATE,
            native_id,
            display_id,
            "nuScenes 项目",
            actor_id,
            json.dumps(bindings),
        )
        # The pre-change creator's ORM defaults are the compatibility reference.
        async with maker.begin() as db:
            legacy = Project(
                id=uuid.uuid4(),
                display_id="P-OLD-" + uuid.uuid4().hex[:10],
                name="nuScenes 项目",
                owner_id=actor_id,
                type_label="点云检测",
                type_key="lidar",
                data_type="lidar",
                scene_mode=True,
                prefer_same_scene_continuation=True,
                tool_bindings=bindings,
                ai_enabled=False,
            )
            db.add(legacy)
            await db.flush()
            ids.append(legacy.id)
            legacy_id = legacy.id
        rows = [
            json.loads(
                await owner.fetchval(
                    "SELECT to_jsonb(p) FROM projects p WHERE id=$1", key
                )
            )
            for key in (native_id, legacy_id)
        ]
        for row in rows:
            for field in ("id", "display_id", "created_at", "updated_at"):
                row.pop(field)
        assert rows[0] == rows[1]
        # A lookup of an existing display ID remains a reference-only read.
        before = await owner.fetchval(
            "SELECT to_jsonb(p) FROM projects p WHERE id=$1", native_id
        )
        assert (
            decode_import_project_reference(
                await client.fetchval(
                    "SELECT aap_workspace_v1.read_import_project($1)", display_id
                ),
                display_id=display_id,
            )
            == native_id
        )
        assert (
            await owner.fetchval(
                "SELECT to_jsonb(p) FROM projects p WHERE id=$1", native_id
            )
            == before
        )


async def test_quality_write_is_match_scoped_noop_and_rolls_back_with_sql_failure(
    test_engine,
):
    async with _world(test_engine) as (_, owner, client, actor_id, project_id, ids):
        other = await owner.fetchval(
            "SELECT to_jsonb(p) FROM projects p WHERE id=$1", ids[1]
        )
        assert (
            decode_point_quality_config_write(
                await client.fetchval(_WRITE, project_id, "{}"), project_id=project_id
            )
            == _FIXED_TIME
        )
        assert (
            await owner.fetchval(
                "SELECT to_jsonb(p) FROM projects p WHERE id=$1", ids[1]
            )
            == other
        )
        original = await owner.fetchval(
            "SELECT to_jsonb(p) FROM projects p WHERE id=$1", project_id
        )
        await client.execute("BEGIN")
        transaction_time = await client.fetchval("SELECT now()")
        assert (
            decode_point_quality_config_write(
                await client.fetchval(_WRITE, project_id, '{"config_revision":2}'),
                project_id=project_id,
            )
            == transaction_time
        )
        assert (
            await owner.fetchval(
                "SELECT to_jsonb(p) FROM projects p WHERE id=$1", project_id
            )
            == original
        )
        display_id = await owner.fetchval(
            "SELECT display_id FROM projects WHERE id=$1", project_id
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await client.fetchval(
                _CREATE, uuid.uuid4(), display_id, "duplicate", actor_id, "{}"
            )
        with pytest.raises(asyncpg.InFailedSQLTransactionError):
            await client.fetchval("SELECT 1")
        await client.execute("ROLLBACK")
        assert (
            await owner.fetchval(
                "SELECT to_jsonb(p) FROM projects p WHERE id=$1", project_id
            )
            == original
        )


@pytest.mark.parametrize(
    "failure", ["length", "foreign_key", "invalid_json", "missing_project"]
)
async def test_native_errors_preserve_sqlstate(test_engine, failure):
    async with _world(test_engine) as (_, _, client, actor_id, project_id, _):
        expected = {
            "length": asyncpg.StringDataRightTruncationError,
            "foreign_key": asyncpg.ForeignKeyViolationError,
            "invalid_json": asyncpg.InvalidParameterValueError,
            "missing_project": asyncpg.NoDataFoundError,
        }[failure]
        with pytest.raises(expected):
            async with client.transaction():
                if failure in {"invalid_json", "missing_project"}:
                    await client.fetchval(
                        _WRITE,
                        uuid.uuid4() if failure == "missing_project" else project_id,
                        "{}" if failure == "missing_project" else "[]",
                    )
                else:
                    await client.fetchval(
                        _CREATE,
                        uuid.uuid4(),
                        "x" * 21
                        if failure == "length"
                        else "P-" + uuid.uuid4().hex[:10],
                        "Project",
                        uuid.uuid4() if failure == "foreign_key" else actor_id,
                        "{}",
                    )
        assert await client.fetchval("SELECT 1") == 1


async def test_quality_writer_waits_for_project_and_retains_caller_lock(test_engine):
    async with _world(test_engine) as (_, owner, client, _, project_id, ids):
        other = await owner.fetchval(
            "SELECT to_jsonb(p) FROM projects p WHERE id=$1", ids[1]
        )
        await owner.execute("BEGIN")
        await owner.fetchval(
            "SELECT id FROM projects WHERE id=$1 FOR UPDATE", project_id
        )
        await client.execute("BEGIN")
        transaction_time = await client.fetchval("SELECT now()")
        pid = await client.fetchval("SELECT pg_backend_pid()")
        pending = asyncio.create_task(
            client.fetchval(_WRITE, project_id, '{"config_revision":2}')
        )
        try:
            for _ in range(100):
                if await owner.fetchval(
                    "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1",
                    pid,
                ):
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("Project writer did not wait on the held row lock")
            assert not pending.done()
            await owner.execute("ROLLBACK")
            result = await asyncio.wait_for(pending, 5)
            assert (
                decode_point_quality_config_write(result, project_id=project_id)
                == transaction_time
            )
            with pytest.raises(asyncpg.LockNotAvailableError):
                async with owner.transaction():
                    await owner.fetchval(
                        "SELECT id FROM projects WHERE id=$1 FOR UPDATE NOWAIT",
                        project_id,
                    )
            await client.execute("COMMIT")
            assert json.loads(
                await owner.fetchval(
                    "SELECT point_cloud_quality_config FROM projects WHERE id=$1",
                    project_id,
                )
            ) == {"config_revision": 2}
            assert (
                await owner.fetchval(
                    "SELECT to_jsonb(p) FROM projects p WHERE id=$1", ids[1]
                )
                == other
            )
        finally:
            if not pending.done():
                await owner.execute("ROLLBACK")
                await asyncio.wait_for(pending, 5)
            if client.is_in_transaction():
                await client.execute("ROLLBACK")


@pytest.mark.parametrize("failure", ["audit", "binding"])
async def test_evaluation_and_project_config_share_caller_rollback(
    test_engine, monkeypatch, failure
):
    async with _world(test_engine) as (maker, _, _, actor_id, project_id, _):
        baseline = load_point_cloud_quality_config({})
        candidate = baseline.model_copy(deep=True)
        candidate.thresholds.minimum_points = baseline.thresholds.minimum_points + 1
        async with maker.begin() as db:
            evaluation = PointCloudQualityEvaluation(
                project_id=project_id,
                created_by_id=actor_id,
                baseline_config_revision=baseline.config_revision,
                baseline_config_digest=point_cloud_quality_config_digest(baseline),
                baseline_config_snapshot=baseline.model_dump(mode="json"),
                candidate_config_digest=point_cloud_quality_config_digest(candidate),
                candidate_config_snapshot=candidate.model_dump(mode="json"),
                cutoff_at=datetime.now(UTC),
                sample_count=0,
                sample_snapshot=[],
                summary={},
                gate_status="promote",
                gate_reasons=[],
            )
            db.add(evaluation)
            await db.flush()
            evaluation_id = evaluation.id
        expected = RuntimeError if failure == "audit" else ProjectMaintenanceUnavailable
        message = (
            "later Audit rejected" if failure == "audit" else "maintenance unavailable"
        )
        with pytest.raises(expected, match=message):
            async with maker.begin() as db:
                if failure == "binding":
                    scalar = db.scalar

                    async def mismatched_reply(statement, *args, **kwargs):
                        result = await scalar(statement, *args, **kwargs)
                        if "aap_workspace_v1.write_point_cloud_quality_config" in str(
                            statement
                        ):
                            return {**result, "project_id": str(uuid.uuid4())}
                        return result

                    monkeypatch.setattr(db, "scalar", mismatched_reply)
                await promote_point_cloud_quality_evaluation(
                    db,
                    project_id=project_id,
                    evaluation_id=evaluation_id,
                    actor_id=actor_id,
                )
                raise RuntimeError("later Audit rejected")
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT point_cloud_quality_config FROM projects WHERE id=:id"
                    ),
                    {"id": project_id},
                )
                == {}
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT promoted_at FROM point_cloud_quality_evaluations WHERE id=:id"
                    ),
                    {"id": evaluation_id},
                )
                is None
            )
        async with maker.begin() as db:
            evaluation, project = await promote_point_cloud_quality_evaluation(
                db,
                project_id=project_id,
                evaluation_id=evaluation_id,
                actor_id=actor_id,
            )
            assert (
                project.point_cloud_quality_config["config_revision"]
                == evaluation.promoted_config_revision
                == 2
            )


async def test_node_consumes_exact_native_capabilities(test_engine):
    async with _world(test_engine) as (_, owner, _, actor_id, project_id, _):
        before = await owner.fetchval(
            "SELECT to_jsonb(p) FROM projects p WHERE id=$1", project_id
        )
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                str(
                    ROOT
                    / "apps/api/tests/fixtures/workspace_project_maintenance_peer.mjs"
                ),
            ],
            input=json.dumps(
                {
                    "database_url": _dsn(test_engine),
                    "owner_id": str(actor_id),
                    "project_id": str(project_id),
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
            cwd=ROOT,
            env={"PATH": os.environ.get("PATH", "")},
        )
        assert result.returncode == 0, "Workspace Node peer failed"
        assert json.loads(result.stdout) == {
            "ok": True,
            "functions": 3,
            "rollback_preserved": True,
        }
        assert (
            await owner.fetchval(
                "SELECT to_jsonb(p) FROM projects p WHERE id=$1", project_id
            )
            == before
        )
