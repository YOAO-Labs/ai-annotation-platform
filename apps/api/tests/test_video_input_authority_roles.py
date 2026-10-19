"""Video projections execute from Assets without cross-owner table access."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aap_assets_video_inputs import authority as c
from tests.test_video_input_authorities_native import _seed_video_task

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data role verifier",
    ),
]


async def test_assets_owner_calls_scoped_video_source_without_labeling_or_jobs_select(
    db_session,
):
    actor, project, _, _, task = await _seed_video_task(db_session)
    await db_session.execute(text("SET LOCAL ROLE assets_code"))
    try:
        assert await db_session.scalar(text("SELECT current_user")) == "assets_code"
        for relation in ("tasks", "async_jobs", "users", "projects"):
            assert not await db_session.scalar(
                text(
                    "SELECT has_any_column_privilege(current_user,:relation,'SELECT,INSERT,UPDATE,REFERENCES') OR has_table_privilege(current_user,:relation,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                ),
                {"relation": "public." + relation},
            ), relation
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(text("SELECT id FROM public.tasks LIMIT 1"))
        read = c.ReadVideoTaskRequest(task_id=task, actor_id=actor)
        raw = await db_session.scalar(
            text("SELECT aap_labeling_v1.read_video_input_task_source(:task,:actor)"),
            {"task": task, "actor": actor},
        )
        source = c.parse_native("read_video_input_task_source", read, raw)
        assert (
            source.binding.task_id == task and source.binding.project_id == project.id
        )
        lock = c.LockVideoTaskRequest(
            task_id=task, actor_id=actor, expected_binding=source.binding
        )
        raw = await db_session.scalar(
            text(
                "SELECT aap_labeling_v1.lock_video_input_task_source(:task,:actor,CAST(:binding AS jsonb))"
            ),
            {"task": task, "actor": actor, "binding": source.binding.model_dump_json()},
        )
        assert c.parse_native("lock_video_input_task_source", lock, raw).upload_ready
    finally:
        await db_session.execute(text("RESET ROLE"))


async def test_only_assets_owner_receives_video_authority_execute_grants(db_session):
    for name, operation in c.OPERATIONS.items():
        signature = (
            operation.namespace + "." + name + "(" + ",".join(operation.sql_types) + ")"
        )
        for role, allowed in [
            ("assets_code", True),
            ("append_client", False),
            ("asset_media_control_client", False),
            ("asset_image_worker_client", False),
        ]:
            observed = await db_session.scalar(
                text(
                    "SELECT has_function_privilege(:role,p.oid,'EXECUTE') FROM pg_catalog.pg_proc p "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname||'.'||p.proname||'('||replace(pg_catalog.oidvectortypes(p.proargtypes),' ','')||')'=:signature"
                ),
                {"role": role, "signature": signature},
            )
            assert observed is allowed, (role, signature)
