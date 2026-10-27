"""Preserve member-authority temporal decode failures in Native Project reads."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0262"
down_revision = "bo0261"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0262_workspace_member_authority_temporals.sql"
)
_ARGUMENTS = (
    "jsonb, text, jsonb, text, text, text[], text[], uuid, text, "
    "timestamptz, timestamptz, bigint, integer"
)
_DOWNGRADE_WRAPPER_FUNCTION = (
    f"aap_workspace_project_query_v1._project_query_step_wrapper_0262_tmp({_ARGUMENTS})"
)

_TRANSFER_ACL_TO_WRAPPER = r"""
DO $acl$
DECLARE
    v_impl_oid oid;
    v_wrapper_oid oid;
    v_owner_oid oid;
    v_owner_name text;
    v_wrapper_grantees oid[];
    v_impl_grantees oid[];
    v_grantee oid;
    v_grantee_sql text;
    v_privilege record;
BEGIN
    v_impl_oid := pg_catalog.to_regprocedure(
        'aap_workspace_project_query_v1._project_query_step_impl(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer)'
    )::oid;
    v_wrapper_oid := pg_catalog.to_regprocedure(
        'aap_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer)'
    )::oid;
    IF v_impl_oid IS NULL OR v_wrapper_oid IS NULL THEN
        RAISE EXCEPTION 'Workspace Project query implementation or wrapper is missing';
    END IF;

    SELECT function.proowner
    INTO v_owner_oid
    FROM pg_catalog.pg_proc AS function
    WHERE function.oid = v_impl_oid;
    v_owner_name := pg_catalog.pg_get_userbyid(v_owner_oid);
    EXECUTE pg_catalog.format(
        'ALTER FUNCTION aap_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer) OWNER TO %I',
        v_owner_name
    );

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT privilege.grantee),
        ARRAY[]::oid[]
    )
    INTO v_wrapper_grantees
    FROM pg_catalog.pg_proc AS function
    CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
        function.proacl,
        pg_catalog.acldefault('f', function.proowner)
    )) AS privilege
    WHERE function.oid = v_wrapper_oid
      AND privilege.privilege_type = 'EXECUTE'
      AND privilege.grantee <> v_owner_oid;
    FOREACH v_grantee IN ARRAY v_wrapper_grantees LOOP
        v_grantee_sql := CASE
            WHEN v_grantee = 0::oid THEN 'PUBLIC'
            ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(v_grantee))
        END;
        EXECUTE pg_catalog.format(
            'REVOKE ALL PRIVILEGES ON FUNCTION aap_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer) FROM %s CASCADE',
            v_grantee_sql
        );
    END LOOP;

    FOR v_privilege IN
        SELECT privilege.grantee,
               pg_catalog.bool_or(privilege.is_grantable) AS is_grantable
        FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
            function.proacl,
            pg_catalog.acldefault('f', function.proowner)
        )) AS privilege
        WHERE function.oid = v_impl_oid
          AND privilege.privilege_type = 'EXECUTE'
          AND privilege.grantee <> v_owner_oid
        GROUP BY privilege.grantee
    LOOP
        v_grantee_sql := CASE
            WHEN v_privilege.grantee = 0::oid THEN 'PUBLIC'
            ELSE pg_catalog.quote_ident(
                pg_catalog.pg_get_userbyid(v_privilege.grantee)
            )
        END;
        EXECUTE pg_catalog.format(
            'GRANT EXECUTE ON FUNCTION aap_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer) TO %s%s',
            v_grantee_sql,
            CASE WHEN v_privilege.is_grantable
                 THEN ' WITH GRANT OPTION' ELSE '' END
        );
    END LOOP;

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT privilege.grantee),
        ARRAY[]::oid[]
    )
    INTO v_impl_grantees
    FROM pg_catalog.pg_proc AS function
    CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
        function.proacl,
        pg_catalog.acldefault('f', function.proowner)
    )) AS privilege
    WHERE function.oid = v_impl_oid
      AND privilege.privilege_type = 'EXECUTE'
      AND privilege.grantee <> v_owner_oid;
    -- Preserve every captured grant before retiring dependent privilege chains.
    FOREACH v_grantee IN ARRAY v_impl_grantees LOOP
        v_grantee_sql := CASE
            WHEN v_grantee = 0::oid THEN 'PUBLIC'
            ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(v_grantee))
        END;
        EXECUTE pg_catalog.format(
            'REVOKE ALL PRIVILEGES ON FUNCTION aap_workspace_project_query_v1._project_query_step_impl(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer) FROM %s CASCADE',
            v_grantee_sql
        );
    END LOOP;
