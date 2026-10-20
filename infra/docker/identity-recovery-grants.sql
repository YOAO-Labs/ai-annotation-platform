-- Grant only the three native recovery, four mail and one mail-settings functions.
-- Database provisioning must revoke PUBLIC TEMP first; this script changes no PUBLIC ACL.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v identity_owner=identity_code -v audit_owner=audit_code \
--   -v settings_owner=settings_code \
--   -v identity_recovery_role=identity_recovery_client \
--   -v identity_mail_role=identity_mail_client \
--   -v identity_mail_settings_role=identity_mail_settings_client \
--   -f infra/docker/identity-recovery-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config('aap.settings_owner', :'settings_owner', true);
SELECT set_config('aap.identity_recovery_role', :'identity_recovery_role', true);
SELECT set_config('aap.identity_mail_role', :'identity_mail_role', true);
SELECT set_config('aap.identity_mail_settings_role', :'identity_mail_settings_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    migration_oid oid;
    owner_oid oid;
    audit_oid oid;
    settings_oid oid;
    audit_function oid := pg_catalog.to_regprocedure(
        'aap_audit_v1.record_identity_recovery_event(text,jsonb,jsonb,jsonb)'
    )::oid;
    audit_context_function oid := pg_catalog.to_regprocedure(
        'aap_audit_v1.valid_request_context(jsonb)'
    )::oid;
    recovery_settings_function oid := pg_catalog.to_regprocedure(
        'aap_settings_v1.identity_recovery_policy(jsonb)'
    )::oid;
    mail_settings_function oid := pg_catalog.to_regprocedure(
        'aap_settings_v1.mail_delivery_policy(jsonb)'
    )::oid;
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.audit_owner'),
        current_setting('aap.settings_owner'),
        current_setting('aap.identity_recovery_role'),
        current_setting('aap.identity_mail_role'),
        current_setting('aap.identity_mail_settings_role')
    ];
    client_roles text[] := ARRAY[
        current_setting('aap.identity_recovery_role'),
        current_setting('aap.identity_mail_role'),
        current_setting('aap.identity_mail_settings_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 6
       OR current_setting('aap.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, Identity, Audit, Settings and client roles must be distinct';
    END IF;
    IF current_setting('aap.identity_recovery_role') <> 'identity_recovery_client'
       OR current_setting('aap.identity_mail_role') <> 'identity_mail_client'
       OR current_setting('aap.identity_mail_settings_role') <> 'identity_mail_settings_client' THEN
        RAISE EXCEPTION 'Recovery and credential mail must use their fixed LOGIN roles';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe Identity recovery role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing Identity recovery role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Identity recovery roles must be restricted and have no memberships: %', role_name;
        END IF;
    END LOOP;

    SELECT role.oid INTO migration_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.migration_role');
    SELECT role.oid INTO owner_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.identity_owner');
    SELECT role.oid INTO audit_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.audit_owner');
    SELECT role.oid INTO settings_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.settings_owner');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Missing Identity, Audit or Settings code owner';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid IN (owner_oid, audit_oid, settings_oid)
          AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY(client_roles)
          AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Code owners must be NOLOGIN and recovery/mail clients LOGIN NOINHERIT';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'aap_identity_recovery_v1'
          AND namespace.nspowner = migration_oid
    ) THEN
        RAISE EXCEPTION 'Identity recovery schema must remain owned by the migration role';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'aap_identity_recovery_v1'
          AND function.prokind = 'f' AND function.proowner <> owner_oid
    ) THEN
        RAISE EXCEPTION 'Identity recovery functions must be owned by the Identity code owner';
    END IF;
    IF audit_function IS NULL OR audit_context_function IS NULL
       OR recovery_settings_function IS NULL OR mail_settings_function IS NULL
       OR NOT pg_catalog.has_function_privilege(owner_oid, audit_function, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(owner_oid, audit_context_function, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(owner_oid, recovery_settings_function, 'EXECUTE')
       OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_proc AS function
           WHERE function.oid = audit_function AND function.proowner = audit_oid
    ) OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_proc AS function
           WHERE function.oid = recovery_settings_function
             AND function.proowner = settings_oid
       ) OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_proc AS function
           WHERE function.oid = mail_settings_function
             AND function.proowner = settings_oid
       ) OR pg_catalog.has_function_privilege(owner_oid, mail_settings_function, 'EXECUTE') THEN
        RAISE EXCEPTION 'Identity recovery owner lacks its typed Audit or Settings capability';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        settings_oid,
        mail_settings_function,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        current_setting('aap.identity_mail_settings_role'),
        mail_settings_function,
        'EXECUTE'
    ) OR pg_catalog.has_function_privilege(
        current_setting('aap.identity_mail_settings_role'),
        recovery_settings_function,
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Mail settings must remain a separate single Settings capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_recovery_role", :"identity_mail_role", :"identity_mail_settings_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_recovery_role", :"identity_mail_role", :"identity_mail_settings_role";
DO $$
DECLARE
    namespace record;
    role_name text;
    client_roles text[] := ARRAY[
        current_setting('aap.identity_recovery_role'),
        current_setting('aap.identity_mail_role'),
        current_setting('aap.identity_mail_settings_role')
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

GRANT USAGE ON SCHEMA aap_identity_recovery_v1
    TO :"identity_recovery_role", :"identity_mail_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_recovery_v1.recovery_policy(),
    aap_identity_recovery_v1.prepare_recovery_command(jsonb),
    aap_identity_recovery_v1.apply_recovery_command(jsonb)
    TO :"identity_recovery_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_recovery_v1.mail_policy(),
    aap_identity_recovery_v1.claim_mail(jsonb),
    aap_identity_recovery_v1.renew_mail(jsonb),
    aap_identity_recovery_v1.finish_mail(jsonb)
    TO :"identity_mail_role";
GRANT USAGE ON SCHEMA aap_settings_v1 TO :"identity_mail_settings_role";
GRANT EXECUTE ON FUNCTION aap_settings_v1.mail_delivery_policy(jsonb)
    TO :"identity_mail_settings_role";

DO $$
DECLARE
    role_entry record;
    role_oid oid;
    allowed_functions oid[];
    callable_count integer;
    recovery_schema oid := pg_catalog.to_regnamespace('aap_identity_recovery_v1');
    settings_schema oid := pg_catalog.to_regnamespace('aap_settings_v1');
BEGIN
    IF recovery_schema IS NULL OR settings_schema IS NULL THEN
        RAISE EXCEPTION 'Identity recovery or Settings schema is missing';
    END IF;
    FOR role_entry IN
        SELECT role_name, allowed_schema, allowed_functions
        FROM (VALUES
            (
                current_setting('aap.identity_recovery_role'),
                'aap_identity_recovery_v1'::text,
                ARRAY[
                    pg_catalog.to_regprocedure('aap_identity_recovery_v1.recovery_policy()')::oid,
                    pg_catalog.to_regprocedure('aap_identity_recovery_v1.prepare_recovery_command(jsonb)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_recovery_v1.apply_recovery_command(jsonb)')::oid
                ]::oid[]
            ),
            (
                current_setting('aap.identity_mail_role'),
                'aap_identity_recovery_v1'::text,
                ARRAY[
                    pg_catalog.to_regprocedure('aap_identity_recovery_v1.mail_policy()')::oid,
                    pg_catalog.to_regprocedure('aap_identity_recovery_v1.claim_mail(jsonb)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_recovery_v1.renew_mail(jsonb)')::oid,
                    pg_catalog.to_regprocedure('aap_identity_recovery_v1.finish_mail(jsonb)')::oid
                ]::oid[]
            ),
            (
                current_setting('aap.identity_mail_settings_role'),
                'aap_settings_v1'::text,
                ARRAY[pg_catalog.to_regprocedure('aap_settings_v1.mail_delivery_policy(jsonb)')::oid]::oid[]
            )
        ) AS expected(role_name, allowed_schema, allowed_functions)
    LOOP
        SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_entry.role_name;
        IF pg_catalog.array_position(role_entry.allowed_functions, NULL::oid) IS NOT NULL
           OR pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
           OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
            RAISE EXCEPTION 'Unsafe or incomplete Identity recovery client: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
              AND namespace.nspname <> role_entry.allowed_schema
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
        ) THEN
            RAISE EXCEPTION 'Identity recovery client has a foreign module schema: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
        ) THEN
            RAISE EXCEPTION 'Identity recovery client can create schema objects: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            CROSS JOIN (VALUES ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_table_privilege(role_oid, relation.oid, privilege.name)
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            CROSS JOIN (VALUES ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_any_column_privilege(role_oid, relation.oid, privilege.name)
        ) THEN
            RAISE EXCEPTION 'Identity recovery client has direct table privileges: %', role_entry.role_name;
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
            RAISE EXCEPTION 'Identity recovery client has direct sequence privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
            WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
              AND (namespace.nspname <> role_entry.allowed_schema
                   OR function.oid <> ALL (role_entry.allowed_functions))
              AND function.prokind IN ('f', 'p')
              AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE')
        ) THEN
            RAISE EXCEPTION 'Identity recovery client has an unapproved module function: %', role_entry.role_name;
        END IF;
        SELECT pg_catalog.count(*) INTO callable_count
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = role_entry.allowed_schema
          AND function.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
        IF callable_count <> pg_catalog.cardinality(role_entry.allowed_functions) THEN
            RAISE EXCEPTION 'Identity recovery client has the wrong function count: %', role_entry.role_name;
        END IF;
    END LOOP;
END $$;
COMMIT;
