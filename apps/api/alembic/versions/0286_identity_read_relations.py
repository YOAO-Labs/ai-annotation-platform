"""Add Identity-owned profile and invitation target views."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0286"
down_revision = "bo0285"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0286_identity_read_relations.sql"
)


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    for view in (
        "aap_identity_v1.invitation_audit_targets",
        "aap_identity_v1.account_profiles",
    ):
        _execute(f"DROP VIEW {view} RESTRICT")
