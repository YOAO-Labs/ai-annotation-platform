\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.notifications_owner', :'notifications_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.channel_delivery_role', :'channel_delivery_role', true);

DO $guard$
DECLARE
    v_owner name := current_setting('yoao.notifications_owner');
    v_api name := current_setting('yoao.api_role');
    v_client name := current_setting('yoao.channel_delivery_role');
    v_owner_oid oid;
    v_api_oid oid;
    v_client_oid oid;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR v_owner <> 'notifications_code'
       OR v_client <> 'yoao_notification_channel_delivery_client'
       OR v_owner = v_api OR v_owner = v_client OR v_api = v_client THEN
        RAISE EXCEPTION 'Unexpected Notifications channel delivery grant target';
    END IF;
    SELECT oid INTO v_owner_oid FROM pg_catalog.pg_roles WHERE rolname = v_owner;
    SELECT oid INTO v_api_oid FROM pg_catalog.pg_roles WHERE rolname = v_api;
    SELECT oid INTO v_client_oid FROM pg_catalog.pg_roles WHERE rolname = v_client;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS r
        WHERE r.oid = v_client_oid AND r.rolcanlogin AND NOT r.rolinherit
          AND NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
                   OR r.rolreplication OR r.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Unsafe or missing Notifications channel delivery role';
    END IF;
END;
$guard$;

REVOKE ALL ON TABLE
    yoao_notifications_v1.notification_channels,
    yoao_notifications_v1.user_notification_channel_bindings,
    yoao_notifications_v1.notification_channel_deliveries
    FROM :"api_role", :"channel_delivery_role", PUBLIC;

REVOKE EXECUTE ON FUNCTION
    yoao_notifications_v1.channel_delivery_policy(),
    yoao_notifications_v1.claim_channel_deliveries(jsonb),
    yoao_notifications_v1.finish_channel_delivery(jsonb)
    FROM :"api_role", PUBLIC;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_notifications_v1 FROM :"channel_delivery_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_labeling_v1 FROM :"channel_delivery_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_diagnostics_v1 FROM :"channel_delivery_role";

GRANT CONNECT ON DATABASE :"expected_database" TO :"channel_delivery_role";
GRANT USAGE ON SCHEMA
    yoao_notifications_v1, yoao_labeling_v1, yoao_diagnostics_v1
    TO :"channel_delivery_role";
GRANT EXECUTE ON FUNCTION
    yoao_notifications_v1.channel_delivery_policy(),
    yoao_notifications_v1.claim_channel_deliveries(jsonb),
    yoao_notifications_v1.finish_channel_delivery(jsonb),
    yoao_notifications_v1.delivery_job_scopes(uuid[]),
    yoao_notifications_v1.delivery_member_pairs(jsonb),
    yoao_notifications_v1.delivery_reviewer_pairs(jsonb),
    yoao_notifications_v1.delivery_owner_pairs(jsonb),
    yoao_notifications_v1.delivery_super_pairs(jsonb),
    yoao_labeling_v1.notification_target_visible_to_actor(uuid,text,uuid,uuid,uuid),
    yoao_diagnostics_v1.bug_report_visible_to_actor(uuid,uuid)
    TO :"channel_delivery_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname = current_setting('yoao.channel_delivery_role'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname = current_setting('yoao.api_role'));
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              pg_catalog.has_table_privilege(
                  v_client, relation.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
              OR pg_catalog.has_any_column_privilege(
                  v_client, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
              )
          )
    ) THEN
        RAISE EXCEPTION 'Channel delivery client retains direct table or view privilege';
    END IF;
    IF pg_catalog.has_function_privilege(
        v_api, 'yoao_notifications_v1.claim_channel_deliveries(jsonb)'::regprocedure, 'EXECUTE'
    ) OR pg_catalog.has_function_privilege(
        v_api, 'yoao_notifications_v1.finish_channel_delivery(jsonb)'::regprocedure, 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'API role retains private channel delivery lease functions';
    END IF;
END;
$verify$;
COMMIT;
