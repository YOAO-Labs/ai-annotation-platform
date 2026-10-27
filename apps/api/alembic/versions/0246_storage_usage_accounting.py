"""Preserve physical facts independently from current-object inventory observations."""

from alembic import op
import sqlalchemy as sa

revision = "0246"
down_revision = "0245"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "storage_usage_snapshots",
        sa.Column("object_generation", sa.UUID(), nullable=True),
    )
    op.execute("""CREATE TABLE storage_usage_mutations (
	id UUID NOT NULL,
	idempotency_key VARCHAR(64) NOT NULL,
	scope_key VARCHAR(64) NOT NULL,
	object_key TEXT NOT NULL,
	key_hash VARCHAR(64) NOT NULL,
	producer VARCHAR(32) NOT NULL,
	purpose VARCHAR(32) NOT NULL,
	owner_id UUID NOT NULL,
	kind VARCHAR(12) NOT NULL,
	expected_version TEXT,
	state VARCHAR(12) DEFAULT 'pending' NOT NULL,
	physical_outcome VARCHAR(16) DEFAULT 'unknown' NOT NULL,
	physical_size_bytes BIGINT,
	current_delta_bytes BIGINT,
	evidence JSONB,
	started_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_usage_mutation_kind CHECK (kind IN ('create','delete')),
	CONSTRAINT ck_usage_mutation_state CHECK (state IN ('pending','confirmed','unknown')),
	CONSTRAINT ck_usage_mutation_physical CHECK (physical_outcome IN ('unknown','written','deleted','not_written')),
	CONSTRAINT ck_usage_mutation_size CHECK (physical_size_bytes IS NULL OR physical_size_bytes >= 0),
	CONSTRAINT ck_usage_mutation_delta CHECK (current_delta_bytes IS NULL OR (state='confirmed' AND finished_at IS NOT NULL)),
	UNIQUE (idempotency_key)
)""")
    op.execute(
        "CREATE INDEX ix_storage_usage_mutations_scope_key ON storage_usage_mutations (scope_key)"
    )
    op.execute(
        "CREATE INDEX ix_usage_mutation_address_window ON storage_usage_mutations (scope_key, key_hash, started_at)"
    )
    op.execute("""CREATE TABLE storage_usage_inventory_objects (
	generation UUID NOT NULL,
	scope_key VARCHAR(64) NOT NULL,
	key_hash VARCHAR(64) NOT NULL,
	object_key TEXT NOT NULL,
	size_bytes BIGINT NOT NULL,
	observed_from TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_through TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (generation, scope_key, key_hash),
	CONSTRAINT ck_usage_inventory_object_size CHECK (size_bytes >= 0),
	CONSTRAINT ck_usage_inventory_object_window CHECK (observed_through >= observed_from)
)""")
    op.execute("""CREATE TABLE storage_usage_reconciliations (
	generation UUID NOT NULL,
	scope_key VARCHAR(64) NOT NULL,
	previous_generation UUID,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	measured_at TIMESTAMP WITH TIME ZONE NOT NULL,
	summary JSONB NOT NULL,
	PRIMARY KEY (generation, scope_key)
)""")
    op.execute("""CREATE TABLE storage_usage_schedules (
	id VARCHAR(20) NOT NULL,
	enabled BOOLEAN DEFAULT 'false' NOT NULL,
	interval_hours INTEGER DEFAULT '24' NOT NULL,
	status VARCHAR(12) DEFAULT 'off' NOT NULL,
	authority JSONB NOT NULL,
	scope JSONB NOT NULL,
	next_due_at TIMESTAMP WITH TIME ZONE,
	last_started_at TIMESTAMP WITH TIME ZONE,
	pause_reason VARCHAR(80),
	PRIMARY KEY (id),
	CONSTRAINT ck_usage_schedule_interval CHECK (interval_hours BETWEEN 1 AND 168),
	CONSTRAINT ck_usage_schedule_status CHECK (status IN ('off','active','paused'))
)""")


def downgrade():
    connection = op.get_bind()
    # Writers must not insert a durable intent between the emptiness check and
    # DROP. A blocked begin then fails before it can issue physical I/O.
    connection.execute(
        sa.text(
            "LOCK TABLE storage_usage_mutations, storage_usage_inventory_objects, storage_usage_reconciliations, storage_usage_schedules, storage_usage_snapshots IN ACCESS EXCLUSIVE MODE"
        )
    )
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM storage_usage_mutations LIMIT 1)")
    ):
        raise RuntimeError(
            "Storage accounting facts must be retained; keep schema when rolling back the application"
        )
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM storage_usage_inventory_objects LIMIT 1)")
    ):
        raise RuntimeError(
            "Storage accounting facts must be retained; keep schema when rolling back the application"
        )
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM storage_usage_reconciliations LIMIT 1)")
    ):
        raise RuntimeError(
            "Storage accounting facts must be retained; keep schema when rolling back the application"
        )
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM storage_usage_schedules LIMIT 1)")
    ):
        raise RuntimeError(
            "Storage accounting facts must be retained; keep schema when rolling back the application"
        )
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM storage_usage_snapshots WHERE object_generation IS NOT NULL LIMIT 1)"
        )
    ):
        raise RuntimeError("Storage accounting baselines must be retained")
    op.drop_table("storage_usage_schedules")
    op.drop_table("storage_usage_reconciliations")
    op.drop_table("storage_usage_inventory_objects")
    op.drop_table("storage_usage_mutations")
    op.drop_column("storage_usage_snapshots", "object_generation")
