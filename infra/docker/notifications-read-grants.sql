-- Grant the public Notifications fact view to the existing API/worker LOGIN.
-- Run after module-data-grants.sql as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v notifications_owner=notifications_code -v api_role=legacy_runtime \
--   -f infra/docker/notifications-read-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.notifications_owner', :'notifications_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);

DO $guard$
DECLARE
    v_owner name := current_setting('aap.notifications_owner');
    v_api name := current_setting('aap.api_role');
    v_owner_oid oid;
    v_api_oid oid;
    v_relation_oid oid := pg_catalog.to_regclass(
        'aap_notifications_v1.notification_facts'
    )::oid;
    v_columns text[];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
       OR v_owner <> 'notifications_code'
       OR v_api = v_owner
       OR v_api = current_user
       OR v_api !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
        RAISE EXCEPTION 'Unexpected Notifications read grant target';
    END IF;

    SELECT role.oid INTO v_owner_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_owner;
    SELECT role.oid INTO v_api_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_api;
    SELECT ARRAY(
        SELECT attribute.attname::text
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_relation_oid
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        ORDER BY attribute.attnum
    ) INTO v_columns;

    IF v_relation_oid IS NULL
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
       OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = v_api_oid
               OR membership.member = v_owner_oid
       )
       OR NOT pg_catalog.has_table_privilege(
            v_owner_oid, 'public.notifications', 'SELECT'
       )
       OR (SELECT relation.relkind FROM pg_catalog.pg_class AS relation
           WHERE relation.oid = v_relation_oid) <> 'v'
       OR (SELECT relation.relowner FROM pg_catalog.pg_class AS relation
           WHERE relation.oid = v_relation_oid) IS DISTINCT FROM v_owner_oid
       OR NOT COALESCE(
            (SELECT relation.reloptions @> ARRAY['security_barrier=true']
             FROM pg_catalog.pg_class AS relation
             WHERE relation.oid = v_relation_oid),
            false
       )
       OR v_columns IS DISTINCT FROM ARRAY[
            'id','user_id','type','target_type','target_id','payload','read_at','created_at'
       ]::text[] THEN
        RAISE EXCEPTION 'Notifications read relation does not match its fixed contract';
    END IF;
END;
$guard$;

REVOKE ALL PRIVILEGES ON TABLE public.notifications FROM :"api_role", PUBLIC;
REVOKE ALL PRIVILEGES (
    id, user_id, type, target_type, target_id, payload, read_at, created_at
) ON TABLE public.notifications FROM :"api_role", PUBLIC;
REVOKE ALL ON aap_notifications_v1.notification_facts FROM :"api_role", PUBLIC;
GRANT USAGE ON SCHEMA aap_notifications_v1 TO :"api_role";
GRANT SELECT ON aap_notifications_v1.notification_facts TO :"api_role";

DO $verify$
DECLARE
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname = current_setting('aap.api_role'));
    v_facts oid := 'aap_notifications_v1.notification_facts'::pg_catalog.regclass;
BEGIN
    IF pg_catalog.has_table_privilege(
           v_api, 'public.notifications', 'SELECT'
       )
       OR pg_catalog.has_any_column_privilege(
           v_api, 'public.notifications', 'SELECT'
       )
       OR pg_catalog.has_table_privilege(
           v_api, 'public.notifications',
           'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       )
       OR pg_catalog.has_any_column_privilege(
           v_api, 'public.notifications', 'INSERT,UPDATE,REFERENCES'
       )
       OR NOT pg_catalog.has_table_privilege(v_api, v_facts, 'SELECT')
       OR pg_catalog.has_table_privilege(
           v_api, v_facts, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       )
       OR pg_catalog.has_any_column_privilege(
           v_api, v_facts, 'INSERT,UPDATE,REFERENCES'
       )
       OR pg_catalog.has_schema_privilege(
           v_api, 'aap_notifications_v1', 'CREATE'
       ) THEN
        RAISE EXCEPTION 'Notifications API relation access exceeds SELECT-only contract';
    END IF;
END;
$verify$;
COMMIT;
