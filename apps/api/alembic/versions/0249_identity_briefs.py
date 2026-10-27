"""Move batched user-brief reads behind the Identity owner boundary."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0249"
down_revision = "bo0248"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0249_identity_briefs_support.sql",
    "0249_identity_briefs.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_FUNCTIONS = (
    "aap_identity_briefs_v1.execute_briefs_command(jsonb)",
    "aap_identity_briefs_v1._has_exact_keys(jsonb,text[])",
    "aap_identity_briefs_v1._failure()",
    "aap_identity_briefs_v1.briefs_policy()",
    "aap_workspace_v1.identity_brief_project_roles(uuid,uuid[])",
)


def _statements(artifact: str) -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / artifact)
        .read_text(encoding="utf-8")
        .split(_STATEMENT_SEPARATOR)
        if statement.strip()
    ]


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for artifact in _ARTIFACTS:
        for statement in _statements(artifact):
            _execute(statement)


def downgrade() -> None:
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    _execute("DROP SCHEMA aap_identity_briefs_v1 RESTRICT")
