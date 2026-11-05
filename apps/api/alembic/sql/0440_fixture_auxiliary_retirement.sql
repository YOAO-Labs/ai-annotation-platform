-- The native command uses this transaction-local marker while deleting an
-- already-qualified row. The private table owner and its members are trusted;
-- ordinary product roles have no access.
CREATE TABLE yoao_fixture_v1.auxiliary_retirement_guard (
    pid integer NOT NULL,
    xid xid8 NOT NULL,
    owner_id uuid NOT NULL,
    PRIMARY KEY (pid, xid, owner_id)
);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_fixture_v1.auxiliary_retirement_guard FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_fixture_v1.guard_auxiliary_retirement()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.auxiliary_retirement_guard AS guard
        WHERE guard.pid = pg_catalog.pg_backend_pid()
          AND guard.xid = pg_catalog.pg_current_xact_id()
          AND guard.owner_id = OLD.id
    ) THEN
        RAISE EXCEPTION 'Auxiliary original responsibility cannot be deleted'
            USING ERRCODE = '23514';
    END IF;
    RETURN OLD;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_auxiliary_retirement() FROM PUBLIC;
-- aap-statement-break
-- Keep the existing INSERT/UPDATE invariant and its restore role semantics.
DROP TRIGGER trg_auxiliary_original ON public.dataset_auxiliary_originals;
-- aap-statement-break
CREATE TRIGGER trg_auxiliary_original BEFORE INSERT OR UPDATE
ON public.dataset_auxiliary_originals
FOR EACH ROW EXECUTE FUNCTION public.guard_auxiliary_original();
-- aap-statement-break
CREATE TRIGGER trg_auxiliary_original_retirement BEFORE DELETE
ON public.dataset_auxiliary_originals
FOR EACH ROW EXECUTE FUNCTION yoao_fixture_v1.guard_auxiliary_retirement();
-- aap-statement-break
CREATE FUNCTION yoao_fixture_v1.retire_auxiliary_original(
    p_run_id uuid, p_dataset_id uuid, p_owner_id uuid
)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_identity text;
    v_owner public.dataset_auxiliary_originals%ROWTYPE;
    v_removed boolean;
