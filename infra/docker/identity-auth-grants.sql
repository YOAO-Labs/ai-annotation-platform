-- Grant the four narrow Identity LOGIN surfaces after migrations and
-- module-data-grants. Roles must already exist and have no memberships.
-- Database provisioning must revoke PUBLIC TEMP first; this script changes no PUBLIC ACL.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v identity_owner=identity_code \
--   -v identity_auth_role=identity_auth_client \
--   -v identity_command_role=identity_command_client \
--   -v identity_fence_role=identity_fence_client \
--   -v identity_operator_role=identity_session_operator \
--   -f infra/docker/identity-auth-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.identity_auth_role', :'identity_auth_role', true);
SELECT set_config('aap.identity_command_role', :'identity_command_role', true);
SELECT set_config('aap.identity_fence_role', :'identity_fence_role', true);
SELECT set_config('aap.identity_operator_role', :'identity_operator_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.identity_auth_role'),
        current_setting('aap.identity_command_role'),
        current_setting('aap.identity_fence_role'),
        current_setting('aap.identity_operator_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 5
       OR current_setting('aap.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, Identity owner and client roles must be distinct';
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

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.identity_owner')
          AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY(ARRAY[
            current_setting('aap.identity_auth_role'),
            current_setting('aap.identity_command_role'),
            current_setting('aap.identity_fence_role'),
            current_setting('aap.identity_operator_role')
        ]) AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Identity owner must be NOLOGIN and clients LOGIN NOINHERIT';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_auth_role", :"identity_command_role",
         :"identity_fence_role", :"identity_operator_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_auth_role", :"identity_command_role",
       :"identity_fence_role", :"identity_operator_role";
DO $$
DECLARE
    namespace record;
    role_name text;
    client_roles text[] := ARRAY[
        current_setting('aap.identity_auth_role'),
        current_setting('aap.identity_command_role'),
        current_setting('aap.identity_fence_role'),
        current_setting('aap.identity_operator_role')
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
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
            );
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name
            );
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name
            );
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name
            );
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA aap_identity_v1 TO
    :"identity_auth_role", :"identity_command_role",
    :"identity_fence_role", :"identity_operator_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_v1.authentication_policy(),
    aap_identity_v1.read_jwt_principal(uuid, uuid, bigint, timestamptz, boolean),
    aap_identity_v1.read_api_key_candidates(text, uuid, integer),
    aap_identity_v1.confirm_api_key_principal(uuid, text)
    TO :"identity_auth_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_v1.get_credential_generation(uuid),
    aap_identity_v1.advance_credential_generation(uuid),
    aap_identity_v1.revoke_jti(uuid, timestamptz),
    aap_identity_v1.is_jti_revoked(uuid),
    aap_identity_v1.retire_account_credentials(uuid)
    TO :"identity_command_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_v1.lock_current_principal(uuid, text, uuid, bigint, timestamptz, text[])
    TO :"identity_fence_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_v1.purge_expired_revocations(integer)
    TO :"identity_operator_role";

-- Fail closed unless each LOGIN has only its exact Identity operation surface.
DO $$
DECLARE
    role_entry record;
    role_oid oid;
    callable_count integer;
BEGIN
    FOR role_entry IN
        SELECT role_name, allowed_functions
        FROM (VALUES
            (
                current_setting('aap.identity_auth_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('aap_identity_v1.authentication_policy()')::oid,
                    pg_catalog.to_regprocedure('aap_identity_v1.read_jwt_principal(uuid,uuid,bigint,timestamptz,boolean)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_v1.read_api_key_candidates(text,uuid,integer)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_v1.confirm_api_key_principal(uuid,text)')::oid
                ]::oid[]
            ),
            (
                current_setting('aap.identity_command_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('aap_identity_v1.get_credential_generation(uuid)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_v1.advance_credential_generation(uuid)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_v1.revoke_jti(uuid,timestamptz)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_v1.is_jti_revoked(uuid)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_v1.retire_account_credentials(uuid)')::oid
                ]::oid[]
            ),
            (
                current_setting('aap.identity_fence_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('aap_identity_v1.lock_current_principal(uuid,text,uuid,bigint,timestamptz,text[])')::oid
                ]::oid[]
            ),
            (
                current_setting('aap.identity_operator_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('aap_identity_v1.purge_expired_revocations(integer)')::oid
                ]::oid[]
            )
        ) AS expected(role_name, allowed_functions)
    LOOP
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_entry.role_name;

        IF pg_catalog.array_position(role_entry.allowed_functions, NULL::oid) IS NOT NULL THEN
            RAISE EXCEPTION 'Identity protocol function is missing for role %', role_entry.role_name;
        END IF;
        IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
           OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
            RAISE EXCEPTION 'Identity LOGIN has database CREATE or TEMP privilege: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS ns
            WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
              AND ns.nspname <> 'aap_identity_v1'
              AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
        ) THEN
            RAISE EXCEPTION 'Identity LOGIN has access to a foreign module schema: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS ns
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
              AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
        ) THEN
            RAISE EXCEPTION 'Identity LOGIN can create schema objects: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS ns
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
            CROSS JOIN (VALUES
                ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
                ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
            ) AS privilege(name)
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_table_privilege(role_oid, relation.oid, privilege.name)
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS ns
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
            CROSS JOIN (VALUES
                ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')
            ) AS privilege(name)
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_any_column_privilege(role_oid, relation.oid, privilege.name)
        ) THEN
            RAISE EXCEPTION 'Identity LOGIN has direct table privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS ns
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind = 'S'
              AND (
                  pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'USAGE')
                  OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'SELECT')
                  OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'UPDATE')
              )
        ) THEN
            RAISE EXCEPTION 'Identity LOGIN has direct sequence privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS proc
            JOIN pg_catalog.pg_namespace AS ns ON ns.oid = proc.pronamespace
            WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
              AND (ns.nspname <> 'aap_identity_v1'
                   OR proc.oid <> ALL (role_entry.allowed_functions))
              AND proc.prokind IN ('f', 'p')
              AND pg_catalog.has_function_privilege(role_oid, proc.oid, 'EXECUTE')
        ) THEN
            RAISE EXCEPTION 'Identity LOGIN has an unapproved module function grant: %', role_entry.role_name;
        END IF;
        SELECT pg_catalog.count(*) INTO callable_count
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = proc.pronamespace
        WHERE ns.nspname = 'aap_identity_v1'
          AND proc.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, proc.oid, 'EXECUTE');
        IF callable_count <> pg_catalog.cardinality(role_entry.allowed_functions) THEN
            RAISE EXCEPTION 'Identity LOGIN has the wrong function count: %', role_entry.role_name;
        END IF;
    END LOOP;
END $$;
COMMIT;
