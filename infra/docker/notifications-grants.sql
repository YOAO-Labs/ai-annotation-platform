\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.grant_section_applied', '0', true);

\if :{?grant_append}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.notifications_owner', :'notifications_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.append_role', :'append_role', true);

DO $guard$
DECLARE
    v_owner name := current_setting('yoao.notifications_owner');
    v_api name := current_setting('yoao.api_role');
    v_client name := current_setting('yoao.append_role');
    v_owner_oid oid;
    v_api_oid oid;
    v_client_oid oid;
    v_append_oid oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.append_notification(jsonb)'
    )::oid;
    v_terminal_oid oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.append_job_terminal_notification(jsonb)'
    )::oid;
    v_preference_oid oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.preference_command(jsonb)'
    )::oid;
    v_read_state_oid oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.read_state_command(jsonb)'
    )::oid;
    v_delete_oid oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.delete_command(pg_catalog.jsonb)'
    )::oid;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR v_owner <> 'notifications_code'
       OR v_client <> 'yoao_notification_append_client'
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
       ) OR pg_catalog.has_schema_privilege(v_client_oid, 'yoao_notifications_v1', 'CREATE')
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
REVOKE ALL ON SCHEMA yoao_notifications_v1 FROM :"append_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_notifications_v1 FROM :"append_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"append_role";
GRANT USAGE ON SCHEMA yoao_notifications_v1 TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION yoao_notifications_v1.append_notification(jsonb)
    TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION yoao_notifications_v1.append_job_terminal_notification(jsonb)
    TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION yoao_notifications_v1.read_state_command_scoped(jsonb),
    yoao_notifications_v1.delete_command_scoped(jsonb)
    TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION yoao_notifications_v1.read_state_command(jsonb)
    TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION yoao_notifications_v1.delete_command(pg_catalog.jsonb)
    TO :"api_role", :"append_role";
GRANT EXECUTE ON FUNCTION yoao_notifications_v1.preference_command(jsonb)
    TO :"append_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname = current_setting('yoao.append_role'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname = current_setting('yoao.api_role'));
    v_allowed oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.append_notification(jsonb)'
    )::oid;
    v_terminal oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.append_job_terminal_notification(jsonb)'
    )::oid;
    v_preference oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.preference_command(jsonb)'
    )::oid;
    v_read_state oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.read_state_command(jsonb)'
    )::oid;
    v_delete oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.delete_command(pg_catalog.jsonb)'
    )::oid;
    v_scoped_read_state oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.read_state_command_scoped(jsonb)'
    )::oid;
    v_scoped_delete oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.delete_command_scoped(jsonb)'
    )::oid;
BEGIN
    IF v_allowed IS NULL OR v_terminal IS NULL OR v_preference IS NULL OR v_read_state IS NULL
       OR v_delete IS NULL OR v_scoped_read_state IS NULL OR v_scoped_delete IS NULL
       OR NOT pg_catalog.has_function_privilege(v_client, v_scoped_read_state, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_api, v_scoped_read_state, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_client, v_scoped_delete, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_api, v_scoped_delete, 'EXECUTE')
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
            WHERE namespace.nspname = 'yoao_notifications_v1'
              AND pg_catalog.has_function_privilege(v_client, function.oid, 'EXECUTE')
              AND function.oid NOT IN (
                  v_allowed, v_terminal, v_preference, v_read_state, v_delete,
                  v_scoped_read_state, v_scoped_delete
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
\endif

\if :{?grant_delivery_read}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.notifications_owner', :'notifications_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.query_client', :'query_client', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.notifications_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.query_client'));
    v_function record;
BEGIN
    IF current_database()<>current_setting('yoao.expected_database')
       OR current_user<>current_setting('yoao.migration_role') THEN
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
            ('yoao_notifications_v1.read_notification(uuid)', 'yoao_notifications_v1.notification_facts', true),
            ('yoao_notifications_v1.find_terminal_notification(uuid,text,uuid)', 'uuid', false),
            ('yoao_notifications_v1.delivery_job_scopes(uuid[])', 'record', true),
            ('yoao_notifications_v1.delivery_member_pairs(jsonb)', 'record', true),
            ('yoao_notifications_v1.delivery_reviewer_pairs(jsonb)', 'record', true),
            ('yoao_notifications_v1.delivery_owner_pairs(jsonb)', 'record', true),
            ('yoao_notifications_v1.delivery_super_pairs(jsonb)', 'record', true)
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

