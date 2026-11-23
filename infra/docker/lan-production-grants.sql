\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.application_role', :'application_role', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.asset_cleanup_role', :'asset_cleanup_role', true);
SET LOCAL search_path = pg_catalog, pg_temp;

DO $$
BEGIN
    IF current_setting('yoao.asset_cleanup_role') IN (
           current_setting('yoao.application_role'), current_user
       )
       OR pg_has_role(current_setting('yoao.asset_cleanup_role'), current_user, 'MEMBER')
       OR EXISTS (
           SELECT 1 FROM pg_catalog.pg_auth_members AS member
           WHERE member.member = (
               SELECT candidate.oid FROM pg_catalog.pg_roles AS candidate
               WHERE candidate.rolname = current_setting('yoao.asset_cleanup_role')
           ) OR member.roleid = (
               SELECT candidate.oid FROM pg_catalog.pg_roles AS candidate
               WHERE candidate.rolname = current_setting('yoao.asset_cleanup_role')
           )
       ) THEN
        RAISE EXCEPTION 'The Assets cleanup role must be distinct and have no role memberships';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = current_setting('yoao.asset_cleanup_role')
          AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'The Assets cleanup role must be a non-admin LOGIN role';
    END IF;
END $$;

\set platform_runtime :application_role
\ir ordinary-runtime-revoke.sql
REVOKE ALL ON DATABASE :"expected_database" FROM PUBLIC, :"application_role";
GRANT CONNECT, TEMPORARY ON DATABASE :"expected_database" TO :"application_role";
\ir m2b-runtime-grants.sql
\ir ordinary-runtime-verify.sql

REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_cleanup_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_cleanup_role";
REVOKE ALL ON SCHEMA public, yoao_identity_v1, yoao_identity_directory_v1,
    yoao_workspace_v1,
    yoao_labeling_v1, yoao_jobs_v1, yoao_learning_v1, yoao_inference_v1,
    yoao_insights_v1, yoao_diagnostics_v1, yoao_export_v1, yoao_audit_v1
    FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_identity_v1, yoao_identity_directory_v1,
    yoao_workspace_v1,
    yoao_labeling_v1, yoao_jobs_v1, yoao_learning_v1, yoao_inference_v1,
    yoao_insights_v1, yoao_diagnostics_v1, yoao_export_v1, yoao_audit_v1
    FROM :"asset_cleanup_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_cleanup_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_cleanup_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
),
    yoao_assets_v1.complete_storage_deletion(uuid, uuid),
    yoao_assets_v1.claim_image_garbage(uuid,integer,integer),
    yoao_assets_v1.complete_image_garbage(uuid,uuid),
    yoao_assets_v1.claim_accepted_input_garbage(uuid,integer,integer),
    yoao_assets_v1.complete_accepted_input_garbage(uuid,uuid),
    yoao_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer),
    yoao_assets_v1.complete_accepted_video_input_garbage(uuid,uuid),
    yoao_assets_v1.claim_native_pyramid_retirement(uuid,integer),
    yoao_assets_v1.prepare_native_pyramid_retirement_page(uuid,uuid,integer),
    yoao_assets_v1.complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)
    TO :"asset_cleanup_role";
DO $$
DECLARE
    relation oid;
    cleanup_functions oid[] := ARRAY[
        'yoao_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_storage_deletion(uuid,uuid)'::regprocedure::oid,
        'yoao_assets_v1.claim_image_garbage(uuid,integer,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_image_garbage(uuid,uuid)'::regprocedure::oid,
        'yoao_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_accepted_input_garbage(uuid,uuid)'::regprocedure::oid,
        'yoao_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)'::regprocedure::oid,
        'yoao_assets_v1.claim_native_pyramid_retirement(uuid,integer)'::regprocedure::oid,
        'yoao_assets_v1.prepare_native_pyramid_retirement_page(uuid,uuid,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)'::regprocedure::oid
    ];
BEGIN
    FOR relation IN
        SELECT catalog.oid
        FROM pg_catalog.pg_class AS catalog
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = catalog.relnamespace
        WHERE namespace.nspname = 'public'
          AND catalog.relkind IN ('r', 'p', 'v', 'm', 'f')
    LOOP
        IF has_any_column_privilege(current_setting('yoao.asset_cleanup_role'),
                                    relation, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('yoao.asset_cleanup_role'),
                                  relation,
                                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'The Assets cleanup role has direct table access';
        END IF;
    END LOOP;

    IF NOT has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                                'yoao_assets_v1', 'USAGE')
       OR has_database_privilege(current_setting('yoao.asset_cleanup_role'),
                                 current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'public', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_assets_v1', 'CREATE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
                                     'yoao_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)',
                                     'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
                                     'yoao_assets_v1.complete_storage_deletion(uuid,uuid)',
                                     'EXECUTE')
       OR has_function_privilege(current_setting('yoao.asset_cleanup_role'),
                                 'yoao_assets_v1.pending_storage_deletions(uuid,integer)',
                                 'EXECUTE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_identity_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_workspace_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_labeling_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_jobs_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_learning_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_inference_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_insights_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_diagnostics_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_export_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_audit_v1', 'USAGE') THEN
        RAISE EXCEPTION 'The Assets cleanup role has access outside Assets';
    END IF;

    IF (SELECT count(*) FROM unnest(cleanup_functions) AS function(oid)
        WHERE has_function_privilege(current_setting('yoao.asset_cleanup_role'),function.oid,'EXECUTE')) <> 11 THEN
        RAISE EXCEPTION 'The Assets cleanup role is missing an approved cleanup function';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = proc.pronamespace
        WHERE (pg_catalog.left(namespace.nspname, 4) = 'aap_'
               OR pg_catalog.left(namespace.nspname, 5) = 'yoao_')
          AND pg_catalog.right(namespace.nspname, 3) = '_v1'
          AND NOT proc.oid = ANY(cleanup_functions)
          AND pg_catalog.has_function_privilege(
              current_setting('yoao.asset_cleanup_role'), proc.oid, 'EXECUTE'
          )
    ) THEN
        RAISE EXCEPTION 'The Assets cleanup role can execute outside its eleven cleanup functions';
    END IF;
END $$;
COMMIT;
