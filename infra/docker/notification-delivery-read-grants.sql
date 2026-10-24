-- Grant Notifications fixed delivery reads to an API and function-only LOGIN.
-- Run after current migrations and module-data-grants.sql as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v notifications_owner=notifications_code -v api_role=api_runtime \
--   -v query_client=notification_delivery_read_client \
--   -f infra/docker/notification-delivery-read-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.notifications_owner', :'notifications_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.query_client', :'query_client', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.notifications_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.query_client'));
    v_function record;
BEGIN
    IF current_database()<>current_setting('aap.expected_database')
       OR current_user<>current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_owner IS NULL OR v_api IS NULL OR v_client IS NULL
       OR v_owner IN (v_api,v_client) OR v_api=v_client THEN
        RAISE EXCEPTION 'Delivery read roles are missing or overlap';
    END IF;
    IF NOT (SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
                   AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
            FROM pg_catalog.pg_roles WHERE oid=v_owner)
       OR NOT (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
                   AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
            FROM pg_catalog.pg_roles WHERE oid=v_api)
       OR NOT (SELECT rolcanlogin AND NOT rolinherit AND NOT rolsuper AND NOT rolcreatedb
                   AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
            FROM pg_catalog.pg_roles WHERE oid=v_client)
       OR EXISTS (
            SELECT FROM pg_catalog.pg_auth_members
            WHERE member IN (v_owner,v_api,v_client) OR roleid IN (v_api,v_client)
               OR (roleid=v_owner AND member<>current_user::pg_catalog.regrole)
       ) THEN
        RAISE EXCEPTION 'Unsafe delivery read owner, API or query LOGIN';
    END IF;
    FOR v_function IN
        SELECT * FROM (VALUES
            ('aap_notifications_v1.read_notification(uuid)', 'aap_notifications_v1.notification_facts', true),
            ('aap_notifications_v1.find_terminal_notification(uuid,text,uuid)', 'uuid', false),
            ('aap_notifications_v1.delivery_job_scopes(uuid[])', 'record', true),
            ('aap_notifications_v1.delivery_member_pairs(jsonb)', 'record', true),
            ('aap_notifications_v1.delivery_reviewer_pairs(jsonb)', 'record', true),
            ('aap_notifications_v1.delivery_owner_pairs(jsonb)', 'record', true),
            ('aap_notifications_v1.delivery_super_pairs(jsonb)', 'record', true)
        ) AS expected(signature, return_type, returns_set)
    LOOP
        IF NOT EXISTS (
            SELECT FROM pg_catalog.pg_proc AS function
            WHERE function.oid=pg_catalog.to_regprocedure(v_function.signature)
              AND function.proowner=v_owner AND function.prosecdef
              AND function.provolatile='s' AND function.prokind='f'
              AND function.prorettype=pg_catalog.to_regtype(v_function.return_type)
              AND function.proretset=v_function.returns_set
              AND function.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
        ) THEN
            RAISE EXCEPTION 'Delivery read function ownership or fixed signature differs';
        END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON SCHEMA aap_notifications_v1 FROM :"query_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_notifications_v1 FROM :"query_client";
REVOKE ALL ON ALL TABLES IN SCHEMA aap_notifications_v1, public FROM :"query_client";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"query_client";
DO $revoke_columns$
DECLARE
    v_relation record;
    v_columns text;
BEGIN
    FOR v_relation IN
        SELECT relation.oid, namespace.nspname, relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname IN ('public','aap_notifications_v1')
          AND relation.relkind IN ('r','p','v','m','f')
    LOOP
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname), ',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns, v_relation.nspname, v_relation.relname,
                current_setting('aap.query_client'));
        END IF;
    END LOOP;
END;
$revoke_columns$;
REVOKE ALL ON aap_notifications_v1.notification_facts FROM :"api_role", PUBLIC;
REVOKE ALL (id,user_id,type,target_type,target_id,payload,read_at,created_at)
    ON aap_notifications_v1.notification_facts FROM :"api_role", PUBLIC;
REVOKE EXECUTE ON FUNCTION
    aap_notifications_v1.read_notification(uuid),
    aap_notifications_v1.find_terminal_notification(uuid,text,uuid),
    aap_notifications_v1.delivery_job_scopes(uuid[]),
    aap_notifications_v1.delivery_member_pairs(jsonb),
    aap_notifications_v1.delivery_reviewer_pairs(jsonb),
    aap_notifications_v1.delivery_owner_pairs(jsonb),
    aap_notifications_v1.delivery_super_pairs(jsonb)
    FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"query_client";
