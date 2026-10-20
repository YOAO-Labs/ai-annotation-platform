-- Grant the exact Identity self/due and Notifications publisher LOGIN surfaces.
-- Run after migrations and module-data-grants as the migration role. Each client
-- must be a distinct LOGIN NOINHERIT role with no memberships.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v identity_owner=identity_code -v audit_owner=audit_code \
--   -v notifications_owner=notifications_code \
--   -v identity_deactivation_role=identity_deactivation_client \
--   -v identity_deactivation_operator_role=identity_deactivation_operator \
--   -v notification_publish_role=notification_publish_client \
--   -f infra/docker/identity-deactivation-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config('aap.notifications_owner', :'notifications_owner', true);
SELECT set_config('aap.identity_deactivation_role', :'identity_deactivation_role', true);
SELECT set_config(
    'aap.identity_deactivation_operator_role', :'identity_deactivation_operator_role', true
);
SELECT set_config('aap.notification_publish_role', :'notification_publish_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    identity_oid oid;
    audit_oid oid;
    notifications_oid oid;
    migration_oid oid;
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.audit_owner'),
        current_setting('aap.notifications_owner'),
        current_setting('aap.identity_deactivation_role'),
        current_setting('aap.identity_deactivation_operator_role'),
        current_setting('aap.notification_publish_role')
    ];
    expected_database text := current_setting('aap.expected_database');
BEGIN
    IF current_database() <> expected_database
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 6
       OR current_setting('aap.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, owners and deactivation clients must be distinct';
    END IF;
    IF current_setting('aap.identity_deactivation_role') <> 'identity_deactivation_client'
       OR current_setting('aap.identity_deactivation_operator_role') <> 'identity_deactivation_operator'
       OR current_setting('aap.notification_publish_role') <> 'notification_publish_client' THEN
        RAISE EXCEPTION 'Unexpected deactivation client role name';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe deactivation role name';
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role WHERE role.rolname = role_name;
        IF NOT FOUND OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Unsafe or missing deactivation role';
        END IF;
    END LOOP;

    SELECT role.oid INTO identity_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.identity_owner');
    SELECT role.oid INTO audit_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.audit_owner');
    SELECT role.oid INTO notifications_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.notifications_owner');
    SELECT role.oid INTO migration_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.migration_role');
    SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.identity_deactivation_role');
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid IN (identity_oid, audit_oid, notifications_oid)
          AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname IN (
            current_setting('aap.identity_deactivation_role'),
            current_setting('aap.identity_deactivation_operator_role'),
            current_setting('aap.notification_publish_role')
        ) AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Owners must be NOLOGIN and deactivation clients LOGIN NOINHERIT';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'aap_identity_deactivation_v1'
          AND namespace.nspowner = migration_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'aap_identity_deactivation_v1'
          AND function.prokind = 'f' AND function.proowner <> identity_oid
    ) THEN
        RAISE EXCEPTION 'Identity deactivation schema or function owner is unexpected';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'aap_identity_v1.lock_super_admin_invariant()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_identity_v1.super_admin_status(uuid)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_audit_v1.record_identity_deactivation_event(text,jsonb,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_notifications_v1.create_identity_deactivation_notifications(uuid,text,jsonb,uuid[])'
           )::oid) IS DISTINCT FROM notifications_oid THEN
        RAISE EXCEPTION 'A shared deactivation capability has the wrong owner';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS function
        WHERE function.oid IN (
            pg_catalog.to_regprocedure('aap_notifications_v1.identity_delivery_policy()')::oid,
            pg_catalog.to_regprocedure('aap_notifications_v1.claim_identity_deliveries(jsonb)')::oid,
            pg_catalog.to_regprocedure('aap_notifications_v1.finish_identity_delivery(jsonb)')::oid
        ) AND function.proowner <> notifications_oid
    ) THEN
        RAISE EXCEPTION 'Notifications publisher functions have the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid,
        pg_catalog.to_regprocedure(
            'aap_audit_v1.record_identity_deactivation_event(text,jsonb,jsonb,jsonb)'
        )::oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,
        pg_catalog.to_regprocedure(
            'aap_notifications_v1.create_identity_deactivation_notifications(uuid,text,jsonb,uuid[])'
        )::oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,
        pg_catalog.to_regprocedure('aap_identity_v1.is_jti_revoked(uuid)')::oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'deactivation_requested_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_reason', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'is_active', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'status', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_by', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'deactivation_reason', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'deactivation_scheduled_at', 'UPDATE'
    ) OR pg_catalog.has_table_privilege(
        identity_oid, 'public.notifications', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
    ) OR pg_catalog.has_table_privilege(
        identity_oid, 'public.notification_preferences', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'actor_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'actor_email', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'actor_role', 'UPDATE'
    ) THEN
        RAISE EXCEPTION 'Identity owner is missing deactivation capabilities';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_deactivation_role", :"identity_deactivation_operator_role",
         :"notification_publish_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_deactivation_role", :"identity_deactivation_operator_role",
       :"notification_publish_role";
