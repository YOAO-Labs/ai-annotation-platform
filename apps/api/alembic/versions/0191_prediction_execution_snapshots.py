"""Persist immutable per-stage prediction execution snapshots.

Revision ID: 0191
Revises: 0190
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0191"
down_revision = "0190"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "prediction_execution_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_prediction_execution_snapshots_project_id",
        "prediction_execution_snapshots",
        ["project_id"],
    )
    for table in ("async_jobs", "failed_predictions"):
        op.add_column(
            table, sa.Column("execution_snapshot_id", postgresql.UUID(as_uuid=True))
        )
        op.create_foreign_key(
            f"fk_{table}_execution_snapshot",
            table,
            "prediction_execution_snapshots",
            ["execution_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            f"ix_{table}_execution_snapshot_id", table, ["execution_snapshot_id"]
        )
    op.execute("""
        CREATE FUNCTION guard_prediction_execution_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.payload IS DISTINCT FROM OLD.payload OR NEW.digest IS DISTINCT FROM OLD.digest
               OR NEW.schema_version IS DISTINCT FROM OLD.schema_version THEN
                RAISE EXCEPTION 'prediction execution snapshots are immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END; $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_prediction_execution_immutable BEFORE UPDATE ON prediction_execution_snapshots
        FOR EACH ROW EXECUTE FUNCTION guard_prediction_execution_snapshot();
    """)


def downgrade():
    op.execute(
        "DROP TRIGGER trg_prediction_execution_immutable ON prediction_execution_snapshots"
    )
    op.execute("DROP FUNCTION guard_prediction_execution_snapshot()")
    for table in ("async_jobs", "failed_predictions"):
        op.drop_index(f"ix_{table}_execution_snapshot_id", table_name=table)
        op.drop_constraint(f"fk_{table}_execution_snapshot", table, type_="foreignkey")
        op.drop_column(table, "execution_snapshot_id")
    op.drop_table("prediction_execution_snapshots")
