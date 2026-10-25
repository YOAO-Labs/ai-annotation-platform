"""Sparse source-fault state and monotonic observation ordering."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0249"
down_revision = "0248"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.schema.CreateSequence(sa.Sequence("storage_fault_observation_seq")))
    op.create_table(
        "storage_fault_states",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope_key", sa.String(64), nullable=False, unique=True),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("operation", sa.String(24), nullable=False),
        sa.Column("node_id", sa.String(255), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.BigInteger(), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("recovered_sequence", sa.BigInteger(), nullable=True),
        sa.Column("scope_facts", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "category IN ('root_unavailable','access_denied','source_changed','preview_failed')",
            name="ck_storage_fault_category",
        ),
        sa.CheckConstraint(
            "state IN ('active','recovered')", name="ck_storage_fault_state"
        ),
        sa.CheckConstraint(
            "failure_count > 0 AND last_sequence > 0 AND "
            "(recovered_sequence IS NULL OR (recovered_sequence > 0 AND recovered_sequence <= last_sequence))",
            name="ck_storage_fault_watermark",
        ),
        sa.CheckConstraint(
            "(recovered_at IS NULL) = (recovered_sequence IS NULL) AND "
            "(state <> 'recovered' OR recovered_at IS NOT NULL)",
            name="ck_storage_fault_recovery",
        ),
    )
    op.create_index(
        "ix_storage_fault_states_location_id", "storage_fault_states", ["location_id"]
    )
    op.create_index(
        "ix_storage_fault_states_source_id", "storage_fault_states", ["source_id"]
    )
    op.create_index(
        "ix_storage_fault_location_state",
        "storage_fault_states",
        ["location_id", "state", "last_failed_at"],
    )


def downgrade():
    op.execute("LOCK TABLE storage_fault_states IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM storage_fault_states)")
    ):
        raise RuntimeError(
            "Storage fault observations must be retained; downgrade requires an empty table"
        )
    op.drop_table("storage_fault_states")
    op.execute(sa.schema.DropSequence(sa.Sequence("storage_fault_observation_seq")))
