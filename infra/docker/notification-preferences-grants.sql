-- Grant the preference command to the existing API LOGIN and a function-only peer.
-- Run after module-data-grants.sql as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v notifications_owner=notifications_code -v api_role=legacy_runtime \
--   -v preference_role=notification_preferences_client \
--   -f infra/docker/notification-preferences-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.notifications_owner', :'notifications_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.preference_role', :'preference_role', true);

DO $guard$
DECLARE
    v_owner name := current_setting('aap.notifications_owner');
    v_api name := current_setting('aap.api_role');
    v_client name := current_setting('aap.preference_role');
    v_owner_oid oid;
    v_api_oid oid;
    v_client_oid oid;
    v_command_oid oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.preference_command(jsonb)'
    )::oid;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
       OR v_owner <> 'notifications_code'
       OR v_client <> 'notification_preferences_client'
       OR v_owner = v_api OR v_owner = v_client OR v_api = v_client THEN
        RAISE EXCEPTION 'Unexpected Notifications preference grant target';
    END IF;
    IF v_api !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_client !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
        RAISE EXCEPTION 'Unsafe Notifications preference role name';
    END IF;

    SELECT role.oid INTO v_owner_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_owner;
    SELECT role.oid INTO v_api_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_api;
    SELECT role.oid INTO v_client_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_client;
    IF v_command_oid IS NULL
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
        RAISE EXCEPTION 'Unsafe or missing Notifications preference role';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_command_oid) IS DISTINCT FROM v_owner_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_notifications_v1._preference_policy()'
           )::oid) IS DISTINCT FROM v_owner_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_notifications_v1._preference_jsonb_truthy(jsonb)'
           )::oid) IS DISTINCT FROM v_owner_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_notifications_v1._preference_object_keys_valid(jsonb,text[],integer,integer)'
           )::oid) IS DISTINCT FROM v_owner_oid
       OR pg_catalog.has_table_privilege(
            v_client_oid, 'public.notification_preferences',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
            v_client_oid, 'public.notification_preferences', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_schema_privilege(v_client_oid, 'aap_notifications_v1', 'CREATE')
       OR pg_catalog.has_database_privilege(
            v_client_oid, current_database(), 'CREATE,TEMP'
       ) THEN
        RAISE EXCEPTION 'Notifications preference client exceeds its fixed capability';
    END IF;
END;
$guard$;

REVOKE ALL ON TABLE public.notification_preferences
    FROM :"api_role", :"preference_role", PUBLIC;
REVOKE SELECT (user_id, type, channels, updated_at),
    INSERT (user_id, type, channels, updated_at),
    UPDATE (user_id, type, channels, updated_at),
    REFERENCES (user_id, type, channels, updated_at)
    ON TABLE public.notification_preferences
    FROM :"api_role", :"preference_role", PUBLIC;
REVOKE ALL ON SCHEMA aap_notifications_v1 FROM :"preference_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_notifications_v1 FROM :"preference_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"preference_role";
GRANT USAGE ON SCHEMA aap_notifications_v1 TO :"api_role", :"preference_role";
GRANT EXECUTE ON FUNCTION aap_notifications_v1.preference_command(jsonb)
    TO :"api_role", :"preference_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname = current_setting('aap.preference_role'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname = current_setting('aap.api_role'));
    v_allowed oid := pg_catalog.to_regprocedure(
        'aap_notifications_v1.preference_command(jsonb)'
    )::oid;
BEGIN
    IF NOT pg_catalog.has_function_privilege(v_client, v_allowed, 'EXECUTE')
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            WHERE namespace.nspname = 'aap_notifications_v1'
              AND pg_catalog.has_function_privilege(v_client, function.oid, 'EXECUTE')
              AND function.oid <> v_allowed
       ) OR pg_catalog.has_table_privilege(
            v_client, 'public.notification_preferences',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
            v_client, 'public.notification_preferences', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
            v_api, 'public.notification_preferences',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
            v_api, 'public.notification_preferences', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Notifications preference client has an unexpected grant';
    END IF;
END;
$verify$;
COMMIT;
