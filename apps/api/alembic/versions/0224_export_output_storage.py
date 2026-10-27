"""Retain each export candidate independently of cache and job retention."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0224"
down_revision = "0223"
branch_labels = None
depends_on = None

# Freeze this check rather than importing changing application models.
_REF_CHECK = """
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'export'
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
    AND storage_ref->>'object_key' = media || '/' || project_id::text || '/'
        || job_id::text || '/' || id::text || '.zip'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""


def upgrade():
    op.create_table(
        "export_outputs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", UUID(as_uuid=True), nullable=False),
        sa.Column("media", sa.String(5), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("media IN ('image','video')", name="ck_export_output_media"),
        sa.CheckConstraint(
            "length(format) BETWEEN 1 AND 1000 AND format !~ '[[:cntrl:]]'",
            name="ck_export_output_format",
        ),
        sa.CheckConstraint("file_count >= 0", name="ck_export_output_file_count"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_export_output_sha256"),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 68719476736", name="ck_export_output_size"
        ),
        sa.CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_export_output_state"
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '168 hours'",
            name="ck_export_output_expiry",
        ),
        sa.CheckConstraint(
            "(state='retired' AND retired_at IS NOT NULL) OR (state<>'retired' AND retired_at IS NULL)",
            name="ck_export_output_retirement",
        ),
        sa.CheckConstraint(_REF_CHECK, name="ck_export_output_storage_ref"),
    )
    op.create_index(
        "ix_export_output_cleanup",
        "export_outputs",
        ["state", "last_attempted_at", "id"],
    )
    op.create_index("ix_export_output_job", "export_outputs", ["job_id"])
    op.add_column("export_artifacts", sa.Column("output_id", UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_export_artifacts_output_id",
        "export_artifacts",
        "export_outputs",
        ["output_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_export_artifacts_output_id", "export_artifacts", ["output_id"])
    op.execute("""
        CREATE FUNCTION guard_export_output_facts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.job_id IS DISTINCT FROM OLD.job_id
               OR NEW.media IS DISTINCT FROM OLD.media
               OR NEW.format IS DISTINCT FROM OLD.format
               OR NEW.file_count IS DISTINCT FROM OLD.file_count
               OR NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
            THEN
                RAISE EXCEPTION 'Export output identity, content and deadline are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_output_facts_immutable';
            END IF;
            IF NOT (NEW.state = OLD.state
                OR (OLD.state='pending' AND NEW.state IN ('ready','retired'))
                OR (OLD.state='ready' AND NEW.state='retired'))
            THEN
                RAISE EXCEPTION 'Export output state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_output_state_forward';
            END IF;
            IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at
            THEN
                RAISE EXCEPTION 'Export output retirement is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_output_retirement_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_export_output_facts BEFORE UPDATE ON export_outputs FOR EACH ROW EXECUTE FUNCTION guard_export_output_facts()"
    )
    op.execute(
        "CREATE TRIGGER trg_export_output_storage_ref BEFORE UPDATE ON export_outputs FOR EACH ROW EXECUTE FUNCTION guard_training_dataset_storage_ref()"
    )


def downgrade():
    op.execute("LOCK TABLE export_outputs, export_artifacts IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM export_outputs) OR EXISTS (SELECT 1 FROM export_artifacts WHERE output_id IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Export output ownership facts prevent destructive downgrade"
        )
    op.drop_index("ix_export_artifacts_output_id", table_name="export_artifacts")
    op.drop_constraint(
        "fk_export_artifacts_output_id", "export_artifacts", type_="foreignkey"
    )
    op.drop_column("export_artifacts", "output_id")
    op.execute("DROP TRIGGER trg_export_output_storage_ref ON export_outputs")
    op.execute("DROP TRIGGER trg_export_output_facts ON export_outputs")
    op.execute("DROP FUNCTION guard_export_output_facts()")
    op.drop_index("ix_export_output_job", table_name="export_outputs")
    op.drop_index("ix_export_output_cleanup", table_name="export_outputs")
    op.drop_table("export_outputs")
