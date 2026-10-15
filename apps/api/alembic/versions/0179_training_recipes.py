"""Immutable project training recipes and independent run provenance.

Revision ID: 0179
Revises: 0178
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0179"
down_revision = "0178"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "training_recipes",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("head_revision", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_training_recipes_project_id", "training_recipes", ["project_id"]
    )
    op.create_table(
        "training_recipe_revisions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "recipe_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_recipes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "creator_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("origin", pg.JSONB()),
        sa.Column("request_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("recipe_id", "ordinal", name="uq_training_recipe_ordinal"),
        sa.UniqueConstraint(
            "project_id", "request_id", name="uq_training_recipe_request"
        ),
    )
    op.add_column("training_runs", sa.Column("origin", pg.JSONB()))


def downgrade():
    op.drop_column("training_runs", "origin")
    op.drop_table("training_recipe_revisions")
    op.drop_table("training_recipes")
