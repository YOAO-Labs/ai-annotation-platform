"""Add Assets-owned legacy upload completion and replay.

Revision ID: bo0213
Revises: bo0212
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0213"
down_revision = "bo0212"
branch_labels = depends_on = None


def upgrade() -> None:
    artifact = (
        Path(__file__).resolve().parents[1] / "sql/0213_assets_source_runtime.sql"
    )
    for statement in artifact.read_text().split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_assets_v1.complete_legacy_dataset_upload(uuid, uuid, uuid, uuid, text, bigint, text, integer, integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.lock_legacy_dataset_upload(uuid, uuid, uuid, uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_assets_v1._legacy_upload_reply(jsonb, text) RESTRICT")
    op.drop_table("dataset_legacy_upload_operations", schema="public")
