"""Publish Learning-owned project deletion retention capabilities.

Revision ID: 0196
Revises: 0195
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0196"
down_revision = "0195"
branch_labels = depends_on = None


def upgrade() -> None:
    artifact = (
        Path(__file__).resolve().parents[1] / "sql/0196_project_deletion_retention.sql"
    )
    op.execute(sa.text(artifact.read_text(encoding="utf-8")))


def downgrade() -> None:
    op.execute(sa.text("DROP SCHEMA IF EXISTS aap_learning_v1 CASCADE"))
