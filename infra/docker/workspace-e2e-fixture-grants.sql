-- Compose after workspace-consumer-grants.sql only for a trusted E2E fixture operator.
-- Ordinary API/worker credentials do not receive fixture creation or cleanup capabilities.
-- psql -v expected_database=... -v migration_role=... -v workspace_owner=...
--      -v consumer_role=... -f workspace-e2e-fixture-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.e2e_fixture_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('aap.e2e_fixture_operator');
    v_workspace name := current_setting('aap.workspace_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
       OR (right(lower(current_database()), 5) <> '_test'
           AND right(lower(current_database()), 4) <> '_e2e')
       OR v_consumer IN (current_user, v_workspace, 'identity_api_consumer')
       OR pg_has_role(v_consumer, v_workspace, 'MEMBER')
       OR pg_has_role(v_consumer, current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Workspace E2E fixture operator ownership';
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
        RAISE EXCEPTION 'E2E fixture operator requires non-admin, non-inheriting LOGIN';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM unnest(ARRAY['public.projects','public.project_templates']) AS private(name)
        WHERE has_table_privilege(v_consumer, private.name,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR has_any_column_privilege(v_consumer, private.name,
                  'SELECT,INSERT,UPDATE,REFERENCES')
    ) THEN
        RAISE EXCEPTION 'E2E fixture operator must use fixed Workspace capabilities';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class AS relation
        JOIN pg_roles AS owner ON owner.oid=relation.relowner
        WHERE relation.oid='aap_workspace_fixture_v1.templates'::regclass
          AND relation.relkind='v'
          AND owner.rolname=v_workspace
          AND COALESCE(relation.reloptions @> ARRAY['security_barrier=true'], false)
          AND ARRAY(
              SELECT attribute.attname::text
              FROM pg_attribute AS attribute
              WHERE attribute.attrelid=relation.oid
                AND attribute.attnum>0
                AND NOT attribute.attisdropped
              ORDER BY attribute.attnum
          )=ARRAY['id','display_id']
    ) THEN
        RAISE EXCEPTION 'Unexpected Workspace E2E template read relation';
    END IF;

    EXECUTE format('GRANT USAGE ON SCHEMA aap_workspace_fixture_v1 TO %I', v_consumer);
    EXECUTE format(
        'GRANT SELECT ON aap_workspace_fixture_v1.templates TO %I', v_consumer
    );
    FOREACH v_signature IN ARRAY ARRAY[
        'aap_workspace_fixture_v1.create_project(jsonb)',
        'aap_workspace_fixture_v1.create_templates(jsonb)',
        'aap_workspace_fixture_v1.guard_project_cleanup(uuid[])',
        'aap_workspace_fixture_v1.delete_projects(uuid[])',
        'aap_workspace_fixture_v1.delete_templates(boolean,text[])'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_proc AS function
            JOIN pg_roles AS owner ON owner.oid=function.proowner
            WHERE function.oid=v_signature::regprocedure
              AND function.prosecdef
              AND function.provolatile='v'
              AND owner.rolname=v_workspace
        ) THEN
            RAISE EXCEPTION 'Unexpected Workspace E2E fixture function owner or security mode';
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', v_signature, v_consumer);
    END LOOP;
END $profile$;
COMMIT;