END;
$acl$;
"""

_RESTORE_ACL_AND_DROP_WRAPPER = r"""
DO $acl$
DECLARE
    v_impl_oid oid;
    v_wrapper_oid oid;
    v_owner_oid oid;
    v_wrapper_owner_oid oid;
    v_impl_grantees oid[];
    v_grantee oid;
    v_grantee_sql text;
    v_privilege record;
BEGIN
    v_impl_oid := pg_catalog.to_regprocedure(
        'aap_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer)'
    )::oid;
    v_wrapper_oid := pg_catalog.to_regprocedure(
        'aap_workspace_project_query_v1._project_query_step_wrapper_0262_tmp(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer)'
    )::oid;
    IF v_impl_oid IS NULL OR v_wrapper_oid IS NULL THEN
        RAISE EXCEPTION 'Workspace Project query downgrade functions are missing';
    END IF;

    SELECT function.proowner
    INTO v_owner_oid
    FROM pg_catalog.pg_proc AS function
    WHERE function.oid = v_impl_oid;
    SELECT function.proowner
    INTO v_wrapper_owner_oid
    FROM pg_catalog.pg_proc AS function
    WHERE function.oid = v_wrapper_oid;
    IF v_owner_oid IS DISTINCT FROM v_wrapper_owner_oid THEN
        RAISE EXCEPTION 'Workspace Project query function owners diverged';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT privilege.grantee),
        ARRAY[]::oid[]
    )
    INTO v_impl_grantees
    FROM pg_catalog.pg_proc AS function
    CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
        function.proacl,
        pg_catalog.acldefault('f', function.proowner)
    )) AS privilege
    WHERE function.oid = v_impl_oid
      AND privilege.privilege_type = 'EXECUTE'
      AND privilege.grantee <> v_owner_oid;
    FOREACH v_grantee IN ARRAY v_impl_grantees LOOP
        v_grantee_sql := CASE
            WHEN v_grantee = 0::oid THEN 'PUBLIC'
            ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(v_grantee))
        END;
        EXECUTE pg_catalog.format(
            'REVOKE ALL PRIVILEGES ON FUNCTION aap_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer) FROM %s CASCADE',
            v_grantee_sql
        );
    END LOOP;

    FOR v_privilege IN
        SELECT privilege.grantee,
               pg_catalog.bool_or(privilege.is_grantable) AS is_grantable
        FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
            function.proacl,
            pg_catalog.acldefault('f', function.proowner)
        )) AS privilege
        WHERE function.oid = v_wrapper_oid
          AND privilege.privilege_type = 'EXECUTE'
          AND privilege.grantee <> v_owner_oid
        GROUP BY privilege.grantee
    LOOP
        v_grantee_sql := CASE
            WHEN v_privilege.grantee = 0::oid THEN 'PUBLIC'
            ELSE pg_catalog.quote_ident(
                pg_catalog.pg_get_userbyid(v_privilege.grantee)
            )
        END;
        EXECUTE pg_catalog.format(
            'GRANT EXECUTE ON FUNCTION aap_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer) TO %s%s',
            v_grantee_sql,
            CASE WHEN v_privilege.is_grantable
                 THEN ' WITH GRANT OPTION' ELSE '' END
        );
    END LOOP;
END;
$acl$;
"""


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())
    _execute(_TRANSFER_ACL_TO_WRAPPER)


def downgrade() -> None:
    _execute(
        f"ALTER FUNCTION aap_workspace_project_query_v1.project_query_step({_ARGUMENTS}) "
        "RENAME TO _project_query_step_wrapper_0262_tmp"
    )
    _execute(
        "ALTER FUNCTION aap_workspace_project_query_v1._project_query_step_impl("
        f"{_ARGUMENTS}) RENAME TO project_query_step"
    )
    _execute(_RESTORE_ACL_AND_DROP_WRAPPER)
    _execute(f"DROP FUNCTION IF EXISTS {_DOWNGRADE_WRAPPER_FUNCTION} RESTRICT")
