"""Add stateless Native Project guide-asset actions."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0269"
down_revision = "bo0268"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0269_workspace_guide_assets.sql"
)
_CONFIGURATION_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0268_workspace_class_rename.sql"
)
_PROJECT_QUERY_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0265_workspace_manager_queries.sql"
)
_CONFIGURATION_STEP = (
    "CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1.configuration_step("
)
_PROJECT_QUERY_STEP = (
    "CREATE OR REPLACE FUNCTION aap_workspace_project_query_v1.project_query_step("
)
_GUIDE_PROJECT_ROW = (
    "aap_workspace_configuration_v1._guide_asset_read_project_row(uuid)"
)
_GUIDE_MEMBER_ROWS = (
    "aap_workspace_configuration_v1._guide_asset_read_member_rows(uuid,uuid)"
)
_CONFIGURATION_STEP_SIGNATURE = (
    "aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])"
)


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def _function_statement(path: Path, prefix: str) -> str:
    source = path.read_text(encoding="utf-8")
    if source.count(prefix) != 1:
        raise RuntimeError(f"Expected one frozen function definition: {prefix}")
    start = source.index(prefix)
    terminator = "$function$;"
    end = source.index(terminator, start)
    return source[start : end + len(terminator)].strip()


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(_function_statement(_CONFIGURATION_SQL_PATH, _CONFIGURATION_STEP))
    _execute(_function_statement(_PROJECT_QUERY_SQL_PATH, _PROJECT_QUERY_STEP))
    _execute(f"DROP FUNCTION {_GUIDE_PROJECT_ROW} RESTRICT")
    _execute(f"DROP FUNCTION {_GUIDE_MEMBER_ROWS} RESTRICT")

    owner = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT pg_catalog.pg_get_userbyid(function.proowner) "
                "FROM pg_catalog.pg_proc AS function "
                "WHERE function.oid = pg_catalog.to_regprocedure(:signature)"
            ),
            {"signature": _CONFIGURATION_STEP_SIGNATURE},
        )
        .scalar_one_or_none()
    )
    if owner is not None:
        quoted_owner = op.get_bind().dialect.identifier_preparer.quote(owner)
        _execute(
            f"REVOKE UPDATE (guide_assets) ON TABLE public.projects FROM {quoted_owner}"
        )
