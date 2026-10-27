"""Preserve audit archive locations across controlled purpose switches."""

from alembic import op
import sqlalchemy as sa

revision = "0247"
down_revision = "0246"
branch_labels = None
depends_on = None

_LEGACY_REF_CHECK = """COALESCE((
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
), false)"""
_REF_CHECK = """(COALESCE((
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
), false)) OR (COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '2'::jsonb AND storage_ref->>'schema' = '2'
    AND storage_ref->>'purpose' = 'audit-archive'
    AND jsonb_typeof(storage_ref->'location_id') = 'string' AND storage_ref->>'location_id' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
    AND jsonb_typeof(storage_ref->'config_revision') = 'number' AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'binding_generation') = 'number' AND storage_ref->>'binding_generation' ~ '^[1-9][0-9]*$' AND (storage_ref->>'binding_generation')::numeric >= 2
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND jsonb_typeof(storage_ref->'object_key') = 'string'
    AND octet_length(storage_ref->>'object_key') <= 1024
    AND (
      (storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
       AND storage_ref->>'config_revision' = '1' AND storage_ref->>'object_key' = year::text || '/' || lpad(month::text,2,'0') || '/' || id::text || '.jsonl.gz')
      OR (storage_ref->>'location_id' <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
       AND storage_ref->>'object_key' !~ '[[:cntrl:]]'
       AND position(chr(92) in (storage_ref->>'object_key')) = 0
       AND storage_ref->>'object_key' !~ '(^|/)[.][.]?(/|$)|//'
       AND storage_ref->>'object_key' ~ ('(^|/)_aap_managed/audit-archive/' || year::text || '/' || lpad(month::text,2,'0') || '/' || id::text || '[.]jsonl[.]gz$'))
    )
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false))"""


def upgrade():
    op.drop_constraint(
        "ck_audit_archive_storage_ref", "audit_archive_outputs", type_="check"
    )
    op.create_check_constraint(
        "ck_audit_archive_storage_ref", "audit_archive_outputs", _REF_CHECK
    )
    op.create_index(
        "ix_audit_archive_location",
        "audit_archive_outputs",
        [sa.text("(storage_ref->>'location_id')")],
    )


def downgrade():
    op.execute(
        "LOCK TABLE audit_archive_outputs, storage_bindings IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM audit_archive_outputs WHERE storage_ref->>'schema' <> '1') OR EXISTS (SELECT 1 FROM storage_bindings WHERE purpose='audit-archive' AND generation <> 1)"
        )
    ):
        raise RuntimeError("Audit archive location facts prevent destructive downgrade")
    op.drop_index("ix_audit_archive_location", table_name="audit_archive_outputs")
    op.drop_constraint(
        "ck_audit_archive_storage_ref", "audit_archive_outputs", type_="check"
    )
    op.create_check_constraint(
        "ck_audit_archive_storage_ref", "audit_archive_outputs", _LEGACY_REF_CHECK
    )
