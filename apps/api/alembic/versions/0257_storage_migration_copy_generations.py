"""Retain linear incremental-copy ancestry and frozen parent evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0257"
down_revision = "0256"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "storage_migration_copies",
        sa.Column("parent_copy_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "storage_migration_copies",
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "storage_migration_copies",
        sa.Column("parent_evidence_sha256", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_migration_copy_parent",
        "storage_migration_copies",
        "storage_migration_copies",
        ["parent_copy_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_migration_copy_parent", "storage_migration_copies", ["parent_copy_id"]
    )
    op.create_check_constraint(
        "ck_migration_copy_generation",
        "storage_migration_copies",
        "(parent_copy_id IS NULL AND generation = 1 AND parent_evidence_sha256 IS NULL) "
        "OR (parent_copy_id IS NOT NULL AND parent_copy_id <> id AND generation > 1 "
        "AND parent_evidence_sha256 IS NOT NULL "
        "AND parent_evidence_sha256 ~ '^[0-9a-f]{64}$')",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM storage_migration_copies "
            "WHERE parent_copy_id IS NOT NULL OR generation <> 1 "
            "OR parent_evidence_sha256 IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Migration generations must retain their ancestry; keep schema for application rollback"
        )
    op.drop_constraint("ck_migration_copy_generation", "storage_migration_copies")
    op.drop_constraint("uq_migration_copy_parent", "storage_migration_copies")
    op.drop_constraint("fk_migration_copy_parent", "storage_migration_copies")
    op.drop_column("storage_migration_copies", "parent_evidence_sha256")
    op.drop_column("storage_migration_copies", "generation")
    op.drop_column("storage_migration_copies", "parent_copy_id")
