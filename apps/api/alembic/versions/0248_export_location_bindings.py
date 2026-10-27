"""Preserve ZIP and media owners across reviewed export-target changes."""

from alembic import op
import sqlalchemy as sa

revision = "0248"
down_revision = "0247"
branch_labels = None
depends_on = None

_OLD_0 = "\nCOALESCE((\n    jsonb_typeof(storage_ref) = 'object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','object_key','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb\n    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'\n    AND storage_ref->>'purpose' = 'export'\n    AND storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n    AND storage_ref->'config_revision' = '1'::jsonb AND storage_ref->>'config_revision' = '1'\n    AND storage_ref->'binding_generation' = '1'::jsonb AND storage_ref->>'binding_generation' = '1'\n    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0\n    AND jsonb_typeof(storage_ref->'bucket') = 'string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'\n    AND position(chr(92) in (storage_ref->>'bucket')) = 0\n    AND storage_ref->>'object_key' = media || '/' || project_id::text || '/'\n        || job_id::text || '/' || id::text || '.zip'\n    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n), false)\n"
_NEW_0 = "(COALESCE((\n    jsonb_typeof(storage_ref) = 'object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','object_key','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb\n    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'\n    AND storage_ref->>'purpose' = 'export'\n    AND storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n    AND storage_ref->'config_revision' = '1'::jsonb AND storage_ref->>'config_revision' = '1'\n    AND storage_ref->'binding_generation' = '1'::jsonb AND storage_ref->>'binding_generation' = '1'\n    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0\n    AND jsonb_typeof(storage_ref->'bucket') = 'string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'\n    AND position(chr(92) in (storage_ref->>'bucket')) = 0\n    AND storage_ref->>'object_key' = media || '/' || project_id::text || '/'\n        || job_id::text || '/' || id::text || '.zip'\n    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n), false)) OR (COALESCE((\n    jsonb_typeof(storage_ref) = 'object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','object_key','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','object_key','storage_identity']::text[] = '{}'::jsonb\n    AND storage_ref->'schema' = '2'::jsonb AND storage_ref->>'schema' = '2'\n    AND storage_ref->>'purpose' = 'export'\n    AND jsonb_typeof(storage_ref->'location_id') = 'string' AND storage_ref->>'location_id' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'\n    AND jsonb_typeof(storage_ref->'config_revision') = 'number' AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'\n    AND jsonb_typeof(storage_ref->'binding_generation') = 'number' AND storage_ref->>'binding_generation' ~ '^[1-9][0-9]*$' AND (storage_ref->>'binding_generation')::numeric >= 2\n    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0\n    AND jsonb_typeof(storage_ref->'bucket') = 'string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'\n    AND position(chr(92) in (storage_ref->>'bucket')) = 0\n    AND jsonb_typeof(storage_ref->'object_key') = 'string'\n    AND octet_length((storage_ref->>'object_key')) <= 1024\n    AND (\n      (storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n       AND storage_ref->>'config_revision' = '1' AND (storage_ref->>'object_key') = media || '/' || project_id::text || '/' || job_id::text || '/' || id::text || '.zip')\n      OR (storage_ref->>'location_id' <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n       AND right((storage_ref->>'object_key'), length(media || '/' || project_id::text || '/' || job_id::text || '/' || id::text || '.zip')) = media || '/' || project_id::text || '/' || job_id::text || '/' || id::text || '.zip'\n       AND left((storage_ref->>'object_key'), length((storage_ref->>'object_key')) - length(media || '/' || project_id::text || '/' || job_id::text || '/' || id::text || '.zip')) ~ '^([^/]+/)*_aap_managed/export/$'\n       AND octet_length(left((storage_ref->>'object_key'), length((storage_ref->>'object_key')) - length(media || '/' || project_id::text || '/' || job_id::text || '/' || id::text || '.zip'))) <= 904\n       AND (storage_ref->>'object_key') !~ '[[:cntrl:]]'\n       AND position(chr(92) in (storage_ref->>'object_key')) = 0\n       AND (storage_ref->>'object_key') !~ '(^|/)[.][.]?(/|$)|//')\n    )\n    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n), false))"

