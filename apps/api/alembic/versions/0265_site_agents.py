"""Independent sites, one-time pairing and retained agent credential revisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0265"
down_revision = "0264"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sites",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("region", sa.String(100)),
        sa.Column(
            "created_by",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "agent_registrations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "site_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("sites.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "credential_revision", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("pair_digest", sa.String(64)),
        sa.Column("pair_expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("draining", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("session_id", pg.UUID(as_uuid=True)),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("roots", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("limits", pg.JSONB(), nullable=False),
        sa.Column("reported_limits", pg.JSONB()),
        sa.Column(
            "created_by",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "protocol_version = 1 AND credential_revision >= 1",
            name="ck_agent_registration_revision",
        ),
        sa.CheckConstraint(
            "(pair_digest IS NULL) = (pair_expires_at IS NULL) AND (pair_digest IS NULL OR pair_digest ~ '^[a-f0-9]{64}$')",
            name="ck_agent_registration_pair",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(roots) = 'array' AND jsonb_typeof(limits) = 'object' AND (reported_limits IS NULL OR jsonb_typeof(reported_limits) = 'object')",
            name="ck_agent_registration_json",
        ),
    )
    op.create_index(
        "ix_agent_registrations_site_created",
        "agent_registrations",
        ["site_id", "created_at", "id"],
    )
    op.create_table(
        "agent_credentials",
        sa.Column(
            "agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("agent_registrations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision >= 1 AND token_digest ~ '^[a-f0-9]{64}$'",
            name="ck_agent_credential_identity",
        ),
    )


def downgrade():
    op.execute(
        "LOCK TABLE agent_credentials, agent_registrations, sites IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM sites) OR EXISTS(SELECT 1 FROM agent_registrations) OR EXISTS(SELECT 1 FROM agent_credentials)"
        )
    ):
        raise RuntimeError(
            "Retain site identities and credential history on application rollback"
        )
    op.drop_table("agent_credentials")
    op.drop_index(
        "ix_agent_registrations_site_created", table_name="agent_registrations"
    )
    op.drop_table("agent_registrations")
    op.drop_table("sites")
