"""Permanent independent ownership for authenticated audit archives."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0226"
down_revision = "0225"
branch_labels = None
depends_on = None

# Freeze the checks rather than importing changing application models.
_REF_CHECK = """
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'audit-archive'
    AND storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
    AND storage_ref->'config_revision' = '1'::jsonb AND storage_ref->>'config_revision' = '1'
    AND storage_ref->'binding_generation' = '1'::jsonb AND storage_ref->>'binding_generation' = '1'
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND storage_ref->>'object_key' = year::text || '/' || lpad(month::text,2,'0') || '/' || id::text || '.jsonl.gz'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""

_MONTH_CHECK = """
CASE WHEN year BETWEEN 2000 AND 9999 AND month BETWEEN 1 AND 12
    AND NOT (year=9999 AND month=12)
THEN source_schema='public'
    AND source_table='audit_logs_y' || year::text || 'm' || lpad(month::text,2,'0')
    AND month_start=make_date(year,month,1)
    AND month_end=(month_start + INTERVAL '1 month')::date
ELSE false END
"""


def upgrade():
    op.create_table(
        "audit_archive_outputs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column(
            "source_schema", sa.String(63), nullable=False, server_default="public"
        ),
        sa.Column("source_table", sa.String(63), nullable=False),
        sa.Column("parent_oid", sa.BigInteger(), nullable=False),
        sa.Column("source_oid", sa.BigInteger(), nullable=False),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("month_end", sa.Date(), nullable=False),
        sa.Column("format_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("source_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_ref", JSONB(none_as_null=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(_MONTH_CHECK, name="ck_audit_archive_month"),
        sa.CheckConstraint(
            "parent_oid BETWEEN 1 AND 4294967295 AND source_oid BETWEEN 1 AND 4294967295",
            name="ck_audit_archive_oids",
        ),
        sa.CheckConstraint("format_revision=1", name="ck_audit_archive_format"),
        sa.CheckConstraint(
            "row_count BETWEEN 0 AND 10000000", name="ck_audit_archive_rows"
        ),
        sa.CheckConstraint(
            "source_size_bytes BETWEEN 0 AND 274877906944",
            name="ck_audit_archive_source_size",
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 68719476736", name="ck_audit_archive_size"
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_audit_archive_sha256",
        ),
        sa.CheckConstraint(
            "state IN ('pending','ready')", name="ck_audit_archive_state"
        ),
        sa.CheckConstraint(
            "(state='pending' AND published_at IS NULL) OR (state='ready' AND published_at IS NOT NULL AND published_at>=created_at)",
            name="ck_audit_archive_publication",
        ),
        sa.CheckConstraint(_REF_CHECK, name="ck_audit_archive_storage_ref"),
    )
    op.create_index(
        "uq_audit_archive_ready_month",
        "audit_archive_outputs",
        ["year", "month"],
        unique=True,
        postgresql_where=sa.text("state='ready'"),
    )
    op.create_index("ix_audit_archive_job", "audit_archive_outputs", ["job_id"])
    op.execute("""
        CREATE FUNCTION guard_audit_archive_facts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' THEN
                RAISE EXCEPTION 'Audit archive ownership is permanent'
                    USING ERRCODE='23514', CONSTRAINT='ck_audit_archive_permanent';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.job_id IS DISTINCT FROM OLD.job_id
               OR NEW.year IS DISTINCT FROM OLD.year
               OR NEW.month IS DISTINCT FROM OLD.month
               OR NEW.source_schema IS DISTINCT FROM OLD.source_schema
               OR NEW.source_table IS DISTINCT FROM OLD.source_table
               OR NEW.parent_oid IS DISTINCT FROM OLD.parent_oid
               OR NEW.source_oid IS DISTINCT FROM OLD.source_oid
               OR NEW.month_start IS DISTINCT FROM OLD.month_start
               OR NEW.month_end IS DISTINCT FROM OLD.month_end
               OR NEW.format_revision IS DISTINCT FROM OLD.format_revision
               OR NEW.row_count IS DISTINCT FROM OLD.row_count
               OR NEW.source_size_bytes IS DISTINCT FROM OLD.source_size_bytes
               OR NEW.source_sha256 IS DISTINCT FROM OLD.source_sha256
               OR NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'Audit archive identity and content are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_audit_archive_facts_immutable';
            END IF;
            IF NOT (NEW.state = OLD.state OR (OLD.state='pending' AND NEW.state='ready')) THEN
                RAISE EXCEPTION 'Audit archive state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_audit_archive_state_forward';
            END IF;
            IF OLD.published_at IS NOT NULL AND NEW.published_at IS DISTINCT FROM OLD.published_at THEN
                RAISE EXCEPTION 'Audit archive publication is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_audit_archive_publication_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_audit_archive_facts BEFORE UPDATE OR DELETE ON audit_archive_outputs FOR EACH ROW EXECUTE FUNCTION guard_audit_archive_facts()"
    )
    # The existing owner-role and transaction-marker whitelist stays unchanged.
    op.execute(
        "CREATE TRIGGER trg_audit_archive_storage_ref BEFORE UPDATE ON audit_archive_outputs FOR EACH ROW EXECUTE FUNCTION guard_training_dataset_storage_ref()"
    )


def downgrade():
    op.execute("LOCK TABLE audit_archive_outputs IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM audit_archive_outputs)")
    ):
        raise RuntimeError(
            "Audit archive ownership facts prevent destructive downgrade"
        )
    op.execute("DROP TRIGGER trg_audit_archive_storage_ref ON audit_archive_outputs")
    op.execute("DROP TRIGGER trg_audit_archive_facts ON audit_archive_outputs")
    op.execute("DROP FUNCTION guard_audit_archive_facts()")
    op.drop_index("ix_audit_archive_job", table_name="audit_archive_outputs")
    op.drop_index("uq_audit_archive_ready_month", table_name="audit_archive_outputs")
    op.drop_table("audit_archive_outputs")
