"""Retain guide image ownership, current references and project list revisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0228"
down_revision = "0227"
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
    AND storage_ref->>'object_key' = 'projects/' || project_id::text || '/guide/' || id::text
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
mime_type IN ('image/png','image/jpeg','image/webp','image/gif','image/svg+xml')
"""


def upgrade():
    op.create_table(
        "guide_asset_uploads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
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
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_guide_asset_sha256"),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 5242880", name="ck_guide_asset_size"
        ),
        sa.CheckConstraint(
            _MIME_CHECK,
            name="ck_guide_asset_mime_type",
        ),
        sa.CheckConstraint(_FILE_NAME_CHECK, name="ck_guide_asset_file_name"),
        sa.CheckConstraint(
            "state IN ('pending','ready','retired')", name="ck_guide_asset_state"
        ),
        sa.CheckConstraint(
            "(state='retired' AND retired_at IS NOT NULL AND retired_at >= created_at) OR (state<>'retired' AND retired_at IS NULL)",
            name="ck_guide_asset_retirement",
        ),
        sa.CheckConstraint(_REF_CHECK, name="ck_guide_asset_storage_ref"),
    )
    op.create_index(
        "ix_guide_asset_cleanup",
        "guide_asset_uploads",
        ["state", "last_attempted_at", "id"],
    )
    op.create_index(
        "ix_guide_asset_original_project", "guide_asset_uploads", ["project_id"]
    )
    op.create_table(
        "project_guide_assets",
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "upload_id",
            UUID(as_uuid=True),
            sa.ForeignKey("guide_asset_uploads.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_project_guide_asset_upload", "project_guide_assets", ["upload_id"]
    )
    op.add_column(
        "projects",
        sa.Column(
            "guide_assets_revision", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.create_check_constraint(
        "ck_project_guide_assets_revision", "projects", "guide_assets_revision > 0"
    )
    op.execute("""
        CREATE FUNCTION guard_guide_asset_facts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.mime_type IS DISTINCT FROM OLD.mime_type
               OR NEW.file_name IS DISTINCT FROM OLD.file_name
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'Guide image identity and content are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_guide_asset_facts_immutable';
            END IF;
            IF NOT (NEW.state = OLD.state
                OR (OLD.state='pending' AND NEW.state IN ('ready','retired'))
                OR (OLD.state='ready' AND NEW.state='retired'))
            THEN
                RAISE EXCEPTION 'Guide image state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_guide_asset_state_forward';
            END IF;
            IF NEW.state='retired' AND OLD.state<>'retired'
               AND EXISTS (SELECT 1 FROM project_guide_assets WHERE upload_id=OLD.id)
            THEN
                RAISE EXCEPTION 'Referenced guide images cannot retire'
                    USING ERRCODE='23514', CONSTRAINT='ck_guide_asset_referenced_ready';
            END IF;
            IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at
            THEN
                RAISE EXCEPTION 'Guide image retirement is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_guide_asset_retirement_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_guide_asset_facts BEFORE UPDATE ON guide_asset_uploads FOR EACH ROW EXECUTE FUNCTION guard_guide_asset_facts()"
    )
    # Preserve the shared owner-role/transaction-marker whitelist unchanged.
    op.execute(
        "CREATE TRIGGER trg_guide_asset_storage_ref BEFORE UPDATE ON guide_asset_uploads FOR EACH ROW EXECUTE FUNCTION guard_training_dataset_storage_ref()"
    )

    op.execute("""
        CREATE FUNCTION guard_project_guide_asset() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE owner_state text;
        BEGIN
            IF TG_OP='UPDATE' THEN
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'Project guide reference facts are immutable'
                        USING ERRCODE='23514', CONSTRAINT='ck_project_guide_asset_immutable';
                END IF;
                RETURN NEW;
            END IF;
            -- Keep direct inserts in the same project-before-owner order as services.
            PERFORM 1 FROM projects WHERE id=NEW.project_id FOR KEY SHARE NOWAIT;
            SELECT state INTO owner_state FROM guide_asset_uploads
                WHERE id=NEW.upload_id FOR UPDATE NOWAIT;
            IF owner_state IS DISTINCT FROM 'ready' THEN
                RAISE EXCEPTION 'Project guide references require a ready owner'
                    USING ERRCODE='23514', CONSTRAINT='ck_project_guide_asset_ready';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_project_guide_asset BEFORE INSERT OR UPDATE ON project_guide_assets FOR EACH ROW EXECUTE FUNCTION guard_project_guide_asset()"
    )
    op.execute("""
        CREATE FUNCTION guard_project_guide_assets_revision() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.guide_assets_revision IS DISTINCT FROM 1 THEN
                    RAISE EXCEPTION 'Project guide revision must start at one'
                        USING ERRCODE='23514', CONSTRAINT='ck_project_guide_assets_revision_initial';
                END IF;
            ELSE
                IF NEW.guide_assets_revision IS DISTINCT FROM OLD.guide_assets_revision THEN
                    RAISE EXCEPTION 'Project guide revision cannot be assigned directly'
                        USING ERRCODE='23514', CONSTRAINT='ck_project_guide_assets_revision_managed';
                END IF;
                IF NEW.guide_assets IS DISTINCT FROM OLD.guide_assets THEN
                    NEW.guide_assets_revision := OLD.guide_assets_revision + 1;
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_project_guide_assets_revision BEFORE INSERT OR UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION guard_project_guide_assets_revision()"
    )


def downgrade():
    op.execute(
        "LOCK TABLE projects, guide_asset_uploads, project_guide_assets IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM guide_asset_uploads) OR EXISTS (SELECT 1 FROM project_guide_assets) OR EXISTS (SELECT 1 FROM projects WHERE guide_assets_revision <> 1)"
        )
    ):
        raise RuntimeError(
            "Guide image ownership, references or revisions prevent destructive downgrade"
        )
    op.execute("DROP TRIGGER trg_project_guide_assets_revision ON projects")
    op.execute("DROP FUNCTION guard_project_guide_assets_revision()")
    op.drop_constraint("ck_project_guide_assets_revision", "projects", type_="check")
    op.drop_column("projects", "guide_assets_revision")
    op.execute("DROP TRIGGER trg_project_guide_asset ON project_guide_assets")
    op.execute("DROP FUNCTION guard_project_guide_asset()")
    op.drop_index("ix_project_guide_asset_upload", table_name="project_guide_assets")
    op.drop_table("project_guide_assets")
    op.execute("DROP TRIGGER trg_guide_asset_storage_ref ON guide_asset_uploads")
    op.execute("DROP TRIGGER trg_guide_asset_facts ON guide_asset_uploads")
    op.execute("DROP FUNCTION guard_guide_asset_facts()")
    op.drop_index("ix_guide_asset_original_project", table_name="guide_asset_uploads")
    op.drop_index("ix_guide_asset_cleanup", table_name="guide_asset_uploads")
    op.drop_table("guide_asset_uploads")
