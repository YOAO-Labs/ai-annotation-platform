"""Native connection capabilities work from the restricted Source LOGIN via psql."""

from __future__ import annotations

import json
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_assets_source.connections_contract import (
    CONNECTION_NATIVE_SIGNATURES,
    RUNTIME_NATIVE_SIGNATURES,
)
from aap_assets_source_runtime.database import ROLE_PREFLIGHT_SQL
from tests.test_asset_storage_cleanup_runtime import _required_env, _verify_postgres
from tests.test_assets_catalog_native import _seed_actor

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data role verifier",
    ),
]

_SOURCE_URL = "AAP_MODULE_DATA_ASSET_SOURCE_URL"
_CONTAINER_ID = "AAP_MODULE_DATA_PG_CONTAINER_ID"
_EXPECTED_SOURCE_CAPABILITIES = {
    "begin_operator_auxiliary",
    "open_operator_storage_operation",
    "record_operator_original_prepared",
    "mark_operator_original_writing",
    "record_operator_original_written",
    "record_operator_original_verified",
    "fail_operator_original",
    "close_operator_storage_operation",
    "reserve_dataset_upload",
    "lock_dataset_upload",
    "find_dataset_upload_duplicate",
    "complete_dataset_upload",
    "discard_dataset_upload",
    "delete_dataset",
    "delete_dataset_item",
    "lock_legacy_dataset_upload",
    "complete_legacy_dataset_upload",
    "begin_dataset_ingest",
    "prepare_dataset_ingest_batch",
    "complete_dataset_ingest_batch",
    "read_dataset_ingest_layout",
    "finish_dataset_ingest",
    "resolve_internal_image_source",
    "schedule_image_media",
    "read_legacy_dataset_upload",
}


