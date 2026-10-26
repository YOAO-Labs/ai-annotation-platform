"""Retain bounded outbound Agent command and physical close receipts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0266"
down_revision = "0265"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "site_agent_commands",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column(
            "location_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("storage_locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("operation_id", pg.UUID(as_uuid=True)),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("lease_session_id", pg.UUID(as_uuid=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_bytes", sa.Integer(), nullable=False),
        sa.Column("relay_released", sa.Boolean(), nullable=False),
        sa.Column("chunks", pg.JSONB(), nullable=False),
        sa.Column("result", pg.JSONB()),
        sa.Column("result_digest", sa.String(64)),
        sa.Column("closed", sa.Boolean()),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id", "credential_revision"],
            ["agent_credentials.agent_id", "agent_credentials.revision"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('queued','leased','received','applied','rejected','unsettled')",
            name="ck_agent_command_state",
        ),
        sa.CheckConstraint(
            "credential_revision >= 1 AND received_bytes BETWEEN 0 AND 67108864 AND priority BETWEEN 0 AND 2",
            name="ck_agent_command_limits",
        ),
        sa.CheckConstraint(
            "command_digest ~ '^[a-f0-9]{64}$' AND (result_digest IS NULL OR result_digest ~ '^[a-f0-9]{64}$')",
            name="ck_agent_command_digest",
        ),
        sa.CheckConstraint(
            "octet_length(payload::text) <= 16384 AND octet_length(chunks::text) <= 262144 AND (result IS NULL OR octet_length(result::text) <= 2097152)",
            name="ck_agent_command_payload",
        ),
        sa.CheckConstraint(
            "(result IS NULL) = (result_digest IS NULL) AND (result IS NULL) = (closed IS NULL)",
            name="ck_agent_command_result",
        ),
    )
    op.create_index(
        "ix_agent_commands_queue",
        "site_agent_commands",
        ["agent_id", "state", "priority", "created_at"],
    )
    op.create_index(
        "ix_agent_commands_location", "site_agent_commands", ["location_id", "state"]
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM site_agent_commands)")
    ).scalar():
        raise RuntimeError("Retained Agent receipts require preserving this schema")
    op.drop_table("site_agent_commands")
