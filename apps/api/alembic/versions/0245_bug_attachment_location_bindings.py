"""Keep bug_attachment locations and actual provider versions across purpose switches."""

from alembic import op
import sqlalchemy as sa

revision = "0245"
down_revision = "0244"
branch_labels = None
depends_on = None

# Frozen SQL; migration execution must not import changing application models.
_LEGACY_REF_CHECK = """
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'bug-reports'
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
    AND storage_ref->>'object_key' = 'bug-report-attachments/' || owner_id::text
        || '/' || id::text || CASE mime_type WHEN 'image/png' THEN '.png'
            WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/webp' THEN '.webp' END
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""

_REF_CHECK = """
(COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'bug-reports'
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
    AND storage_ref->>'object_key' = 'bug-report-attachments/' || owner_id::text
        || '/' || id::text || CASE mime_type WHEN 'image/png' THEN '.png'
            WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/webp' THEN '.webp' END
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)) OR (COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '2'::jsonb AND storage_ref->>'schema' = '2'
    AND storage_ref->>'purpose' = 'bug-reports'
    AND jsonb_typeof(storage_ref->'location_id') = 'string' AND storage_ref->>'location_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND jsonb_typeof(storage_ref->'config_revision') = 'number' AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'binding_generation') = 'number' AND storage_ref->>'binding_generation' ~ '^([2-9]|[1-9][0-9]+)$'
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND jsonb_typeof(storage_ref->'object_key') = 'string'
    AND octet_length(storage_ref->>'object_key') <= 1024
    AND storage_ref->>'object_key' !~ '[[:cntrl:]]'
    AND position(chr(92) in (storage_ref->>'object_key')) = 0
    AND position('//' in (storage_ref->>'object_key')) = 0
    AND left(storage_ref->>'object_key', 1) <> '/'
    AND storage_ref->>'object_key' !~ '(^|/)[.]{1,2}(/|$)'
    AND right(storage_ref->>'object_key', length(('bug-report-attachments/' || owner_id::text || '/' || id::text || CASE mime_type WHEN 'image/png' THEN '.png' WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/webp' THEN '.webp' END))) = ('bug-report-attachments/' || owner_id::text || '/' || id::text || CASE mime_type WHEN 'image/png' THEN '.png' WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/webp' THEN '.webp' END)
    AND (
        (storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
         AND storage_ref->>'config_revision' = '1'
         AND storage_ref->>'object_key' = ('bug-report-attachments/' || owner_id::text || '/' || id::text || CASE mime_type WHEN 'image/png' THEN '.png' WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/webp' THEN '.webp' END))
        OR
        (storage_ref->>'location_id' <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
         AND storage_ref->>'object_key' ~ '(^|/)_aap_managed/bug-reports/bug-report-attachments/[0-9a-f-]{36}/[0-9a-f-]{36}[.](png|jpg|webp)$')
    )
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false))
"""

_VERSION_CHECK = "object_version_id IS NULL OR (length(object_version_id) BETWEEN 1 AND 1024 AND object_version_id !~ '[[:cntrl:]]')"


def upgrade():
    op.add_column(
        "bug_attachment_uploads",
        sa.Column("object_version_id", sa.String(1024), nullable=True),
    )
    op.drop_constraint(
        "ck_bug_attachment_storage_ref", "bug_attachment_uploads", type_="check"
    )
    op.create_check_constraint(
        "ck_bug_attachment_storage_ref", "bug_attachment_uploads", _REF_CHECK
    )
    op.create_check_constraint(
        "ck_bug_attachment_object_version", "bug_attachment_uploads", _VERSION_CHECK
    )
    op.execute("""
        CREATE FUNCTION guard_bug_attachment_upload_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.object_version_id IS DISTINCT FROM OLD.object_version_id
               AND NOT (OLD.state = 'pending' AND OLD.object_version_id IS NULL
                        AND NEW.object_version_id IS NOT NULL)
            THEN
                RAISE EXCEPTION 'Bug attachment provider receipt can only be recorded once while pending'
                    USING ERRCODE='23514', CONSTRAINT='ck_bug_attachment_version_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_bug_attachment_upload_version BEFORE UPDATE ON bug_attachment_uploads "
        "FOR EACH ROW EXECUTE FUNCTION guard_bug_attachment_upload_version()"
    )


def downgrade():
    op.execute(
        "LOCK TABLE bug_attachment_uploads, storage_bindings IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM bug_attachment_uploads
            WHERE storage_ref->>'schema' <> '1' OR object_version_id IS NOT NULL
        ) OR EXISTS (
            SELECT 1 FROM storage_bindings WHERE purpose='bug-reports' AND generation <> 1
        )
    """)
    ):
        raise RuntimeError(
            "Bug attachment location or provider receipts prevent destructive downgrade"
        )
    op.execute(
        "DROP TRIGGER trg_bug_attachment_upload_version ON bug_attachment_uploads"
    )
    op.execute("DROP FUNCTION guard_bug_attachment_upload_version()")
    op.drop_constraint(
        "ck_bug_attachment_storage_ref", "bug_attachment_uploads", type_="check"
    )
    op.create_check_constraint(
        "ck_bug_attachment_storage_ref", "bug_attachment_uploads", _LEGACY_REF_CHECK
    )
    op.drop_constraint(
        "ck_bug_attachment_object_version", "bug_attachment_uploads", type_="check"
    )
    op.drop_column("bug_attachment_uploads", "object_version_id")