REVOKE ALL ON SCHEMA yoao_notifications_v1 FROM :"query_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_notifications_v1 FROM :"query_client";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_notifications_v1, public FROM :"query_client";
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
        WHERE namespace.nspname IN ('public','yoao_notifications_v1')
          AND relation.relkind IN ('r','p','v','m','f')
    LOOP
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname), ',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns, v_relation.nspname, v_relation.relname,
                current_setting('yoao.query_client'));
        END IF;
    END LOOP;
END;
$revoke_columns$;
REVOKE ALL ON yoao_notifications_v1.notification_facts FROM :"api_role", PUBLIC;
REVOKE ALL (id,user_id,type,target_type,target_id,payload,read_at,created_at)
    ON yoao_notifications_v1.notification_facts FROM :"api_role", PUBLIC;
REVOKE EXECUTE ON FUNCTION
    yoao_notifications_v1.read_notification(uuid),
    yoao_notifications_v1.find_terminal_notification(uuid,text,uuid),
    yoao_notifications_v1.delivery_job_scopes(uuid[]),
    yoao_notifications_v1.delivery_member_pairs(jsonb),
    yoao_notifications_v1.delivery_reviewer_pairs(jsonb),
    yoao_notifications_v1.delivery_owner_pairs(jsonb),
    yoao_notifications_v1.delivery_super_pairs(jsonb)
    FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"query_client";
GRANT USAGE ON SCHEMA yoao_notifications_v1 TO :"query_client", :"api_role";
GRANT EXECUTE ON FUNCTION
    yoao_notifications_v1.read_notification(uuid),
    yoao_notifications_v1.find_terminal_notification(uuid,text,uuid),
    yoao_notifications_v1.delivery_job_scopes(uuid[]),
    yoao_notifications_v1.delivery_member_pairs(jsonb),
    yoao_notifications_v1.delivery_reviewer_pairs(jsonb),
    yoao_notifications_v1.delivery_owner_pairs(jsonb),
    yoao_notifications_v1.delivery_super_pairs(jsonb)
    TO :"query_client", :"api_role";

DO $verify$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.notifications_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.query_client'));
    v_allowed oid[] := ARRAY[
        'yoao_notifications_v1.read_notification(uuid)'::regprocedure::oid,
        'yoao_notifications_v1.find_terminal_notification(uuid,text,uuid)'::regprocedure::oid,
        'yoao_notifications_v1.delivery_job_scopes(uuid[])'::regprocedure::oid,
        'yoao_notifications_v1.delivery_member_pairs(jsonb)'::regprocedure::oid,
        'yoao_notifications_v1.delivery_reviewer_pairs(jsonb)'::regprocedure::oid,
        'yoao_notifications_v1.delivery_owner_pairs(jsonb)'::regprocedure::oid,
        'yoao_notifications_v1.delivery_super_pairs(jsonb)'::regprocedure::oid
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
            'yoao_notifications_v1.notification_facts','SELECT')
       OR pg_catalog.has_any_column_privilege(v_api,
            'yoao_notifications_v1.notification_facts','SELECT')
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
          AND (pg_catalog.left(namespace.nspname,4)='aap_' OR pg_catalog.left(namespace.nspname,5)='yoao_'
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
               OR (namespace.nspname NOT IN ('public','yoao_notifications_v1')
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
\endif

\if :{?grant_inbox}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.notifications_owner', :'notifications_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.query_client', :'query_client', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.notifications_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.query_client'));
    v_function record;
