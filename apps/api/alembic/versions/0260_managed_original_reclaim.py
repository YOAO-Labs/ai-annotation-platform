"""Preserve exact-version reclamation and reference reconciliation responsibility."""

from alembic import op
import sqlalchemy as sa

revision = "0260"
down_revision = "0259"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE managed_original_reclaim_attempts (
	id UUID NOT NULL,
	retirement_id UUID NOT NULL,
	creation_id UUID NOT NULL,
	async_job_id UUID,
	retry_of_id UUID,
	requested_by UUID NOT NULL,
	task_id VARCHAR(255) NOT NULL,
	authority JSONB NOT NULL,
	target JSONB NOT NULL,
	input_fingerprint VARCHAR(64) NOT NULL,
	version_id TEXT NOT NULL,
	expected_sha256 VARCHAR(64) NOT NULL,
	expected_size_bytes BIGINT NOT NULL,
	maintenance_revision INTEGER NOT NULL,
	reason VARCHAR(1000) NOT NULL,
	status VARCHAR(20) DEFAULT 'pending' NOT NULL,
	phase VARCHAR(24) DEFAULT 'claimed' NOT NULL,
	outcome VARCHAR(24),
	error_code VARCHAR(80),
	operation_id UUID,
	physical_settled BOOLEAN DEFAULT true NOT NULL,
	read_verified JSONB,
	delete_ack JSONB,
	observation JSONB DEFAULT '{}' NOT NULL,
	requested_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	delete_intent_at TIMESTAMP WITH TIME ZONE,
	absence_verified_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	cancel_requested_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_reclaim_status CHECK (status IN ('pending','running','completed','failed','cancelled','unknown')),
	CONSTRAINT ck_reclaim_phase CHECK (phase IN ('claimed','reading','delete_intent','delete_ack','absence_verified','settled')),
	CONSTRAINT ck_reclaim_outcome CHECK (outcome IS NULL OR outcome IN ('deleted','already_absent','missing','changed','failed','unknown','cancelled')),
	CONSTRAINT ck_reclaim_identity CHECK (expected_size_bytes >= 0 AND expected_sha256 ~ '^[0-9a-f]{64}$' AND input_fingerprint ~ '^[0-9a-f]{64}$' AND maintenance_revision >= 1),
	CONSTRAINT ck_reclaim_version CHECK (length(version_id)>0 AND version_id<>'null'),
	CONSTRAINT ck_reclaim_error CHECK (error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'),
	CONSTRAINT ck_reclaim_terminal CHECK ((status IN ('pending','running') AND completed_at IS NULL AND outcome IS NULL) OR (status NOT IN ('pending','running') AND completed_at IS NOT NULL AND outcome IS NOT NULL)),
	CONSTRAINT ck_reclaim_ack_intent CHECK (delete_ack IS NULL OR delete_intent_at IS NOT NULL),
	CONSTRAINT ck_reclaim_absence CHECK (absence_verified_at IS NULL OR delete_intent_at IS NOT NULL OR retry_of_id IS NOT NULL),
	CONSTRAINT ck_reclaim_success CHECK (status<>'completed' OR (outcome IN ('deleted','already_absent') AND absence_verified_at IS NOT NULL AND physical_settled)),
	FOREIGN KEY(retirement_id) REFERENCES managed_original_retirements (id) ON DELETE RESTRICT,
	FOREIGN KEY(creation_id) REFERENCES managed_original_creations (id) ON DELETE RESTRICT,
	UNIQUE (async_job_id),
	FOREIGN KEY(async_job_id) REFERENCES async_jobs (id) ON DELETE SET NULL,
	FOREIGN KEY(retry_of_id) REFERENCES managed_original_reclaim_attempts (id) ON DELETE RESTRICT,
	UNIQUE (task_id),
	UNIQUE (operation_id)
)
    """)
    op.execute(
        "CREATE INDEX ix_reclaim_history ON managed_original_reclaim_attempts (retirement_id, requested_at, id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_reclaim_active_creation ON managed_original_reclaim_attempts (creation_id) WHERE status IN ('pending','running')"
    )
    op.execute("""
CREATE TABLE managed_original_reclaim_claims (
	id UUID NOT NULL,
	bucket VARCHAR(255) NOT NULL,
	object_key TEXT NOT NULL,
	storage_identity VARCHAR(64) NOT NULL,
	creation_id UUID NOT NULL,
	version_id TEXT NOT NULL,
	current_attempt_id UUID NOT NULL,
	state VARCHAR(20) DEFAULT 'claimed' NOT NULL,
	revision INTEGER DEFAULT '1' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_reclaim_physical_group UNIQUE (bucket, object_key),
	CONSTRAINT ck_reclaim_claim_state CHECK (state IN ('released','claimed','tombstone','deleted') AND revision>=1),
	CONSTRAINT ck_reclaim_claim_identity CHECK (length(version_id)>0 AND version_id<>'null' AND storage_identity ~ '^[0-9a-f]{64}$'),
	FOREIGN KEY(creation_id) REFERENCES managed_original_creations (id) ON DELETE RESTRICT,
	FOREIGN KEY(current_attempt_id) REFERENCES managed_original_reclaim_attempts (id) ON DELETE RESTRICT
)
    """)
    op.execute("""
CREATE TABLE managed_original_reference_reconciliation (
	id INTEGER NOT NULL,
	status VARCHAR(20) DEFAULT 'idle' NOT NULL,
	async_job_id UUID,
	task_id VARCHAR(255),
	measured_at TIMESTAMP WITH TIME ZONE,
	last_success_at TIMESTAMP WITH TIME ZONE,
	counts JSONB DEFAULT '{}' NOT NULL,
	unknown JSONB DEFAULT '{}' NOT NULL,
	deltas JSONB DEFAULT '[]' NOT NULL,
	error_code VARCHAR(80),
	PRIMARY KEY (id),
	CONSTRAINT ck_reference_reconciliation_singleton CHECK (id=1 AND status IN ('idle','pending','running','completed','failed')),
	FOREIGN KEY(async_job_id) REFERENCES async_jobs (id) ON DELETE SET NULL
)
    """)

    op.execute("""
    CREATE FUNCTION guard_managed_original_reclaim() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Reclaim history cannot be deleted' USING ERRCODE='23514';
      END IF;
      IF NEW.async_job_id IS DISTINCT FROM OLD.async_job_id AND NEW.async_job_id IS NOT NULL THEN
        RAISE EXCEPTION 'Reclaim job identity is immutable' USING ERRCODE='23514';
      END IF;
      IF ROW(NEW.id,NEW.retirement_id,NEW.creation_id,NEW.retry_of_id,NEW.requested_by,NEW.task_id,
             NEW.authority,NEW.target,NEW.input_fingerprint,NEW.version_id,NEW.expected_sha256,
             NEW.expected_size_bytes,NEW.maintenance_revision,NEW.reason,NEW.requested_at)
         IS DISTINCT FROM
         ROW(OLD.id,OLD.retirement_id,OLD.creation_id,OLD.retry_of_id,OLD.requested_by,OLD.task_id,
             OLD.authority,OLD.target,OLD.input_fingerprint,OLD.version_id,OLD.expected_sha256,
             OLD.expected_size_bytes,OLD.maintenance_revision,OLD.reason,OLD.requested_at) THEN
        RAISE EXCEPTION 'Reclaim target identity is immutable' USING ERRCODE='23514';
      END IF;
      IF (OLD.operation_id IS NOT NULL AND NEW.operation_id IS DISTINCT FROM OLD.operation_id)
         OR (OLD.read_verified IS NOT NULL AND NEW.read_verified IS DISTINCT FROM OLD.read_verified)
         OR (OLD.delete_intent_at IS NOT NULL AND NEW.delete_intent_at IS DISTINCT FROM OLD.delete_intent_at)
         OR (OLD.delete_ack IS NOT NULL AND NEW.delete_ack IS DISTINCT FROM OLD.delete_ack)
         OR (OLD.absence_verified_at IS NOT NULL AND NEW.absence_verified_at IS DISTINCT FROM OLD.absence_verified_at) THEN
        RAISE EXCEPTION 'Reclaim physical facts are immutable' USING ERRCODE='23514';
      END IF;
      IF OLD.status NOT IN ('pending','running') AND
         (to_jsonb(NEW)-'async_job_id') IS DISTINCT FROM (to_jsonb(OLD)-'async_job_id') THEN
        RAISE EXCEPTION 'Reclaim terminal facts are immutable' USING ERRCODE='23514';
      END IF;
      IF array_position(ARRAY['claimed','reading','delete_intent','delete_ack','absence_verified','settled'],NEW.phase)
         < array_position(ARRAY['claimed','reading','delete_intent','delete_ack','absence_verified','settled'],OLD.phase) THEN
        RAISE EXCEPTION 'Reclaim phase cannot regress' USING ERRCODE='23514';
      END IF;
      IF OLD.status='running' AND NEW.status='pending' THEN
        RAISE EXCEPTION 'Reclaim attempt cannot restart' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER managed_original_reclaim_immutable BEFORE UPDATE OR DELETE ON managed_original_reclaim_attempts
      FOR EACH ROW EXECUTE FUNCTION guard_managed_original_reclaim();
    """)
    op.execute("""
    CREATE FUNCTION guard_managed_original_reclaim_claim() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Reclaim fence cannot be deleted' USING ERRCODE='23514';
      END IF;
      IF NEW.revision < OLD.revision OR
         (ROW(NEW.state,NEW.current_attempt_id) IS DISTINCT FROM ROW(OLD.state,OLD.current_attempt_id) AND NEW.revision<>OLD.revision+1) THEN
        RAISE EXCEPTION 'Reclaim fence revision is monotonic' USING ERRCODE='23514';
      END IF;
      IF ROW(NEW.id,NEW.bucket,NEW.object_key,NEW.storage_identity,NEW.creation_id,NEW.version_id,NEW.created_at)
         IS DISTINCT FROM ROW(OLD.id,OLD.bucket,OLD.object_key,OLD.storage_identity,OLD.creation_id,OLD.version_id,OLD.created_at)
         OR (OLD.state='deleted' AND NEW.state<>'deleted')
         OR (OLD.state='tombstone' AND NEW.state NOT IN ('tombstone','deleted')) THEN
        RAISE EXCEPTION 'Permanent reclaim identity cannot be released' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER managed_original_reclaim_claim_immutable BEFORE UPDATE OR DELETE ON managed_original_reclaim_claims
      FOR EACH ROW EXECUTE FUNCTION guard_managed_original_reclaim_claim();
    """)


def downgrade():
    for table in (
        "managed_original_reclaim_claims",
        "managed_original_reclaim_attempts",
        "managed_original_reference_reconciliation",
    ):
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})")):
            raise RuntimeError(
                "Reclamation facts are retained; keep schema for application rollback"
            )
    op.execute("DROP FUNCTION guard_managed_original_reclaim_claim() CASCADE")
    op.execute("DROP FUNCTION guard_managed_original_reclaim() CASCADE")
    op.drop_table("managed_original_reclaim_claims")
    op.drop_table("managed_original_reclaim_attempts")
    op.drop_table("managed_original_reference_reconciliation")