_OLD_1 = "\nCOALESCE((\n    jsonb_typeof(storage_ref) = 'object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[] = '{}'::jsonb\n    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'\n    AND storage_ref->>'purpose' = 'export'\n    AND storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n    AND storage_ref->'config_revision' = '1'::jsonb AND storage_ref->>'config_revision' = '1'\n    AND storage_ref->'binding_generation' = '1'::jsonb AND storage_ref->>'binding_generation' = '1'\n    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0\n    AND jsonb_typeof(storage_ref->'bucket') = 'string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'\n    AND position(chr(92) in (storage_ref->>'bucket')) = 0\n    AND storage_ref->>'key_prefix' = 'lidar-export-media/objects/' || id::text || '/'\n    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n), false)\n"
_NEW_1 = "(COALESCE((\n    jsonb_typeof(storage_ref) = 'object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[] = '{}'::jsonb\n    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'\n    AND storage_ref->>'purpose' = 'export'\n    AND storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n    AND storage_ref->'config_revision' = '1'::jsonb AND storage_ref->>'config_revision' = '1'\n    AND storage_ref->'binding_generation' = '1'::jsonb AND storage_ref->>'binding_generation' = '1'\n    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0\n    AND jsonb_typeof(storage_ref->'bucket') = 'string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'\n    AND position(chr(92) in (storage_ref->>'bucket')) = 0\n    AND storage_ref->>'key_prefix' = 'lidar-export-media/objects/' || id::text || '/'\n    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n), false)) OR (COALESCE((\n    jsonb_typeof(storage_ref) = 'object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[] = '{}'::jsonb\n    AND storage_ref->'schema' = '2'::jsonb AND storage_ref->>'schema' = '2'\n    AND storage_ref->>'purpose' = 'export'\n    AND jsonb_typeof(storage_ref->'location_id') = 'string' AND storage_ref->>'location_id' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'\n    AND jsonb_typeof(storage_ref->'config_revision') = 'number' AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'\n    AND jsonb_typeof(storage_ref->'binding_generation') = 'number' AND storage_ref->>'binding_generation' ~ '^[1-9][0-9]*$' AND (storage_ref->>'binding_generation')::numeric >= 2\n    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0\n    AND jsonb_typeof(storage_ref->'bucket') = 'string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'\n    AND position(chr(92) in (storage_ref->>'bucket')) = 0\n    AND jsonb_typeof(storage_ref->'key_prefix') = 'string'\n    AND octet_length((storage_ref->>'key_prefix')) <= 984\n    AND (\n      (storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n       AND storage_ref->>'config_revision' = '1' AND (storage_ref->>'key_prefix') = 'lidar-export-media/objects/' || id::text || '/')\n      OR (storage_ref->>'location_id' <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n       AND right((storage_ref->>'key_prefix'), length('lidar-export-media/objects/' || id::text || '/')) = 'lidar-export-media/objects/' || id::text || '/'\n       AND left((storage_ref->>'key_prefix'), length((storage_ref->>'key_prefix')) - length('lidar-export-media/objects/' || id::text || '/')) ~ '^([^/]+/)*_aap_managed/export/$'\n       AND octet_length(left((storage_ref->>'key_prefix'), length((storage_ref->>'key_prefix')) - length('lidar-export-media/objects/' || id::text || '/'))) <= 904\n       AND (storage_ref->>'key_prefix') !~ '[[:cntrl:]]'\n       AND position(chr(92) in (storage_ref->>'key_prefix')) = 0\n       AND (storage_ref->>'key_prefix') !~ '(^|/)[.][.]?(/|$)|//')\n    )\n    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n), false))"


_OLD_MEMBER_KEY = r"""member.key ~ ('^' || (NEW.storage_ref->>'key_prefix')
                            || '[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\.bin$')"""
_NEW_MEMBER_KEY = r"""left(member.key, length(NEW.storage_ref->>'key_prefix')) = NEW.storage_ref->>'key_prefix'
                        AND substring(member.key FROM length(NEW.storage_ref->>'key_prefix') + 1)
                            ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\.bin$'"""


def _replace_guard(*, upgrade):
    definition = op.get_bind().scalar(
        sa.text(
            "SELECT pg_get_functiondef('guard_export_media_bundle()'::regprocedure)"
        )
    )
    before, after = (
        (_OLD_MEMBER_KEY, _NEW_MEMBER_KEY)
        if upgrade
        else (_NEW_MEMBER_KEY, _OLD_MEMBER_KEY)
    )
    if (
        not isinstance(definition, str)
        or definition.count(before) != 1
        or after in definition
    ):
        raise RuntimeError("Unexpected export media member guard definition")
    op.execute(definition.replace(before, after))


def upgrade():
    _replace_guard(upgrade=True)
    op.drop_constraint("ck_export_output_storage_ref", "export_outputs", type_="check")
    op.create_check_constraint("ck_export_output_storage_ref", "export_outputs", _NEW_0)
    op.create_index(
        "ix_export_output_location",
        "export_outputs",
        [sa.text("(storage_ref->>'location_id')")],
    )
    op.drop_constraint(
        "ck_export_media_storage_ref", "export_media_bundles", type_="check"
    )
    op.create_check_constraint(
        "ck_export_media_storage_ref", "export_media_bundles", _NEW_1
    )
    op.create_index(
        "ix_export_media_location",
        "export_media_bundles",
        [sa.text("(storage_ref->>'location_id')")],
    )


def downgrade():
    op.execute(
        "LOCK TABLE export_outputs, export_media_bundles, storage_bindings IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM export_outputs WHERE storage_ref->>'schema' <> '1') OR EXISTS (SELECT 1 FROM export_media_bundles WHERE storage_ref->>'schema' <> '1') OR EXISTS (SELECT 1 FROM storage_bindings WHERE purpose='export' AND generation <> 1)"
        )
    ):
        raise RuntimeError("Export location facts prevent destructive downgrade")
    _replace_guard(upgrade=False)
    op.drop_index("ix_export_output_location", table_name="export_outputs")
    op.drop_constraint("ck_export_output_storage_ref", "export_outputs", type_="check")
    op.create_check_constraint("ck_export_output_storage_ref", "export_outputs", _OLD_0)
    op.drop_index("ix_export_media_location", table_name="export_media_bundles")
    op.drop_constraint(
        "ck_export_media_storage_ref", "export_media_bundles", type_="check"
    )
    op.create_check_constraint(
        "ck_export_media_storage_ref", "export_media_bundles", _OLD_1
    )
