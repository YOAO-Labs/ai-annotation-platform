"""Retain registered managed-original locators after business deletion."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0216"
down_revision = "0215"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "managed_original_retirements",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("assertion_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("dataset_id", UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_item_id", UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", UUID(as_uuid=True), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(1000), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("location_config_revision", sa.Integer()),
        sa.Column("binding_generation", sa.Integer()),
        sa.Column("storage_identity", sa.String(64)),
        sa.Column("identity_state", sa.String(24), nullable=False),
        sa.Column("identity_error_code", sa.String(64)),
        sa.Column("asset_verification_state", sa.String(20), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("reference_state", sa.String(20), nullable=False),
        sa.Column("etag", sa.Text()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("dataset_lifecycle_revision", sa.Integer(), nullable=False),
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("policy_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retain_until", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "role = 'managed_original'", name="ck_managed_retirement_role"
        ),
        sa.CheckConstraint(
            "identity_state IN ('verified_binding', 'unknown')",
            name="ck_managed_retirement_identity_state",
        ),
        sa.CheckConstraint(
            "(identity_state = 'verified_binding' AND storage_identity IS NOT NULL "
            "AND identity_error_code IS NULL) OR "
            "(identity_state = 'unknown' AND storage_identity IS NULL "
            "AND identity_error_code IS NOT NULL)",
            name="ck_managed_retirement_identity_evidence",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="ck_managed_retirement_size"
        ),
        sa.CheckConstraint(
            "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_managed_retirement_sha256",
        ),
        sa.CheckConstraint(
            "policy_revision >= 1 AND dataset_lifecycle_revision >= 1",
            name="ck_managed_retirement_revisions",
        ),
    )
    op.create_index(
        "ix_managed_retirement_location",
        "managed_original_retirements",
        ["location_id", "retired_at", "id"],
    )
    op.create_index(
        "ix_managed_retirement_asset", "managed_original_retirements", ["asset_id"]
    )


def downgrade():
    op.execute("LOCK TABLE managed_original_retirements IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM managed_original_retirements)")
    ):
        raise RuntimeError(
            "Managed original retirement assertions prevent destructive downgrade"
        )
    op.drop_table("managed_original_retirements")
