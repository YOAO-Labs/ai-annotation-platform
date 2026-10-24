-- Grant the three Notifications inbox reads to an API and function-only LOGIN.
-- Run after current migrations and module-data-grants.sql as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v notifications_owner=notifications_code -v api_role=api_runtime \
--   -v query_client=notification_inbox_query_client \
--   -f infra/docker/notification-inbox-grants.sql
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
        RAISE EXCEPTION 'Inbox roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe inbox owner, API or query LOGIN';
    END IF;
    FOR v_function IN
        SELECT * FROM (VALUES
            ('aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)',
                'aap_notifications_v1.notification_facts', true),
            ('aap_notifications_v1.inbox_total_count(uuid,text)', 'bigint', false),
            ('aap_notifications_v1.inbox_unread_count(uuid,text)', 'bigint', false),
            ('aap_notifications_v1._visible_inbox_rows(uuid,text)',
                'aap_notifications_v1.notification_facts', true)
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
            RAISE EXCEPTION 'Inbox function ownership or fixed signature differs';
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
REVOKE EXECUTE ON FUNCTION aap_notifications_v1._visible_inbox_rows(uuid,text)
    FROM :"api_role", :"query_client", PUBLIC;
REVOKE EXECUTE ON FUNCTION
    aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer),
    aap_notifications_v1.inbox_total_count(uuid,text),
    aap_notifications_v1.inbox_unread_count(uuid,text)
    FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"query_client";
GRANT USAGE ON SCHEMA aap_notifications_v1 TO :"query_client", :"api_role";
GRANT EXECUTE ON FUNCTION
    aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer),
    aap_notifications_v1.inbox_total_count(uuid,text),
    aap_notifications_v1.inbox_unread_count(uuid,text)
    TO :"query_client", :"api_role";

DO $verify$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.notifications_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.query_client'));
    v_allowed oid[] := ARRAY[
        'aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)'::regprocedure::oid,
        'aap_notifications_v1.inbox_total_count(uuid,text)'::regprocedure::oid,
        'aap_notifications_v1.inbox_unread_count(uuid,text)'::regprocedure::oid
    ];
    v_function oid;
    v_private text;
BEGIN
    FOREACH v_function IN ARRAY v_allowed LOOP
        IF NOT pg_catalog.has_function_privilege(v_client,v_function,'EXECUTE')
           OR NOT pg_catalog.has_function_privilege(v_api,v_function,'EXECUTE') THEN
            RAISE EXCEPTION 'Missing fixed inbox EXECUTE';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed || ARRAY[
            'aap_notifications_v1._visible_inbox_rows(uuid,text)'::regprocedure::oid
        ]) AND acl.grantee=0 AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'PUBLIC retains inbox EXECUTE';
    END IF;
    IF pg_catalog.has_function_privilege(v_api,
            'aap_notifications_v1._visible_inbox_rows(uuid,text)','EXECUTE')
       OR NOT pg_catalog.has_table_privilege(v_api,
            'aap_notifications_v1.notification_facts','SELECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Inbox grants widen helpers or remove required API reads';
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
        RAISE EXCEPTION 'Inbox LOGIN has data or capabilities beyond three reads';
    END IF;
    FOREACH v_private IN ARRAY ARRAY[
        'public.users','public.projects','public.project_members','public.async_jobs'
    ] LOOP
        IF pg_catalog.has_table_privilege(v_owner,v_private,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_owner,v_private,
                'SELECT,INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Inbox owner has foreign private data privileges';
        END IF;
    END LOOP;
END;
$verify$;
COMMIT;