BEGIN
    -- Bind the authority to the authenticated private guard-table owner, not
    -- to a role switch or a SECURITY DEFINER caller.
    IF NOT pg_catalog.pg_has_role(
        SESSION_USER,
        pg_catalog.pg_get_userbyid((
            SELECT relowner FROM pg_catalog.pg_class
            WHERE oid = 'yoao_fixture_v1.auxiliary_retirement_guard'::pg_catalog.regclass
        )),
        'MEMBER'
    ) THEN
        RAISE EXCEPTION 'fixture auxiliary retirement requires the private owner session'
            USING ERRCODE = '42501';
    END IF;

    -- Classify first so its root -> run -> resource locks precede every source
    -- lock, including when this command is called directly by the owner.
    SELECT owner.storage_identity INTO v_identity
    FROM public.dataset_auxiliary_originals AS owner WHERE owner.id = p_owner_id;
    IF NOT FOUND OR aap_assets_v1.classify_fixture_original_reclaim(p_owner_id, v_identity)
           IS DISTINCT FROM 'qualified' THEN
        RETURN false;
    END IF;
    SELECT owner.* INTO v_owner FROM public.dataset_auxiliary_originals AS owner
    WHERE owner.id = p_owner_id FOR UPDATE;
    IF NOT FOUND OR v_owner.dataset_id IS DISTINCT FROM p_dataset_id
       OR v_owner.storage_identity IS DISTINCT FROM v_identity THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.managed_original_creations AS creation
    WHERE creation.id = p_owner_id FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM 1 FROM public.managed_original_retirements AS retirement
    WHERE retirement.creation_id = p_owner_id FOR SHARE;

    IF EXISTS (SELECT 1 FROM public.datasets WHERE id = p_dataset_id)
       OR EXISTS (SELECT 1 FROM public.dataset_items
                  WHERE dataset_id = p_dataset_id OR nuscenes_raw_original_id = p_owner_id)
       OR EXISTS (SELECT 1 FROM public.scenes
                  WHERE dataset_id = p_dataset_id OR nuscenes_map_original_id = p_owner_id)
       OR EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_admissions
                  WHERE run_id = p_run_id AND state <> 'settled')
       OR (SELECT count(*) FROM public.managed_original_retirements
           WHERE creation_id = p_owner_id) <> 1
       OR NOT EXISTS (
           SELECT 1
           FROM yoao_fixture_v1.fixture_auxiliary_intents AS intent
           JOIN public.managed_original_creations AS creation ON creation.id = intent.creation_id
           JOIN public.managed_original_retirements AS retirement
             ON retirement.creation_id = creation.id
           WHERE intent.run_id = p_run_id AND intent.dataset_id = p_dataset_id
             AND intent.creation_id = p_owner_id
             AND creation.id = v_owner.creation_id AND creation.dataset_id = p_dataset_id
             AND creation.owner_kind = 'dataset_auxiliary' AND creation.status = 'verified'
             AND creation.settled_at IS NOT NULL AND creation.readback_at IS NOT NULL
             AND creation.version_id IS NOT NULL AND creation.version_id <> '' AND creation.version_id <> 'null'
             AND creation.etag IS NOT NULL AND creation.etag <> ''
             AND creation.sha256 ~ '^[0-9a-f]{64}$' AND creation.size_bytes >= 0
             AND NOT EXISTS (SELECT 1 FROM public.storage_operations AS operation
                             WHERE operation.id = creation.operation_id)
             AND creation.receipt #> '{response,ResponseMetadata,HTTPStatusCode}' = '200'::jsonb
             AND creation.receipt ->> 'version_id' = creation.version_id
             AND creation.receipt #>> '{response,VersionId}' = creation.version_id
             AND creation.verification ->> 'sha256' = creation.sha256
             AND creation.verification -> 'size_bytes' = pg_catalog.to_jsonb(creation.size_bytes)
             AND v_owner.kind = intent.kind AND v_owner.source_contract = intent.source_contract
             AND v_owner.sha256 = intent.sha256 AND v_owner.size_bytes = intent.size_bytes
             AND v_owner.location_id = creation.location_id
             AND v_owner.storage_identity = creation.storage_identity
             AND v_owner.bucket = creation.bucket AND v_owner.object_key = creation.object_key
             AND v_owner.sha256 = creation.sha256 AND v_owner.size_bytes = creation.size_bytes
             AND v_owner.etag = creation.etag AND v_owner.version_id = creation.version_id
             AND v_owner.storage_ref = creation.configuration -> 'original_ref'
             AND retirement.owner_type = 'dataset_auxiliary'
             AND retirement.auxiliary_original_id = p_owner_id
             AND retirement.dataset_id = p_dataset_id
             AND retirement.dataset_item_id IS NULL AND retirement.asset_id IS NULL
             AND retirement.role = 'managed_original'
             AND retirement.location_id = v_owner.location_id
             AND retirement.storage_identity = v_owner.storage_identity
             AND retirement.bucket = v_owner.bucket AND retirement.object_key = v_owner.object_key
             AND retirement.location_config_revision = (v_owner.storage_ref ->> 'config_revision')::integer
             AND retirement.binding_generation = (v_owner.storage_ref ->> 'binding_generation')::integer
             AND retirement.identity_state = 'verified_binding'
             AND retirement.asset_verification_state = 'verified'
             AND retirement.reference_state = 'present'
             AND retirement.sha256 = v_owner.sha256 AND retirement.size_bytes = v_owner.size_bytes
             AND retirement.etag = v_owner.etag AND retirement.verified_at = creation.readback_at
       ) THEN
        RETURN false;
    END IF;

    INSERT INTO yoao_fixture_v1.auxiliary_retirement_guard (pid, xid, owner_id)
    VALUES (pg_catalog.pg_backend_pid(), pg_catalog.pg_current_xact_id(), p_owner_id);
    DELETE FROM public.dataset_auxiliary_originals WHERE id = p_owner_id;
    v_removed := FOUND;
    DELETE FROM yoao_fixture_v1.auxiliary_retirement_guard
    WHERE pid = pg_catalog.pg_backend_pid() AND xid = pg_catalog.pg_current_xact_id()
      AND owner_id = p_owner_id;
    RETURN v_removed;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.retire_auxiliary_original(uuid,uuid,uuid) FROM PUBLIC;
