"""Add users.avatar_ref for uploaded and built-in pixel avatars.

Revision ID: 0172
Revises: 0171
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision = "0172"
down_revision: str | Sequence[str] | None = "0171"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL keeps the historical initials-only rendering, so no backfill is needed.
    op.add_column("users", sa.Column("avatar_ref", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_ref")
