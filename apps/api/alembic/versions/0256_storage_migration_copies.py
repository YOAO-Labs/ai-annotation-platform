"""Retain frozen copy plans and physical evidence beyond task retention."""

from alembic import op
import sqlalchemy as sa

revision = "0256"
down_revision = "0255"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE storage_migration_copies (\n\tid UUID NOT NULL, \n\tinput_fingerprint VARCHAR(64) NOT NULL, \n\tsource_inventory_job_id UUID, \n\tsource_report BYTEA NOT NULL, \n\tsource_report_sha256 VARCHAR(64) NOT NULL, \n\tsource_complete BOOLEAN NOT NULL, \n\tsource JSONB NOT NULL, \n\ttarget_location_id UUID NOT NULL, \n\ttarget JSONB NOT NULL, \n\thandling JSONB NOT NULL, \n\tlimits JSONB NOT NULL, \n\tcatalog BYTEA, \n\tcatalog_sha256 VARCHAR(64), \n\tbaseline BYTEA, \n\tbaseline_sha256 VARCHAR(64), \n\tsummary JSONB, \n\tlast_attempt_id UUID, \n\tdelta_sequence INTEGER DEFAULT '0' NOT NULL, \n\tdelta_bytes INTEGER DEFAULT '0' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_migration_copy_source_bytes CHECK (octet_length(source_report) BETWEEN 1 AND 33554432), \n\tCONSTRAINT ck_migration_copy_catalog_bytes CHECK (catalog IS NULL OR octet_length(catalog) BETWEEN 1 AND 67108864), \n\tCONSTRAINT ck_migration_copy_baseline_bytes CHECK (baseline IS NULL OR octet_length(baseline) BETWEEN 1 AND 268435456), \n\tCONSTRAINT ck_migration_copy_delta_budget CHECK (delta_bytes BETWEEN 0 AND 268435456 AND delta_sequence >= 0), \n\tUNIQUE (input_fingerprint), \n\tFOREIGN KEY(source_inventory_job_id) REFERENCES async_jobs (id) ON DELETE SET NULL, \n\tFOREIGN KEY(target_location_id) REFERENCES storage_locations (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE INDEX ix_migration_copy_history ON storage_migration_copies (created_at, id)"
    )
    op.execute(
        "CREATE INDEX ix_storage_migration_copies_target_location_id ON storage_migration_copies (target_location_id)"
    )
    op.execute(
        "CREATE TABLE storage_migration_copy_attempts (\n\tid UUID NOT NULL, \n\tcopy_id UUID NOT NULL, \n\tasync_job_id UUID, \n\ttask_id VARCHAR(64) NOT NULL, \n\trequested_by UUID NOT NULL, \n\tauthority JSONB NOT NULL, \n\tscope JSONB NOT NULL, \n\toperation VARCHAR(12) NOT NULL, \n\tretry_failed BOOLEAN NOT NULL, \n\tretry_object_ids JSONB, \n\tstatus VARCHAR(20) NOT NULL, \n\tsource_operation_id UUID, \n\ttarget_operation_id UUID, \n\tprocess JSONB, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tcancel_requested_at TIMESTAMP WITH TIME ZONE, \n\terror_code VARCHAR(80), \n\tresult JSONB, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_migration_copy_attempt_status CHECK (status IN ('pending','running','completed','failed','cancelled')), \n\tCONSTRAINT ck_migration_copy_attempt_operation CHECK (operation IN ('prepare','run','verify')), \n\tFOREIGN KEY(copy_id) REFERENCES storage_migration_copies (id) ON DELETE CASCADE, \n\tUNIQUE (async_job_id), \n\tFOREIGN KEY(async_job_id) REFERENCES async_jobs (id) ON DELETE SET NULL, \n\tUNIQUE (task_id), \n\tUNIQUE (source_operation_id), \n\tUNIQUE (target_operation_id)\n)"
    )
    op.execute(
        "CREATE INDEX ix_storage_migration_copy_attempts_copy_id ON storage_migration_copy_attempts (copy_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_migration_copy_active_attempt ON storage_migration_copy_attempts (copy_id) WHERE status IN ('pending','running')"
    )
    op.execute(
        "CREATE TABLE storage_migration_copy_deltas (\n\tcopy_id UUID NOT NULL, \n\tobject_id VARCHAR(64) NOT NULL, \n\tsequence INTEGER NOT NULL, \n\tdata JSONB NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\tsize_bytes INTEGER NOT NULL, \n\tPRIMARY KEY (copy_id, object_id), \n\tCONSTRAINT ck_migration_copy_delta_row CHECK (sequence >= 1 AND size_bytes BETWEEN 1 AND 262144), \n\tFOREIGN KEY(copy_id) REFERENCES storage_migration_copies (id) ON DELETE CASCADE\n)"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM storage_migration_copies)")
    ):
        raise RuntimeError(
            "Migration copy evidence must be retained; keep the schema for application rollback"
        )
    op.drop_table("storage_migration_copy_deltas")
    op.drop_table("storage_migration_copy_attempts")
    op.drop_table("storage_migration_copies")
