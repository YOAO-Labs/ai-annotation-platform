"""Least-privilege contract for the native preannotation result executor."""

from __future__ import annotations

import re
import os
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[3]
GRANTS = ROOT / "infra/docker/preannotation-result-data-grants.sql"

EXPECTED = {
    "aap_jobs_v1.claim_batch_result_execution(jsonb)",
    "aap_jobs_v1.heartbeat_batch_result_execution(jsonb)",
    "aap_jobs_v1.release_batch_result_execution(jsonb)",
    "aap_jobs_v1.stage_batch_result_members(jsonb)",
    "aap_jobs_v1.start_batch_result_execution(jsonb)",
    "aap_jobs_v1.read_batch_result_execution(jsonb)",
    "aap_jobs_v1.mark_batch_result_overwrite(jsonb)",
    "aap_jobs_v1.skip_batch_result_member(jsonb)",
    "aap_jobs_v1.finish_batch_result_execution(jsonb)",
    "aap_jobs_v1.start_batch_result_member(jsonb)",
    "aap_labeling_v1.begin_batch_result(jsonb)",
    "aap_labeling_v1.put_batch_result_part(jsonb)",
    "aap_labeling_v1.seal_batch_result(jsonb)",
    "aap_labeling_v1.read_batch_result(jsonb)",
}


def test_executor_login_gets_only_fourteen_public_operations():
    script = GRANTS.read_text(encoding="utf-8")
    grant = re.search(
        r"(?is)GRANT EXECUTE ON FUNCTION\s+(.*?)\s+TO\s+:\"result_role\";",
        script,
    )
    assert grant is not None
    functions = set(
        re.findall(
            r"aap_(?:jobs|labeling)_v1\.[a-z_]+\([^)]*\)", grant.group(1).lower()
        )
    )
    assert functions == EXPECTED
    assert "result_role=preannotation_result_client" in script
    assert "exactly 14 executor capabilities" in script


def test_executor_has_no_raw_table_or_writer_capability():
    script = GRANTS.read_text(encoding="utf-8")
    assert 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"result_role";' in script
    assert 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"result_role";' in script
    assert (
        'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_jobs_v1, aap_labeling_v1 FROM :"result_role";'
        in script
    )
    assert "has_table_privilege" in script
    assert "has_database_privilege(role_name, current_database(), 'TEMP')" in script
    assert "raw table privileges" in script
    assert "no role memberships" in script
    for writer in (
        "lock_batch_result_write",
        "finish_batch_result_write",
        "finish_batch_result_failure",
        "lock_batch_result_execution",
    ):
        assert writer in script
        assert not re.search(
            rf"GRANT EXECUTE ON FUNCTION[^;]*aap_(?:jobs|labeling)_v1\.{writer}\(",
            script,
            re.IGNORECASE | re.DOTALL,
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the owned module-data LOGIN role qualification",
)
async def test_provisioned_executor_role_has_exact_functions_and_no_table_access(
    db_session,
):
    role = "preannotation_result_client"
    for signature in EXPECTED:
        assert await db_session.scalar(
            text(
                "SELECT pg_catalog.has_function_privilege(:role,:signature,'EXECUTE')"
            ),
            {"role": role, "signature": signature},
        ), signature

    callable_count = await db_session.scalar(
        text(
            "SELECT count(*) FROM pg_catalog.pg_proc AS proc "
            "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=proc.pronamespace "
            "WHERE namespace.nspname IN ('aap_jobs_v1','aap_labeling_v1') "
            "AND proc.prokind='f' AND pg_catalog.has_function_privilege(:role,proc.oid,'EXECUTE')"
        ),
        {"role": role},
    )
    assert callable_count == 14

    for signature in (
        "aap_jobs_v1.lock_batch_result_execution(jsonb)",
        "aap_labeling_v1.lock_batch_result_write(jsonb)",
        "aap_labeling_v1.finish_batch_result_write(jsonb)",
        "aap_labeling_v1.finish_batch_result_failure(jsonb)",
    ):
        assert not await db_session.scalar(
            text(
                "SELECT pg_catalog.has_function_privilege(:role,:signature,'EXECUTE')"
            ),
            {"role": role, "signature": signature},
        ), signature

    direct_table_access = await db_session.scalar(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace "
            "WHERE namespace.nspname='public' AND relation.relkind IN ('r','p','v','m','f') "
            "AND (pg_catalog.has_table_privilege(:role,relation.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
            "OR pg_catalog.has_any_column_privilege(:role,relation.oid,'SELECT,INSERT,UPDATE,REFERENCES'))"
            ")"
        ),
        {"role": role},
    )
    assert direct_table_access is False
