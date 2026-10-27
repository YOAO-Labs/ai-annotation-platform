"""Add minimal same-transaction operational account provisioning."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0252"
down_revision = "bo0251"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0252_identity_provisioning_support.sql",
    "0252_identity_provisioning.sql",
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def _statements() -> list[str]:
    return [
        statement.strip()
        for artifact in _ARTIFACTS
        for statement in (_SQL_DIR / artifact)
        .read_text(encoding="utf-8")
        .split(_STATEMENT_SEPARATOR)
        if statement.strip()
    ]


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _statements():
        _execute(statement)


def downgrade() -> None:
    for signature in (
        "aap_identity_v1.create_provisioning_account(jsonb)",
        "aap_identity_v1.read_provisioning_account(text)",
        "aap_audit_v1.record_identity_bootstrap_admin(uuid,text,text)",
    ):
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
