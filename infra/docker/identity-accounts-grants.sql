-- Grant the exact self-account and presence function surfaces after migrations
-- and module-data-grants. The restricted LOGIN roles must have no memberships.
-- Database provisioning must revoke PUBLIC TEMP first; this script changes no PUBLIC ACL.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v identity_owner=identity_code \
--   -v identity_account_role=identity_account_client \
--   -v identity_presence_role=identity_presence_client \
--   -f infra/docker/identity-accounts-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.identity_account_role', :'identity_account_role', true);
SELECT set_config('aap.identity_presence_role', :'identity_presence_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    owner_oid oid;
    migration_oid oid;
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.identity_account_role'),
        current_setting('aap.identity_presence_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 3
       OR current_setting('aap.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, Identity owner, account and presence roles must be distinct';
    END IF;
    IF current_setting('aap.identity_account_role') <> 'identity_account_client'
       OR current_setting('aap.identity_presence_role') <> 'identity_presence_client' THEN
        RAISE EXCEPTION 'Self-account and presence must use their restricted LOGIN roles';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe Identity role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Identity roles must have no role memberships: %', role_name;
        END IF;
    END LOOP;

    SELECT role.oid INTO owner_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.identity_owner');
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid = owner_oid AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY(ARRAY[
            current_setting('aap.identity_account_role'),
            current_setting('aap.identity_presence_role')
        ]) AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Identity owner must be NOLOGIN and clients LOGIN NOINHERIT';
    END IF;
    SELECT role.oid INTO migration_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.migration_role');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Missing migration role';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'aap_identity_accounts_v1'
          AND namespace.nspowner = migration_oid
    ) THEN
        RAISE EXCEPTION 'Identity account schema must remain owned by the migration role';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'aap_identity_accounts_v1'
          AND function.prokind = 'f' AND function.proowner <> owner_oid
    ) THEN
        RAISE EXCEPTION 'Identity account functions must be owned by the Identity code owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        owner_oid,
        pg_catalog.to_regprocedure(
            'aap_audit_v1.record_identity_account_event(text,jsonb,jsonb,jsonb)'
        )::oid,
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity code owner cannot call the Audit-owned account event capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_account_role", :"identity_presence_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_account_role", :"identity_presence_role";
DO $$
DECLARE
    namespace record;
    role_name text;
    client_roles text[] := ARRAY[
        current_setting('aap.identity_account_role'),
        current_setting('aap.identity_presence_role')
    ];
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        FOREACH role_name IN ARRAY client_roles LOOP
            EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA aap_identity_accounts_v1
    TO :"identity_account_role", :"identity_presence_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_accounts_v1.account_policy(),
    aap_identity_accounts_v1.prepare_account_command(jsonb),
    aap_identity_accounts_v1.apply_account_command(jsonb)
    TO :"identity_account_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)
    TO :"identity_presence_role";

-- Fail closed unless each LOGIN has exactly its designated function surface.
DO $$
DECLARE
    role_entry record;
    role_oid oid;
    audit_function oid := pg_catalog.to_regprocedure(
        'aap_audit_v1.record_identity_account_event(text,jsonb,jsonb,jsonb)'
    )::oid;
    callable_count integer;
BEGIN
    IF audit_function IS NULL THEN
        RAISE EXCEPTION 'Identity account Audit function is missing';
    END IF;
    FOR role_entry IN
        SELECT role_name, allowed_functions
        FROM (VALUES
            (
                current_setting('aap.identity_account_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('aap_identity_accounts_v1.account_policy()')::oid,
                    pg_catalog.to_regprocedure('aap_identity_accounts_v1.prepare_account_command(jsonb)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_accounts_v1.apply_account_command(jsonb)')::oid
                ]::oid[]
            ),
            (
                current_setting('aap.identity_presence_role'),
                ARRAY[
                    pg_catalog.to_regprocedure(
                        'aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)'
                    )::oid
                ]::oid[]
            )
        ) AS expected(role_name, allowed_functions)
    LOOP
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_entry.role_name;
        IF pg_catalog.array_position(role_entry.allowed_functions, NULL::oid) IS NOT NULL THEN
            RAISE EXCEPTION 'Identity account protocol function is missing for %', role_entry.role_name;
        END IF;
        IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
           OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
            RAISE EXCEPTION 'Identity account LOGIN has database CREATE or TEMP: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
              AND namespace.nspname <> 'aap_identity_accounts_v1'
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN has a foreign module schema: %', role_entry.role_name;
        END IF;
        IF pg_catalog.has_schema_privilege(role_oid, 'aap_audit_v1', 'USAGE')
           OR pg_catalog.has_function_privilege(role_oid, audit_function, 'EXECUTE') THEN
            RAISE EXCEPTION 'Identity account LOGIN has direct Audit capability: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN can create schema objects: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            CROSS JOIN (VALUES
                ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
                ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
            ) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_table_privilege(role_oid, relation.oid, privilege.name)
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            CROSS JOIN (VALUES
                ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')
            ) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_any_column_privilege(role_oid, relation.oid, privilege.name)
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN has direct table privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind = 'S'
              AND (pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'USAGE')
                   OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'SELECT')
                   OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'UPDATE'))
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN has direct sequence privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
            WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
              AND (namespace.nspname <> 'aap_identity_accounts_v1'
                   OR function.oid <> ALL (role_entry.allowed_functions))
              AND function.prokind IN ('f', 'p')
              AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE')
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN has an unapproved module function: %', role_entry.role_name;
        END IF;
        SELECT pg_catalog.count(*) INTO callable_count
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'aap_identity_accounts_v1'
          AND function.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
        IF callable_count <> pg_catalog.cardinality(role_entry.allowed_functions) THEN
            RAISE EXCEPTION 'Identity account LOGIN has the wrong function count: %', role_entry.role_name;
        END IF;
    END LOOP;
END $$;
COMMIT;
