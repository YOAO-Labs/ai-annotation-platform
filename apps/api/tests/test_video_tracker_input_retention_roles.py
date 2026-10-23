"""The accepted tracker input retention fact remains inside Jobs ownership."""

import os

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data role verifier",
    ),
]


async def _function_allowed(db, role: str, signature: str) -> bool:
    return bool(
        await db.scalar(
            text(
                "SELECT pg_catalog.has_function_privilege(:role,:signature,'EXECUTE')"
            ),
            {"role": role, "signature": signature},
        )
    )


async def test_jobs_input_retention_fact_has_only_labeling_owner_as_caller(db_session):
    signature = "aap_jobs_v1.video_tracker_input_retention_facts(uuid,uuid)"
    owner = await db_session.scalar(
        text(
            "SELECT pg_catalog.pg_get_userbyid(proc.proowner) "
            "FROM pg_catalog.pg_proc AS proc "
            "WHERE proc.oid=CAST(:signature AS regprocedure)"
        ),
        {"signature": signature},
    )
    assert owner == "jobs_code"

    for role, expected in (
        ("jobs_code", True),
        ("labeling_code", True),
        ("assets_code", False),
        ("legacy_runtime", False),
        ("asset_cleanup_client", False),
        ("video_execution_client", False),
    ):
        assert await _function_allowed(db_session, role, signature) is expected, role

    public_execute = await db_session.scalar(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_proc AS proc, "
            "LATERAL pg_catalog.aclexplode(COALESCE("
            "proc.proacl,pg_catalog.acldefault('f',proc.proowner))) AS acl "
            "WHERE proc.oid=CAST(:signature AS regprocedure) "
            "AND acl.grantee=0 AND acl.privilege_type='EXECUTE')"
        ),
        {"signature": signature},
    )
    assert public_execute is False

    assets_fact = "aap_assets_v1.video_execution_input_facts(uuid)"
    assert await _function_allowed(db_session, "jobs_code", assets_fact)
    assert not await _function_allowed(db_session, "labeling_code", assets_fact)
