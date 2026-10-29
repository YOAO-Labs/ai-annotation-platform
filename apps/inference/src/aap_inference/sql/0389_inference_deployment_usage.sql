-- Inference owns frozen pipeline payloads; consumers receive only a predicate.
CREATE FUNCTION aap_inference_pipelines_v1.frozen_snapshot_uses_deployment(
    p_snapshot_id uuid, p_deployment_id uuid
)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT EXISTS (
        SELECT FROM public.prediction_execution_snapshots AS snapshot
        WHERE snapshot.id = p_snapshot_id
          AND snapshot.payload @> pg_catalog.jsonb_build_object(
              'stages', pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
                  'frozen_model_ref', pg_catalog.jsonb_build_object(
                      'deployment_id', p_deployment_id::text))))
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_pipelines_v1.frozen_snapshot_uses_deployment(uuid,uuid) FROM PUBLIC;
