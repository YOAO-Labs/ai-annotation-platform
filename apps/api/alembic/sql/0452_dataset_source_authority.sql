-- Minimal current Dataset facts for the caller-owned source write transaction.
CREATE FUNCTION aap_assets_v1.read_dataset_source_authority(
    p_dataset_id uuid, p_lock boolean
)
RETURNS TABLE (
    dataset_id uuid,
    created_by uuid,
    archived_at timestamptz,
    lifecycle_revision integer,
    directory_protected boolean
)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL OR p_lock IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset source authority request';
    END IF;
    IF p_lock THEN
        PERFORM 1 FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id FOR NO KEY UPDATE;
    END IF;
    RETURN QUERY
        SELECT dataset.id, dataset.created_by, dataset.archived_at,
               dataset.lifecycle_revision,
               EXISTS (SELECT 1 FROM public.dataset_sources AS source
                       WHERE source.dataset_id = dataset.id)
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_dataset_source_authority(uuid, boolean) FROM PUBLIC;