GRANT USAGE ON SCHEMA aap_notifications_v1 TO :"query_client", :"api_role";
GRANT EXECUTE ON FUNCTION
    aap_notifications_v1.read_notification(uuid),
    aap_notifications_v1.find_terminal_notification(uuid,text,uuid),
    aap_notifications_v1.delivery_job_scopes(uuid[]),
    aap_notifications_v1.delivery_member_pairs(jsonb),
    aap_notifications_v1.delivery_reviewer_pairs(jsonb),
    aap_notifications_v1.delivery_owner_pairs(jsonb),
    aap_notifications_v1.delivery_super_pairs(jsonb)
    TO :"query_client", :"api_role";

DO $verify$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.notifications_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.query_client'));
    v_allowed oid[] := ARRAY[
        'aap_notifications_v1.read_notification(uuid)'::regprocedure::oid,
        'aap_notifications_v1.find_terminal_notification(uuid,text,uuid)'::regprocedure::oid,
        'aap_notifications_v1.delivery_job_scopes(uuid[])'::regprocedure::oid,
        'aap_notifications_v1.delivery_member_pairs(jsonb)'::regprocedure::oid,
        'aap_notifications_v1.delivery_reviewer_pairs(jsonb)'::regprocedure::oid,
        'aap_notifications_v1.delivery_owner_pairs(jsonb)'::regprocedure::oid,
        'aap_notifications_v1.delivery_super_pairs(jsonb)'::regprocedure::oid
    ];
    v_function oid;
    v_private text;
BEGIN
    FOREACH v_function IN ARRAY v_allowed LOOP
        IF NOT pg_catalog.has_function_privilege(v_client,v_function,'EXECUTE')
           OR NOT pg_catalog.has_function_privilege(v_api,v_function,'EXECUTE') THEN
            RAISE EXCEPTION 'Missing fixed delivery read EXECUTE';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0 AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'PUBLIC retains delivery read EXECUTE';
    END IF;
    IF pg_catalog.has_table_privilege(v_api,
            'aap_notifications_v1.notification_facts','SELECT')
       OR pg_catalog.has_any_column_privilege(v_api,
            'aap_notifications_v1.notification_facts','SELECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Delivery read grants expose direct API fact reads';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
        JOIN pg_catalog.pg_type AS return_type ON return_type.oid=function.prorettype
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND function.prokind IN ('f','p','a','w')
          AND NOT (return_type.typnamespace='pg_catalog'::regnamespace
              AND return_type.typname IN ('trigger','event_trigger'))
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend AS dependency
              WHERE dependency.classid='pg_catalog.pg_proc'::regclass
                AND dependency.objid=function.oid
                AND dependency.refclassid='pg_catalog.pg_extension'::regclass
                AND dependency.deptype='e')
          AND (pg_catalog.left(namespace.nspname,4)='aap_'
              OR function.prosecdef
              OR EXISTS (
                  SELECT FROM pg_catalog.aclexplode(COALESCE(function.proacl,
                      pg_catalog.acldefault('f',function.proowner))) AS acl
                  WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                    AND pg_catalog.pg_has_role(v_client,acl.grantee,'USAGE')))
          AND function.oid<>ALL(v_allowed)
          AND pg_catalog.has_function_privilege(v_client,function.oid,'EXECUTE')
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','aap_notifications_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND ((relation.relkind IN ('r','p','v','m','f')
                AND (pg_catalog.has_table_privilege(v_client,relation.oid,
                        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR pg_catalog.has_any_column_privilege(v_client,relation.oid,
                        'SELECT,INSERT,UPDATE,REFERENCES')))
               OR (relation.relkind='S' AND pg_catalog.has_sequence_privilege(
                    v_client,relation.oid,'SELECT,UPDATE,USAGE')))
    ) THEN
        RAISE EXCEPTION 'Delivery read LOGIN has data or capabilities beyond fixed delivery reads';
    END IF;
    FOREACH v_private IN ARRAY ARRAY[
        'public.users','public.projects','public.project_members','public.async_jobs'
    ] LOOP
        IF pg_catalog.has_table_privilege(v_owner,v_private,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_owner,v_private,
                'SELECT,INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Delivery read owner has foreign private data privileges';
        END IF;
    END LOOP;
END;
$verify$;
COMMIT;
