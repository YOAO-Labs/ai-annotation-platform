"""Persist registered-access migration windows and exact task bindings."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0258"
down_revision = "0257"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "storage_migration_windows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_location_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("storage_maintenance_gates.location_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source", postgresql.JSONB(), nullable=False),
        sa.Column("source_config_revision", sa.Integer(), nullable=False),
        sa.Column(
            "parent_copy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("storage_migration_copies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_location_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("storage_locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="draining"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_gate_revision", sa.Integer(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True)),
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
        sa.CheckConstraint(
            "state IN ('draining','stopped','closing','closed')",
            name="ck_migration_window_state",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND source_gate_revision >= 1 AND source_config_revision >= 1",
            name="ck_migration_window_revisions",
        ),
    )
    op.create_index(
        "uq_migration_window_active_source",
        "storage_migration_windows",
        ["source_location_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'closed'"),
    )
    op.create_index(
        "ix_migration_window_history", "storage_migration_windows", ["created_at", "id"]
    )
    for table, label in (
        ("storage_migration_inventories", "inventory"),
        ("storage_migration_copies", "copy"),
    ):
        op.add_column(table, sa.Column("window_id", postgresql.UUID(as_uuid=True)))
        op.add_column(table, sa.Column("window_revision", sa.Integer()))
        op.create_foreign_key(
            f"fk_migration_{label}_window",
            table,
            "storage_migration_windows",
            ["window_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_check_constraint(
            f"ck_migration_{label}_window",
            table,
            "(window_id IS NULL AND window_revision IS NULL) OR (window_id IS NOT NULL AND window_revision IS NOT NULL AND window_revision >= 1)",
        )
    op.execute("""
        CREATE FUNCTION guard_storage_migration_window() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.source_location_id, NEW.source, NEW.source_config_revision, NEW.parent_copy_id,
                   NEW.target_location_id, NEW.target, NEW.created_by, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.source_location_id, OLD.source, OLD.source_config_revision, OLD.parent_copy_id,
                   OLD.target_location_id, OLD.target, OLD.created_by, OLD.created_at) THEN
                RAISE EXCEPTION 'Saved migration window identity is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER storage_migration_window_identity BEFORE UPDATE ON storage_migration_windows
        FOR EACH ROW EXECUTE FUNCTION guard_storage_migration_window();
    """)
    op.execute("""
        CREATE FUNCTION guard_storage_migration_window_binding() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.window_id, NEW.window_revision) IS DISTINCT FROM ROW(OLD.window_id, OLD.window_revision) THEN
                RAISE EXCEPTION 'Saved migration window binding is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    for table in ("storage_migration_inventories", "storage_migration_copies"):
        op.execute(
            f"CREATE TRIGGER storage_migration_window_binding BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_storage_migration_window_binding()"
        )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM storage_migration_windows) OR EXISTS(SELECT 1 FROM storage_migration_inventories WHERE window_id IS NOT NULL OR window_revision IS NOT NULL) OR EXISTS(SELECT 1 FROM storage_migration_copies WHERE window_id IS NOT NULL OR window_revision IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Migration windows retain access responsibility; keep schema for application rollback"
        )
    for table, label in (
        ("storage_migration_copies", "copy"),
        ("storage_migration_inventories", "inventory"),
    ):
        op.execute(f"DROP TRIGGER storage_migration_window_binding ON {table}")
        op.drop_constraint(f"ck_migration_{label}_window", table)
        op.drop_constraint(f"fk_migration_{label}_window", table)
        op.drop_column(table, "window_revision")
        op.drop_column(table, "window_id")
    op.execute(
        "DROP TRIGGER storage_migration_window_identity ON storage_migration_windows"
    )
    op.execute("DROP FUNCTION guard_storage_migration_window()")
    op.execute("DROP FUNCTION guard_storage_migration_window_binding()")
    op.drop_index("ix_migration_window_history", "storage_migration_windows")
    op.drop_index("uq_migration_window_active_source", "storage_migration_windows")
    op.drop_table("storage_migration_windows")
