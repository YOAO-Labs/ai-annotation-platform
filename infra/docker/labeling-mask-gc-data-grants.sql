-- Grant the isolated Mask GC worker only its six Labeling operation functions.
-- The role must be provisioned as a LOGIN with no memberships before this runs.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v labeling_owner=labeling_code -v mask_gc_role=labeling_mask_gc_client
--   -f infra/docker/labeling-mask-gc-data-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.mask_gc_role', :'mask_gc_role', true);

DO $$
DECLARE
    v_gc oid;
    v_owner oid;
BEGIN
    SELECT oid INTO v_gc FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('aap.mask_gc_role')
      AND rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    SELECT oid INTO v_owner FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('aap.labeling_owner')
      AND NOT rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
       OR v_gc IS NULL OR v_owner IS NULL OR v_gc = v_owner
       OR pg_catalog.pg_has_role(v_gc, v_owner, 'MEMBER') THEN
        RAISE EXCEPTION 'Unexpected database, caller or Labeling GC roles';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = v_gc OR membership.roleid = v_gc
    ) THEN
        RAISE EXCEPTION 'The Labeling Mask GC role must have no role memberships';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"mask_gc_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"mask_gc_role";
REVOKE ALL ON SCHEMA public, aap_labeling_v1 FROM :"mask_gc_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"mask_gc_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"mask_gc_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public, aap_labeling_v1 FROM :"mask_gc_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_labeling_v1 FROM PUBLIC;
GRANT USAGE ON SCHEMA aap_labeling_v1 TO :"mask_gc_role";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.claim_mask_gc_sweep(jsonb),
    aap_labeling_v1.stage_mask_gc_page(jsonb),
    aap_labeling_v1.claim_mask_gc_deletes(jsonb),
    aap_labeling_v1.finish_mask_gc_delete(jsonb),
    aap_labeling_v1.read_mask_gc_progress(jsonb),
    aap_labeling_v1.inspect_mask_gc_page(jsonb)
    TO :"mask_gc_role";

DO $$
DECLARE
    role_name text := current_setting('aap.mask_gc_role');
    v_callable integer;
BEGIN
    IF has_schema_privilege(role_name, 'public', 'CREATE')
       OR has_schema_privilege(role_name, 'aap_labeling_v1', 'CREATE') THEN
        RAISE EXCEPTION 'The Labeling Mask GC role has direct schema access';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              has_table_privilege(
                  role_name, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
              OR has_any_column_privilege(role_name, relation.oid, 'SELECT')
              OR has_any_column_privilege(role_name, relation.oid, 'INSERT')
              OR has_any_column_privilege(role_name, relation.oid, 'UPDATE')
              OR has_any_column_privilege(role_name, relation.oid, 'REFERENCES')
          )
    ) THEN
        RAISE EXCEPTION 'The Labeling Mask GC role has raw table privileges';
    END IF;
    SELECT count(*) INTO v_callable
    FROM pg_catalog.pg_proc AS proc
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = proc.pronamespace
    WHERE namespace.nspname = 'aap_labeling_v1'
      AND proc.prokind = 'f'
      AND has_function_privilege(role_name, proc.oid, 'EXECUTE');
    IF v_callable <> 6
       OR has_function_privilege(
            role_name, 'aap_labeling_v1.read_mask_write_epochs(jsonb)', 'EXECUTE'
       )
       OR has_function_privilege(
            role_name, 'aap_labeling_v1.begin_mask_content_write(jsonb)', 'EXECUTE'
       )
       OR has_function_privilege(
            role_name, 'aap_labeling_v1.lock_mask_write_epochs(jsonb)', 'EXECUTE'
       ) THEN
        RAISE EXCEPTION 'The Labeling Mask GC role must execute only the six GC operations';
    END IF;
END $$;
COMMIT;
