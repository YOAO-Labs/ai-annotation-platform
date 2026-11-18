-- Move the three read-only Workspace query owners to the current namespace.
-- Legacy query admission must be stopped and its callers drained before this transaction.
DO $cutover$
DECLARE
    v_sources constant text[] := ARRAY[
        'aap_workspace_template_query_v1',
        'aap_workspace_project_query_v1',
        'aap_workspace_directory_query_v1'
    ];
    v_destinations constant text[] := ARRAY[
        'yoao_workspace_template_query_v1',
        'yoao_workspace_project_query_v1',
        'yoao_workspace_directory_query_v1'
    ];
    v_legacy_roles constant text[] := ARRAY[
        'workspace_template_query_client',
        'workspace_project_query_client',
        'workspace_directory_query_client'
    ];
    v_signatures text[];
    v_namespace oid;
    v_expected integer;
    v_index integer;
    v_signature text;
    v_dependents text[];
    v_legacy oid;
    v_legacy_role text;
BEGIN
    FOR v_index IN 1..pg_catalog.cardinality(v_sources) LOOP
        v_namespace := pg_catalog.to_regnamespace(v_sources[v_index]);
        IF v_namespace IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Workspace query source namespace is missing',
                DETAIL = v_sources[v_index];
        END IF;
        IF pg_catalog.to_regnamespace(v_destinations[v_index]) IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42P06',
                MESSAGE = 'Workspace query destination namespace already exists',
                DETAIL = v_destinations[v_index];
        END IF;
        v_signatures := CASE v_index
            WHEN 1 THEN ARRAY[
                'aap_workspace_template_query_v1._has_exact_keys(jsonb,text[])',
                'aap_workspace_template_query_v1._template_row_json(public.project_templates)',
                'aap_workspace_template_query_v1.template_query_policy()',
                'aap_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text)'
            ]::text[]
            WHEN 2 THEN ARRAY[
                'aap_workspace_project_query_v1._has_exact_keys(jsonb,text[])',
                'aap_workspace_project_query_v1._project_query_step_impl(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)',
                'aap_workspace_project_query_v1._project_row_json(jsonb,integer,integer,integer,integer,text)',
                'aap_workspace_project_query_v1.project_query_policy()',
                'aap_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)',
                'aap_workspace_project_query_v1.project_search_member_ids(uuid,text)',
                'aap_workspace_project_query_v1.project_search_scope(uuid,text)'
            ]::text[]
            ELSE ARRAY[
                'aap_workspace_directory_query_v1._has_exact_keys(jsonb,text[])',
                'aap_workspace_directory_query_v1.directory_query_policy()',
                'aap_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)'
            ]::text[]
        END;
        SELECT pg_catalog.count(*) INTO v_expected
        FROM pg_catalog.pg_proc AS function
        WHERE function.pronamespace = v_namespace;
        IF v_expected <> pg_catalog.cardinality(v_signatures) THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query source namespace is not the owned function surface',
                DETAIL = v_sources[v_index];
        END IF;
        FOREACH v_signature IN ARRAY v_signatures LOOP
            IF pg_catalog.to_regprocedure(v_signature) IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = '42704',
                    MESSAGE = 'Workspace query source function is missing',
                    DETAIL = v_signature;
            END IF;
        END LOOP;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_proc AS function
            WHERE function.pronamespace = v_namespace
              AND (function.prokind <> 'f' OR NOT function.prosecdef)
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_class
            WHERE relnamespace = v_namespace
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_type
            WHERE typnamespace = v_namespace
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query source namespace is not the owned function surface',
                DETAIL = v_sources[v_index];
        END IF;
    END LOOP;
    SELECT pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname COLLATE "C",
                 function.proname COLLATE "C",
                 pg_catalog.oidvectortypes(function.proargtypes) COLLATE "C"
    ) INTO v_dependents
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE namespace.nspname <> ALL (v_sources)
      AND (
          function.prosrc LIKE '%aap_workspace_template_query_v1.%'
          OR function.prosrc LIKE '%aap_workspace_project_query_v1.%'
          OR function.prosrc LIKE '%aap_workspace_directory_query_v1.%'
      );
    IF v_dependents IS DISTINCT FROM ARRAY[
        'aap_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])',
        'aap_jobs_v1._async_job_list_rows(uuid, text, text[], text[], uuid, text)',
        'aap_notifications_v1._visible_inbox_rows(uuid, text)',
        'aap_notifications_v1.delivery_owner_pairs(jsonb)',
        'aap_notifications_v1.delivery_super_pairs(jsonb)',
        'aap_workspace_configuration_v1._class_rename_project_row(uuid, text)',
        'aap_workspace_configuration_v1._guide_asset_read_project_row(uuid)',
        'aap_workspace_v1.lock_project_row(uuid, boolean)',
        'aap_workspace_v1.project_access_step(uuid, uuid, text)'
    ]::text[] THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace query namespaces have unexpected function dependents',
            DETAIL = COALESCE(
                pg_catalog.array_to_string(v_dependents, ', '), 'none'
            );
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_views
        WHERE definition LIKE '%aap_workspace_template_query_v1.%'
           OR definition LIKE '%aap_workspace_project_query_v1.%'
           OR definition LIKE '%aap_workspace_directory_query_v1.%'
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_matviews
        WHERE definition LIKE '%aap_workspace_template_query_v1.%'
           OR definition LIKE '%aap_workspace_project_query_v1.%'
           OR definition LIKE '%aap_workspace_directory_query_v1.%'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace query namespaces have unexpected view dependents';
    END IF;
    FOREACH v_legacy_role IN ARRAY v_legacy_roles LOOP
        SELECT role.oid INTO v_legacy
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = v_legacy_role;
        CONTINUE WHEN v_legacy IS NULL;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_roles AS role
            WHERE role.oid = v_legacy AND role.rolcanlogin
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55006',
                MESSAGE = 'Disable legacy Workspace query admission before namespace cutover',
                DETAIL = v_legacy_role;
        END IF;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_roles AS role
            WHERE role.oid = v_legacy
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = v_legacy OR membership.roleid = v_legacy
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Legacy Workspace query role is not a closed client',
                DETAIL = v_legacy_role;
        END IF;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname = ANY (v_sources)
              AND namespace.nspowner = v_legacy
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_proc AS function
            WHERE function.pronamespace IN (
                SELECT oid FROM pg_catalog.pg_namespace
                WHERE nspname = ANY (v_sources)
            )
              AND function.proowner = v_legacy
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_class AS relation
            WHERE relation.relnamespace IN (
                SELECT oid FROM pg_catalog.pg_namespace
                WHERE nspname = ANY (v_sources)
            )
              AND relation.relowner = v_legacy
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_type AS type
            WHERE type.typnamespace IN (
                SELECT oid FROM pg_catalog.pg_namespace
                WHERE nspname = ANY (v_sources)
            )
              AND type.typowner = v_legacy
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Legacy Workspace query role owns query objects',
                DETAIL = v_legacy_role;
        END IF;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_stat_activity AS activity
            WHERE activity.usesysid = v_legacy
              AND activity.pid <> pg_catalog.pg_backend_pid()
              AND COALESCE(activity.backend_type, 'client backend') = 'client backend'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55006',
                MESSAGE = 'Drain Workspace query callers before namespace cutover',
                DETAIL = v_legacy_role;
        END IF;
    END LOOP;
