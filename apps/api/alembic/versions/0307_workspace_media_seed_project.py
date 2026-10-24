"""Add fixed Workspace Project capabilities for media seed operators."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op


revision = "0307"
down_revision = "0306"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in (
        (_SQL / "0307_workspace_media_seed_project.sql")
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    for signature in (
        "write_seed_project_projection(uuid,jsonb)",
        "create_media_seed_project(uuid,text,text,uuid,text,jsonb)",
    ):
        op.execute(f"DROP FUNCTION aap_workspace_v1.{signature} RESTRICT")
