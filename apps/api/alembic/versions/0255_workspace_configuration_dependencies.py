"""Add narrow domain-owned dependencies for Workspace configuration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0255"
down_revision = "bo0254"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0255_workspace_configuration_dependencies.sql"
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_v1.workspace_configuration_org_scope(uuid,uuid)",
    "aap_assets_v1.workspace_configuration_datasets(uuid)",
    "aap_labeling_v1.workspace_configuration_video_facts(uuid)",
    "aap_inference_v1.enable_workspace_project_backend(uuid,uuid)",
)


def _statements() -> list[str]:
    return [
        statement.strip()
        for statement in _SQL_PATH.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        )
        if statement.strip()
    ]


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _statements():
        _execute(statement)


def downgrade() -> None:
    for signature in reversed(_FUNCTIONS):
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