END;
$cutover$;
-- aap-statement-break
ALTER SCHEMA aap_workspace_template_query_v1 RENAME TO yoao_workspace_template_query_v1;
-- aap-statement-break
ALTER SCHEMA aap_workspace_project_query_v1 RENAME TO yoao_workspace_project_query_v1;
-- aap-statement-break
ALTER SCHEMA aap_workspace_directory_query_v1 RENAME TO yoao_workspace_directory_query_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA yoao_workspace_template_query_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_workspace_template_query_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON SCHEMA yoao_workspace_project_query_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_workspace_project_query_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON SCHEMA yoao_workspace_directory_query_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_workspace_directory_query_v1 FROM PUBLIC;
-- aap-statement-break
DO $retire$
DECLARE
    v_destinations constant text[] := ARRAY[
        'yoao_workspace_template_query_v1',
        'yoao_workspace_project_query_v1',
        'yoao_workspace_directory_query_v1'
    ];
    v_legacy_roles constant text[] := ARRAY[
        'workspace_template_query_client',
        'workspace_project_query_client',
        'workspace_directory_query_client'
    ];
    v_destination text;
    v_role text;
BEGIN
    FOREACH v_destination IN ARRAY v_destinations LOOP
        FOREACH v_role IN ARRAY v_legacy_roles LOOP
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = v_role
            ) THEN
                EXECUTE pg_catalog.format(
                    'REVOKE ALL ON SCHEMA %I FROM %I', v_destination, v_role
                );
                EXECUTE pg_catalog.format(
                    'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
                    v_destination, v_role
                );
                EXECUTE pg_catalog.format(
                    'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
                    v_destination, v_role
                );
                EXECUTE pg_catalog.format(
                    'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
                    v_destination, v_role
                );
            END IF;
        END LOOP;
    END LOOP;
