"""Retained model references for fresh training, recipes and policies.

Revision ID: 0185
Revises: 0184
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0185"
down_revision = "0184"
branch_labels = depends_on = None

REFERENCES = {
    "training_runs": "fk_training_run_initialization_model",
    "training_recipe_revisions": "fk_training_recipe_initialization_model",
    "learning_policies": "fk_learning_policy_initialization_model",
}


def index_name(table):
    return (
        f"ix_{table}_initialization_model_id"
        if table == "learning_policies"
        else f"ix_lineage_{table}_initialization_model_id"
    )


def upgrade():
    for table, name in REFERENCES.items():
        op.add_column(
            table, sa.Column("initialization_model_id", pg.UUID(as_uuid=True))
        )
        op.create_foreign_key(
            name,
            table,
            "trained_model_versions",
            ["initialization_model_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        columns = ["initialization_model_id"] + (
            [] if table == "learning_policies" else ["created_at", "id"]
        )
        op.create_index(index_name(table), table, columns)


def downgrade():
    for table, name in reversed(REFERENCES.items()):
        op.drop_index(index_name(table), table_name=table)
        op.drop_constraint(name, table, type_="foreignkey")
        op.drop_column(table, "initialization_model_id")
