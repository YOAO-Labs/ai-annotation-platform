"""Retain atomic nine-purpose cutovers and read-only rollback target claims."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0264"
down_revision = "0263"
branch_labels = None
depends_on = None
TABLE = "storage_migration_cutovers"


def upgrade():
    columns = [
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "storage_migration_read_publications.id", ondelete="RESTRICT"
            ),
            primary_key=True,
        ),
        sa.Column(
            "window_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("storage_migration_windows.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("source_location_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "target_location_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("storage_locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("review", postgresql.JSONB(), nullable=False),
        sa.Column("review_sha256", sa.String(64), nullable=False),
        sa.Column("before_bindings", postgresql.JSONB(), nullable=False),
        sa.Column("after_bindings", postgresql.JSONB(), nullable=False),
        sa.Column("target_gate_revision", sa.Integer(), nullable=False),
        sa.Column("rollback_claim", postgresql.JSONB()),
        sa.Column("rollback_native", postgresql.JSONB()),
        sa.Column("source_release_operation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True)),
    ]
    op.create_table(
        TABLE,
        *columns,
        sa.CheckConstraint(
            "state IN ('active','rollback_draining','rollback_bound','rollback_restoring','rolled_back')",
            name="ck_migration_cutover_state",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND target_gate_revision >= 1",
            name="ck_migration_cutover_revision",
        ),
        sa.CheckConstraint(
            "review_sha256 ~ '^[a-f0-9]{64}$' AND octet_length(review::text) <= 131072 AND octet_length(before_bindings::text) <= 16384 AND octet_length(after_bindings::text) <= 16384 AND (rollback_claim IS NULL OR octet_length(rollback_claim::text) <= 4096) AND (rollback_native IS NULL OR octet_length(rollback_native::text) <= 65536)",
            name="ck_migration_cutover_budget",
        ),
        sa.CheckConstraint(
            "(rollback_claim IS NULL) = (rollback_native IS NULL) AND (state IN ('rollback_bound','rollback_restoring','rolled_back')) = (rollback_claim IS NOT NULL AND rollback_native IS NOT NULL) AND (state IN ('rollback_restoring','rolled_back')) = (source_release_operation_id IS NOT NULL) AND (state = 'rolled_back') = (rolled_back_at IS NOT NULL)",
            name="ck_migration_cutover_phase_facts",
        ),
    )
    op.create_index(
        "ix_migration_cutover_target", TABLE, ["target_location_id", "state"]
    )
    op.execute("""
    CREATE FUNCTION guard_storage_migration_cutover() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE purpose text; before_fact jsonb; after_fact jsonb;
        purposes text[] := ARRAY['annotations','datasets','bug-reports','media-cache','audit-archive','import','export','avatars','training'];
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Cutover responsibility and history must be retained' USING ERRCODE='23514';
        END IF;
        IF NOT COALESCE(jsonb_typeof(NEW.before_bindings)='object' AND jsonb_typeof(NEW.after_bindings)='object'
            AND NEW.before_bindings ?& purposes AND NEW.after_bindings ?& purposes
            AND NEW.before_bindings - purposes='{}'::jsonb AND NEW.after_bindings - purposes='{}'::jsonb,false) THEN
            RAISE EXCEPTION 'Cutover requires exactly nine purposes' USING ERRCODE='23514';
        END IF;
        FOREACH purpose IN ARRAY purposes LOOP
            before_fact := NEW.before_bindings->purpose; after_fact := NEW.after_bindings->purpose;
            IF NOT COALESCE(jsonb_typeof(before_fact)='object' AND jsonb_typeof(after_fact)='object'
                AND before_fact ?& ARRAY['location_id','bucket','generation'] AND after_fact ?& ARRAY['location_id','bucket','generation']
                AND before_fact - ARRAY['location_id','bucket','generation']='{}'::jsonb
                AND after_fact - ARRAY['location_id','bucket','generation']='{}'::jsonb
                AND before_fact->>'location_id'=NEW.source_location_id::text AND after_fact->>'location_id'=NEW.target_location_id::text
                AND jsonb_typeof(before_fact->'bucket')='string' AND length(before_fact->>'bucket') BETWEEN 1 AND 255
                AND jsonb_typeof(after_fact->'bucket')='string' AND length(after_fact->>'bucket') BETWEEN 1 AND 255
                AND jsonb_typeof(before_fact->'generation')='number' AND before_fact->>'generation' ~ '^[1-9][0-9]{0,9}$'
                AND jsonb_typeof(after_fact->'generation')='number' AND after_fact->>'generation' ~ '^[1-9][0-9]{0,9}$',false) THEN
                RAISE EXCEPTION 'Invalid cutover binding facts' USING ERRCODE='23514';
            END IF;
            IF (before_fact->>'generation')::bigint > 2147483645
                OR (after_fact->>'generation')::bigint <> (before_fact->>'generation')::bigint+1 THEN
                RAISE EXCEPTION 'Invalid cutover binding generations' USING ERRCODE='23514';
            END IF;
        END LOOP;
        IF TG_OP = 'INSERT' THEN
            IF NEW.state <> 'active' OR NEW.revision <> 1 THEN
                RAISE EXCEPTION 'Cutover starts active atomically' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END IF;
        IF ROW(NEW.id,NEW.window_id,NEW.source_location_id,NEW.target_location_id,NEW.review,NEW.review_sha256,
            NEW.before_bindings,NEW.after_bindings,NEW.created_by,NEW.created_at)
            IS DISTINCT FROM ROW(OLD.id,OLD.window_id,OLD.source_location_id,OLD.target_location_id,OLD.review,OLD.review_sha256,
            OLD.before_bindings,OLD.after_bindings,OLD.created_by,OLD.created_at) THEN
            RAISE EXCEPTION 'Cutover identity and vectors are immutable' USING ERRCODE='23514';
        END IF;
        IF (OLD.rollback_claim IS NOT NULL AND NEW.rollback_claim IS DISTINCT FROM OLD.rollback_claim)
            OR (OLD.rollback_native IS NOT NULL AND NEW.rollback_native IS DISTINCT FROM OLD.rollback_native)
            OR (OLD.source_release_operation_id IS NOT NULL AND NEW.source_release_operation_id IS DISTINCT FROM OLD.source_release_operation_id)
            OR (OLD.rolled_back_at IS NOT NULL AND NEW.rolled_back_at IS DISTINCT FROM OLD.rolled_back_at) THEN
            RAISE EXCEPTION 'Cutover rollback evidence is immutable' USING ERRCODE='23514';
        END IF;
        IF NEW IS DISTINCT FROM OLD AND (NEW.revision <> OLD.revision+1 OR NOT (
            (OLD.state='active' AND NEW.state='rollback_draining') OR
            (OLD.state='rollback_draining' AND NEW.state='rollback_bound') OR
            (OLD.state='rollback_bound' AND NEW.state='rollback_restoring') OR
            (OLD.state='rollback_restoring' AND NEW.state='rolled_back'))) THEN
            RAISE EXCEPTION 'Invalid cutover transition or revision' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER storage_migration_cutover_facts BEFORE INSERT OR UPDATE OR DELETE ON storage_migration_cutovers
        FOR EACH ROW EXECUTE FUNCTION guard_storage_migration_cutover();
    """)


def downgrade():
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {TABLE})")):
        raise RuntimeError(
            "Cutovers retain native responsibility; keep schema for application rollback"
        )
    op.execute(f"DROP TRIGGER storage_migration_cutover_facts ON {TABLE}")
    op.execute("DROP FUNCTION guard_storage_migration_cutover()")
    op.drop_table(TABLE)