END;
$retire$;
-- aap-statement-break
DO $rewrite$
DECLARE
    v_targets oid[];
    v_target oid;
    v_definition text;
BEGIN
    SELECT pg_catalog.array_agg(function.oid ORDER BY function.oid)
    INTO v_targets
    FROM pg_catalog.pg_proc AS function
    WHERE function.prosrc LIKE '%aap_workspace_template_query_v1.%'
       OR function.prosrc LIKE '%aap_workspace_project_query_v1.%'
       OR function.prosrc LIKE '%aap_workspace_directory_query_v1.%';
    IF v_targets IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace query namespace rewrite found no functions';
    END IF;
    FOREACH v_target IN ARRAY v_targets LOOP
        v_definition := pg_catalog.pg_get_functiondef(v_target);
        v_definition := pg_catalog.replace(
            v_definition,
            'aap_workspace_template_query_v1.',
            'yoao_workspace_template_query_v1.'
        );
        v_definition := pg_catalog.replace(
            v_definition,
            'aap_workspace_project_query_v1.',
            'yoao_workspace_project_query_v1.'
        );
        v_definition := pg_catalog.replace(
            v_definition,
            'aap_workspace_directory_query_v1.',
            'yoao_workspace_directory_query_v1.'
        );
        EXECUTE v_definition;
    END LOOP;
END;
$rewrite$;
-- aap-statement-break
DO $verify$
DECLARE
    v_sources constant text[] := ARRAY[
        'aap_workspace_template_query_v1',
        'aap_workspace_project_query_v1',
        'aap_workspace_directory_query_v1'
    ];
    v_destinations constant text[] := ARRAY[
        'yoao_workspace_template_query_v1',
        'yoao_workspace_project_query_v1',
        'yoao_workspace_directory_query_v1'
    ];
    v_legacy_roles constant text[] := ARRAY[
        'workspace_template_query_client',
        'workspace_project_query_client',
        'workspace_directory_query_client'
    ];
    v_signatures text[];
    v_namespace oid;
    v_expected integer;
    v_index integer;
    v_signature text;
    v_role text;
