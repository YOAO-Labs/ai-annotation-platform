-- Assets-owned admission gate for publication inside a core owner's transaction.
--
-- New object I/O captures a revision and must compare it at publication. Linking
-- an already-ready immutable object performs no I/O and may pass NULL to check
-- the current active gate. Both paths hold FOR SHARE NOWAIT until commit,
-- without exposing the private gate table or granting storage mutation.
--
-- The SECURITY DEFINER owner needs SELECT plus one column's UPDATE privilege
-- on public.storage_maintenance_gates for the row lock. Returned codes match
-- app/services/storage_access.py validation:
--   storage_maintenance_changed  (missing row or revision mismatch)
--   storage_maintenance_busy     (NOWAIT lock conflict/deadlock)
--   storage_maintenance          (gate is draining/stopped)
CREATE OR REPLACE FUNCTION aap_assets_v1.lock_publication_storage_gate(
    p_location_id uuid,
    p_expected_revision bigint
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_state text;
    v_revision bigint;
BEGIN
    IF p_location_id IS NULL
       OR (p_expected_revision IS NOT NULL AND p_expected_revision < 1) THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'storage_maintenance'
        );
    END IF;
    BEGIN
        SELECT gate.state, gate.revision::bigint
        INTO v_state, v_revision
        FROM public.storage_maintenance_gates AS gate
        WHERE gate.location_id = p_location_id
        FOR SHARE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false, 'code', 'storage_maintenance_busy'
            );
    END;
    IF v_state IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'storage_maintenance_changed'
        );
    END IF;
    IF p_expected_revision IS NOT NULL AND v_revision IS DISTINCT FROM p_expected_revision THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'storage_maintenance_changed'
        );
    END IF;
    IF v_state IS DISTINCT FROM 'active' THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'storage_maintenance'
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'ok', true, 'state', v_state, 'revision', v_revision
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_publication_storage_gate(uuid, bigint)
    FROM PUBLIC;
