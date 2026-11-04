CREATE OR REPLACE FUNCTION aap_inference_backend_endpoint_v1.open_execution_endpoint(
    p_subject text, p_registry_id uuid, p_request_id uuid, p_authority_json text
) RETURNS text LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp SET TimeZone TO 'UTC' SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3 AS $function$
    SELECT aap_inference_backend_endpoint_v1._open_endpoint_for_purpose(
        p_subject,p_registry_id,p_request_id,'execution',p_authority_json)
$function$;
-- aap-statement-break
DROP FUNCTION aap_inference_topology_v1.set_plugin_asset_approval(uuid,integer,text);
-- aap-statement-break
DROP FUNCTION aap_inference_topology_v1.plugin_asset_approval(uuid);
-- aap-statement-break
DROP TABLE aap_inference_topology_v1.plugin_asset_approvals;
