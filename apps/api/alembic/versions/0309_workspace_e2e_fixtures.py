"""Own fixed native capabilities for guarded Workspace E2E fixtures."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0309"
down_revision = "bo0308"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0309_workspace_e2e_fixtures.sql"
)
_FUNCTIONS = (
    "create_project(jsonb)",
    "create_templates(jsonb)",
    "guard_project_cleanup(uuid[])",
    "delete_projects(uuid[])",
    "delete_templates(boolean,text[])",
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    for signature in _FUNCTIONS:
        op.execute(f"DROP FUNCTION aap_workspace_fixture_v1.{signature} RESTRICT")
    op.execute("DROP VIEW aap_workspace_fixture_v1.templates RESTRICT")
    op.execute("DROP SCHEMA aap_workspace_fixture_v1 RESTRICT")
