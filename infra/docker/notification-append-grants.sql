-- Grant bounded append, read-state, and deletion functions to the API LOGIN and function-only peer.
-- The peer also composes the existing preference command in its borrowed transaction.
-- Run after module-data-grants.sql and notification-preferences-grants.sql as migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v notifications_owner=notifications_code -v api_role=legacy_runtime \
--   -v append_role=notification_append_client \
--   -f infra/docker/notification-append-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.notifications_owner', :'notifications_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.append_role', :'append_role', true);

DO $guard$
DECLARE
    v_owner name := current_setting('aap.notifications_owner');
    v_api name := current_setting('aap.api_role');
    v_client name := current_setting('aap.append_role');
    v_owner_oid oid;
    v_api_oid oid;
    v_client_oid oid;
    v_append_oid oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.append_notification(jsonb)'
    )::oid;
    v_terminal_oid oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.append_job_terminal_notification(jsonb)'
    )::oid;
    v_preference_oid oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.preference_command(jsonb)'
    )::oid;
    v_read_state_oid oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.read_state_command(jsonb)'
    )::oid;
    v_delete_oid oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.delete_command(pg_catalog.jsonb)'
    )::oid;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
       OR v_owner <> 'notifications_code'
       OR v_client <> 'notification_append_client'
       OR v_owner = v_api OR v_owner = v_client OR v_api = v_client THEN
        RAISE EXCEPTION 'Unexpected Notifications append grant target';
    END IF;
    IF v_api !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_client !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
        RAISE EXCEPTION 'Unsafe Notifications append role name';
    END IF;

    SELECT role.oid INTO v_owner_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_owner;
    SELECT role.oid INTO v_api_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_api;
    SELECT role.oid INTO v_client_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_client;
    IF v_append_oid IS NULL OR v_terminal_oid IS NULL OR v_preference_oid IS NULL
       OR v_read_state_oid IS NULL OR v_delete_oid IS NULL
       OR NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = v_owner_oid AND NOT role.rolcanlogin
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
       )
       OR NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = v_api_oid AND role.rolcanlogin
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
       )
       OR NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = v_client_oid AND role.rolcanlogin AND NOT role.rolinherit
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
       ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member IN (v_owner_oid, v_api_oid, v_client_oid)
               OR membership.roleid = v_client_oid
       ) THEN
        RAISE EXCEPTION 'Unsafe or missing Notifications append role';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_append_oid) IS DISTINCT FROM v_owner_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = v_terminal_oid) IS DISTINCT FROM v_owner_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = v_preference_oid) IS DISTINCT FROM v_owner_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = v_read_state_oid) IS DISTINCT FROM v_owner_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = v_delete_oid) IS DISTINCT FROM v_owner_oid
       OR NOT pg_catalog.has_table_privilege(
            v_owner_oid, 'public.notifications', 'INSERT'
       )
       OR NOT pg_catalog.has_table_privilege(
            v_owner_oid, 'public.notifications', 'DELETE'
       )
       OR pg_catalog.has_table_privilege(
            v_owner_oid, 'public.notifications', 'UPDATE'
       )
       OR NOT pg_catalog.has_column_privilege(
            v_owner_oid, 'public.notifications', 'read_at', 'UPDATE'
       )
       OR pg_catalog.has_column_privilege(
            v_owner_oid, 'public.notifications', 'payload', 'UPDATE'
       )
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_attribute AS attribute
            WHERE attribute.attrelid = 'public.notifications'::pg_catalog.regclass
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND attribute.attname <> 'read_at'
              AND pg_catalog.has_column_privilege(
                  v_owner_oid, 'public.notifications', attribute.attname, 'UPDATE'
              )
       )
       OR pg_catalog.has_table_privilege(
            v_client_oid, 'public.notifications',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
            v_client_oid, 'public.notifications', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
            v_client_oid, 'public.notification_preferences',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
            v_client_oid, 'public.notification_preferences',
            'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_schema_privilege(v_client_oid, 'aap_notifications_v1', 'CREATE')
       OR pg_catalog.has_database_privilege(
            v_client_oid, current_database(), 'CREATE,TEMP'
       ) THEN
        RAISE EXCEPTION 'Notifications append client exceeds its fixed capability';
    END IF;
END;
$guard$;

REVOKE INSERT ON TABLE public.notifications FROM :"api_role", PUBLIC;
REVOKE INSERT (
    id, user_id, type, target_type, target_id, payload, read_at, created_at
) ON TABLE public.notifications FROM :"api_role", PUBLIC;
REVOKE UPDATE ON TABLE public.notifications FROM :"api_role", PUBLIC;
REVOKE UPDATE (
    id, user_id, type, target_type, target_id, payload, read_at, created_at
) ON TABLE public.notifications FROM :"api_role", PUBLIC;
REVOKE DELETE ON TABLE public.notifications FROM :"api_role", PUBLIC;
REVOKE ALL ON SCHEMA aap_notifications_v1 FROM :"append_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_notifications_v1 FROM :"append_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"append_role";
GRANT USAGE ON SCHEMA aap_notifications_v1 TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION aap_notifications_v1.append_notification(jsonb)
    TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION aap_notifications_v1.append_job_terminal_notification(jsonb)
    TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION aap_notifications_v1.read_state_command(jsonb)
    TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION aap_notifications_v1.delete_command(pg_catalog.jsonb)
    TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION aap_notifications_v1.preference_command(jsonb)
    TO :"append_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname = current_setting('aap.append_role'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname = current_setting('aap.api_role'));
    v_allowed oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.append_notification(jsonb)'
    )::oid;
    v_terminal oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.append_job_terminal_notification(jsonb)'
    )::oid;
    v_preference oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.preference_command(jsonb)'
    )::oid;
    v_read_state oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.read_state_command(jsonb)'
    )::oid;
    v_delete oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.delete_command(pg_catalog.jsonb)'
    )::oid;
BEGIN
    IF v_allowed IS NULL OR v_terminal IS NULL OR v_preference IS NULL OR v_read_state IS NULL
       OR v_delete IS NULL
       OR NOT pg_catalog.has_function_privilege(v_client, v_allowed, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_client, v_terminal, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_client, v_preference, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_client, v_read_state, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_api, v_allowed, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_api, v_terminal, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_api, v_read_state, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_api, v_delete, 'EXECUTE')
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            WHERE namespace.nspname = 'aap_notifications_v1'
              AND pg_catalog.has_function_privilege(v_client, function.oid, 'EXECUTE')
              AND function.oid NOT IN (
                  v_allowed, v_terminal, v_preference, v_read_state, v_delete
              )
       ) OR pg_catalog.has_table_privilege(
            v_client, 'public.notifications',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
            v_client, 'public.notifications', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
            v_api, 'public.notifications',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
            v_api, 'public.notifications', 'INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Notifications append grant has an unexpected privilege';
    END IF;
END;
$verify$;
COMMIT;