BEGIN
    IF current_database()<>current_setting('yoao.expected_database')
       OR current_user<>current_setting('yoao.migration_role') THEN
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
            ('yoao_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)',
                'yoao_notifications_v1.notification_facts', true),
            ('yoao_notifications_v1.inbox_total_count(uuid,text)', 'bigint', false),
            ('yoao_notifications_v1.inbox_unread_count(uuid,text)', 'bigint', false),
            ('yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer)',
                'yoao_notifications_v1.notification_facts', true),
            ('yoao_notifications_v1.inbox_total_count_scoped(uuid,text)', 'bigint', false),
            ('yoao_notifications_v1.inbox_unread_count_scoped(uuid,text)', 'bigint', false),
            ('yoao_notifications_v1._notification_is_migration_inventory(uuid)', 'boolean', false),
            ('yoao_notifications_v1._notification_unknown_job_scope(uuid)', 'boolean', false),
            ('yoao_notifications_v1._visible_inbox_rows_scoped(uuid,text)',
                'yoao_notifications_v1.notification_facts', true),
            ('yoao_notifications_v1._visible_inbox_rows(uuid,text)',
                'yoao_notifications_v1.notification_facts', true)
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

REVOKE ALL ON SCHEMA yoao_notifications_v1 FROM :"query_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_notifications_v1 FROM :"query_client";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_notifications_v1, public FROM :"query_client";
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
        WHERE namespace.nspname IN ('public','yoao_notifications_v1')
          AND relation.relkind IN ('r','p','v','m','f')
    LOOP
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname), ',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns, v_relation.nspname, v_relation.relname,
                current_setting('yoao.query_client'));
        END IF;
    END LOOP;
END;
$revoke_columns$;
REVOKE EXECUTE ON FUNCTION yoao_notifications_v1._visible_inbox_rows(uuid,text),
    yoao_notifications_v1._visible_inbox_rows_scoped(uuid,text),
    yoao_notifications_v1._notification_is_migration_inventory(uuid),
    yoao_notifications_v1._notification_unknown_job_scope(uuid)
    FROM :"api_role", :"query_client", PUBLIC;
REVOKE EXECUTE ON FUNCTION
    yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer),
    yoao_notifications_v1.inbox_total_count_scoped(uuid,text),
    yoao_notifications_v1.inbox_unread_count_scoped(uuid,text),
    yoao_notifications_v1.inbox_page(uuid,text,boolean,integer,integer),
    yoao_notifications_v1.inbox_total_count(uuid,text),
    yoao_notifications_v1.inbox_unread_count(uuid,text)
    FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"query_client";
GRANT USAGE ON SCHEMA yoao_notifications_v1 TO :"query_client", :"api_role";
GRANT EXECUTE ON FUNCTION
    yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer),
    yoao_notifications_v1.inbox_total_count_scoped(uuid,text),
    yoao_notifications_v1.inbox_unread_count_scoped(uuid,text),
    yoao_notifications_v1.inbox_page(uuid,text,boolean,integer,integer),
    yoao_notifications_v1.inbox_total_count(uuid,text),
    yoao_notifications_v1.inbox_unread_count(uuid,text)
    TO :"query_client", :"api_role";

