-- The physical reader alone settles its exact permit.  PostgreSQL delivers the
-- empty, run-specific notification only if this deletion commits.
CREATE OR REPLACE FUNCTION aap_assets_v1._finish_media_original_read(p_creation_id uuid, p_operation_id uuid)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run_id uuid;
BEGIN
    DELETE FROM public.storage_operations AS operation
    USING public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id AND operation.id = p_operation_id
      AND operation.location_id = creation.location_id
      AND operation.purpose = 'media_original_' || pg_catalog.replace(creation.id::text, '-', '')
      AND operation.node_id = 'aap-assets-media';
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT source.run_id INTO v_run_id
    FROM yoao_fixture_v1.fixture_run_resources AS source
    WHERE source.kind = 'creation' AND source.creation_id = p_creation_id;
    IF v_run_id IS NOT NULL THEN
        PERFORM pg_catalog.pg_notify('aap_fixture_reader_' || pg_catalog.replace(v_run_id::text, '-', ''), '');
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._finish_media_original_read(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
-- Only the private fixture owner may query this sealed-child status. NULL is
-- an invalid/unavailable scope, not an empty operation inventory.
CREATE FUNCTION aap_assets_v1.fixture_original_readers_empty(p_run_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'fixture reader status requires a test database';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        WHERE run.id = p_run_id AND run.parent_run_id IS NOT NULL
          AND run.family = 'owned' AND run.state = 'sealing'
    ) THEN RETURN NULL; END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS source
        JOIN public.managed_original_creations AS creation ON creation.id = source.creation_id
        JOIN public.storage_operations AS operation
          ON operation.location_id = creation.location_id
         AND operation.purpose = 'media_original_' || pg_catalog.replace(creation.id::text, '-', '')
         AND operation.node_id = 'aap-assets-media'
        WHERE source.run_id = p_run_id AND source.kind = 'creation'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_original_readers_empty(uuid) FROM PUBLIC;
