\set ON_ERROR_STOP on
BEGIN;
SELECT pg_catalog.set_config('yoao.control_database', :'expected_database', true),
       pg_catalog.set_config('yoao.migration_role', :'migration_role', true),
       pg_catalog.set_config('yoao.application_role', :'application_role', true),
       pg_catalog.set_config('yoao.storage_migration_control_role', :'storage_migration_control_role', true);
DO $guard$
DECLARE
    v_control pg_catalog.pg_roles%ROWTYPE;
    v_name text := pg_catalog.current_setting('yoao.storage_migration_control_role');
BEGIN
    IF current_database() <> pg_catalog.current_setting('yoao.control_database')
       OR current_user <> pg_catalog.current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected storage migration control database or installer';
    END IF;
    IF v_name !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_name IN (current_user, pg_catalog.current_setting('yoao.application_role')) THEN
        RAISE EXCEPTION 'Storage migration control requires a distinct dedicated LOGIN';
    END IF;
    SELECT * INTO v_control FROM pg_catalog.pg_roles WHERE rolname = v_name;
    IF NOT FOUND OR NOT v_control.rolcanlogin OR v_control.rolinherit
       OR v_control.rolsuper OR v_control.rolcreatedb OR v_control.rolcreaterole
       OR v_control.rolreplication OR v_control.rolbypassrls
       OR EXISTS (
           SELECT FROM pg_catalog.pg_auth_members AS m
           WHERE m.member = v_control.oid OR m.roleid = v_control.oid
       ) OR EXISTS (
           SELECT FROM pg_catalog.pg_class AS c WHERE c.relowner = v_control.oid
       ) OR EXISTS (
           SELECT FROM pg_catalog.pg_proc AS f WHERE f.proowner = v_control.oid
       ) OR EXISTS (
           SELECT FROM pg_catalog.pg_namespace AS n WHERE n.nspowner = v_control.oid
       ) OR pg_catalog.has_database_privilege(v_control.oid, current_database(), 'CREATE')
         OR pg_catalog.has_schema_privilege(v_control.oid, 'public', 'CREATE')
         OR pg_catalog.has_schema_privilege(v_control.oid, 'yoao_assets_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Unsafe storage migration control role';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r','p','v','m','f')
          AND pg_catalog.has_table_privilege(
              v_control.oid, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
          )
    ) OR pg_catalog.has_any_column_privilege(
        v_control.oid, 'public.storage_connections', 'SELECT,INSERT,UPDATE,REFERENCES'
    ) THEN
        RAISE EXCEPTION 'Control LOGIN has unscoped relation or connector privileges';
    END IF;
END
$guard$;
GRANT CONNECT ON DATABASE :"expected_database" TO :"storage_migration_control_role";
GRANT USAGE ON SCHEMA public, yoao_assets_v1, yoao_assets_storage_v1, yoao_identity_v1, yoao_audit_v1
    TO :"storage_migration_control_role";
GRANT SELECT (id, window_id, state, revision, window_revision, source_location_id, source, source_gate_revision,
              source_config_revision, parent_copy_id, target_location_id, target, claim, native, copy_id,
              inventory_job_id, token, evidence, error_code, created_by, created_at, updated_at, sealed_at, invalidated_at),
      INSERT (id, window_id, state, revision, window_revision, source_location_id, source, source_gate_revision,
              source_config_revision, parent_copy_id, target_location_id, target, claim, native, copy_id,
              inventory_job_id, token, evidence, error_code, created_by, sealed_at, invalidated_at),
      UPDATE (state, revision, updated_at, copy_id, inventory_job_id, token, native, evidence, sealed_at, invalidated_at)
    ON public.storage_migration_finalizations TO :"storage_migration_control_role";