BEGIN
    FOREACH v_signature IN ARRAY v_sources LOOP
        IF pg_catalog.to_regnamespace(v_signature) IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query legacy namespace still exists',
                DETAIL = v_signature;
        END IF;
    END LOOP;
    FOR v_index IN 1..pg_catalog.cardinality(v_destinations) LOOP
        v_namespace := pg_catalog.to_regnamespace(v_destinations[v_index]);
        IF v_namespace IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Workspace query destination namespace is missing',
                DETAIL = v_destinations[v_index];
        END IF;
        v_signatures := CASE v_index
            WHEN 1 THEN ARRAY[
                'yoao_workspace_template_query_v1._has_exact_keys(jsonb,text[])',
                'yoao_workspace_template_query_v1._template_row_json(public.project_templates)',
                'yoao_workspace_template_query_v1.template_query_policy()',
                'yoao_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text)'
            ]::text[]
            WHEN 2 THEN ARRAY[
                'yoao_workspace_project_query_v1._has_exact_keys(jsonb,text[])',
                'yoao_workspace_project_query_v1._project_query_step_impl(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)',
                'yoao_workspace_project_query_v1._project_row_json(jsonb,integer,integer,integer,integer,text)',
                'yoao_workspace_project_query_v1.project_query_policy()',
                'yoao_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)',
                'yoao_workspace_project_query_v1.project_search_member_ids(uuid,text)',
                'yoao_workspace_project_query_v1.project_search_scope(uuid,text)'
            ]::text[]
            ELSE ARRAY[
                'yoao_workspace_directory_query_v1._has_exact_keys(jsonb,text[])',
                'yoao_workspace_directory_query_v1.directory_query_policy()',
                'yoao_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)'
            ]::text[]
        END;
        SELECT pg_catalog.count(*) INTO v_expected
        FROM pg_catalog.pg_proc AS function
        WHERE function.pronamespace = v_namespace;
        IF v_expected <> pg_catalog.cardinality(v_signatures) THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query destination namespace is not the owned function surface',
                DETAIL = v_destinations[v_index];
        END IF;
        FOREACH v_signature IN ARRAY v_signatures LOOP
            IF pg_catalog.to_regprocedure(v_signature) IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = '42704',
                    MESSAGE = 'Workspace query destination function is missing',
                    DETAIL = v_signature;
            END IF;
        END LOOP;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_proc AS function
            WHERE function.pronamespace = v_namespace
              AND (function.prokind <> 'f' OR NOT function.prosecdef)
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_class
            WHERE relnamespace = v_namespace
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_type
            WHERE typnamespace = v_namespace
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query destination namespace is not the owned function surface',
                DETAIL = v_destinations[v_index];
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
                namespace.nspacl,
                pg_catalog.acldefault('n', namespace.nspowner)
            )) AS acl
            WHERE namespace.oid = v_namespace
              AND acl.grantee = 0
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Workspace query destination schema still grants PUBLIC',
                DETAIL = v_destinations[v_index];
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
                function.proacl,
                pg_catalog.acldefault('f', function.proowner)
            )) AS acl
            WHERE function.pronamespace = v_namespace
              AND acl.grantee = 0
              AND acl.privilege_type = 'EXECUTE'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Workspace query destination function still grants PUBLIC',
                DETAIL = v_destinations[v_index];
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        WHERE function.prosrc LIKE '%aap_workspace_template_query_v1.%'
           OR function.prosrc LIKE '%aap_workspace_project_query_v1.%'
           OR function.prosrc LIKE '%aap_workspace_directory_query_v1.%'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace query namespace still has legacy function references';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_views
        WHERE definition LIKE '%aap_workspace_template_query_v1.%'
           OR definition LIKE '%aap_workspace_project_query_v1.%'
           OR definition LIKE '%aap_workspace_directory_query_v1.%'
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_matviews
        WHERE definition LIKE '%aap_workspace_template_query_v1.%'
           OR definition LIKE '%aap_workspace_project_query_v1.%'
           OR definition LIKE '%aap_workspace_directory_query_v1.%'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace query namespace still has legacy view references';
    END IF;
    FOREACH v_role IN ARRAY v_legacy_roles LOOP
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = v_role
              AND (
                  pg_catalog.has_schema_privilege(
                      role.oid, 'yoao_workspace_template_query_v1', 'USAGE'
                  )
                  OR pg_catalog.has_schema_privilege(
                      role.oid, 'yoao_workspace_project_query_v1', 'USAGE'
                  )
                  OR pg_catalog.has_schema_privilege(
                      role.oid, 'yoao_workspace_directory_query_v1', 'USAGE'
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_proc AS function
                      WHERE function.pronamespace IN (
                          SELECT oid FROM pg_catalog.pg_namespace
                          WHERE nspname IN (
                              'yoao_workspace_template_query_v1',
                              'yoao_workspace_project_query_v1',
                              'yoao_workspace_directory_query_v1'
                          )
                      )
                        AND pg_catalog.has_function_privilege(
                            role.oid, function.oid, 'EXECUTE'
                        )
                  )
              )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Legacy Workspace query role retains capabilities',
                DETAIL = v_role;
        END IF;
    END LOOP;
END;
$verify$;