DO $verify$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.notifications_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.query_client'));
    v_allowed oid[] := ARRAY[
        'yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer)'::regprocedure::oid,
        'yoao_notifications_v1.inbox_total_count_scoped(uuid,text)'::regprocedure::oid,
        'yoao_notifications_v1.inbox_unread_count_scoped(uuid,text)'::regprocedure::oid,
        'yoao_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)'::regprocedure::oid,
        'yoao_notifications_v1.inbox_total_count(uuid,text)'::regprocedure::oid,
        'yoao_notifications_v1.inbox_unread_count(uuid,text)'::regprocedure::oid
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
            'yoao_notifications_v1._visible_inbox_rows_scoped(uuid,text)'::regprocedure::oid,
            'yoao_notifications_v1._notification_is_migration_inventory(uuid)'::regprocedure::oid,
            'yoao_notifications_v1._notification_unknown_job_scope(uuid)'::regprocedure::oid,
            'yoao_notifications_v1._visible_inbox_rows(uuid,text)'::regprocedure::oid
        ]) AND acl.grantee=0 AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'PUBLIC retains inbox EXECUTE';
    END IF;
    IF pg_catalog.has_function_privilege(v_api,
            'yoao_notifications_v1._visible_inbox_rows(uuid,text)','EXECUTE')
       OR pg_catalog.has_function_privilege(v_api,
            'yoao_notifications_v1._visible_inbox_rows_scoped(uuid,text)','EXECUTE')
       OR pg_catalog.has_function_privilege(v_api,
            'yoao_notifications_v1._notification_is_migration_inventory(uuid)','EXECUTE')
       OR pg_catalog.has_function_privilege(v_api,
            'yoao_notifications_v1._notification_unknown_job_scope(uuid)','EXECUTE')
       OR pg_catalog.has_table_privilege(v_api,
            'yoao_notifications_v1.notification_facts','SELECT')
       OR pg_catalog.has_any_column_privilege(v_api,
            'yoao_notifications_v1.notification_facts','SELECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Inbox grants expose helpers or direct API fact reads';
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
          AND (pg_catalog.left(namespace.nspname,4)='aap_' OR pg_catalog.left(namespace.nspname,5)='yoao_'
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
               OR (namespace.nspname NOT IN ('public','yoao_notifications_v1')
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
        RAISE EXCEPTION 'Inbox LOGIN has data or capabilities beyond fixed inbox reads';
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
\endif

\if :{?grant_preferences}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.notifications_owner', :'notifications_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.preference_role', :'preference_role', true);

DO $guard$
DECLARE
    v_owner name := current_setting('yoao.notifications_owner');
    v_api name := current_setting('yoao.api_role');
    v_client name := current_setting('yoao.preference_role');
    v_owner_oid oid;
    v_api_oid oid;
    v_client_oid oid;
    v_command_oid oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.preference_command(jsonb)'
    )::oid;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR v_owner <> 'notifications_code'
       OR v_client <> 'yoao_notification_preferences_client'
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
               'yoao_notifications_v1._preference_policy()'
           )::oid) IS DISTINCT FROM v_owner_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_notifications_v1._preference_jsonb_truthy(jsonb)'
           )::oid) IS DISTINCT FROM v_owner_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_notifications_v1._preference_object_keys_valid(jsonb,text[],integer,integer)'
           )::oid) IS DISTINCT FROM v_owner_oid
       OR pg_catalog.has_table_privilege(
            v_client_oid, 'public.notification_preferences',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
            v_client_oid, 'public.notification_preferences', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_schema_privilege(v_client_oid, 'yoao_notifications_v1', 'CREATE')
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
REVOKE ALL ON SCHEMA yoao_notifications_v1 FROM :"preference_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_notifications_v1 FROM :"preference_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"preference_role";
GRANT USAGE ON SCHEMA yoao_notifications_v1 TO :"api_role", :"preference_role";
GRANT EXECUTE ON FUNCTION yoao_notifications_v1.preference_command(jsonb)
    TO :"api_role", :"preference_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname = current_setting('yoao.preference_role'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname = current_setting('yoao.api_role'));
    v_allowed oid := pg_catalog.to_regprocedure(
        'yoao_notifications_v1.preference_command(jsonb)'
    )::oid;
BEGIN
    IF NOT pg_catalog.has_function_privilege(v_client, v_allowed, 'EXECUTE')
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            WHERE namespace.nspname = 'yoao_notifications_v1'
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
\endif

\if :{?grant_read}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.notifications_owner', :'notifications_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);

DO $guard$
DECLARE
    v_owner name := current_setting('yoao.notifications_owner');
    v_api name := current_setting('yoao.api_role');
    v_owner_oid oid;
    v_api_oid oid;
    v_relation_oid oid := pg_catalog.to_regclass(
        'yoao_notifications_v1.notification_facts'
    )::oid;
    v_columns text[];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
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
REVOKE ALL ON yoao_notifications_v1.notification_facts FROM :"api_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_notifications_v1 TO :"api_role";
GRANT SELECT ON yoao_notifications_v1.notification_facts TO :"api_role";

DO $verify$
DECLARE
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname = current_setting('yoao.api_role'));
    v_facts oid := 'yoao_notifications_v1.notification_facts'::pg_catalog.regclass;
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
           v_api, 'yoao_notifications_v1', 'CREATE'
       ) THEN
        RAISE EXCEPTION 'Notifications API relation access exceeds SELECT-only contract';
    END IF;
END;
$verify$;
\endif

DO $$
BEGIN
    IF current_setting('yoao.grant_section_applied', true) IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION 'No capability section selected; pass -v grant_<capability>=1';
    END IF;
END $$;
COMMIT;
