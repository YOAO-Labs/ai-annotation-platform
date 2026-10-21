"""Owned PostgreSQL acceptance for transaction-bound Backend probe snapshots."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_POLICY = "aap_inference_backend_probe_v1.backend_probe_policy()"
_OPEN = "aap_inference_backend_probe_v1.open_backend_probe_subject"
_WORKSPACE_BACKEND = "aap_inference_v1.workspace_configuration_backend"
_KEY_TABLE = "aap_inference_backend_probe_v1.probe_secret"


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


async def _connect(test_engine, *, role: str | None = None):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=10
    )
    if role is not None:
        await connection.execute(f"SET ROLE {role}")
        assert await connection.fetchval("SELECT current_user") == role
    return connection


async def _insert_backend(
    connection,
    registry_id: uuid.UUID,
    *,
    url: str,
    token: str,
    extra_params: dict,
) -> None:
    await connection.execute(
        """INSERT INTO public.ml_backend_registry (
               id, name, url, state, is_interactive, auth_method, auth_token,
               extra_params, source
           ) VALUES ($1, $2, $3, 'disconnected', false, 'token', $4, $5::jsonb, 'manual')""",
        registry_id,
        f"Probe snapshot {registry_id}",
        url,
        token,
        json.dumps(extra_params, ensure_ascii=False),
    )


async def _workspace_snapshot(connection, registry_id: uuid.UUID) -> dict:
    value = await connection.fetchval(
        f"SELECT {_WORKSPACE_BACKEND}($1::uuid)", registry_id
    )
    return json.loads(value)


async def _open_subject(connection, subject: str, registry_id: uuid.UUID) -> str:
    return await connection.fetchval(
        f"SELECT {_OPEN}($1::text, $2::uuid)", subject, registry_id
    )


async def _assert_invalid_subject(
    connection, subject: str | None, registry_id: uuid.UUID | None
) -> None:
    with pytest.raises(asyncpg.PostgresError) as caught:
        await connection.fetchval(
            f"SELECT {_OPEN}($1::text, $2::uuid)", subject, registry_id
        )
    assert caught.value.sqlstate == "22023"
    assert caught.value.message == "invalid backend probe subject"


async def _delete_backend(test_engine, registry_id: uuid.UUID) -> None:
    connection = await _connect(test_engine)
    try:
        await connection.execute(
            "DELETE FROM public.ml_backend_registry WHERE id = $1::uuid",
            registry_id,
        )
    finally:
        await connection.close()


async def test_probe_subject_preserves_uncommitted_snapshot_and_rejects_rollback(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    origin = await _connect(test_engine)
    probe = await _connect(test_engine, role="inference_backend_probe_client")
    registry_id = uuid.uuid4()
    original = {
        "id": str(registry_id),
        "url": f"https://probe-{registry_id}.example.test/base",
        "auth_method": "token",
        "auth_token": f"probe-secret-{uuid.uuid4().hex}",
        "extra_params": {"max_concurrency": 7, "unicode": "图片", "value": None},
    }
    transaction = origin.transaction()
    started = False
    try:
        await transaction.start()
        started = True
        await _insert_backend(
            origin,
            registry_id,
            url=original["url"],
            token=original["auth_token"],
            extra_params=original["extra_params"],
        )
        await origin.execute("SET LOCAL ROLE workspace_code")
        reply = await _workspace_snapshot(origin, registry_id)

        assert reply["schema"] == "aap-inference-workspace-configuration/v1"
        assert reply["registry_id"] == str(registry_id)
        assert set(reply["backend"]) == {"id", "probe_subject"}
        assert reply["backend"]["id"] == str(registry_id)
        subject = reply["backend"]["probe_subject"]
        assert subject.startswith("v1.")
        assert "https://" not in subject
        assert original["auth_token"] not in subject

        policy = json.loads(await probe.fetchval(f"SELECT {_POLICY}"))
        assert policy == {
            "schema": "aap-inference-backend-probe-native/v1",
            "probe_protocol": "aap-inference-backend-probe/v1",
            "subject_protocol": "aap-inference-backend-probe-subject/v1",
            "origin_transaction": "in_progress",
            "functions": [
                "aap_inference_backend_probe_v1.backend_probe_policy()",
                "aap_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)",
            ],
            "ready": True,
        }
        assert json.loads(await _open_subject(probe, subject, registry_id)) == original

        # The caller can rotate its uncommitted row after the probe snapshot. Opening
        # the subject must use the authenticated snapshot, not a second registry read.
        await origin.execute("RESET ROLE")
        await origin.execute(
            "UPDATE public.ml_backend_registry "
            "SET url = $2, auth_token = $3, extra_params = $4::jsonb "
            "WHERE id = $1::uuid",
            registry_id,
            f"https://rotated-{registry_id}.example.test/",
            "rotated-probe-secret",
            json.dumps({"max_concurrency": 19}),
        )
        assert json.loads(await _open_subject(probe, subject, registry_id)) == original

        tampered = subject[:3] + ("A" if subject[3] != "A" else "B") + subject[4:]
        await _assert_invalid_subject(probe, tampered, registry_id)
        await _assert_invalid_subject(probe, subject, uuid.uuid4())
        await _assert_invalid_subject(probe, None, None)

        await transaction.rollback()
        started = False
        await _assert_invalid_subject(probe, subject, registry_id)
    finally:
        if started:
            await transaction.rollback()
        await probe.close()
        await origin.close()
        await _delete_backend(test_engine, registry_id)


async def test_probe_snapshot_does_not_lock_or_reread_rotated_registry(test_engine):
    await assert_owned_test_database(test_engine)
    registry_id = uuid.uuid4()
    original_url = f"https://probe-lock-{registry_id}.example.test/old"
    original_token = f"probe-lock-secret-{uuid.uuid4().hex}"
    seeder = await _connect(test_engine)
    try:
        await _insert_backend(
            seeder,
            registry_id,
            url=original_url,
            token=original_token,
            extra_params={"source": "captured"},
        )
    finally:
        await seeder.close()
    origin = await _connect(test_engine)
    locker = await _connect(test_engine)
    probe = await _connect(test_engine, role="inference_backend_probe_client")
    origin_transaction = origin.transaction()
    locker_transaction = locker.transaction()
    origin_started = locker_started = False
    try:
        await origin_transaction.start()
        origin_started = True
        await origin.execute("SET LOCAL ROLE workspace_code")
        reply = await _workspace_snapshot(origin, registry_id)
        subject = reply["backend"]["probe_subject"]

        # A concurrent writer can lock and rotate the row, proving the fact function
        # did not leave a row lock while external setup I/O is pending.
        await locker_transaction.start()
        locker_started = True
        assert (
            await locker.fetchval(
                "SELECT id FROM public.ml_backend_registry "
                "WHERE id = $1::uuid FOR UPDATE NOWAIT",
                registry_id,
            )
            == registry_id
        )
        await locker.execute(
            "UPDATE public.ml_backend_registry "
            "SET url = $2, auth_token = $3, extra_params = $4::jsonb "
            "WHERE id = $1::uuid",
            registry_id,
            f"https://probe-lock-{registry_id}.example.test/new",
            "rotated-secret",
            json.dumps({"source": "rotated"}),
        )
        await locker_transaction.commit()
        locker_started = False

        opened = json.loads(await _open_subject(probe, subject, registry_id))
        assert opened == {
            "id": str(registry_id),
            "url": original_url,
            "auth_method": "token",
            "auth_token": original_token,
            "extra_params": {"source": "captured"},
        }

        await origin_transaction.commit()
        origin_started = False
        await _assert_invalid_subject(probe, subject, registry_id)
    finally:
        if locker_started:
            await locker_transaction.rollback()
        if origin_started:
            await origin_transaction.rollback()
        await probe.close()
        await locker.close()
        await origin.close()
        await _delete_backend(test_engine, registry_id)


async def test_probe_client_has_only_fixed_rpcs_and_no_secret_or_registry_access(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    connection = await _connect(test_engine)
    probe = await _connect(test_engine, role="inference_backend_probe_client")
    try:
        signatures = await connection.fetch(
            """SELECT namespace.nspname || '.' || function.proname || '(' ||
                          pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
               FROM pg_catalog.pg_proc AS function
               JOIN pg_catalog.pg_namespace AS namespace
                 ON namespace.oid = function.pronamespace
               WHERE namespace.nspname = 'aap_inference_backend_probe_v1'
                 AND function.prokind = 'f'
                 AND pg_catalog.has_function_privilege(
                     'inference_backend_probe_client', function.oid, 'EXECUTE'
                 )
               ORDER BY signature"""
        )
        assert [row["signature"] for row in signatures] == [
            "aap_inference_backend_probe_v1.backend_probe_policy()",
            "aap_inference_backend_probe_v1.open_backend_probe_subject(text, uuid)",
        ]
        function_metadata = await connection.fetch(
            """SELECT namespace.nspname || '.' || function.proname || '(' ||
                          pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature,
                      pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                      function.prosecdef AS security_definer,
                      function.proconfig AS settings,
                      EXISTS (
                          SELECT 1
                          FROM pg_catalog.aclexplode(COALESCE(
                              function.proacl,
                              pg_catalog.acldefault('f', function.proowner)
                          )) AS privilege
                          WHERE privilege.grantee = 0
                            AND privilege.privilege_type = 'EXECUTE'
                      ) AS public_execute
               FROM pg_catalog.pg_proc AS function
               JOIN pg_catalog.pg_namespace AS namespace
                 ON namespace.oid = function.pronamespace
               WHERE namespace.nspname = 'aap_inference_backend_probe_v1'
                 AND function.prokind = 'f'
               ORDER BY signature"""
        )
        assert len(function_metadata) == 2
        for metadata in function_metadata:
            assert metadata["owner"] == "inference_code"
            assert metadata["security_definer"] is True
            assert metadata["public_execute"] is False
            assert metadata["settings"] is not None
            assert "search_path=pg_catalog, pg_temp" in metadata["settings"]

        assert (
            await connection.fetchval(
                "SELECT pg_catalog.has_table_privilege("
                "'inference_backend_probe_client', $1, 'SELECT')",
                _KEY_TABLE,
            )
            is False
        )
        assert (
            await connection.fetchval(
                "SELECT pg_catalog.has_table_privilege("
                "'inference_backend_probe_client', 'public.ml_backend_registry', 'SELECT')"
            )
            is False
        )
        assert (
            await connection.fetchval(
                "SELECT pg_catalog.has_function_privilege("
                "'workspace_code', $1, 'EXECUTE')",
                "aap_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)",
            )
            is False
        )
        assert (
            await connection.fetchval(
                "SELECT pg_catalog.has_table_privilege('workspace_code', $1, 'SELECT')",
                _KEY_TABLE,
            )
            is False
        )

        key_row = await connection.fetchrow(
            f"SELECT secret.singleton, pg_catalog.octet_length(secret.secret) "
            f"AS key_bytes, pg_catalog.pg_get_userbyid(class.relowner) AS owner "
            f"FROM {_KEY_TABLE} AS secret "
            "JOIN pg_catalog.pg_class AS class "
            "ON class.oid = pg_catalog.to_regclass($1)",
            _KEY_TABLE,
        )
        assert key_row["singleton"] is True
        assert key_row["key_bytes"] == 32
        assert key_row["owner"] == "inference_code"
        assert (
            await connection.fetchval(
                "SELECT pg_catalog.pg_has_role("
                "'inference_backend_probe_client', 'inference_code', 'MEMBER')"
            )
            is False
        )
        assert (
            await connection.fetchval(
                "SELECT pg_catalog.has_database_privilege("
                "'inference_backend_probe_client', current_database(), 'CREATE')"
            )
            is False
        )
        assert (
            await connection.fetchval(
                "SELECT pg_catalog.has_database_privilege("
                "'inference_backend_probe_client', current_database(), 'TEMP')"
            )
            is False
        )

        for statement in (
            f"SELECT secret FROM {_KEY_TABLE}",
            "SELECT auth_token FROM public.ml_backend_registry LIMIT 1",
            "CREATE TEMP TABLE backend_probe_forbidden(id integer)",
            "CREATE TABLE aap_inference_backend_probe_v1.forbidden(id integer)",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await probe.execute(statement)
    finally:
        await probe.close()
        await connection.close()
