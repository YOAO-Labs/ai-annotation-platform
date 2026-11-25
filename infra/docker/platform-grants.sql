\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.grant_section_applied', '0', true);

\if :{?grant_insights_refresh}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.refresh_role', :'refresh_role', true);

DO $guard$
DECLARE
    refresh_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('yoao.refresh_role'));
    capability oid := to_regprocedure('yoao_insights_v1.refresh_user_perf_daily()');
    table_permissions text := 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER';
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF NOT EXISTS (
           SELECT FROM pg_roles
           WHERE oid = refresh_oid AND rolcanlogin AND NOT rolinherit
             AND rolname NOT IN (current_user, session_user)
             AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       ) OR EXISTS (
           SELECT FROM pg_auth_members
           WHERE refresh_oid IN (member, roleid)
       ) THEN
        RAISE EXCEPTION 'Insights refresh requires a distinct restricted LOGIN with no memberships';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_proc AS routine
        JOIN pg_class AS mv ON mv.oid = to_regclass('public.mv_user_perf_daily')
        JOIN pg_roles AS owner ON owner.oid = routine.proowner
        WHERE routine.oid = capability AND routine.proowner = mv.relowner
          AND (owner.rolname = current_user OR NOT owner.rolcanlogin)
          AND mv.relkind = 'm' AND routine.proowner <> refresh_oid AND routine.prosecdef
          AND routine.prokind = 'f' AND routine.prorettype = 'void'::regtype
          AND routine.provolatile = 'v' AND routine.proparallel = 'u'
          AND routine.proconfig = ARRAY['search_path=pg_catalog, pg_temp']::text[]
          AND has_database_privilege(routine.proowner, current_database(), 'TEMP')
    ) THEN
        RAISE EXCEPTION 'Insights refresh function is missing or does not match its materialized-view owner';
    END IF;
    IF has_database_privilege(refresh_oid, current_database(), 'CREATE,TEMP')
       OR EXISTS (SELECT FROM pg_database WHERE datdba = refresh_oid)
       OR EXISTS (
           SELECT FROM pg_namespace AS namespace
           WHERE namespace.nspowner = refresh_oid
              OR has_schema_privilege(refresh_oid, namespace.oid, 'CREATE')
              OR (namespace.nspname NOT IN ('public', 'yoao_insights_v1', 'pg_catalog', 'information_schema')
                  AND left(namespace.nspname, 8) NOT IN ('pg_toast', 'pg_temp_')
                  AND has_schema_privilege(refresh_oid, namespace.oid, 'USAGE'))
       ) THEN
        RAISE EXCEPTION 'Insights refresh LOGIN has database creation or foreign schema privileges';
    END IF;
    IF current_setting('server_version_num')::integer >= 170000 THEN
        table_permissions := table_permissions || ',MAINTAIN';
    END IF;
    IF EXISTS (
        SELECT FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND left(namespace.nspname, 8) NOT IN ('pg_toast', 'pg_temp_')
          AND CASE
              WHEN relation.relkind = 'S' THEN relation.relowner = refresh_oid
                  OR has_sequence_privilege(refresh_oid, relation.oid, 'SELECT,UPDATE,USAGE')
              WHEN relation.relkind IN ('r','p','v','m','f') THEN
                  relation.relowner = refresh_oid
                  OR has_table_privilege(refresh_oid, relation.oid, table_permissions)
                  OR has_any_column_privilege(refresh_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES')
              ELSE false
          END
    ) THEN
        RAISE EXCEPTION 'Insights refresh LOGIN has direct data privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_proc AS routine
        JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
        WHERE routine.proowner = refresh_oid
           OR (routine.oid <> capability
               AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
               AND left(namespace.nspname, 8) NOT IN ('pg_toast', 'pg_temp_')
               AND NOT EXISTS (
                   SELECT FROM pg_depend AS dependency
                   WHERE dependency.classid = 'pg_proc'::regclass AND dependency.objid = routine.oid
                     AND dependency.refclassid = 'pg_extension'::regclass AND dependency.deptype = 'e'
               )
               AND (left(namespace.nspname, 5) = 'yoao_' OR left(namespace.nspname, 4) = 'aap_'
                    OR routine.prosecdef OR EXISTS (
                        SELECT FROM aclexplode(routine.proacl) AS acl
                        WHERE acl.grantee = refresh_oid AND acl.privilege_type = 'EXECUTE'
                    ))
               AND has_function_privilege(refresh_oid, routine.oid, 'EXECUTE'))
    ) THEN
        RAISE EXCEPTION 'Insights refresh LOGIN has another application routine';
    END IF;
