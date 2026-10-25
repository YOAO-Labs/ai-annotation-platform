-- Compose after workspace-consumer-grants.sql only for a trusted seed operator.
-- Ordinary API/worker credentials do not receive these six fixed writes.
-- psql -v expected_database=... -v migration_role=... -v workspace_owner=...
--      -v consumer_role=... -f workspace-seed-operator-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.seed_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('aap.seed_operator');
    v_workspace name := current_setting('aap.workspace_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
       OR v_consumer IN (current_user, v_workspace, 'identity_api_consumer')
       OR pg_has_role(v_consumer, v_workspace, 'MEMBER')
       OR pg_has_role(v_consumer, current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Workspace seed operator ownership';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_consumer AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members
        WHERE member=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
           OR (roleid=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
               AND member<>(SELECT oid FROM pg_roles
                            WHERE rolname=current_setting('aap.migration_role')))
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_workspace AND NOT rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Seed operator requires non-admin, non-inheriting LOGIN';
    END IF;
    IF has_table_privilege(v_consumer, 'public.projects',
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR has_any_column_privilege(v_consumer, 'public.projects',
        'SELECT,INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'Apply the ordinary Workspace consumer fence first';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)',
        'aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)',
        'aap_workspace_v1.write_seed_members(jsonb)',
        'aap_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)',
        'aap_workspace_v1.write_seed_project_projection(uuid,jsonb)',
        'aap_workspace_v1.pin_seed_member_assignment(uuid,timestamptz)'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner
            WHERE p.oid=v_signature::regprocedure AND p.prosecdef
              AND owner.rolname=v_workspace
        ) THEN
            RAISE EXCEPTION 'Unexpected seed function owner or security mode';
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', v_signature, v_consumer);
    END LOOP;
END $profile$;
COMMIT;