GRANT SELECT (id, window_id, copy_id, source_location_id, target_location_id, state, revision, finalization_revision,
              window_revision, source, target, target_revisions, token, evidence, native, mapping_sha256, route_count,
              created_by, created_at, updated_at, withdrawing_at, withdrawn_at),
      INSERT (id, window_id, copy_id, source_location_id, target_location_id, state, revision, finalization_revision,
              window_revision, source, target, target_revisions, token, evidence, native, mapping_sha256, route_count,
              created_by, withdrawing_at, withdrawn_at),
      UPDATE (state, revision, updated_at, withdrawing_at, withdrawn_at)
    ON public.storage_migration_read_publications TO :"storage_migration_control_role";
GRANT INSERT (publication_id, source_object_id, endpoint_url, bucket, object_key, source_version_id, source_etag,
              sha256, size_bytes, is_latest, target_key, target_version_id, target_etag, target_content_sha256,
              nonce, creation_plan_uuid)
    ON public.storage_migration_read_routes TO :"storage_migration_control_role";
GRANT SELECT (id, window_id, source_location_id, target_location_id, state, revision, review, review_sha256,
              before_bindings, after_bindings, target_gate_revision, rollback_claim, rollback_native,
              source_release_operation_id, created_by, created_at, updated_at, rolled_back_at),
      INSERT (id, window_id, source_location_id, target_location_id, state, revision, review, review_sha256,
              before_bindings, after_bindings, target_gate_revision, rollback_claim, rollback_native,
              source_release_operation_id, created_by, rolled_back_at),
      UPDATE (state, revision, updated_at, target_gate_revision, rollback_claim, rollback_native,
              source_release_operation_id, rolled_back_at)
    ON public.storage_migration_cutovers TO :"storage_migration_control_role";
GRANT SELECT (purpose, location_id, bucket, generation), UPDATE (location_id, bucket, generation)
    ON public.storage_bindings TO :"storage_migration_control_role";
GRANT SELECT (location_id, state, revision, changed_at, changed_by), UPDATE (state, revision, changed_at, changed_by)
    ON public.storage_maintenance_gates TO :"storage_migration_control_role";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid), yoao_identity_v1.lock_accounts_share(uuid[],boolean),
    yoao_audit_v1.append_events(jsonb)
    TO :"storage_migration_control_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_storage_v1.location_facts(uuid,text,uuid,text,boolean),
    yoao_assets_storage_v1.connector_origin(uuid,text),
    yoao_assets_storage_v1.gate_facts(uuid,text),
    yoao_assets_storage_v1.initialize_gate(uuid),
    yoao_assets_storage_v1.maintenance_status(uuid),
    yoao_assets_storage_v1.physical_io_is_open(text,uuid,uuid),
    yoao_assets_storage_v1.begin_physical_io(uuid,uuid,text,text,integer),
    yoao_assets_storage_v1.settle_physical_io(uuid,uuid,integer)
    TO :"storage_migration_control_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.migration_operator_copy_window_unsettled(uuid),
    yoao_assets_v1.migration_window_has_active_inventory(uuid),
    yoao_assets_v1.read_migration_window(uuid,text),
    yoao_assets_v1.bump_migration_window_finalization_revision(uuid,integer),
    yoao_assets_v1.migration_window_open_operations(uuid),
    yoao_assets_v1.transition_migration_window(uuid,integer,text,uuid),
    yoao_assets_v1.migration_finalization_for_window(uuid),
    yoao_assets_v1.migration_finalization_blocks_window(uuid,boolean),
    yoao_assets_v1.migration_copy_is_frozen(uuid),
    yoao_assets_v1.migration_copy_plan(uuid,text),
    yoao_assets_v1.migration_copy_private_inputs(uuid),
    yoao_assets_v1.migration_copy_attempt(uuid,uuid,text),
    yoao_assets_v1.migration_copy_delta_page(uuid,integer),
    yoao_assets_v1.migration_copy_successor(uuid),
    yoao_assets_v1.original_reclaim_gate_blocked(uuid)
    TO :"storage_migration_control_role";
COMMIT;