END;
$guard$;

REVOKE ALL ON FUNCTION yoao_insights_v1.refresh_user_perf_daily() FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"refresh_role";
GRANT USAGE ON SCHEMA yoao_insights_v1 TO :"refresh_role";
GRANT EXECUTE ON FUNCTION yoao_insights_v1.refresh_user_perf_daily() TO :"refresh_role";

DO $verify$
DECLARE
    refresh_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('yoao.refresh_role'));
    capability oid := 'yoao_insights_v1.refresh_user_perf_daily()'::regprocedure;
BEGIN
    IF NOT has_database_privilege(refresh_oid, current_database(), 'CONNECT')
       OR has_database_privilege(refresh_oid, current_database(), 'CONNECT WITH GRANT OPTION')
       OR NOT has_schema_privilege(refresh_oid, 'yoao_insights_v1', 'USAGE')
       OR has_schema_privilege(refresh_oid, 'yoao_insights_v1', 'USAGE WITH GRANT OPTION')
       OR NOT has_function_privilege(refresh_oid, capability, 'EXECUTE')
       OR has_function_privilege(refresh_oid, capability, 'EXECUTE WITH GRANT OPTION')
       OR EXISTS (
           SELECT FROM pg_proc AS routine
           CROSS JOIN LATERAL aclexplode(COALESCE(routine.proacl, acldefault('f', routine.proowner))) AS acl
           WHERE routine.oid = capability AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
       ) THEN
        RAISE EXCEPTION 'Insights refresh LOGIN is not limited to its exact capability';
    END IF;
END;
$verify$;
\endif

\if :{?grant_module_execution}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.jobs_owner', :'jobs_owner', true);
SELECT set_config('yoao.export_owner', :'export_owner', true);
SELECT set_config('yoao.jobs_publication_role', :'jobs_publication_role', true);
SELECT set_config('yoao.export_execution_role', :'export_execution_role', true);

