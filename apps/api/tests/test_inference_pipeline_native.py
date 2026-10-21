"""Owned-role tests for the Inference named-pipeline NativeSQL owner."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from tests.factory import create_user
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_POLICY = "aap_inference_pipelines_v1.pipeline_policy()"
_STEP = "aap_inference_pipelines_v1.pipeline_step"
_PIPELINE_COLUMNS = [
    "id",
    "scope",
    "project_id",
    "organization_id",
    "name",
    "stages",
    "is_default",
    "created_by",
    "usage_count",
    "created_at",
    "updated_at",
]
_ACTIONS = [
    "begin",
    "status",
    "organization_ids",
    "pipeline_get",
    "pipeline_list",
    "backend_get",
    "backend_pool",
    "backend_enabled",
    "project_query",
    "project_access",
    "default_clear",
    "pipeline_insert",
    "pipeline_update",
    "pipeline_delete",
    "source_usage_increment",
    "finish",
]


def _inference_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


async def _connect_inference(test_engine):
    connection = await asyncpg.connect(
        _inference_dsn(test_engine), timeout=5, command_timeout=10
    )
    await connection.execute("SET ROLE inference_code")
    assert await connection.fetchval("SELECT current_user") == "inference_code"
    return connection


async def _step(connection, request, action, parameters, columns=(), values=()):
    raw = await connection.fetchval(
        f"SELECT {_STEP}($1::jsonb, $2::text, $3::jsonb, $4::text[], $5::text[])",
        json.dumps(request),
        action,
        json.dumps(parameters),
        list(columns),
        list(values),
    )
    return json.loads(raw) if isinstance(raw, str) else raw


async def test_inference_pipeline_owner_receipts_projection_and_closed_actions(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    operation_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    command = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "super_admin",
                f"inference-native-{uuid.uuid4().hex}@example.test",
                "Inference Native Owner",
            )
            actor_id = actor.id

        connection = await _connect_inference(test_engine)
        try:
            policy = await connection.fetchval(f"SELECT {_POLICY}")
            policy = json.loads(policy) if isinstance(policy, str) else policy
            assert policy == {
                "schema": "aap-inference-pipelines-policy/v1",
                "request_schema": "aap-inference-pipelines/v1",
                "step_schema": "aap-inference-pipelines-step/v1",
                "request_body_max_bytes": 16777216,
                "response_body_max_bytes": 16777216,
                "pipeline_columns": _PIPELINE_COLUMNS,
                "insert_columns": [
                    "id",
                    "scope",
                    "project_id",
                    "organization_id",
                    "name",
                    "stages",
                    "is_default",
                    "created_by",
                ],
                "update_columns": [
                    "scope",
                    "project_id",
                    "organization_id",
                    "name",
                    "stages",
                    "is_default",
                ],
                "actions": _ACTIONS,
                "serialization": {
                    "timezone": "UTC",
                    "date_style": "ISO,YMD",
                    "extra_float_digits": 3,
                },
            }

            with pytest.raises(asyncpg.PostgresError) as invalid:
                await connection.fetchval(
                    f"SELECT {_STEP}(NULL::jsonb, 'begin', '{{}}'::jsonb, ARRAY[]::text[], ARRAY[]::text[])"
                )
            assert invalid.value.sqlstate == "22023"

            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch("SELECT * FROM public.projects LIMIT 1")

            command = {
                "schema": "aap-inference-pipelines/v1",
                "operation_id": str(operation_id),
                "operation": "create_pipeline",
                "admitted_actor": {"id": str(actor_id), "role": "super_admin"},
                "target_id": None,
                "payload_ascii": json.dumps(
                    {
                        "scope": "public",
                        "project_id": None,
                        "organization_id": None,
                        "name": "Native receipt pipeline",
                        "stages": [],
                        "is_default": False,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            }
            transaction = connection.transaction()
            await transaction.start()
            started = await _step(connection, command, "begin", {})
            assert started["ok"] is True
            assert started["facts"] == {"replay": None}

            duplicate = await _step(connection, command, "begin", {})
            assert duplicate["ok"] is False
            assert duplicate["error"]["status_code"] == 409
            assert duplicate["error"]["detail"] == {"reason": "operation_busy"}
            assert duplicate["error"]["rollback"] is True
            await transaction.rollback()

            transaction = connection.transaction()
            await transaction.start()
            started = await _step(connection, command, "begin", {})
            assert started["ok"] is True
            assert started["facts"] == {"replay": None}

            inserted = await _step(
                connection,
                command,
                "pipeline_insert",
                {},
                [
                    "id",
                    "scope",
                    "project_id",
                    "organization_id",
                    "name",
                    "stages",
                    "is_default",
                    "created_by",
                ],
                [
                    str(pipeline_id),
                    "public",
                    None,
                    None,
                    "Native receipt pipeline",
                    "[]",
                    "false",
                    str(actor_id),
                ],
            )
            assert inserted["facts"] == {"resource_id": str(pipeline_id)}
            finished = await _step(
                connection,
                command,
                "finish",
                {"resource_id": str(pipeline_id)},
            )
            assert finished["facts"] == {"resource_id": str(pipeline_id)}
            pending_status = await _step(
                connection,
                {
                    "schema": "aap-inference-pipelines/v1",
                    "request_id": str(uuid.uuid4()),
                    "operation": "get_operation_status",
                    "command": command,
                },
                "status",
                {},
            )
            assert pending_status["facts"] == {"state": "unknown", "resource_id": None}
            pending_projection = await _step(
                connection,
                {
                    "schema": "aap-inference-pipelines/v1",
                    "request_id": str(uuid.uuid4()),
                    "operation": "get_pipeline_projection",
                    "admitted_actor": command["admitted_actor"],
                    "target_id": str(pipeline_id),
                    "filters": None,
                    "evidence": command,
                },
                "pipeline_get",
                {"pipeline_id": str(pipeline_id)},
            )
            # Receipt-bound reads retain caller-owned/savepoint transactions;
            # unlike status, a projection does not claim global commit.
            assert json.loads(pending_projection["facts"]["row_json"])["id"] == str(
                pipeline_id
            )
            await transaction.commit()
        finally:
            await connection.close()

        status_request = {
            "schema": "aap-inference-pipelines/v1",
            "request_id": str(uuid.uuid4()),
            "operation": "get_operation_status",
            "command": command,
        }
        connection = await _connect_inference(test_engine)
        try:
            async with connection.transaction(readonly=True):
                status = await _step(connection, status_request, "status", {})
                assert status["facts"] == {
                    "state": "committed",
                    "resource_id": str(pipeline_id),
                }

                changed_command = {**command, "payload_ascii": '{"different":true}'}
                unknown = await _step(
                    connection,
                    {**status_request, "command": changed_command},
                    "status",
                    {},
                )
                assert unknown["facts"] == {"state": "unknown", "resource_id": None}

                projection = {
                    "schema": "aap-inference-pipelines/v1",
                    "request_id": str(uuid.uuid4()),
                    "operation": "get_pipeline_projection",
                    "admitted_actor": command["admitted_actor"],
                    "target_id": str(pipeline_id),
                    "filters": None,
                    "evidence": command,
                }
                projected = await _step(
                    connection,
                    projection,
                    "pipeline_get",
                    {"pipeline_id": str(pipeline_id)},
                )
                row = json.loads(projected["facts"]["row_json"])
                assert set(row) == set(_PIPELINE_COLUMNS)
                assert row["id"] == str(pipeline_id)
                assert row["created_by"] == str(actor_id)
                assert row["stages"] == []
        finally:
            await connection.close()
    finally:
        cleanup = await _connect_inference(test_engine)
        try:
            await cleanup.execute(
                "DELETE FROM aap_inference_pipelines_v1.operation_receipts "
                "WHERE operation_id = $1::uuid",
                operation_id,
            )
            await cleanup.execute(
                "DELETE FROM public.project_pipelines WHERE id = $1::uuid",
                pipeline_id,
            )
        finally:
            await cleanup.close()
        async with maker.begin() as db:
            if actor_id is not None:
                await db.execute(delete(User).where(User.id == actor_id))


async def test_inference_pipeline_delete_retains_success_after_selected_row_disappears(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    operation_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    actor_id = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "super_admin",
                f"inference-delete-{uuid.uuid4().hex}@example.test",
                "Inference delete owner",
            )
            actor_id = actor.id
            db.add(
                ProjectPipeline(
                    id=pipeline_id,
                    scope="public",
                    name="Concurrently deleted pipeline",
                    stages=[],
                    created_by=actor_id,
                )
            )

        command = {
            "schema": "aap-inference-pipelines/v1",
            "operation_id": str(operation_id),
            "operation": "delete_pipeline",
            "admitted_actor": {"id": str(actor_id), "role": "super_admin"},
            "target_id": str(pipeline_id),
            "payload_ascii": "{}",
        }
        connection = await _connect_inference(test_engine)
        try:
            async with connection.transaction():
                assert (await _step(connection, command, "begin", {}))["ok"] is True
                selected = await _step(
                    connection,
                    command,
                    "pipeline_get",
                    {"pipeline_id": str(pipeline_id)},
                )
                assert json.loads(selected["facts"]["row_json"])["id"] == str(
                    pipeline_id
                )
                # The legacy ORM DELETE accepts zero matched rows after its
                # visibility snapshot; READ COMMITTED retains that outcome.
                async with maker.begin() as db:
                    await db.execute(
                        delete(ProjectPipeline).where(ProjectPipeline.id == pipeline_id)
                    )
                deleted = await _step(
                    connection,
                    command,
                    "pipeline_delete",
                    {"pipeline_id": str(pipeline_id)},
                )
                assert deleted["facts"] == {"resource_id": str(pipeline_id)}
                finished = await _step(
                    connection, command, "finish", {"resource_id": str(pipeline_id)}
                )
                assert finished["facts"] == {"resource_id": str(pipeline_id)}

            status = await _step(
                connection,
                {
                    "schema": "aap-inference-pipelines/v1",
                    "request_id": str(uuid.uuid4()),
                    "operation": "get_operation_status",
                    "command": command,
                },
                "status",
                {},
            )
            assert status["facts"] == {
                "state": "committed",
                "resource_id": str(pipeline_id),
            }
        finally:
            await connection.close()
    finally:
        cleanup = await _connect_inference(test_engine)
        try:
            await cleanup.execute(
                "DELETE FROM aap_inference_pipelines_v1.operation_receipts "
                "WHERE operation_id = $1::uuid",
                operation_id,
            )
        finally:
            await cleanup.close()
        async with maker.begin() as db:
            await db.execute(
                delete(ProjectPipeline).where(ProjectPipeline.id == pipeline_id)
            )
            if actor_id is not None:
                await db.execute(delete(User).where(User.id == actor_id))
