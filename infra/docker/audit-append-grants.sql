-- Grant one client the versioned append command without table/sequence access.
-- Run after migrations, module-data-grants.sql and lan-production-grants.sql.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v audit_owner=audit_code -v api_role=api_runtime \
--   -v append_client=audit_append_client \
--   -f infra/docker/audit-append-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.append_client', :'append_client', true);

DO $$
DECLARE
    owner_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('aap.audit_owner'));
    api_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('aap.api_role'));
    client_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('aap.append_client'));
    append_function oid := pg_catalog.to_regprocedure('aap_audit_v1.append_events(jsonb)')::oid;
    audit_sequence oid := pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id')::regclass::oid;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF owner_oid IS NULL OR api_oid IS NULL OR client_oid IS NULL
       OR append_function IS NULL OR owner_oid = api_oid
       OR owner_oid = client_oid OR api_oid = client_oid THEN
        RAISE EXCEPTION 'Audit owner, API role, append client, or function is missing';
    END IF;
    IF (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
        FROM pg_roles WHERE oid = owner_oid) THEN
        RAISE EXCEPTION 'Audit owner must be a restricted NOLOGIN role';
    END IF;
    IF NOT (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
            AND NOT rolreplication AND NOT rolbypassrls AND NOT rolinherit
            FROM pg_roles WHERE oid = client_oid) THEN
        RAISE EXCEPTION 'Audit append client must be a restricted LOGIN NOINHERIT role';
    END IF;
    IF NOT (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
            AND NOT rolreplication AND NOT rolbypassrls
            FROM pg_roles WHERE oid = api_oid) THEN
        RAISE EXCEPTION 'API role must be a restricted LOGIN';
    END IF;
    IF EXISTS (
        SELECT FROM pg_auth_members
        WHERE member IN (client_oid, api_oid) OR roleid IN (client_oid, api_oid)
    ) THEN
        RAISE EXCEPTION 'API and append client roles must not have memberships';
    END IF;
    IF pg_catalog.has_table_privilege(client_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(client_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'Audit append client has direct audit_logs privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace = 'public'::regnamespace
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              pg_catalog.has_table_privilege(
                  client_oid, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
              OR pg_catalog.has_any_column_privilege(
                  client_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
              )
          )
    ) THEN
        RAISE EXCEPTION 'Audit append client has privileges on another public relation';
    END IF;
    IF pg_catalog.has_sequence_privilege(client_oid, audit_sequence, 'USAGE')
       OR pg_catalog.has_sequence_privilege(client_oid, audit_sequence, 'SELECT')
       OR pg_catalog.has_sequence_privilege(client_oid, audit_sequence, 'UPDATE') THEN
        RAISE EXCEPTION 'Audit append client has direct sequence privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS public_sequence
        WHERE public_sequence.relnamespace = 'public'::regnamespace
          AND CASE WHEN public_sequence.relkind = 'S'
              THEN pg_catalog.has_sequence_privilege(
                  client_oid, public_sequence.oid, 'USAGE,SELECT,UPDATE'
              )
              ELSE false END
    ) THEN
        RAISE EXCEPTION 'Audit append client has privileges on a public sequence';
    END IF;
    IF pg_catalog.has_database_privilege(client_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(client_oid, current_database(), 'TEMP')
       OR pg_catalog.has_schema_privilege(client_oid, 'public', 'CREATE')
       OR pg_catalog.has_schema_privilege(client_oid, 'aap_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Audit append client has database or schema creation privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname LIKE 'aap\_%' ESCAPE '\'
          AND namespace.nspname <> 'aap_audit_v1'
          AND pg_catalog.has_schema_privilege(client_oid, namespace.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Audit append client has a foreign module schema grant';
    END IF;
    IF NOT (SELECT prosecdef
            FROM pg_proc WHERE oid = append_function AND proowner = owner_oid) THEN
        RAISE EXCEPTION 'Audit append function must be owned by the Audit code role';
    END IF;
    IF NOT pg_catalog.has_table_privilege(api_oid, 'public.audit_logs', 'SELECT') THEN
        RAISE EXCEPTION 'API role must retain its current Audit SELECT surface';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_partition_tree('public.audit_logs') AS partition
        JOIN pg_catalog.pg_class AS relation ON relation.oid = partition.relid
        WHERE relation.relowner = api_oid
    ) THEN
        RAISE EXCEPTION 'API role owns audit_logs or a partition; partition ownership must move before strict append grants';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_default_acl AS defaults
        CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
        WHERE defaults.defaclrole = (
            SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_setting('aap.audit_owner')
        )
          AND defaults.defaclnamespace IN (0, 'public'::regnamespace)
          AND defaults.defaclobjtype = 'r'
          AND acl.grantee IN (api_oid, 0)
          AND acl.privilege_type IN (
              'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
              'REFERENCES', 'TRIGGER'
          )
    ) THEN
        RAISE EXCEPTION 'API role or PUBLIC has default access to future Audit partitions';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_default_acl AS defaults
        CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
        WHERE defaults.defaclrole = (
            SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_setting('aap.audit_owner')
        )
          AND defaults.defaclnamespace IN (0, 'public'::regnamespace)
          AND defaults.defaclobjtype = 'S'
          AND acl.grantee IN (api_oid, 0)
          AND acl.privilege_type IN ('USAGE', 'SELECT', 'UPDATE')
    ) THEN
        RAISE EXCEPTION 'API role or PUBLIC has default access to future Audit sequences';
    END IF;
END $$;

