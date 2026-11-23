\set ON_ERROR_STOP on

GRANT USAGE ON SCHEMA yoao_identity_v1, yoao_settings_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid),
    yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid),
    yoao_settings_v1.resolve_connector_host_allowlist(jsonb) TO :"assets_owner";
GRANT SELECT (id, kind, scope, project_id, revision, config, secret_enc)
    ON public.storage_connections TO :"assets_owner";
GRANT SELECT (id, enabled, kind, config_revision, connection_id)
    ON public.storage_locations TO :"assets_owner";
GRANT SELECT (purpose, location_id, generation) ON public.storage_bindings TO :"assets_owner";
GRANT SELECT (location_id, state, revision) ON public.storage_maintenance_gates TO :"assets_owner";
GRANT SELECT (id, purpose, location_id, revision) ON public.storage_operations TO :"assets_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER TABLE yoao_assets_v1.saved_binding_probe OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_io_allowlist(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_io_connector(uuid,integer,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_io_target(uuid,integer,integer,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._saved_binding_probe_authority(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._saved_binding_probe_facts(uuid,text,uuid,integer,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_saved_binding_probe(uuid,uuid,uuid,text,text,uuid,integer,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_saved_binding_probe(uuid,uuid,boolean) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
