"""Install Labeling's bounded video selection projections.

Revision ID: 0224
Revises: 0223
"""

from __future__ import annotations

from pathlib import Path
import re

from alembic import op
import sqlalchemy as sa

revision = "0224"
down_revision = "0223"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UPGRADE_ARTIFACTS = ("0224_video_input_selection.sql",)
_SCHEMA_ARTIFACTS = ("0224_video_input_selection.schema.json",)


def _statements() -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / _UPGRADE_ARTIFACTS[0])
        .read_text()
        .split("-- aap-statement-break")
        if statement.strip()
    ]


def _signatures() -> list[str]:
    result = []
    for statement in _statements():
        match = re.match(
            r"(?is)^REVOKE\s+ALL\s+ON\s+FUNCTION\s+(.+?)\s+FROM\s+PUBLIC\s*;\s*$",
            statement,
        )
        if match:
            result.append(" ".join(match.group(1).split()))
    return result


def upgrade() -> None:
    for statement in _statements():
        op.execute(sa.text(statement.replace(":", r"\:")))


def downgrade() -> None:
    for signature in reversed(_signatures()):
        op.execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