-- Keep the parent read used by existing consumers; Audit writes use owner functions.
REVOKE INSERT ON TABLE public.audit_logs FROM :"api_role";
DO $$
DECLARE
    v_columns text;
    v_partition record;
BEGIN
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname), ',' ORDER BY attnum)
    INTO v_columns
    FROM pg_catalog.pg_attribute
    WHERE attrelid = 'public.audit_logs'::regclass
      AND attnum > 0 AND NOT attisdropped;
    EXECUTE pg_catalog.format(
        'REVOKE INSERT (%1$s) ON TABLE public.audit_logs FROM %I',
        v_columns, current_setting('aap.api_role')
    );
    FOR v_partition IN
        SELECT relation.oid, namespace.nspname, relation.relname
        FROM pg_catalog.pg_partition_tree('public.audit_logs') AS partition
        JOIN pg_catalog.pg_class AS relation ON relation.oid = partition.relid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE relation.oid <> 'public.audit_logs'::regclass
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE INSERT ON TABLE %I.%I FROM %I',
            v_partition.nspname, v_partition.relname, current_setting('aap.api_role')
        );
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname), ',' ORDER BY attnum)
        INTO v_columns
        FROM pg_catalog.pg_attribute
        WHERE attrelid = v_partition.oid AND attnum > 0 AND NOT attisdropped;
        EXECUTE pg_catalog.format(
            'REVOKE INSERT (%s) ON TABLE %I.%I FROM %I',
            v_columns, v_partition.nspname, v_partition.relname,
            current_setting('aap.api_role')
        );
    END LOOP;
    EXECUTE pg_catalog.format(
        'REVOKE USAGE, UPDATE ON SEQUENCE %s FROM %I',
        pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id'),
        current_setting('aap.api_role')
    );
END $$;
REVOKE ALL ON SCHEMA aap_audit_v1 FROM :"append_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_audit_v1 FROM :"append_client";
REVOKE ALL ON TABLE public.audit_logs FROM :"append_client";
DO $$
BEGIN
    EXECUTE format(
        'REVOKE ALL ON SEQUENCE %s FROM %I',
        pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id'),
        current_setting('aap.append_client')
    );
END $$;
GRANT CONNECT ON DATABASE :"expected_database" TO :"append_client";
GRANT USAGE ON SCHEMA aap_audit_v1 TO :"append_client";
GRANT EXECUTE ON FUNCTION aap_audit_v1.append_events(jsonb) TO :"append_client";
GRANT USAGE ON SCHEMA aap_audit_v1 TO :"api_role";
GRANT EXECUTE ON FUNCTION aap_audit_v1.append_events(jsonb) TO :"api_role";

DO $$
DECLARE
    api_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('aap.api_role'));
    client_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('aap.append_client'));
    append_function oid := pg_catalog.to_regprocedure('aap_audit_v1.append_events(jsonb)')::oid;
    audit_sequence oid := pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id')::regclass::oid;
BEGIN
    IF NOT pg_catalog.has_database_privilege(client_oid, current_database(), 'CONNECT')
       OR NOT pg_catalog.has_schema_privilege(client_oid, 'aap_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_function_privilege(client_oid, append_function, 'EXECUTE')
       OR pg_catalog.has_table_privilege(client_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(client_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_sequence_privilege(client_oid, audit_sequence, 'USAGE')
       OR pg_catalog.has_sequence_privilege(client_oid, audit_sequence, 'SELECT')
       OR pg_catalog.has_sequence_privilege(client_oid, audit_sequence, 'UPDATE') THEN
        RAISE EXCEPTION 'Audit append client does not have the exact function-only profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace = 'public'::regnamespace
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              pg_catalog.has_table_privilege(
                  client_oid, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
              OR pg_catalog.has_any_column_privilege(
                  client_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
              )
          )
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_class AS public_sequence
        WHERE public_sequence.relnamespace = 'public'::regnamespace
          AND CASE WHEN public_sequence.relkind = 'S'
              THEN pg_catalog.has_sequence_privilege(
                  client_oid, public_sequence.oid, 'USAGE,SELECT,UPDATE'
              )
              ELSE false END
    ) THEN
        RAISE EXCEPTION 'Audit append client has privileges outside its function';
    END IF;
    IF NOT pg_catalog.has_table_privilege(api_oid, 'public.audit_logs', 'SELECT')
       OR NOT pg_catalog.has_schema_privilege(api_oid, 'aap_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_function_privilege(api_oid, append_function, 'EXECUTE')
       OR pg_catalog.has_table_privilege(api_oid, 'public.audit_logs',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(api_oid, 'public.audit_logs',
            'INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_sequence_privilege(api_oid, audit_sequence, 'USAGE,SELECT,UPDATE') THEN
        RAISE EXCEPTION 'API role must retain Audit parent SELECT and lose direct writes';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_partition_tree('public.audit_logs') AS partition
        WHERE partition.relid <> 'public.audit_logs'::pg_catalog.regclass
          AND (
              pg_catalog.has_table_privilege(api_oid, partition.relid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR pg_catalog.has_any_column_privilege(api_oid, partition.relid,
                  'SELECT,INSERT,UPDATE,REFERENCES')
          )
    ) THEN
        RAISE EXCEPTION 'API role retains direct access to an existing Audit partition';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS schema ON schema.oid = function.pronamespace
        WHERE schema.nspname = 'aap_audit_v1'
          AND pg_catalog.has_function_privilege(client_oid, function.oid, 'EXECUTE')
          AND function.oid <> append_function
    ) THEN
        RAISE EXCEPTION 'Audit append client has an extra Audit function grant';
    END IF;
END $$;
COMMIT;