DO $$
DECLARE
    namespace record;
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        current_setting('aap.identity_deactivation_role'),
        current_setting('aap.identity_deactivation_operator_role'),
        current_setting('aap.notification_publish_role')
    ] LOOP
        FOR namespace IN
            SELECT ns.nspname
            FROM pg_catalog.pg_namespace AS ns
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
        LOOP
            EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA aap_identity_deactivation_v1
    TO :"identity_deactivation_role", :"identity_deactivation_operator_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_deactivation_v1.deactivation_policy(),
    aap_identity_deactivation_v1.execute_deactivation_command(jsonb)
    TO :"identity_deactivation_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_deactivation_v1.due_policy(),
    aap_identity_deactivation_v1.process_due_deactivations(jsonb)
    TO :"identity_deactivation_operator_role";
GRANT USAGE ON SCHEMA aap_notifications_v1 TO :"notification_publish_role";
GRANT EXECUTE ON FUNCTION
    aap_notifications_v1.identity_delivery_policy(),
    aap_notifications_v1.claim_identity_deliveries(jsonb),
    aap_notifications_v1.finish_identity_delivery(jsonb)
    TO :"notification_publish_role";

-- Verify exact function allowlists and deny direct table/foreign-schema access.
DO $$
DECLARE
    role_name text;
    role_oid oid;
    allowed_schema text;
    allowed_functions text[];
    actual_functions text[];
    expected_functions text[];
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        current_setting('aap.identity_deactivation_role'),
        current_setting('aap.identity_deactivation_operator_role'),
        current_setting('aap.notification_publish_role')
    ] LOOP
        IF role_name = current_setting('aap.identity_deactivation_role') THEN
            allowed_schema := 'aap_identity_deactivation_v1';
            allowed_functions := ARRAY[
                'aap_identity_deactivation_v1.deactivation_policy()',
                'aap_identity_deactivation_v1.execute_deactivation_command(jsonb)'
            ];
        ELSIF role_name = current_setting('aap.identity_deactivation_operator_role') THEN
            allowed_schema := 'aap_identity_deactivation_v1';
            allowed_functions := ARRAY[
                'aap_identity_deactivation_v1.due_policy()',
                'aap_identity_deactivation_v1.process_due_deactivations(jsonb)'
            ];
        ELSE
            allowed_schema := 'aap_notifications_v1';
            allowed_functions := ARRAY[
                'aap_notifications_v1.identity_delivery_policy()',
                'aap_notifications_v1.claim_identity_deliveries(jsonb)',
                'aap_notifications_v1.finish_identity_delivery(jsonb)'
            ];
        END IF;
        SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        SELECT COALESCE(pg_catalog.array_agg(
            namespace.nspname || '.' || function.proname || '('
            || pg_catalog.oidvectortypes(function.proargtypes) || ')'
            ORDER BY namespace.nspname || '.' || function.proname || '('
            || pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ), ARRAY[]::text[]) INTO actual_functions
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE left(namespace.nspname, 4) = 'aap_'
          AND function.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
        SELECT COALESCE(pg_catalog.array_agg(item.signature ORDER BY item.signature), ARRAY[]::text[])
        INTO expected_functions
        FROM pg_catalog.unnest(allowed_functions) AS item(signature);
        IF actual_functions IS DISTINCT FROM expected_functions THEN
            RAISE EXCEPTION 'Unexpected function allowlist for %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
              AND namespace.nspname <> allowed_schema
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
        ) THEN
            RAISE EXCEPTION 'Unexpected foreign module schema for %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_proc AS function ON function.pronamespace = namespace.oid
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND left(namespace.nspname, 4) <> 'aap_'
              AND function.prokind IN ('f', 'p')
              AND function.prosecdef
              AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE')
        ) THEN
            RAISE EXCEPTION 'Unexpected non-module SECURITY DEFINER capability for %', role_name;
        END IF;
        IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE,TEMP')
           OR EXISTS (
               SELECT 1 FROM pg_catalog.pg_namespace AS namespace
               WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                 AND namespace.nspname NOT LIKE 'pg_toast%'
                 AND namespace.nspname NOT LIKE 'pg_temp_%'
                 AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
           ) OR EXISTS (
               SELECT 1
               FROM pg_catalog.pg_namespace AS namespace
               JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
               WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                 AND namespace.nspname NOT LIKE 'pg_toast%'
                 AND namespace.nspname NOT LIKE 'pg_temp_%'
                 AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                 AND (pg_catalog.has_table_privilege(role_oid, relation.oid,
                          'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                   OR pg_catalog.has_any_column_privilege(role_oid, relation.oid,
                          'SELECT,INSERT,UPDATE,REFERENCES'))
           ) THEN
            RAISE EXCEPTION 'Unexpected direct data/schema access for %', role_name;
        END IF;
    END LOOP;
END $$;
COMMIT;
