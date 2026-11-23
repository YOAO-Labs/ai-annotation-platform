"""The normal application LOGIN may call contracts, not access their backing tables."""

import json
import os

import asyncpg
import pytest

from tests.test_workspace_project_authority_locking import _api_url


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable verifier and the real application LOGIN",
    ),
]

_EFFECTIVE_ACCESS = r"""
WITH relations AS (
    SELECT c.oid, n.nspname, c.relname, c.relkind
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE (n.nspname = 'public' OR n.nspname LIKE 'yoao\_%')
      AND c.relkind IN ('r', 'p', 'm', 'S')
      AND NOT EXISTS (
        SELECT FROM pg_catalog.pg_depend d
        WHERE d.classid = 'pg_catalog.pg_class'::regclass
          AND d.objid = c.oid AND d.deptype = 'e')
), rights AS (
    SELECT nspname, relname, relkind, ARRAY(
        SELECT perm FROM unnest(CASE WHEN relkind = 'S'
            THEN ARRAY['SELECT','UPDATE','USAGE']
            ELSE ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']
        END) perm
        WHERE CASE WHEN relkind = 'S'
            THEN pg_catalog.has_sequence_privilege(oid, perm)
            ELSE pg_catalog.has_table_privilege(oid, perm) END
    ) AS relation_rights, ARRAY(
        SELECT a.attname || ':' || perm
        FROM pg_catalog.pg_attribute a
        CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) perm
        WHERE relkind <> 'S' AND a.attrelid = oid AND a.attnum > 0 AND NOT a.attisdropped
          AND NOT pg_catalog.has_table_privilege(oid, perm)
          AND pg_catalog.has_column_privilege(oid, a.attnum, perm)
        ORDER BY a.attnum, perm
    ) AS column_only_rights FROM relations
)
SELECT nspname || '.' || relname AS relation, relkind::text AS kind,
       relation_rights, column_only_rights FROM rights
WHERE cardinality(relation_rights) + cardinality(column_only_rights) > 0
ORDER BY nspname, relname
"""


async def test_default_login_cannot_directly_use_private_relations(
    test_engine, record_property
):
    url = _api_url(test_engine).set(drivername="postgresql")
    connection = await asyncpg.connect(url.render_as_string(hide_password=False))
    try:
        assert await connection.fetchval("SELECT current_user") == url.username
        access = [dict(row) for row in await connection.fetch(_EFFECTIVE_ACCESS)]
    finally:
        await connection.close()
    record_property("ordinary_login", url.username)
    record_property(
        "effective_private_access", json.dumps(access, separators=(",", ":"))
    )
    assert not access, (
        f"{url.username} has effective rights on {len(access)} private relations; "
        "see effective_private_access in the JUnit report"
    )