DO $grants$
DECLARE
    profile record;
    capability text;
    capability_oid oid;
    capability_oids oid[];
    namespace record;
    client_oid oid;
    owner_oid oid;
    names text[] := ARRAY[
        current_setting('yoao.jobs_owner'),
        current_setting('yoao.export_owner'),
        current_setting('yoao.jobs_publication_role'),
        current_setting('yoao.export_execution_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR current_user = ANY(names)
       OR (SELECT count(DISTINCT name) FROM unnest(names) AS name) <> 4 THEN
        RAISE EXCEPTION 'Unexpected execution grant database or overlapping roles';
    END IF;
    FOR profile IN SELECT * FROM (VALUES
        (current_setting('yoao.jobs_publication_role'), current_setting('yoao.jobs_owner'),
         'yoao_jobs_v1', ARRAY[
            'yoao_jobs_v1.begin_dispatch_publication(uuid,uuid,text,bigint,uuid)',
            'yoao_jobs_v1.finish_dispatch_publication(uuid,uuid,text,bigint,uuid)',
            'yoao_jobs_v1.refuse_dispatch_publication(uuid,uuid,text,bigint,uuid)',
            'yoao_jobs_v1.retry_publication_intent(uuid,text,text,jsonb,jsonb)',
            'yoao_jobs_v1.begin_retry_publication(uuid,uuid,text,text,jsonb,jsonb,integer,uuid)',
            'yoao_jobs_v1.finish_retry_publication(uuid,uuid,text,integer,uuid)',
            'yoao_jobs_v1.begin_fixture_media_publication(uuid,uuid)',
            'yoao_jobs_v1.finish_fixture_media_publication(uuid,uuid,text)'
         ]::text[]),
        (current_setting('yoao.export_execution_role'), current_setting('yoao.export_owner'),
         'yoao_export_v1', ARRAY[
            'yoao_export_v1.begin_execution(uuid,uuid,text,uuid,integer)',
            'yoao_export_v1.enter_execution_io(uuid,uuid)',
            'yoao_export_v1.quiesce_execution_io(uuid,uuid)',
            'yoao_export_v1.finish_execution(uuid,uuid)'
         ]::text[])
    ) AS profiles(client_name, owner_name, schema_name, signatures)
    LOOP
        IF profile.client_name !~ '^[a-z_][a-z0-9_]{0,62}$'
           OR profile.owner_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe execution role name';
        END IF;
        SELECT oid INTO client_oid FROM pg_catalog.pg_roles
        WHERE rolname=profile.client_name AND rolcanlogin AND NOT rolinherit
          AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
          AND NOT rolreplication AND NOT rolbypassrls;
        SELECT oid INTO owner_oid FROM pg_catalog.pg_roles
        WHERE rolname=profile.owner_name AND NOT rolcanlogin AND NOT rolinherit
          AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
          AND NOT rolreplication AND NOT rolbypassrls;
        IF client_oid IS NULL OR owner_oid IS NULL OR EXISTS (
            SELECT FROM pg_catalog.pg_auth_members
            WHERE member=client_oid OR roleid=client_oid
        ) THEN
            RAISE EXCEPTION 'Unsafe execution owner or client LOGIN';
        END IF;
        capability_oids := ARRAY[]::oid[];
        FOREACH capability IN ARRAY profile.signatures LOOP
            capability_oid := pg_catalog.to_regprocedure(capability);
            IF capability_oid IS NULL OR NOT EXISTS (
                SELECT FROM pg_catalog.pg_proc AS function
                WHERE function.oid=capability_oid AND function.proowner=owner_oid
                  AND function.prosecdef AND function.prokind='f'
                  AND NOT function.proretset AND function.proparallel='u'
                  AND function.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
            ) THEN
                RAISE EXCEPTION 'Execution capability ownership or configuration differs: %', capability;
            END IF;
            capability_oids := array_append(capability_oids, capability_oid);
        END LOOP;
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', current_database(), profile.client_name);
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), profile.client_name);
        FOR namespace IN
            SELECT nspname FROM pg_catalog.pg_namespace
            WHERE nspname NOT IN ('pg_catalog','information_schema')
              AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%'
        LOOP
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, profile.client_name);
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, profile.client_name);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, profile.client_name);
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, profile.client_name);
        END LOOP;
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', profile.schema_name, profile.client_name);
        FOREACH capability IN ARRAY profile.signatures LOOP
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', capability);
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', capability, profile.client_name);
        END LOOP;
        IF pg_catalog.has_database_privilege(client_oid,current_database(),'CREATE,TEMP')
           OR EXISTS (
               SELECT FROM pg_catalog.pg_namespace AS ns
               WHERE ns.nspname NOT IN ('pg_catalog','information_schema')
                 AND ns.nspname NOT LIKE 'pg_toast%' AND ns.nspname NOT LIKE 'pg_temp_%'
                 AND (pg_catalog.has_schema_privilege(client_oid,ns.oid,'CREATE')
                      OR (ns.nspname NOT IN ('public',profile.schema_name)
                          AND pg_catalog.has_schema_privilege(client_oid,ns.oid,'USAGE')))
           ) OR EXISTS (
               SELECT FROM pg_catalog.pg_class AS relation
               JOIN pg_catalog.pg_namespace AS ns ON ns.oid=relation.relnamespace
               WHERE ns.nspname NOT IN ('pg_catalog','information_schema')
                 AND ns.nspname NOT LIKE 'pg_toast%' AND ns.nspname NOT LIKE 'pg_temp_%'
                 AND ((relation.relkind IN ('r','p','v','m','f')
                       AND (pg_catalog.has_table_privilege(client_oid,relation.oid,
                                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                            OR pg_catalog.has_any_column_privilege(client_oid,relation.oid,
                                'SELECT,INSERT,UPDATE,REFERENCES')))
                      OR (relation.relkind='S' AND pg_catalog.has_sequence_privilege(
                              client_oid,relation.oid,'USAGE,SELECT,UPDATE')))
           ) OR EXISTS (
               SELECT FROM pg_catalog.pg_proc AS function
               JOIN pg_catalog.pg_namespace AS ns ON ns.oid=function.pronamespace
               WHERE (ns.nspname LIKE 'aap\_%' ESCAPE '\'
                      OR ns.nspname LIKE 'yoao\_%' ESCAPE '\')
                 AND function.oid<>ALL(capability_oids)
                 AND pg_catalog.has_function_privilege(client_oid,function.oid,'EXECUTE')
           ) OR EXISTS (
               SELECT FROM unnest(capability_oids) AS allowed(function_oid)
               WHERE NOT pg_catalog.has_function_privilege(client_oid,function_oid,'EXECUTE')
           ) THEN
            RAISE EXCEPTION 'Execution client has unexpected effective privileges: %', profile.client_name;
        END IF;
    END LOOP;
END;
$grants$;
\endif

\if :{?grant_storage_migration_control}
SELECT set_config('yoao.grant_section_applied', '1', true);
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
\endif

DO $$
BEGIN
    IF current_setting('yoao.grant_section_applied', true) IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION 'No capability section selected; pass -v grant_<capability>=1';
    END IF;
END $$;
COMMIT;
