"""A restricted LOGIN cannot forge fixture physical-retirement evidence."""

import os
import secrets
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.services import export_media_retirement

from tests.test_asset_storage_cleanup_runtime import _sqlstate, _verify_postgres

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable PostgreSQL verifier",
    ),
]


async def test_fixture_receipts_reject_restricted_login_even_with_stale_grants(
    test_engine,
):
    _verify_postgres(test_engine.url)
    role = "fixture_export_probe_" + uuid.uuid4().hex
    password = secrets.token_urlsafe(32)
    async with test_engine.begin() as owner:
        await owner.execute(
            text(
                "SELECT set_config('aap.test_role_name', :role, true), "
                "set_config('aap.test_role_password', :password, true)"
            ),
            {"role": role, "password": password},
        )
        await owner.exec_driver_sql(
            """DO $create_fixture_export_probe$
            BEGIN
                EXECUTE format(
                    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                    'NOREPLICATION NOBYPASSRLS PASSWORD %L',
                    current_setting('aap.test_role_name'),
                    current_setting('aap.test_role_password')
                );
            END;
            $create_fixture_export_probe$;"""
        )
    quoted = test_engine.dialect.identifier_preparer.quote_identifier(role)
    quoted_owner = None
    database = test_engine.dialect.identifier_preparer.quote_identifier(
        test_engine.url.database
    )
    runtime = None
    try:
        async with test_engine.begin() as owner:
            assert await export_media_retirement.fixture_owner_session(owner)
            await owner.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {database} TO {quoted}"
            )
            await owner.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA public, yoao_fixture_v1 TO {quoted}"
            )
            receipt_owner = await owner.scalar(
                text(
                    "SELECT pg_catalog.pg_get_userbyid(relowner) FROM pg_catalog.pg_class "
                    "WHERE oid = CAST('public.fixture_export_zip_retirements' AS regclass)"
                )
            )
            quoted_owner = test_engine.dialect.identifier_preparer.quote_identifier(
                receipt_owner
            )
            await owner.exec_driver_sql(f"SET LOCAL ROLE {quoted_owner}")
            await owner.exec_driver_sql(
                f"GRANT INSERT ON public.fixture_export_zip_retirements TO {quoted}"
            )
            await owner.exec_driver_sql(
                f"GRANT INSERT ON yoao_fixture_v1.fixture_native_export_outputs TO {quoted}"
            )
            await owner.exec_driver_sql(
                "GRANT EXECUTE ON FUNCTION "
                f"public.retire_fixture_native_export_output(uuid) TO {quoted}"
            )
            await owner.exec_driver_sql("RESET ROLE")
        url = make_url(test_engine.url).set(username=role, password=password)
        runtime = create_async_engine(url, hide_parameters=True)
        async with runtime.connect() as connection:
            assert await connection.scalar(text("SELECT session_user")) == role
            values = {
                "bundle": uuid.uuid4(),
                "output": uuid.uuid4(),
                "run": uuid.uuid4(),
                "project": uuid.uuid4(),
                "job": uuid.uuid4(),
            }
            statements = (
                text(
                    "INSERT INTO public.fixture_export_zip_retirements "
                    "(bundle_id, output_id, fixture_run_id, project_id, job_id, sha256, size_bytes) "
                    "VALUES (:bundle, :output, :run, :project, :job, repeat('a', 64), 1)"
                ),
                text(
                    "INSERT INTO yoao_fixture_v1.fixture_native_export_outputs "
                    "(output_id, job_id, run_id, project_id, sha256, size_bytes, retired_at) "
                    "VALUES (:output, :job, :run, :project, repeat('a', 64), 1, clock_timestamp())"
                ),
                text("SELECT public.retire_fixture_native_export_output(:output)"),
            )
            for statement in statements:
                with pytest.raises(DBAPIError) as error:
                    async with connection.begin_nested():
                        await connection.execute(statement, values)
                assert _sqlstate(error.value) == "42501"
    finally:
        if runtime is not None:
            await runtime.dispose()
        async with test_engine.begin() as owner:
            if quoted_owner is not None:
                await owner.exec_driver_sql(f"SET LOCAL ROLE {quoted_owner}")
                await owner.exec_driver_sql(
                    f"REVOKE INSERT ON public.fixture_export_zip_retirements FROM {quoted}"
                )
                await owner.exec_driver_sql(
                    f"REVOKE INSERT ON yoao_fixture_v1.fixture_native_export_outputs FROM {quoted}"
                )
                await owner.exec_driver_sql(
                    "REVOKE EXECUTE ON FUNCTION "
                    f"public.retire_fixture_native_export_output(uuid) FROM {quoted}"
                )
                await owner.exec_driver_sql("RESET ROLE")
            await owner.exec_driver_sql(
                f"REVOKE USAGE ON SCHEMA public, yoao_fixture_v1 FROM {quoted}"
            )
            await owner.exec_driver_sql(
                f"REVOKE CONNECT ON DATABASE {database} FROM {quoted}"
            )
            await owner.exec_driver_sql(f"DROP ROLE {quoted}")
