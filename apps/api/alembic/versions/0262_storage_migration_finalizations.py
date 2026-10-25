"""Retain operator-bound final snapshots and their exact native fence epoch."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0262"
down_revision = "0261"
branch_labels = None
depends_on = None


def upgrade():
    columns = [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "window_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("storage_migration_windows.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("window_revision", sa.Integer(), nullable=False),
        sa.Column("source_location_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_gate_revision", sa.Integer(), nullable=False),
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
        sa.Column(
            "copy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("storage_migration_copies.id", ondelete="RESTRICT"),
        ),
        sa.Column("inventory_job_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
    ]
    columns.extend(
        sa.Column(name, postgresql.JSONB(), nullable=nullable)
        for name, nullable in (
            ("source", False),
            ("target", False),
            ("claim", False),
            ("native", False),
            ("token", True),
            ("evidence", True),
        )
    )
    columns.extend(
        sa.Column(
            name,
            sa.DateTime(timezone=True),
            nullable=nullable,
            server_default=sa.func.now() if not nullable else None,
        )
        for name, nullable in (
            ("created_at", False),
            ("updated_at", False),
            ("sealed_at", True),
            ("invalidated_at", True),
        )
    )
    op.create_table(
        "storage_migration_finalizations",
        *columns,
        sa.CheckConstraint(
            "state IN ('bound','sealing','sealed','invalidating','invalidated')",
            name="ck_migration_finalization_state",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND window_revision >= 1 AND source_gate_revision >= 1 AND source_config_revision >= 1",
            name="ck_migration_finalization_revision",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'",
            name="ck_migration_finalization_error",
        ),
        sa.CheckConstraint(
            "octet_length(claim::text) <= 4096 AND octet_length(native::text) <= 65536 AND (token IS NULL OR octet_length(token::text) <= 65536) AND (evidence IS NULL OR octet_length(evidence::text) <= 65536)",
            name="ck_migration_finalization_budget",
        ),
        sa.CheckConstraint(
            "state <> 'sealed' OR (copy_id IS NOT NULL AND token IS NOT NULL AND evidence IS NOT NULL AND sealed_at IS NOT NULL)",
            name="ck_migration_finalization_sealed",
        ),
        sa.CheckConstraint(
            "(state = 'invalidated') = (invalidated_at IS NOT NULL)",
            name="ck_migration_finalization_invalidated",
        ),
    )
    op.create_index(
        "uq_migration_finalization_active_window",
        "storage_migration_finalizations",
        ["window_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'invalidated'"),
    )
    op.create_index(
        "ix_migration_finalization_copy", "storage_migration_finalizations", ["copy_id"]
    )
    op.execute("""
        CREATE FUNCTION guard_storage_migration_finalization() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Finalization responsibility and history must be retained' USING ERRCODE='23514';
            END IF;
            IF ROW(NEW.id, NEW.window_id, NEW.window_revision, NEW.source_location_id, NEW.source, NEW.source_gate_revision,
                   NEW.source_config_revision, NEW.parent_copy_id, NEW.target_location_id, NEW.target, NEW.claim, NEW.created_by, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.window_id, OLD.window_revision, OLD.source_location_id, OLD.source, OLD.source_gate_revision,
                   OLD.source_config_revision, OLD.parent_copy_id, OLD.target_location_id, OLD.target, OLD.claim, OLD.created_by, OLD.created_at) THEN
                RAISE EXCEPTION 'Finalization identity is immutable' USING ERRCODE='23514';
            END IF;
            IF (OLD.copy_id IS NOT NULL AND NEW.copy_id IS DISTINCT FROM OLD.copy_id)
               OR (OLD.inventory_job_id IS NOT NULL AND NEW.inventory_job_id IS DISTINCT FROM OLD.inventory_job_id)
               OR (OLD.token IS NOT NULL AND NEW.token IS DISTINCT FROM OLD.token)
               OR (OLD.evidence IS NOT NULL AND NEW.evidence IS DISTINCT FROM OLD.evidence)
               OR (OLD.sealed_at IS NOT NULL AND NEW.sealed_at IS DISTINCT FROM OLD.sealed_at) THEN
                RAISE EXCEPTION 'Finalization evidence is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.state = 'invalidated' AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Invalidated finalization is historical' USING ERRCODE='23514';
            END IF;
            IF NEW.state <> OLD.state AND NOT (
                (OLD.state = 'bound' AND NEW.state IN ('sealing','invalidating')) OR
                (OLD.state = 'sealing' AND NEW.state IN ('sealed','invalidating')) OR
                (OLD.state = 'sealed' AND NEW.state = 'invalidating') OR
                (OLD.state = 'invalidating' AND NEW.state = 'invalidated')) THEN
                RAISE EXCEPTION 'Invalid finalization transition' USING ERRCODE='23514';
            END IF;
            IF NEW IS DISTINCT FROM OLD AND NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'Finalization revision must advance once' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER storage_migration_finalization_facts BEFORE UPDATE OR DELETE ON storage_migration_finalizations
        FOR EACH ROW EXECUTE FUNCTION guard_storage_migration_finalization();
    """)


def downgrade():
    op.execute("LOCK TABLE storage_migration_finalizations IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM storage_migration_finalizations)")
    ):
        raise RuntimeError(
            "Finalizations retain native responsibility; keep schema for application rollback"
        )
    op.execute(
        "DROP TRIGGER storage_migration_finalization_facts ON storage_migration_finalizations"
    )
    op.execute("DROP FUNCTION guard_storage_migration_finalization()")
    op.drop_index("ix_migration_finalization_copy", "storage_migration_finalizations")
    op.drop_index(
        "uq_migration_finalization_active_window", "storage_migration_finalizations"
    )
    op.drop_table("storage_migration_finalizations")
