"""Real database lock interleavings for the named-pipeline owner bindings."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from tests.factory import create_project, create_user
from tests.inference_pipeline_owner_helpers import (
    _BINDINGS,
    api_pipeline_owner_binding,
)
from tests.labeling_class_query_helpers import (
    api_db_binding,
    assert_owned_test_database,
    clear_local_identity,
    make_request_runtime,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_LOCKS_PATH = Path(__file__).parent / "fixtures/named_pipeline_legacy/locks.json"
_LOCKS_SHA256 = "4317025325d702dfa56bc0c1b07e359da325fc5f3844b71f82aab7d8cea0e511"
_PIPELINE_FUNCTION = "aap_inference_pipelines_v1.pipeline_step"
_PIPELINE_SIGNATURE = (
    "aap_inference_pipelines_v1.pipeline_step(jsonb,text,jsonb,text[],text[])"
)
_FENCE_PROBE_PREFIX = "_pipeline_step_fence_probe_"


def _frozen_samples() -> tuple[dict, ...]:
    contents = _LOCKS_PATH.read_bytes()
    assert hashlib.sha256(contents).hexdigest() == _LOCKS_SHA256
    document = json.loads(contents)
    assert document["capture_kind"] == "actual-pre-extraction-named-pipeline-locks"
    samples = tuple(document["samples"])
    assert {sample["label"] for sample in samples} == {
        "key_share_and_deleted_source_snapshot",
        "atomic_insert_failure",
    }
    return samples


_FROZEN_SAMPLES = _frozen_samples()


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


def _normalise(value, aliases):
    if isinstance(value, dict):
        return {
            key: "$timestamp"
            if key in {"created_at", "updated_at"}
            else _normalise(item, aliases)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalise(item, aliases) for item in value]
    return aliases.get(value, value) if isinstance(value, str) else value


async def _seed(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        actor = await create_user(
            db,
            "project_admin",
            f"pipeline-fence-{uuid.uuid4().hex}@example.test",
            "Pipeline owner",
        )
        project = await create_project(
            db, owner_id=actor.id, name="Named pipeline fence target"
        )
        source = ProjectPipeline(
            id=uuid.uuid4(),
            scope="public",
            name="Frozen source",
            stages=[],
            is_default=False,
            created_by=actor.id,
        )
        prior_default = ProjectPipeline(
            id=uuid.uuid4(),
            scope="private",
            project_id=project.id,
            name="Prior default",
            stages=[],
            is_default=True,
            created_by=actor.id,
        )
        db.add_all([source, prior_default])
    return maker, actor, project.id, source.id, prior_default.id


async def _cleanup(maker, actor_id, project_id, source_id):
    async with maker.begin() as db:
        await db.execute(
            delete(ProjectPipeline).where(ProjectPipeline.project_id == project_id)
        )
        await db.execute(delete(ProjectPipeline).where(ProjectPipeline.id == source_id))
        await db.execute(delete(Project).where(Project.id == project_id))
        await db.execute(delete(User).where(User.id == actor_id))


async def _snapshot(maker, project_id, source_id, aliases):
    async with maker() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id::text, name, is_default, usage_count "
                        "FROM public.project_pipelines "
                        "WHERE project_id=:project OR id=:source ORDER BY name,id"
                    ),
                    {"project": project_id, "source": source_id},
                )
            )
            .mappings()
            .all()
        )
        counter = await db.scalar(
            select(Project.total_tasks).where(Project.id == project_id)
        )
    return {
        "pipelines": _normalise([dict(row) for row in rows], aliases),
        "counter": counter,
    }


async def _assert_project_update_lock_conflict(test_engine, project_id):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=5
    )
    transaction = connection.transaction()
    await transaction.start()
    try:
        with pytest.raises(asyncpg.PostgresError) as error:
            await connection.fetchval(
                "SELECT id FROM public.projects WHERE id=$1::uuid FOR UPDATE NOWAIT",
                project_id,
            )
        assert error.value.sqlstate == "55P03"
    finally:
        await transaction.rollback()
        await connection.close()


async def _mutate_counter_and_delete_source(maker, project_id, source_id):
    async with maker.begin() as db:
        updated = await db.execute(
            text(
                "UPDATE public.projects SET total_tasks=total_tasks+1 "
                "WHERE id=:project_id"
            ),
            {"project_id": project_id},
        )
        assert updated.rowcount == 1
        deleted = await db.execute(
            delete(ProjectPipeline).where(ProjectPipeline.id == source_id)
        )
        return deleted.rowcount


async def _function_metadata(connection, function_name):
    metadata = (
        (
            await connection.execute(
                text(
                    """SELECT function.oid::bigint AS oid,
                          pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                          function.prosecdef AS security_definer,
                          function.provolatile AS volatility,
                          function.proparallel AS parallel,
                          function.proconfig AS configuration,
                          pg_catalog.obj_description(function.oid,'pg_proc') AS comment,
                          pg_catalog.pg_get_functiondef(function.oid) AS definition
                   FROM pg_catalog.pg_proc AS function
                   JOIN pg_catalog.pg_namespace AS namespace
                     ON namespace.oid=function.pronamespace
                   WHERE namespace.nspname='aap_inference_pipelines_v1'
                     AND function.proname=:function_name
                     AND pg_catalog.oidvectortypes(function.proargtypes) =
                         'jsonb, text, jsonb, text[], text[]'"""
                ),
                {"function_name": function_name},
            )
        )
        .mappings()
        .one()
    )
    grants = (
        (
            await connection.execute(
                text(
                    """SELECT COALESCE(role.rolname,'PUBLIC') AS grantee,
                              COALESCE(grantor.rolname,'PUBLIC') AS grantor,
                          acl.privilege_type,
                          acl.is_grantable
                   FROM pg_catalog.pg_proc AS function
                   JOIN pg_catalog.pg_namespace AS namespace
                     ON namespace.oid=function.pronamespace
                   CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
                       function.proacl,
                       pg_catalog.acldefault('f',function.proowner)
                   )) AS acl
                   LEFT JOIN pg_catalog.pg_roles AS role ON role.oid=acl.grantee
                   LEFT JOIN pg_catalog.pg_roles AS grantor ON grantor.oid=acl.grantor
                   WHERE namespace.nspname='aap_inference_pipelines_v1'
                     AND function.proname=:function_name
                     AND pg_catalog.oidvectortypes(function.proargtypes) =
                         'jsonb, text, jsonb, text[], text[]'
                   ORDER BY 1, 2, 3, 4"""
                ),
                {"function_name": function_name},
            )
        )
        .mappings()
        .all()
    )
    result = dict(metadata)
    result["acl"] = tuple(
        (
            row["grantee"],
            row["privilege_type"],
            row["is_grantable"],
            row["grantor"],
        )
        for row in grants
    )
    return result


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def _replace_execute_acl(connection, function_name, desired_acl, owner):
    await connection.exec_driver_sql(f"SET ROLE {_quote_identifier(owner)}")
    try:
        current_acl = await _function_metadata(connection, function_name)
        grantees = {entry[0] for entry in current_acl["acl"]}
        grantees.add("PUBLIC")
        for grantee in sorted(grantees):
            if grantee == owner:
                continue
            rendered = "PUBLIC" if grantee == "PUBLIC" else _quote_identifier(grantee)
            await connection.exec_driver_sql(
                f"REVOKE ALL ON FUNCTION aap_inference_pipelines_v1.{function_name}"
                "(jsonb,text,jsonb,text[],text[]) FROM "
                f"{rendered}"
            )
        for grantee, privilege, grantable, grantor in desired_acl:
            if grantee == owner:
                continue
            rendered = "PUBLIC" if grantee == "PUBLIC" else _quote_identifier(grantee)
            grant_option = " WITH GRANT OPTION" if grantable else ""
            await connection.exec_driver_sql("RESET ROLE")
            await connection.exec_driver_sql(f"SET ROLE {_quote_identifier(grantor)}")
            await connection.exec_driver_sql(
                f"GRANT {privilege} ON FUNCTION aap_inference_pipelines_v1.{function_name}"
                "(jsonb,text,jsonb,text[],text[]) TO "
                f"{rendered}{grant_option}"
            )
            await connection.exec_driver_sql("RESET ROLE")
    finally:
        await connection.exec_driver_sql("RESET ROLE")


@asynccontextmanager
async def _gated_pipeline_step(test_engine, *, project_id, lock_key):
    probe_name = f"{_FENCE_PROBE_PREFIX}{uuid.uuid4().hex}"
    probe_signature = (
        f"aap_inference_pipelines_v1.{probe_name}(jsonb,text,jsonb,text[],text[])"
    )
    wrapper_signature = _PIPELINE_SIGNATURE
    async with test_engine.connect() as reader:
        original = await _function_metadata(reader, "pipeline_step")
    installed = False
    try:
        async with test_engine.begin() as owner:
            await owner.execute(
                text(
                    f"ALTER FUNCTION {_PIPELINE_SIGNATURE} "
                    f"RENAME TO {_quote_identifier(probe_name)}"
                )
            )
            await _replace_execute_acl(owner, probe_name, (), original["owner"])
            await owner.exec_driver_sql(
                f"""CREATE FUNCTION {_PIPELINE_FUNCTION}(
                    p_request jsonb,
                    p_action text,
                    p_parameters jsonb,
                    p_column_names text[],
                    p_column_values text[]
                ) RETURNS jsonb
                LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
                SET search_path = pg_catalog, pg_temp
                SET timezone TO 'UTC'
                SET DateStyle TO 'ISO, YMD'
                SET extra_float_digits TO 3
                AS $fence$
                BEGIN
                    IF p_action = 'source_usage_increment'
                       AND p_request->>'operation' = 'apply_pipeline'
                       AND p_request->>'target_id' = '{project_id}' THEN
                        PERFORM pg_catalog.pg_advisory_xact_lock({lock_key}::bigint);
                    END IF;
                    RETURN aap_inference_pipelines_v1.{probe_name}(
                        p_request, p_action, p_parameters,
                        p_column_names, p_column_values
                    );
                END;
                $fence$"""
            )
            await owner.execute(
                text(
                    f"ALTER FUNCTION {wrapper_signature} OWNER TO "
                    f"{_quote_identifier(original['owner'])}"
                )
            )
            await _replace_execute_acl(
                owner, "pipeline_step", original["acl"], original["owner"]
            )
            wrapped = await _function_metadata(owner, "pipeline_step")
            assert wrapped["owner"] == original["owner"]
            assert wrapped["security_definer"] is True
            assert wrapped["volatility"] == original["volatility"]
            assert wrapped["parallel"] == original["parallel"]
            assert wrapped["configuration"] == original["configuration"]
        installed = True
        yield
    finally:
        if installed:
            async with test_engine.begin() as owner:
                await owner.execute(text(f"DROP FUNCTION {wrapper_signature} RESTRICT"))
                await owner.execute(
                    text(f"ALTER FUNCTION {probe_signature} RENAME TO pipeline_step")
                )
                await _replace_execute_acl(
                    owner, "pipeline_step", original["acl"], original["owner"]
                )
                restored = await _function_metadata(owner, "pipeline_step")
                for field in (
                    "owner",
                    "security_definer",
                    "volatility",
                    "parallel",
                    "configuration",
                    "comment",
                    "definition",
                    "acl",
                ):
                    assert restored[field] == original[field]


@asynccontextmanager
async def _held_advisory_gate(test_engine, lock_key):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=10
    )
    locked = False
    try:
        locked = await connection.fetchval(
            "SELECT pg_catalog.pg_advisory_lock($1::bigint)", lock_key
        )
        assert locked is None
        yield connection
    finally:
        if connection.is_closed() is False:
            try:
                await connection.fetchval(
                    "SELECT pg_catalog.pg_advisory_unlock($1::bigint)", lock_key
                )
            finally:
                await connection.close()


async def _wait_for_advisory_waiter(test_engine, lock_key, timeout_seconds=20):
    classid = (lock_key >> 32) & 0xFFFFFFFF
    objid = lock_key & 0xFFFFFFFF
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    async with test_engine.connect() as observer:
        while loop.time() < deadline:
            await observer.execute(text("SELECT pg_catalog.pg_stat_clear_snapshot()"))
            row = (
                (
                    await observer.execute(
                        text(
                            """SELECT activity.pid, activity.application_name,
                                  activity.query, pglock.waitstart
                           FROM pg_catalog.pg_locks AS pglock
                           JOIN pg_catalog.pg_stat_activity AS activity
                             ON activity.pid=pglock.pid
                           WHERE pglock.locktype='advisory'
                             AND pglock.database=(SELECT oid FROM pg_catalog.pg_database
                                                  WHERE datname=current_database())
                             AND pglock.classid=CAST(:classid AS oid)
                             AND pglock.objid=CAST(:objid AS oid)
                             AND pglock.objsubid=1
                             AND pglock.granted IS FALSE
                             AND activity.wait_event_type='Lock'
                             AND activity.query ILIKE '%aap_inference_pipelines_v1.pipeline_step%'
                           ORDER BY pglock.waitstart DESC NULLS LAST
                           LIMIT 1"""
                        ),
                        {"classid": classid, "objid": objid},
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                return dict(row)
            await asyncio.sleep(0.025)
    raise AssertionError("pipeline NativeSQL did not wait on the owned advisory gate")


async def _request_apply(
    app_module,
    actor,
    project_id,
    source_id,
):
    token = create_access_token(subject=str(actor.id), role=actor.role)
    transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://named-pipeline-fences",
        timeout=30,
    ) as client:
        return await client.post(
            f"/api/v1/projects/{project_id}/pipelines/apply",
            headers={"Authorization": f"Bearer {token}"},
            json={"pipeline_id": str(source_id), "set_default": True},
        )


async def _run_interleaved_success(
    test_engine,
    app_module,
    monkeypatch,
    tmp_path,
    runtime_maker,
    mode,
    actor,
    project_id,
    source_id,
    maker,
):
    lock_key = int(uuid.uuid4().hex[:15], 16)
    request_task = None
    response = None
    observations = {}
    try:
        async with api_db_binding(app_module, runtime_maker):
            async with _gated_pipeline_step(
                test_engine, project_id=project_id, lock_key=lock_key
            ):
                async with api_pipeline_owner_binding(
                    test_engine, monkeypatch, mode, tmp_path
                ):
                    async with _held_advisory_gate(test_engine, lock_key) as gate:
                        request_task = asyncio.create_task(
                            _request_apply(app_module, actor, project_id, source_id)
                        )
                        gate_held = True
                        try:
                            waiter = await _wait_for_advisory_waiter(
                                test_engine, lock_key
                            )
                            assert waiter["pid"] is not None
                            await _assert_project_update_lock_conflict(
                                test_engine, project_id
                            )
                            observations["project_update_lock_sqlstate"] = "55P03"
                            observations[
                                "source_deleted_while_fenced"
                            ] = await asyncio.wait_for(
                                _mutate_counter_and_delete_source(
                                    maker, project_id, source_id
                                ),
                                timeout=5,
                            )
                            assert observations["source_deleted_while_fenced"] == 1
                            unlocked = await gate.fetchval(
                                "SELECT pg_catalog.pg_advisory_unlock($1::bigint)",
                                lock_key,
                            )
                            assert unlocked is True
                            gate_held = False
                            response = await asyncio.wait_for(request_task, timeout=20)
                        finally:
                            if gate_held:
                                await gate.fetchval(
                                    "SELECT pg_catalog.pg_advisory_unlock($1::bigint)",
                                    lock_key,
                                )
                            if request_task is not None and not request_task.done():
                                done, _ = await asyncio.wait({request_task}, timeout=15)
                                if not done:
                                    request_task.cancel()
                                await asyncio.gather(
                                    request_task, return_exceptions=True
                                )
        assert response is not None
        return response, observations
    finally:
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)


@asynccontextmanager
async def _atomic_insert_failure(test_engine, project_id):
    owner = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=10
    )
    suffix = uuid.uuid4().hex
    trigger = f"pipeline_fence_insert_{suffix}"
    function = f"pipeline_fence_insert_{suffix}"
    try:
        await owner.execute(
            f"""CREATE FUNCTION public.{function}() RETURNS trigger
                LANGUAGE plpgsql AS $failure$
                BEGIN
                    IF NEW.project_id = '{project_id}'::uuid
                       AND NEW.name = 'Frozen source' THEN
                        RAISE EXCEPTION 'owned named-pipeline insertion failure';
                    END IF;
                    RETURN NEW;
                END;
                $failure$"""
        )
        await owner.execute(
            f"CREATE TRIGGER {trigger} BEFORE INSERT ON public.project_pipelines "
            f"FOR EACH ROW EXECUTE FUNCTION public.{function}()"
        )
        yield
    finally:
        try:
            await owner.execute(
                f"DROP TRIGGER IF EXISTS {trigger} ON public.project_pipelines"
            )
            await owner.execute(f"DROP FUNCTION IF EXISTS public.{function}()")
        finally:
            await owner.close()


@pytest.mark.parametrize("mode", _BINDINGS)
@pytest.mark.parametrize(
    "frozen",
    _FROZEN_SAMPLES,
    ids=lambda sample: sample["label"],
)
async def test_named_pipeline_native_bindings_preserve_frozen_apply_lock_observations(
    test_engine, app_module, monkeypatch, tmp_path, mode, frozen
):
    await assert_owned_test_database(test_engine)
    maker, actor, project_id, source_id, prior_default_id = await _seed(test_engine)
    aliases = {
        str(actor.id): "$actor",
        str(project_id): "$project",
        str(source_id): "$source",
        str(prior_default_id): "$prior_default",
    }
    runtime_engine = None
    try:
        runtime_engine, runtime_maker = await make_request_runtime(
            test_engine, application_name=f"inference-pipeline-fences-{mode}"
        )
        clear_local_identity(monkeypatch)
        before = await _snapshot(maker, project_id, source_id, aliases)
        assert before == _normalise(frozen["before"], aliases)

        if frozen["label"] == "key_share_and_deleted_source_snapshot":
            response, observations = await _run_interleaved_success(
                test_engine,
                app_module,
                monkeypatch,
                tmp_path,
                runtime_maker,
                mode,
                actor,
                project_id,
                source_id,
                maker,
            )
        else:
            async with _atomic_insert_failure(test_engine, project_id):
                async with api_db_binding(app_module, runtime_maker):
                    async with api_pipeline_owner_binding(
                        test_engine, monkeypatch, mode, tmp_path
                    ):
                        response = await _request_apply(
                            app_module, actor, project_id, source_id
                        )
            observations = {}

        if frozen["label"] == "key_share_and_deleted_source_snapshot":
            assert response.status_code == 201, response.text
            body = response.json()
            aliases[body["id"]] = "$copy"
            assert body["created_by_name"] is None
            assert body["stages"] == []
            assert body["is_default"] is True
        else:
            assert response.status_code == 500
            body = response.text

        after = await _snapshot(maker, project_id, source_id, aliases)
        if frozen["label"] == "atomic_insert_failure":
            assert after == before
        else:
            assert after["counter"] == before["counter"] + 1
            assert all(row["id"] != "$source" for row in after["pipelines"])
            assert len(after["pipelines"]) == 2
            assert [row["id"] for row in after["pipelines"] if row["is_default"]] == [
                "$copy"
            ]

        observed = {
            "label": frozen["label"],
            "before": before,
            "observations": observations,
            "response": {
                "status": response.status_code,
                "content_type": response.headers["content-type"],
                "body": _normalise(body, aliases),
            },
            "after": after,
        }
        assert observed == frozen
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()
        await _cleanup(maker, actor.id, project_id, source_id)
