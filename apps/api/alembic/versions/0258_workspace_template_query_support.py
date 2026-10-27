"""Add the typed Identity fact used by Workspace template queries."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0258"
down_revision = "bo0257"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0258_workspace_template_query_support.sql"
)
_FUNCTIONS = ("aap_identity_v1.workspace_template_query_org_ids(uuid)",)


def _execute(statement):
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade():
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade():
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
