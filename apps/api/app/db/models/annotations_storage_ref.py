"""Closed saved annotation-file address checks shared by existing owners."""


def annotations_ref_check(logical_key):
    return f"""
COALESCE((
    jsonb_typeof(storage_ref)='object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity','key_prefix']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','object_key','storage_identity','key_prefix']::text[]='{{}}'::jsonb
    AND storage_ref->'schema'='2'::jsonb AND storage_ref->>'schema'='2'
    AND storage_ref->>'purpose'='annotations'
    AND jsonb_typeof(storage_ref->'location_id')='string'
    AND storage_ref->>'location_id' ~ '^[0-9a-f]{{8}}(-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}}$'
    AND jsonb_typeof(storage_ref->'config_revision')='number'
    AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'binding_generation')='number'
    AND storage_ref->>'binding_generation' ~ '^[1-9][0-9]*$'
    AND jsonb_typeof(storage_ref->'endpoint_url')='string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url'))=0
    AND jsonb_typeof(storage_ref->'bucket')='string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:][:cntrl:]/?#@%:]'
    AND position(chr(92) in (storage_ref->>'bucket'))=0
    AND jsonb_typeof(storage_ref->'object_key')='string'
    AND length(storage_ref->>'object_key') BETWEEN 1 AND 1000
    AND octet_length(storage_ref->>'object_key')<=1024
    AND jsonb_typeof(storage_ref->'key_prefix')='string'
    AND length(storage_ref->>'key_prefix')<=896
    AND octet_length(storage_ref->>'key_prefix')<=920
    AND storage_ref->>'object_key'=(storage_ref->>'key_prefix') || ({logical_key})
    AND jsonb_typeof(storage_ref->'storage_identity')='string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{{64}}$'
    AND ((storage_ref->>'location_id'='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
          AND storage_ref->>'config_revision'='1' AND storage_ref->>'key_prefix'='')
      OR (storage_ref->>'location_id'<>'4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
          AND storage_ref->>'key_prefix' ~ '^([^/]+/)*_aap_managed/annotations/$'
          AND storage_ref->>'key_prefix' !~ '[[:cntrl:]]|(^|/)[.][.]?(/|$)|//'
          AND position(chr(92) in (storage_ref->>'key_prefix'))=0))
),false)
"""