def _native_psql(
    source_url,
    *,
    actor_id,
    create_operation_id,
    update_operation_id,
    test_operation_id,
    delete_operation_id,
):
    container_id = _required_env(_CONTAINER_ID)
    sql = """\
BEGIN;
SELECT current_user || '|' || session_user || '|' || current_database();
SELECT pg_catalog.has_schema_privilege(session_user, 'aap_assets_v1', 'USAGE')::text
    || '|' || pg_catalog.has_schema_privilege(session_user, 'aap_identity_v1', 'USAGE')::text
    || '|' || pg_catalog.has_schema_privilege(session_user, 'aap_settings_v1', 'USAGE')::text
    || '|' || pg_catalog.has_schema_privilege(session_user, 'aap_audit_v1', 'USAGE')::text
    || '|' || pg_catalog.has_schema_privilege(session_user, 'aap_assets_v1', 'CREATE')::text
    || '|' || (
        pg_catalog.has_table_privilege(session_user, 'public.storage_connections', 'SELECT')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connections', 'INSERT')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connections', 'UPDATE')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connections', 'DELETE')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connections', 'TRUNCATE')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connections', 'REFERENCES')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connections', 'TRIGGER')
    )::text
    || '|' || (
        pg_catalog.has_any_column_privilege(session_user, 'public.storage_connections', 'SELECT')
        OR pg_catalog.has_any_column_privilege(session_user, 'public.storage_connections', 'INSERT')
        OR pg_catalog.has_any_column_privilege(session_user, 'public.storage_connections', 'UPDATE')
        OR pg_catalog.has_any_column_privilege(session_user, 'public.storage_connections', 'REFERENCES')
    )::text
    || '|' || (
        pg_catalog.has_table_privilege(session_user, 'public.storage_connection_operations', 'SELECT')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connection_operations', 'INSERT')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connection_operations', 'UPDATE')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connection_operations', 'DELETE')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connection_operations', 'TRUNCATE')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connection_operations', 'REFERENCES')
        OR pg_catalog.has_table_privilege(session_user, 'public.storage_connection_operations', 'TRIGGER')
    )::text
    || '|' || (
        pg_catalog.has_any_column_privilege(session_user, 'public.storage_connection_operations', 'SELECT')
        OR pg_catalog.has_any_column_privilege(session_user, 'public.storage_connection_operations', 'INSERT')
        OR pg_catalog.has_any_column_privilege(session_user, 'public.storage_connection_operations', 'UPDATE')
        OR pg_catalog.has_any_column_privilege(session_user, 'public.storage_connection_operations', 'REFERENCES')
    )::text
    || '|' || EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace=namespace.oid
        WHERE relation.relkind='S'
          AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND (
              pg_catalog.has_sequence_privilege(session_user, relation.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege(session_user, relation.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege(session_user, relation.oid, 'UPDATE')
          )
    )::text;
SELECT COALESCE(
    pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object('name', proc.proname, 'args', proc.proargnames)
        ORDER BY proc.proname
    ), '[]'::jsonb
)
FROM pg_catalog.pg_proc AS proc
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=proc.pronamespace
WHERE namespace.nspname='aap_assets_v1' AND proc.prokind='f'
  AND pg_catalog.has_function_privilege(session_user, proc.oid, 'EXECUTE');
SELECT aap_assets_v1.list_storage_connections(
    actor_id => :'actor_id'::uuid,
    after_created_at => NULL::timestamptz,
    after_id => NULL::uuid,
    page_size => 25
);
SELECT aap_assets_v1.read_storage_connection_policy(
    actor_id => :'actor_id'::uuid,
    fallback_allowlist => '[]'::jsonb
);
SELECT aap_assets_v1.create_storage_connection(
    operation_id => :'create_operation_id'::uuid,
    actor_id => :'actor_id'::uuid,
    intent => '{"name":"psql native connection","kind":"s3","config":{"endpoint":"https://psql.example.test","bucket":"psql-fixture","use_ssl":true},"scope":"owner","secret_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'::jsonb,
    config => '{"endpoint":"https://psql.example.test","bucket":"psql-fixture","use_ssl":true}'::jsonb,
    secret_enc => 'cHNsLXNlYWxlZC1jaXBoZXJ0ZXh0',
    policy_digest => aap_assets_v1.read_storage_connection_policy(
        actor_id => :'actor_id'::uuid,
        fallback_allowlist => '[]'::jsonb
    ) #>> '{data,policy_digest}',
    fallback_allowlist => '[]'::jsonb,
    audit_context => '{}'::jsonb
);
SELECT aap_assets_v1.read_storage_connection(
    actor_id => :'actor_id'::uuid,
    connection_id => :'create_operation_id'::uuid
);
SELECT aap_assets_v1.capture_storage_connection(
    actor_id => :'actor_id'::uuid,
    connection_id => :'create_operation_id'::uuid,
    access => 'admin'
);
WITH captured AS (
    SELECT aap_assets_v1.capture_storage_connection(
        actor_id => :'actor_id'::uuid,
        connection_id => :'create_operation_id'::uuid,
        access => 'admin'
    ) -> 'data' AS data
)
SELECT aap_assets_v1.update_storage_connection_authorized(
    operation_id => :'update_operation_id'::uuid,
    actor_id => :'actor_id'::uuid,
    connection_id => :'create_operation_id'::uuid,
    intent => jsonb_build_object(
        'name', 'psql native updated', 'config', NULL, 'secret_digest', NULL
    ),
    config => captured.data -> 'connection' -> 'config',
    secret_enc => NULL,
    source_snapshot => captured.data ->> 'source_snapshot',
    policy_digest => NULL,
    fallback_allowlist => '[]'::jsonb,
    audit_context => '{}'::jsonb,
    api_key_id => NULL,
    required_scopes => ARRAY[]::text[],
    expected_proof => NULL
)
FROM captured;
WITH captured AS (
    SELECT aap_assets_v1.capture_storage_connection(
        actor_id => :'actor_id'::uuid,
        connection_id => :'create_operation_id'::uuid,
        access => 'use'
    ) -> 'data' AS data
), policy AS (
    SELECT aap_assets_v1.read_storage_connection_policy(
        actor_id => :'actor_id'::uuid,
        fallback_allowlist => '[]'::jsonb
    ) -> 'data' AS data
)
SELECT aap_assets_v1.record_storage_connection_test(
    operation_id => :'test_operation_id'::uuid,
    actor_id => :'actor_id'::uuid,
    connection_id => :'create_operation_id'::uuid,
    source_snapshot => captured.data ->> 'source_snapshot',
    policy_digest => policy.data ->> 'policy_digest',
    fallback_allowlist => '[]'::jsonb,
    result => '{"ok":false,"message":"连接失败: TimeoutError","sample_count":null}'::jsonb,
    audit_context => '{}'::jsonb
)
FROM captured CROSS JOIN policy;
SELECT aap_assets_v1.read_storage_connection_receipt(
    operation_id => :'test_operation_id'::uuid,
    actor_id => :'actor_id'::uuid,
    action => 'test',
    connection_id => :'create_operation_id'::uuid,
    intent => '{}'::jsonb
);
SELECT aap_assets_v1.delete_storage_connection(
    operation_id => :'delete_operation_id'::uuid,
    actor_id => :'actor_id'::uuid,
    connection_id => :'create_operation_id'::uuid,
    audit_context => '{}'::jsonb
);
ROLLBACK;
"""
    environment = os.environ.copy()
    environment["PGPASSWORD"] = source_url.password or ""
    command = [
        "docker",
        "exec",
        "-i",
        "-e",
        "PGPASSWORD",
        container_id,
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-h",
        "127.0.0.1",
        "-p",
        "5432",
        "-U",
        "asset_source_client",
        "-d",
        source_url.database,
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"actor_id={actor_id}",
        "-v",
        f"create_operation_id={create_operation_id}",
        "-v",
        f"update_operation_id={update_operation_id}",
        "-v",
        f"test_operation_id={test_operation_id}",
        "-v",
        f"delete_operation_id={delete_operation_id}",
        "-f",
        "-",
    ]
    completed = subprocess.run(
        command,
        input=sql,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        pytest.fail(
            "restricted Source psql connection transaction failed", pytrace=False
        )
    return completed.stdout.splitlines()


def _json_object(line: str) -> dict:
    value = json.loads(line)
    assert isinstance(value, dict)
    return value


def _success(value: dict) -> dict:
    assert set(value) == {"schema", "ok", "data"}
    assert value["schema"] == "aap-assets-connections/v1"
    assert value["ok"] is True
    assert isinstance(value["data"], dict)
    return value["data"]


async def test_restricted_source_psql_calls_frozen_connection_surface_and_rolls_back(
    test_engine,
):
    raw_url = os.environ.get(_SOURCE_URL)
    if not raw_url:
        pytest.fail(f"the module-data verifier did not provide {_SOURCE_URL}")
    try:
        source_url = make_url(raw_url)
    except Exception:
        pytest.fail("the restricted Source connection URL is invalid", pytrace=False)
    if not (
        source_url.username == "asset_source_client"
        and source_url.password
        and source_url.host == "127.0.0.1"
        and source_url.port is not None
        and source_url.database
        and source_url.database.endswith("_test")
        and test_engine.url.database == source_url.database
    ):
        pytest.fail("Source LOGIN URL did not target the owned test database")

    async with test_engine.connect() as owner:
        owner_database = await owner.scalar(text("SELECT current_database()"))
    if owner_database != source_url.database or not owner_database.endswith("_test"):
        pytest.fail("owner connection did not target the Source test database")
    _verify_postgres(source_url)
    restricted_engine = create_async_engine(source_url, hide_parameters=True)
    try:
        async with restricted_engine.connect() as restricted:
            assert await restricted.scalar(text(ROLE_PREFLIGHT_SQL)) is True
    finally:
        await restricted_engine.dispose()

    actor_id = None
    create_operation_id, update_operation_id = uuid4(), uuid4()
    test_operation_id, delete_operation_id = uuid4(), uuid4()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with maker() as owner:
            actor_id = await _seed_actor(owner, role="project_admin")
            await owner.commit()

        lines = _native_psql(
            source_url,
            actor_id=actor_id,
            create_operation_id=create_operation_id,
            update_operation_id=update_operation_id,
            test_operation_id=test_operation_id,
            delete_operation_id=delete_operation_id,
        )
        assert len(lines) == 12
        assert (
            lines[0] == f"asset_source_client|asset_source_client|{source_url.database}"
        )
        assert lines[1].split("|") == ["true"] + ["false"] * 9

        capabilities = json.loads(lines[2])
        assert isinstance(capabilities, list)
        by_name = {entry["name"]: entry["args"] for entry in capabilities}
        assert set(by_name) == (
            _EXPECTED_SOURCE_CAPABILITIES
            | set(CONNECTION_NATIVE_SIGNATURES)
            | set(RUNTIME_NATIVE_SIGNATURES)
        )

        listing = _success(_json_object(lines[3]))
        assert listing["actor_id"] == str(actor_id)
        assert set(listing) == {
            "actor_id",
            "after",
            "next_cursor",
            "snapshot_digest",
            "total",
            "complete",
            "connections",
        }
        policy = _success(_json_object(lines[4]))
        assert policy["actor_id"] == str(actor_id)
        assert policy["platform_role"] == "project_admin"
        assert len(policy["policy_digest"]) == 64

        created = _success(_json_object(lines[5]))
        assert created["action"] == "create"
        assert created["operation_id"] == str(create_operation_id)
        assert created["connection_id"] == str(create_operation_id)
        assert created["connection"]["project_id"] is None
        assert created["connection"]["scope"] == "owner"
        assert "secret_enc" not in created["connection"]

        read = _success(_json_object(lines[6]))
        assert read["actor_id"] == str(actor_id)
        assert read["connection_id"] == str(create_operation_id)
        assert read["connection"]["secret_set"] is True
        assert "secret_enc" not in read["connection"]

        capture = _success(_json_object(lines[7]))
        assert capture["connection_id"] == str(create_operation_id)
        assert capture["access"] == "admin"
        assert capture["secret_enc"] == "cHNsLXNlYWxlZC1jaXBoZXJ0ZXh0"
        assert len(capture["source_snapshot"]) == 64
        assert "policy_digest" not in capture

        updated = _success(_json_object(lines[8]))
        assert updated["action"] == "update"
        assert updated["operation_id"] == str(update_operation_id)
        assert updated["connection"]["name"] == "psql native updated"

        probe = _success(_json_object(lines[9]))
        assert probe == {
            "ok": False,
            "message": "连接失败: TimeoutError",
            "sample_count": None,
            "operation_id": str(test_operation_id),
            "actor_id": str(actor_id),
            "connection_id": str(create_operation_id),
            "action": "test",
        }
        receipt = _success(_json_object(lines[10]))
        assert receipt["result"] == probe
        deleted = _success(_json_object(lines[11]))
        assert deleted == {
            "operation_id": str(delete_operation_id),
            "actor_id": str(actor_id),
            "action": "delete",
            "connection_id": str(create_operation_id),
            "connection": None,
        }

        async with maker.begin() as owner:
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.storage_connections WHERE id=:id"
                    ),
                    {"id": create_operation_id},
                )
                == 0
            )
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.storage_connection_operations "
                        "WHERE operation_id=ANY(:ids)"
                    ),
                    {
                        "ids": [
                            create_operation_id,
                            update_operation_id,
                            test_operation_id,
                            delete_operation_id,
                        ]
                    },
                )
                == 0
            )
    finally:
        if actor_id is not None:
            async with maker.begin() as owner:
                await owner.execute(
                    text("DELETE FROM public.users WHERE id=:id"), {"id": actor_id}
                )
