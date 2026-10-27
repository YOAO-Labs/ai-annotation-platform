"""Retain general comment attachment facts independently of business records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0227"
down_revision = "0226"
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
    AND storage_ref->>'purpose' = 'annotations'
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
    AND storage_ref->>'object_key' = 'comment-attachments/' || annotation_id::text || '/' || id::text
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""
_FILE_NAME_CHECK = """
length(file_name) BETWEEN 1 AND 255
AND file_name NOT IN ('.','..')
AND position('/' in file_name) = 0
AND position(chr(92) in file_name) = 0
AND file_name !~ '[[:cntrl:]]'
AND file_name !~ ('[' || chr(127) || '-' || chr(159) || ']')
"""
_MIME_CHECK = """
length(mime_type) BETWEEN 3 AND 128
AND mime_type ~ '^[a-z0-9!#$%&''*+.^_`|~-]+/[a-z0-9!#$%&''*+.^_`|~-]+$'
"""


def upgrade():
    op.create_table(
        "comment_attachment_uploads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
        sa.Column("annotation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("storage_ref", JSONB(none_as_null=True), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
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
        sa.Column("bound_comment_id", UUID(as_uuid=True)),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_comment_attachment_sha256"
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 0 AND 20971520", name="ck_comment_attachment_size"
        ),
        sa.CheckConstraint(
            _MIME_CHECK,
            name="ck_comment_attachment_mime_type",
        ),
        sa.CheckConstraint(_FILE_NAME_CHECK, name="ck_comment_attachment_file_name"),
        sa.CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_comment_attachment_state"
        ),
        sa.CheckConstraint(
            "expires_at = created_at + INTERVAL '2160 hours'",
            name="ck_comment_attachment_expiry",
        ),
        sa.CheckConstraint(
            "(state='retired' AND retired_at IS NOT NULL AND retired_at >= created_at) OR (state<>'retired' AND retired_at IS NULL)",
            name="ck_comment_attachment_retirement",
        ),
        sa.CheckConstraint(
            "state <> 'pending' OR bound_comment_id IS NULL",
            name="ck_comment_attachment_pending_unbound",
        ),
        sa.CheckConstraint(_REF_CHECK, name="ck_comment_attachment_storage_ref"),
    )
    op.create_index(
        "ix_comment_attachment_cleanup",
        "comment_attachment_uploads",
        ["state", "last_attempted_at", "id"],
    )
    op.create_index(
        "ix_comment_attachment_bound_comment",
        "comment_attachment_uploads",
        ["bound_comment_id"],
    )
    op.create_index(
        "ix_comment_attachment_annotation",
        "comment_attachment_uploads",
        ["annotation_id"],
    )
    op.execute("""
        CREATE FUNCTION guard_comment_attachment_facts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
               OR NEW.annotation_id IS DISTINCT FROM OLD.annotation_id
               OR NEW.task_id IS DISTINCT FROM OLD.task_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.mime_type IS DISTINCT FROM OLD.mime_type
               OR NEW.file_name IS DISTINCT FROM OLD.file_name
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
            THEN
                RAISE EXCEPTION 'Comment attachment identity, content and deadline are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_comment_attachment_facts_immutable';
            END IF;
            IF NOT (NEW.state = OLD.state
                OR (OLD.state='pending' AND NEW.state IN ('ready','retired'))
                OR (OLD.state='ready' AND NEW.state='retired'))
            THEN
                RAISE EXCEPTION 'Comment attachment state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_comment_attachment_state_forward';
            END IF;
            IF NEW.bound_comment_id IS DISTINCT FROM OLD.bound_comment_id
               AND NOT (OLD.bound_comment_id IS NULL AND NEW.bound_comment_id IS NOT NULL
                        AND OLD.state='ready' AND NEW.state='ready')
            THEN
                RAISE EXCEPTION 'Comment attachment binding requires first use of a ready upload'
                    USING ERRCODE='23514', CONSTRAINT='ck_comment_attachment_binding_immutable';
            END IF;
            IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at
            THEN
                RAISE EXCEPTION 'Comment attachment retirement is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_comment_attachment_retirement_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_comment_attachment_facts BEFORE UPDATE ON comment_attachment_uploads FOR EACH ROW EXECUTE FUNCTION guard_comment_attachment_facts()"
    )
    # Preserve the shared owner-role/transaction-marker whitelist unchanged.
    op.execute(
        "CREATE TRIGGER trg_comment_attachment_storage_ref BEFORE UPDATE ON comment_attachment_uploads FOR EACH ROW EXECUTE FUNCTION guard_training_dataset_storage_ref()"
    )


def downgrade():
    op.execute("LOCK TABLE comment_attachment_uploads IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM comment_attachment_uploads)")
    ):
        raise RuntimeError(
            "Comment attachment ownership facts prevent destructive downgrade"
        )
    op.execute(
        "DROP TRIGGER trg_comment_attachment_storage_ref ON comment_attachment_uploads"
    )
    op.execute(
        "DROP TRIGGER trg_comment_attachment_facts ON comment_attachment_uploads"
    )
    op.execute("DROP FUNCTION guard_comment_attachment_facts()")
    op.drop_index(
        "ix_comment_attachment_annotation", table_name="comment_attachment_uploads"
    )
    op.drop_index(
        "ix_comment_attachment_bound_comment", table_name="comment_attachment_uploads"
    )
    op.drop_index(
        "ix_comment_attachment_cleanup", table_name="comment_attachment_uploads"
    )
    op.drop_table("comment_attachment_uploads")
