"""Add bucket-filtered keyset paging for the independent Assets cleanup worker.

Revision ID: bo0210
Revises: bo0209
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0210"
down_revision = "bo0209"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0210_asset_storage_cleanup.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def _artifact_statements() -> list[str]:
    artifact = Path(__file__).resolve().parents[1] / "sql" / _ARTIFACT_NAME
    return [
        statement.strip()
        for statement in artifact.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        )
        if statement.strip()
    ]


def upgrade() -> None:
    for statement in _artifact_statements():
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_assets_v1.pending_storage_deletions_v2(text, timestamptz, uuid, uuid, integer) RESTRICT"
    )
    op.execute(
        "DROP INDEX public.ix_asset_storage_deletions_pending_bucket_created RESTRICT"
    )
