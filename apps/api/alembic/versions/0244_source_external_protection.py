"""Preserve external-original responsibility independently of content revisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0244"
down_revision = "0243"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dataset_sources",
        sa.Column(
            "external_protection_revision",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "dataset_sources", sa.Column("external_protection", JSONB(), nullable=True)
    )
    op.create_check_constraint(
        "ck_dataset_source_external_protection",
        "dataset_sources",
        "(external_protection_revision = 0 AND external_protection IS NULL) OR "
        "(external_protection_revision > 0 AND "
        "COALESCE(jsonb_typeof(external_protection) = 'object', false))",
    )


def downgrade():
    op.execute("LOCK TABLE dataset_sources IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM dataset_sources "
            "WHERE external_protection_revision <> 0 OR external_protection IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "External-original responsibility prevents destructive downgrade"
        )
    op.drop_constraint(
        "ck_dataset_source_external_protection", "dataset_sources", type_="check"
    )
    op.drop_column("dataset_sources", "external_protection")
    op.drop_column("dataset_sources", "external_protection_revision")
