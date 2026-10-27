"""Keep avatar locations and actual provider versions across purpose switches."""

from alembic import op
import sqlalchemy as sa

revision = "0243"
down_revision = "0242"
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
    AND storage_ref->>'purpose' = 'avatars'
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
    AND storage_ref->>'object_key' = token || '.webp'
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
    AND storage_ref->>'purpose' = 'avatars'
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
    AND storage_ref->>'object_key' = token || '.webp'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)) OR (COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '2'::jsonb AND storage_ref->>'schema' = '2'
    AND storage_ref->>'purpose' = 'avatars'
    AND jsonb_typeof(storage_ref->'location_id') = 'string'
    AND storage_ref->>'location_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND jsonb_typeof(storage_ref->'config_revision') = 'number'
    AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'binding_generation') = 'number'
    AND storage_ref->>'binding_generation' ~ '^([2-9]|[1-9][0-9]+)$'
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
    AND right(storage_ref->>'object_key', 37) = token || '.webp'
    AND (
        (storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
         AND storage_ref->>'config_revision' = '1'
         AND storage_ref->>'object_key' = token || '.webp')
        OR
        (storage_ref->>'location_id' <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
         AND storage_ref->>'object_key' ~ '(^|/)_aap_managed/avatars/[0-9a-f]{32}[.]webp$')
    )
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false))
"""

_VERSION_CHECK = "object_version_id IS NULL OR (length(object_version_id) BETWEEN 1 AND 1024 AND object_version_id !~ '[[:cntrl:]]')"


def upgrade():
    op.add_column(
        "avatar_uploads", sa.Column("object_version_id", sa.String(1024), nullable=True)
    )
    op.drop_constraint("ck_avatar_upload_storage_ref", "avatar_uploads", type_="check")
    op.create_check_constraint(
        "ck_avatar_upload_storage_ref", "avatar_uploads", _REF_CHECK
    )
    op.create_check_constraint(
        "ck_avatar_upload_object_version", "avatar_uploads", _VERSION_CHECK
    )
    op.execute("""
        CREATE FUNCTION guard_avatar_upload_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.object_version_id IS DISTINCT FROM OLD.object_version_id
               AND NOT (OLD.state = 'pending' AND OLD.object_version_id IS NULL
                        AND NEW.object_version_id IS NOT NULL)
            THEN
                RAISE EXCEPTION 'Avatar provider receipt can only be recorded once while pending'
                    USING ERRCODE='23514', CONSTRAINT='ck_avatar_upload_version_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_avatar_upload_version BEFORE UPDATE ON avatar_uploads "
        "FOR EACH ROW EXECUTE FUNCTION guard_avatar_upload_version()"
    )


def downgrade():
    op.execute("LOCK TABLE avatar_uploads, storage_bindings IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM avatar_uploads
            WHERE storage_ref->>'schema' <> '1' OR object_version_id IS NOT NULL
        ) OR EXISTS (
            SELECT 1 FROM storage_bindings WHERE purpose='avatars' AND generation <> 1
        )
    """)
    ):
        raise RuntimeError(
            "Avatar location or provider receipts prevent destructive downgrade"
        )
    op.execute("DROP TRIGGER trg_avatar_upload_version ON avatar_uploads")
    op.execute("DROP FUNCTION guard_avatar_upload_version()")
    op.drop_constraint("ck_avatar_upload_storage_ref", "avatar_uploads", type_="check")
    op.create_check_constraint(
        "ck_avatar_upload_storage_ref", "avatar_uploads", _LEGACY_REF_CHECK
    )
    op.drop_constraint(
        "ck_avatar_upload_object_version", "avatar_uploads", type_="check"
    )
    op.drop_column("avatar_uploads", "object_version_id")
