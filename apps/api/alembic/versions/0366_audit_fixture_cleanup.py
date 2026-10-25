"""Add fixed test-database cleanup for filtering fixture Audit events."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0366"
down_revision = "0365"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UP_SQL = _SQL_DIR / "0366_audit_fixture_cleanup.sql"
_PREVIOUS_SQL = _SQL_DIR / "0333_audit_maintenance.sql"


def _replace_function(sql: str) -> None:
    statement = sql.strip().replace(":", r"\:")
    op.execute(sa.text(statement))


def upgrade() -> None:
    # CREATE OR REPLACE preserves the established Audit owner and function ACL.
    _replace_function(_UP_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Restore the immutable 0333 body without changing owner or ACL metadata.
    statement = _PREVIOUS_SQL.read_text(encoding="utf-8").split(
        "-- aap-statement-break", 1
    )[0]
    _replace_function(
        statement.replace(
            "CREATE FUNCTION aap_audit_v1.maintenance_command(p_request jsonb)",
            "CREATE OR REPLACE FUNCTION aap_audit_v1.maintenance_command(p_request jsonb)",
            1,
        )
    )
