"""Ordinary Labeling access stays behind fixed owner capabilities."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.test_asset_storage_cleanup_runtime import _sqlstate, _verify_postgres
from tests.test_jobs_creation_runtime_native import _lan_runtime

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data PostgreSQL verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
ROLE = "identity_api_consumer"


def _acl() -> str:
    sql = (ROOT / "infra/docker/lan-production-grants.sql").read_text()
    return (
        "DO $labeling_private_acl$"
        + sql.split("DO $labeling_private_acl$", 1)[1].split(
            "$labeling_private_acl$;", 1
        )[0]
        + "$labeling_private_acl$;"
    )


async def test_labeling_acl_removes_columns_partitions_sequences_and_defaults(
    test_engine,
):
    _verify_postgres(test_engine.url)
    runtime = await _lan_runtime(test_engine)
    try:
        async with test_engine.connect() as connection:
            transaction = await connection.begin()
            try:
                leaf = await connection.scalar(
                    text(
                        "SELECT relid::pg_catalog.regclass::text "
                        "FROM pg_catalog.pg_partition_tree('public.predictions') "
                        "WHERE isleaf ORDER BY relid LIMIT 1"
                    )
                )
                assert leaf is not None
                for target in (ROLE, "PUBLIC"):
                    for statement in (
                        f"GRANT SELECT (id), UPDATE (status) ON public.tasks TO {target}",
                        f"GRANT SELECT ON {leaf} TO {target}",
                        f"GRANT USAGE,SELECT,UPDATE ON public.display_seq_batches TO {target}",
                        f"GRANT UPDATE (status) ON aap_labeling_v1.task_rows TO {target}",
                        f"ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO {target}",
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {target}",
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {target}",
                    ):
                        await connection.exec_driver_sql(statement)
                await connection.execute(
                    text(
                        "SELECT set_config('aap.application_role',:role,true), "
                        "set_config('aap.expected_database',current_database(),true)"
                    ),
                    {"role": ROLE},
                )
                await connection.exec_driver_sql(_acl())
                await connection.exec_driver_sql(
                    "CREATE TABLE public.labeling_acl_future_probe(id bigserial PRIMARY KEY)"
                )
                for relation in (
                    "public.tasks",
                    leaf,
                    "public.labeling_acl_future_probe",
                ):
                    assert not await connection.scalar(
                        text(
                            "SELECT pg_catalog.has_table_privilege(:role,:relation,"
                            "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                            "pg_catalog.has_any_column_privilege(:role,:relation,"
                            "'SELECT,INSERT,UPDATE,REFERENCES')"
                        ),
                        {"role": ROLE, "relation": relation},
                    )
                for sequence in (
                    "public.display_seq_tasks",
                    "public.display_seq_batches",
                    "public.labeling_acl_future_probe_id_seq",
                ):
                    assert not await connection.scalar(
                        text(
                            "SELECT pg_catalog.has_sequence_privilege(:role,:sequence,'USAGE,SELECT,UPDATE')"
                        ),
                        {"role": ROLE, "sequence": sequence},
                    )
                await connection.exec_driver_sql(f"SET LOCAL ROLE {ROLE}")
                assert (
                    await connection.scalar(
                        text("SELECT aap_labeling_v1.next_batch_display_id()")
                    )
                ).startswith("BT-")
                await connection.exec_driver_sql(
                    "SELECT id FROM aap_labeling_v1.task_rows LIMIT 1"
                )
                for statement in (
                    "SELECT id FROM public.tasks LIMIT 1",
                    f"SELECT id FROM {leaf} LIMIT 1",
                    "SELECT secret FROM public.mask_gc_write_permit_secrets",
                    "SELECT nextval('public.display_seq_batches')",
                    "UPDATE aap_labeling_v1.task_rows SET status='pending' WHERE false",
                ):
                    async with connection.begin_nested() as savepoint:
                        with pytest.raises(DBAPIError) as rejected:
                            await connection.exec_driver_sql(statement)
                        assert _sqlstate(rejected.value) == "42501"
                        await savepoint.rollback()
            finally:
                await transaction.rollback()
    finally:
        await runtime.dispose()


async def test_labeling_acl_rejects_inherited_storage_access(test_engine):
    _verify_postgres(test_engine.url)
    runtime = await _lan_runtime(test_engine)
    try:
        async with test_engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.exec_driver_sql(
                    "CREATE ROLE labeling_acl_bypass NOLOGIN"
                )
                await connection.exec_driver_sql(
                    "GRANT SELECT (id) ON public.tasks TO labeling_acl_bypass"
                )
                await connection.exec_driver_sql(
                    f"GRANT labeling_acl_bypass TO {ROLE} WITH INHERIT TRUE"
                )
                await connection.execute(
                    text(
                        "SELECT set_config('aap.application_role',:role,true), "
                        "set_config('aap.expected_database',current_database(),true)"
                    ),
                    {"role": ROLE},
                )
                with pytest.raises(
                    DBAPIError, match="Runtime retains private Labeling access"
                ):
                    await connection.exec_driver_sql(_acl())
            finally:
                await transaction.rollback()
    finally:
        await runtime.dispose()
