"""Caller-owned exclusive Project snapshots on disposable native resources."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url

from aap_workspace_contract.project_lock import decode_project_lock_reply
from aap_workspace_contract.project_query_values import materialize_project_base_row

from app.application.project_workspace import lock_workspace_project
from app.application import point_cloud_quality_promotion
from app.db.models.point_cloud_quality import PointCloudQualityEvaluation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.annotation import Annotation
from app.core.security import create_access_token
from app.config import settings
from app.services.project_workspace import lock_project_row
from app.services.point_cloud_quality.config import (
    load_point_cloud_quality_config,
    point_cloud_quality_config_digest,
)
from tests.test_project_orphan_cleanup_transactions import (
    _asgi_client_for_maker,
    _audit_count,
    _headers,
    _seed_cleanup_world,
)
from tests.test_workspace_project_authority_locking import (
    _assert_private_denied,
    _world,
)
from tests.test_workspace_project_access_step_native import _assert_nowait_conflict

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


def _configure_api_auth(test_engine, monkeypatch):
    auth = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_AUTH_URL"])
    assert auth.username == "identity_auth_client"
    assert (auth.host, auth.port, auth.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    monkeypatch.setattr(settings, "identity_url", "")
    monkeypatch.setattr(settings, "identity_auth_database_url_file", "")
    monkeypatch.setattr(
        settings,
        "identity_auth_database_url",
        auth.render_as_string(hide_password=False),
    )
    monkeypatch.setattr(settings, "audit_async", False)


@pytest.mark.parametrize("nowait", [True, False])
async def test_api_login_has_only_explicit_public_lock_and_same_transaction(
    test_engine, nowait
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner() as metadata:
            assert (
                await metadata.execute(
                    text(
                        "SELECT pg_get_userbyid(proowner),prosecdef FROM pg_proc WHERE oid='aap_workspace_v1.lock_project_row(uuid,boolean)'::regprocedure"
                    )
                )
            ).one() == ("workspace_code", True)
            for role, expected in (
                ("identity_api_consumer", True),
                ("legacy_runtime", True),
                ("append_client", False),
                ("workspace_project_deletion_client", False),
                ("workspace_project_maintenance_client", False),
                ("workspace_configuration_client", False),
            ):
                assert (
                    await metadata.scalar(
                        text(
                            "SELECT has_function_privilege(:role,'aap_workspace_v1.lock_project_row(uuid,boolean)','EXECUTE')"
                        ),
                        {"role": role},
                    )
                    is expected
                )
            assert not await metadata.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid='aap_workspace_v1.lock_project_row(uuid,boolean)'::regprocedure AND a.grantee=0 AND a.privilege_type='EXECUTE')"
                )
            )
        async with api() as db:
            await _assert_private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            project = await lock_project_row(
                db, world["project_id"], nowait=nowait, populate_existing=True
            )
            assert project.id == world["project_id"] and project is await db.get(
                Project, project.id
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            await _assert_nowait_conflict(
                test_engine,
                "SELECT id FROM public.projects WHERE id=$1 FOR KEY SHARE NOWAIT",
                world["project_id"],
            )
            async with owner() as independent:
                assert (
                    await independent.scalar(
                        text(
                            "SELECT id FROM public.users WHERE id=:id FOR UPDATE NOWAIT"
                        ),
                        {"id": world["owner_id"]},
                    )
                    == world["owner_id"]
                )


@pytest.mark.parametrize("nowait", [True, False])
async def test_busy_modes_keep_http_mapping_or_raw_database_timeout(
    test_engine, nowait
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner() as blocker, api() as db:
            await blocker.execute(
                text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                {"id": world["project_id"]},
            )
            await db.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(HTTPException if nowait else DBAPIError) as caught:
                if nowait:
                    await lock_workspace_project(db, world["project_id"])
                else:
                    await lock_project_row(
                        db, world["project_id"], nowait=False, populate_existing=False
                    )
            if nowait:
                assert (caught.value.status_code, caught.value.detail) == (
                    409,
                    {"reason": "project_busy"},
                )
            else:
                assert caught.value.orig.sqlstate == "55P03"
            with pytest.raises(DBAPIError) as aborted:
                await db.execute(text("SELECT 1"))
            assert aborted.value.orig.sqlstate == "25P02"
            await db.rollback()


async def test_bad_lock_scope_rolls_back_prior_caller_write(test_engine, monkeypatch):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            original = await db.scalar(
                text("SELECT name FROM public.projects WHERE id=:id"),
                {"id": world["project_id"]},
            )
        with pytest.raises(HTTPException) as caught:
            async with owner.begin() as db:
                scalar = db.scalar

                async def wrong_scope(statement, *args, **kwargs):
                    raw = await scalar(statement, *args, **kwargs)
                    if "aap_workspace_v1.lock_project_row" in str(statement):
                        return {**raw, "project_id": str(uuid.uuid4())}
                    return raw

                monkeypatch.setattr(db, "scalar", wrong_scope)
                await db.execute(
                    text(
                        "UPDATE public.projects SET name='uncommitted lock probe' WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
                await lock_workspace_project(db, world["project_id"])
        assert caught.value.status_code == 503
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == original
            )


@pytest.mark.parametrize("null_id", [True, False])
async def test_null_native_arguments_remain_database_errors(test_engine, null_id):
    async with _world(test_engine) as (_owner, api, world, _url):
        async with api() as db:
            with pytest.raises(DBAPIError) as caught:
                await db.scalar(
                    text(
                        "SELECT aap_workspace_v1.lock_project_row(CAST(:id AS uuid),CAST(:nowait AS boolean))"
                    ),
                    {
                        "id": None if null_id else world["project_id"],
                        "nowait": True if null_id else None,
                    },
                )
            assert caught.value.orig.sqlstate == "22023"
            await db.rollback()


@pytest.mark.parametrize("nowait", [True, False])
async def test_missing_project_keeps_null_snapshot_and_existing_helper_404(
    test_engine, nowait
):
    async with _world(test_engine) as (_owner, api, _world_data, _url):
        async with api() as db:
            missing = uuid.uuid4()
            assert (
                await lock_project_row(
                    db, missing, nowait=nowait, populate_existing=True
                )
                is None
            )
            with pytest.raises(HTTPException) as caught:
                await lock_workspace_project(db, missing)
            assert (caught.value.status_code, caught.value.detail) == (
                404,
                "项目不存在",
            )
            assert await db.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
async def test_waiting_mode_preserves_snapshot_isolation_serialization_error(
    test_engine, isolation
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with api() as db:
            await db.execute(text(f"SET TRANSACTION ISOLATION LEVEL {isolation}"))
            await db.scalar(
                text(
                    "SELECT aap_workspace_v1.project_access_step(CAST(:actor AS uuid),CAST(:project AS uuid),'read_project_row')"
                ),
                {"actor": world["member_id"], "project": world["project_id"]},
            )
            async with owner.begin() as writer:
                await writer.execute(
                    text(
                        "UPDATE public.projects SET name='committed after snapshot' WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
            with pytest.raises(DBAPIError) as caught:
                await lock_project_row(
                    db, world["project_id"], nowait=False, populate_existing=False
                )
            assert caught.value.orig.sqlstate == "40001"
            await db.rollback()


async def test_actual_cleanup_api_uses_owner_lock_without_private_project_reads(
    test_engine, app_module, monkeypatch
):
    _configure_api_auth(test_engine, monkeypatch)
    async with _world(test_engine, retain_audited=True) as (
        owner,
        api,
        _world_data,
        _url,
    ):
        world = await _seed_cleanup_world(owner)
        async with _asgi_client_for_maker(app_module, api) as client:
            response = await client.post(
                f"/api/v1/projects/{world['project_id']}/cleanup-orphans",
                headers=_headers(world),
                json={"dry_run": False},
            )
        assert response.status_code == 200, response.text
        assert response.json()["orphan_annotations"] == 1
        async with owner() as observer:
            assert (
                await observer.get(Annotation, world["annotation_id"])
            ).is_active is False
            assert await _audit_count(observer, world["project_id"]) == 1


async def test_actual_promotion_api_keeps_project_evaluation_and_audit_commit(
    test_engine, app_module, monkeypatch
):
    _configure_api_auth(test_engine, monkeypatch)
    async with _world(test_engine, retain_audited=True) as (owner, api, world, _url):
        baseline = load_point_cloud_quality_config({})
        candidate = baseline.model_copy(deep=True)
        candidate.thresholds.minimum_points += 1
        async with owner.begin() as seed:
            await seed.execute(
                text(
                    "UPDATE public.projects SET data_type='lidar',type_key='lidar' WHERE id=:id"
                ),
                {"id": world["project_id"]},
            )
            evaluation = PointCloudQualityEvaluation(
                project_id=world["project_id"],
                created_by_id=world["owner_id"],
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
            seed.add(evaluation)
            await seed.flush()
            evaluation_id = evaluation.id
        token = create_access_token(
            subject=str(world["owner_id"]), role="project_admin"
        )
        async with _asgi_client_for_maker(app_module, api) as client:
            response = await client.post(
                f"/api/v1/projects/{world['project_id']}/point-cloud-quality/evaluations/{evaluation_id}/promote",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["promoted_config_revision"] == 2
        async with owner() as observer:
            assert (
                await observer.get(Project, world["project_id"])
            ).point_cloud_quality_config["config_revision"] == 2
            assert (
                await observer.get(PointCloudQualityEvaluation, evaluation_id)
            ).promoted_at is not None
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE target_id=:id AND action='point_cloud_quality.evaluation_promote'"
                    ),
                    {"id": str(evaluation_id)},
                )
                == 1
            )


async def test_node_uses_both_exclusive_modes_without_python_proxy(test_engine):
    async with _world(test_engine) as (_owner, _api, world, url):
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).parent / "fixtures/workspace_project_lock_peer.mjs"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(
                    json.dumps(
                        {
                            "database_url": url.set(
                                drivername="postgresql"
                            ).render_as_string(hide_password=False),
                            "project_id": str(world["project_id"]),
                            "owner_id": str(world["owner_id"]),
                        }
                    ).encode()
                ),
                timeout=20,
            )
            assert process.returncode == 0, err.decode()
            assert json.loads(out) == {
                "ok": True,
                "modes": [True, False],
                "transaction_unchanged": True,
            }
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_waiting_native_snapshot_includes_the_committed_counter_update(
    test_engine,
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner() as blocker, api() as db:
            await _assert_private_denied(db)
            pid = await db.scalar(text("SELECT pg_backend_pid()"))
            await blocker.execute(
                text(
                    "UPDATE public.projects SET name='committed while waiting',total_tasks=17,completed_tasks=3,batch_summary=CAST(:summary AS jsonb) WHERE id=:id"
                ),
                {"id": world["project_id"], "summary": json.dumps({"pending": 17})},
            )
            pending = asyncio.create_task(
                db.scalar(
                    text(
                        "SELECT aap_workspace_v1.lock_project_row(CAST(:id AS uuid),false)"
                    ),
                    {"id": world["project_id"]},
                )
            )
            try:
                waiting = False
                async with owner() as observer:
                    for _ in range(100):
                        waiting = await observer.scalar(
                            text(
                                "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": pid},
                        )
                        if waiting:
                            break
                        await asyncio.sleep(0.02)
                assert waiting is True and not pending.done()
                await blocker.commit()
                raw = await asyncio.wait_for(pending, 5)
                row = materialize_project_base_row(
                    decode_project_lock_reply(
                        raw, project_id=world["project_id"], nowait=False
                    ),
                    target_id=world["project_id"],
                )
                assert (
                    row["name"],
                    row["total_tasks"],
                    row["completed_tasks"],
                    row["batch_summary"],
                ) == ("committed while waiting", 17, 3, {"pending": 17})
                await _assert_nowait_conflict(
                    test_engine,
                    "SELECT id FROM public.projects WHERE id=$1 FOR UPDATE NOWAIT",
                    world["project_id"],
                )
            finally:
                await blocker.rollback()
                if not pending.done():
                    await asyncio.wait_for(pending, 5)
                await db.rollback()


async def test_exclusive_snapshot_refreshes_dirty_cache_without_flushing_pending_task(
    test_engine,
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            cached = await db.get(Project, world["project_id"])
            tools = {"bbox": {"classes": ["fresh exclusive configuration"]}}
            async with owner.begin() as change:
                await change.execute(
                    text(
                        "UPDATE public.projects SET name='fresh exclusive snapshot',tool_bindings=CAST(:tools AS jsonb) WHERE id=:id"
                    ),
                    {"id": world["project_id"], "tools": json.dumps(tools)},
                )
            cached.name = "discarded local project edit"
            cached.tool_bindings = {"discarded": True}
            pending = Task(
                id=uuid.uuid4(),
                project_id=world["project_id"],
                display_id=f"T-EXC-{uuid.uuid4().hex[:12]}",
                file_name="pending.png",
                file_path=f"exclusive/{uuid.uuid4().hex}.png",
                file_type="image",
                status="pending",
            )
            db.add(pending)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            project = await lock_workspace_project(db, world["project_id"])
            assert project is cached
            assert (project.name, project.tool_bindings) == (
                "fresh exclusive snapshot",
                tools,
            )
            assert cached not in db.dirty and pending in db.new
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.tasks WHERE id=:id"),
                    {"id": pending.id},
                )
                == 0
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            await _assert_nowait_conflict(
                test_engine,
                "SELECT id FROM public.projects WHERE id=$1 FOR KEY SHARE NOWAIT",
                world["project_id"],
            )
            await db.rollback()


@pytest.mark.parametrize("autoflush", [True, False])
@pytest.mark.parametrize("cache", ["clean", "dirty", "expired", "partial"])
async def test_promotion_retains_orm_cache_and_autoflush_before_business_write(
    test_engine, monkeypatch, autoflush, cache
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as seed:
            evaluation = PointCloudQualityEvaluation(
                project_id=world["project_id"],
                created_by_id=world["owner_id"],
                baseline_config_revision=1,
                baseline_config_digest="0" * 64,
                baseline_config_snapshot={},
                candidate_config_digest="1" * 64,
                candidate_config_snapshot={},
                cutoff_at=datetime.now(UTC),
                sample_count=0,
                sample_snapshot=[],
                summary={},
                gate_status="hold",
                gate_reasons=[],
            )
            seed.add(evaluation)
            await seed.flush()
            evaluation_id = evaluation.id
        async with owner() as db:
            cached = await db.get(Project, world["project_id"])
            old_name, old_status, old_config = (
                cached.name,
                cached.status,
                cached.point_cloud_quality_config,
            )
            fresh_config = {"config_revision": 17}
            local_config = {"config_revision": 5}
            async with owner.begin() as change:
                await change.execute(
                    text(
                        "UPDATE public.projects SET name='new committed name',status='completed',point_cloud_quality_config=CAST(:config AS jsonb) WHERE id=:id"
                    ),
                    {"id": world["project_id"], "config": json.dumps(fresh_config)},
                )
            if cache == "dirty":
                cached.name = "pending name"
                cached.point_cloud_quality_config = local_config
            elif cache == "expired":
                db.expire(cached)
            elif cache == "partial":
                db.expire(cached, ["status", "point_cloud_quality_config"])
            pending = Task(
                id=uuid.uuid4(),
                project_id=world["project_id"],
                display_id=f"T-PRO-{uuid.uuid4().hex[:12]}",
                file_name="pending.png",
                file_path=f"promotion/{uuid.uuid4().hex}.png",
                file_type="image",
                status="pending",
            )
            db.add(pending)
            observed = []

            class BeforeBusinessWrite(Exception):
                pass

            def inspect_snapshot(actual_evaluation, *, current_config):
                assert actual_evaluation.id == evaluation_id
                observed.append(current_config)
                raise BeforeBusinessWrite()

            monkeypatch.setattr(
                point_cloud_quality_promotion,
                "prepare_evaluation_promotion",
                inspect_snapshot,
            )
            with nullcontext() if autoflush else db.no_autoflush:
                with pytest.raises(BeforeBusinessWrite):
                    await point_cloud_quality_promotion.promote_point_cloud_quality_evaluation(
                        db,
                        project_id=world["project_id"],
                        evaluation_id=evaluation_id,
                        actor_id=world["owner_id"],
                    )
            expected_config = (
                local_config
                if cache == "dirty"
                else fresh_config
                if cache in {"expired", "partial"}
                else old_config
            )
            expected_name = (
                "pending name"
                if cache == "dirty"
                else "new committed name"
                if cache == "expired"
                else old_name
            )
            assert (cached.name, cached.status, cached.point_cloud_quality_config) == (
                expected_name,
                "completed" if cache in {"expired", "partial"} else old_status,
                expected_config,
            )
            assert observed == [expected_config]
            assert (cached in db.dirty) is (cache == "dirty" and not autoflush)
            assert (pending in db.new) is not autoflush
            assert await db.scalar(
                text("SELECT count(*) FROM public.tasks WHERE id=:id"),
                {"id": pending.id},
            ) == int(autoflush)
            await _assert_nowait_conflict(
                test_engine,
                "SELECT id FROM public.projects WHERE id=$1 FOR UPDATE NOWAIT",
                world["project_id"],
            )
            await db.rollback()
