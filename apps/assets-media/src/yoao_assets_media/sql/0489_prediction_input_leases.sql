CREATE FUNCTION yoao_assets_v1.create_prediction_input_lease(
    p_id uuid, p_owner_id uuid, p_owner_kind text, p_task_id uuid,
    p_contract_version integer, p_storage_ref jsonb, p_maintenance_revision integer,
    p_source_ref jsonb, p_started_at timestamptz
) RETURNS TABLE (expires_at timestamptz, retain_until timestamptz)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
    INSERT INTO public.prediction_input_leases AS lease (
        id, owner_id, owner_kind, task_id, contract_version, storage_ref, bucket,
        storage_identity, maintenance_revision, source_ref, inventory, object_count,
        size_bytes, state, uncertain_write, created_at, expires_at, retain_until, next_cleanup_at
    ) VALUES (
        p_id, p_owner_id, p_owner_kind, p_task_id, p_contract_version, p_storage_ref,
        p_storage_ref->>'bucket', p_storage_ref->>'storage_identity', p_maintenance_revision,
        p_source_ref, '{"schema":1,"objects":{}}'::jsonb, 0, 0, 'active', false,
        p_started_at, p_started_at + interval '2 hours', p_started_at + interval '2 hours 7 days',
        p_started_at + interval '2 hours'
    )
    RETURNING lease.expires_at, lease.retain_until
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.prediction_input_lease_facts(
    p_id uuid, p_lock boolean, p_due_at timestamptz
) RETURNS TABLE (
    id uuid, owner_id uuid, owner_kind text, task_id uuid, contract_version integer,
    bucket text, storage_identity text, storage_ref jsonb, maintenance_revision integer,
    created_at timestamptz, expires_at timestamptz, retain_until timestamptz, source_ref jsonb,
    state text, uncertain_write boolean, object_count integer, size_bytes bigint
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_due_at IS NOT NULL THEN
        PERFORM 1 FROM public.prediction_input_leases AS lease
        WHERE lease.id = p_id AND lease.next_cleanup_at <= p_due_at
          AND (lease.state = 'cleanup_pending' OR (lease.state = 'active' AND lease.expires_at <= p_due_at))
        FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN RETURN; END IF;
    ELSIF p_lock THEN
        PERFORM 1 FROM public.prediction_input_leases AS lease WHERE lease.id = p_id FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY
    SELECT lease.id, lease.owner_id, lease.owner_kind::text, lease.task_id, lease.contract_version,
           lease.bucket::text, lease.storage_identity::text, lease.storage_ref, lease.maintenance_revision,
           lease.created_at, lease.expires_at, lease.retain_until, lease.source_ref,
           lease.state::text, lease.uncertain_write, lease.object_count, lease.size_bytes
    FROM public.prediction_input_leases AS lease WHERE lease.id = p_id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.prediction_input_objects_pending(p_id uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_path_exists(
        lease.inventory,
        '$.objects.* ? (@.state != "verified" || @.version_id == null || @.version_id == "null")'::jsonpath
    )
    FROM public.prediction_input_leases AS lease WHERE lease.id = p_id
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_prediction_input_inventory(p_id uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT lease.inventory FROM public.prediction_input_leases AS lease
    WHERE lease.id = p_id AND pg_catalog.octet_length(lease.inventory::text) <= 4194304
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_prediction_input_object(p_id uuid, p_relative text)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT lease.inventory->'objects'->p_relative FROM public.prediction_input_leases AS lease
    WHERE lease.id = p_id AND lease.state = 'active'
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reserve_prediction_input_object(p_id uuid, p_relative text, p_planned jsonb)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
    UPDATE public.prediction_input_leases AS lease
    SET inventory = pg_catalog.jsonb_set(lease.inventory, ARRAY['objects', p_relative], p_planned),
        object_count = lease.object_count + 1,
        size_bytes = lease.size_bytes + (p_planned->>'size_bytes')::bigint
    WHERE lease.id = p_id AND lease.state = 'active' AND lease.expires_at > pg_catalog.statement_timestamp()
      AND lease.contract_version IN (2, 3) AND p_planned->>'state' = 'planned'
      AND NOT (lease.inventory->'objects' ? p_relative)
    RETURNING true
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.verify_prediction_input_object(
    p_id uuid, p_relative text, p_expected jsonb, p_etag text, p_version_id text
) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
    UPDATE public.prediction_input_leases AS lease
    SET inventory = pg_catalog.jsonb_set(
        lease.inventory, ARRAY['objects', p_relative],
        p_expected || pg_catalog.jsonb_build_object('state', 'verified', 'etag', p_etag, 'version_id', p_version_id),
        false
    )
    WHERE lease.id = p_id AND lease.state = 'active' AND lease.expires_at > pg_catalog.statement_timestamp()
      AND lease.contract_version IN (2, 3) AND p_expected->>'state' = 'planned'
      AND lease.inventory->'objects'->p_relative = p_expected
      AND p_etag IS NOT NULL AND p_version_id NOT IN ('', 'null')
    RETURNING true
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.seal_prediction_input_lease(p_id uuid, p_uncertain boolean, p_closed_at timestamptz)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
    UPDATE public.prediction_input_leases AS lease
    SET state = 'cleanup_pending', uncertain_write = lease.uncertain_write OR p_uncertain,
        next_cleanup_at = p_closed_at
    WHERE lease.id = p_id AND lease.state IN ('active', 'cleanup_pending')
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.prepare_prediction_input_cleanup(
    p_id uuid, p_checked_at timestamptz
) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
    WITH permits AS (
        SELECT EXISTS (
            SELECT 1 FROM public.storage_operations AS operation
            WHERE operation.purpose = 'prediction_input_' || pg_catalog.replace(p_id::text, '-', '')
        ) AS unsettled
    )
    UPDATE public.prediction_input_leases AS lease
    SET last_cleanup_at = p_checked_at, next_cleanup_at = p_checked_at + interval '60 seconds',
        error_code = CASE
            WHEN lease.contract_version NOT IN (2, 3) OR lease.storage_ref IS NULL THEN 'legacy_unpinned'
            WHEN permits.unsettled THEN 'storage_operation_unsettled'
            ELSE lease.error_code
        END,
        state = CASE
            WHEN lease.contract_version IN (2, 3) AND lease.storage_ref IS NOT NULL AND NOT permits.unsettled
            THEN 'cleanup_pending' ELSE lease.state
        END,
        uncertain_write = lease.uncertain_write OR (
            lease.state = 'active' AND lease.contract_version IN (2, 3)
            AND lease.storage_ref IS NOT NULL AND NOT permits.unsettled
        )
    FROM permits
    WHERE lease.id = p_id AND lease.next_cleanup_at <= p_checked_at
      AND (lease.state = 'cleanup_pending' OR (lease.state = 'active' AND lease.expires_at <= p_checked_at))
    RETURNING lease.contract_version IN (2, 3) AND lease.storage_ref IS NOT NULL AND NOT permits.unsettled
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_prediction_input_cleanup_failure(p_id uuid, p_code text, p_uncertain boolean)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
    UPDATE public.prediction_input_leases AS lease
    SET error_code = p_code, uncertain_write = lease.uncertain_write OR p_uncertain
    WHERE lease.id = p_id AND lease.state = 'cleanup_pending'
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.finish_prediction_input_cleanup(
    p_id uuid, p_permit_id uuid, p_permit_revision integer, p_finished_at timestamptz
)
RETURNS text
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
    WITH permits AS (
        SELECT operation.id, operation.location_id, operation.revision
        FROM public.storage_operations AS operation
        WHERE operation.purpose = 'prediction_input_' || pg_catalog.replace(p_id::text, '-', '')
    )
    UPDATE public.prediction_input_leases AS lease
    SET state = CASE
            WHEN lease.uncertain_write AND lease.retain_until > p_finished_at THEN 'cleanup_pending'
            ELSE 'deleted'
        END,
        deleted_at = CASE
            WHEN lease.uncertain_write AND lease.retain_until > p_finished_at THEN NULL
            ELSE p_finished_at
        END,
        error_code = NULL
    WHERE lease.id = p_id AND lease.state = 'cleanup_pending' AND lease.contract_version IN (2, 3)
      AND EXISTS (
          SELECT 1 FROM permits
          WHERE permits.id = p_permit_id AND permits.revision = p_permit_revision
            AND permits.location_id::text = lease.storage_ref->>'location_id'
      )
      AND NOT EXISTS (SELECT 1 FROM permits WHERE permits.id <> p_permit_id)
    RETURNING CASE WHEN lease.state = 'deleted' THEN 'removed' ELSE 'retained' END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.list_due_prediction_input_cleanups(p_checked_at timestamptz, p_limit integer)
RETURNS TABLE (id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT lease.id FROM public.prediction_input_leases AS lease
    WHERE lease.next_cleanup_at <= p_checked_at
      AND (lease.state = 'cleanup_pending' OR (lease.state = 'active' AND lease.expires_at <= p_checked_at))
    ORDER BY lease.next_cleanup_at, lease.id LIMIT LEAST(p_limit, 1000)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.prediction_input_binding_history()
RETURNS TABLE (lease_active bigint, lease_cleanup_pending bigint, lease_deleted bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(*) FILTER (WHERE lease.state = 'active'),
           pg_catalog.count(*) FILTER (WHERE lease.state = 'cleanup_pending'),
           pg_catalog.count(*) FILTER (WHERE lease.state = 'deleted')
    FROM public.prediction_input_leases AS lease
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1.create_prediction_input_lease(uuid,uuid,text,uuid,integer,jsonb,integer,jsonb,timestamptz),
    yoao_assets_v1.prediction_input_lease_facts(uuid,boolean,timestamptz),
    yoao_assets_v1.prediction_input_objects_pending(uuid),
    yoao_assets_v1.read_prediction_input_inventory(uuid),
    yoao_assets_v1.read_prediction_input_object(uuid,text),
    yoao_assets_v1.reserve_prediction_input_object(uuid,text,jsonb),
    yoao_assets_v1.verify_prediction_input_object(uuid,text,jsonb,text,text),
    yoao_assets_v1.seal_prediction_input_lease(uuid,boolean,timestamptz),
    yoao_assets_v1.prepare_prediction_input_cleanup(uuid,timestamptz),
    yoao_assets_v1.record_prediction_input_cleanup_failure(uuid,text,boolean),
    yoao_assets_v1.finish_prediction_input_cleanup(uuid,uuid,integer,timestamptz),
    yoao_assets_v1.list_due_prediction_input_cleanups(timestamptz,integer),
    yoao_assets_v1.prediction_input_binding_history()
FROM PUBLIC;
