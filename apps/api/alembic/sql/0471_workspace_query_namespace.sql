-- Move the three read-only Workspace query owners to the current namespace.
-- Legacy admission must be stopped, exact callers drained and the old roles
-- closed before this transaction. Function bodies are rewritten only for the
-- exact 21-signature surface captured before the schema rename; OIDs, owners
-- and ACLs stay untouched.
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
    v_signatures constant text[] := ARRAY[
        'aap_workspace_template_query_v1._has_exact_keys(jsonb,text[])',
        'aap_workspace_template_query_v1._template_row_json(public.project_templates)',
        'aap_workspace_template_query_v1.template_query_policy()',
        'aap_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text)',
        'aap_workspace_project_query_v1._has_exact_keys(jsonb,text[])',
        'aap_workspace_project_query_v1._project_query_step_impl(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)',
        'aap_workspace_project_query_v1._project_row_json(jsonb,integer,integer,integer,integer,text)',
        'aap_workspace_project_query_v1.project_query_policy()',
        'aap_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)',
        'aap_workspace_project_query_v1.project_search_member_ids(uuid,text)',
        'aap_workspace_project_query_v1.project_search_scope(uuid,text)',
        'aap_workspace_directory_query_v1._has_exact_keys(jsonb,text[])',
        'aap_workspace_directory_query_v1.directory_query_policy()',
        'aap_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)',
        'aap_inference_pipelines_v1.pipeline_step(jsonb,text,jsonb,text[],text[])',
        'aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)',
        'aap_notifications_v1._visible_inbox_rows(uuid,text)',
        'aap_notifications_v1.delivery_owner_pairs(jsonb)',
        'aap_notifications_v1.delivery_super_pairs(jsonb)',
        'aap_workspace_configuration_v1._class_rename_project_row(uuid,text)',
        'aap_workspace_configuration_v1._guide_asset_read_project_row(uuid)'
    ];
    v_hashes constant text[] := ARRAY[
        'f3501bfa87fdfa62a5378bff6aa98b4425002028cb190af79b74c88ecacd46c0',
        '35892c346c59ae915e9ebb65bb6a1f6b272c1a8e3417ea7e06c3bd5fae4e50b8',
        '23ce38a987aee6e92cda0c031f58ad5bd583def2da573f2e11d3028ede17b670',
        'b8bdfff7c178b98d00490e1ecb0673351459ac8d5439dac6d1d01076a56758d3',
        '8b0165fb7f3453071c3e3ff07c97852827755a289b6cc4fe13bc68053f1b9f9a',
        '7d247dd0c6f19515e32d2853710193b9076f8ee61ec2b13af821102250e2e73a',
        'a06ef963c7ba18a39ef223847e0e5bf01423a21c1728cdd03e0b230969383b49',
        '2baf59ba7ece567cf43f6cb11dab36ad61e84d6ffb39980d11781ab9924cfe4a',
        '2f33752df8bde23f33d41932449cd41515d61fb45fcc9862f518a9137e829ca6',
        '20c861dbf08110f9907b5755fdbbae6dc3a2c79a7eaaf03e5e8678886b726546',
        'ffea9e477aa3b57f57d7379290a76e1048081b6cd2db1be636140f50d4fe9cca',
        'd04d207530d6b2d4a095bfdad02d30158f1b32063290d2aea75665253ad3a1b8',
        '9f6e558254e3985aaa00c85fbd2551c438371aa72d8068cf006a917e76e337f5',
        '15540f1a0a656c223194a30c1bc29acc7b726e29dd0ec72273a1db36321de818',
        '7d7506a745b6ca43f97cc0012eafebc4c102a9153d5f657ec2a598909908a068',
        'd38f568d38eae3586e89a2d4776b3705c54549af29a25ea30b57483049d70197',
        '59b4520b6a47468eae2a80ed48fc305b1bc88964a94a60b6c1ce5dc4b50a2967',
        '7894b2cfc046378d418151ccaafbbc58422d26d190e93599020326cafd1b1bbd',
        '41d0ec6e1de40a79d8861e60a493d621ab65c6022dd3bec529de23da2d6bff99',
        '9f98a2804face59742b286bc91773d3056e1044c059e9a2b6208b4babe36e296',
        '941de280fa56ee5c3c5950eba8495fb1313818389c2724ce091b4a7fc599ab85'
    ];
    v_expected_counts constant integer[] := ARRAY[4, 7, 3];
    v_function oid;
    v_new_function oid;
    v_namespace oid;
    v_owner oid;
    v_acl text;
    v_before_owners oid[] := ARRAY[]::oid[];
    v_before_acls text[] := ARRAY[]::text[];
    v_oids oid[] := ARRAY[]::oid[];
    v_definition text;
    v_new_signature text;
    v_index integer;
    v_schema_index integer;
    v_role text;
    v_legacy oid;
    v_count integer;
BEGIN
    FOR v_schema_index IN 1..pg_catalog.cardinality(v_sources) LOOP
        v_namespace := pg_catalog.to_regnamespace(v_sources[v_schema_index]);
        IF v_namespace IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Workspace query source namespace is missing',
                DETAIL = v_sources[v_schema_index];
        END IF;
        IF pg_catalog.to_regnamespace(v_destinations[v_schema_index]) IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42P06',
                MESSAGE = 'Workspace query destination namespace already exists',
                DETAIL = v_destinations[v_schema_index];
        END IF;
        SELECT pg_catalog.count(*) INTO v_count
        FROM pg_catalog.pg_proc AS function
        WHERE function.pronamespace = v_namespace;
        IF v_count <> v_expected_counts[v_schema_index] THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query source namespace has an unexpected function count',
                DETAIL = v_sources[v_schema_index];
        END IF;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_class
            WHERE relnamespace = v_namespace
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_type
            WHERE typnamespace = v_namespace
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query source namespace is not a function-only surface',
                DETAIL = v_sources[v_schema_index];
        END IF;
    END LOOP;
    FOR v_index IN 1..pg_catalog.cardinality(v_signatures) LOOP
        v_function := pg_catalog.to_regprocedure(v_signatures[v_index])::oid;
        IF v_function IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Workspace query dependent function is missing',
                DETAIL = v_signatures[v_index];
        END IF;
        SELECT function.proowner, COALESCE(function.proacl::text, '')
        INTO v_owner, v_acl
        FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_function;
        IF NOT EXISTS (
            SELECT FROM pg_catalog.pg_proc AS function
            WHERE function.oid = v_function
              AND function.prokind = 'f'
              AND function.prosecdef
              AND COALESCE(function.proconfig, ARRAY[]::text[])
                  @> ARRAY['search_path=pg_catalog, pg_temp']::text[]
              AND pg_catalog.encode(
                  pg_catalog.sha256(pg_catalog.convert_to(function.prosrc, 'UTF8')),
                  'hex'
              ) = v_hashes[v_index]
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query function identity differs from the frozen surface',
                DETAIL = v_signatures[v_index];
        END IF;
        v_oids[v_index] := v_function;
        v_before_owners[v_index] := v_owner;
        v_before_acls[v_index] := v_acl;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        WHERE function.oid <> ALL (v_oids)
          AND (
              pg_catalog.strpos(function.prosrc, 'aap_workspace_template_query_v1.') > 0
              OR pg_catalog.strpos(function.prosrc, 'aap_workspace_project_query_v1.') > 0
              OR pg_catalog.strpos(function.prosrc, 'aap_workspace_directory_query_v1.') > 0
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace query namespace has an unlisted function dependent';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_views
        WHERE pg_catalog.strpos(definition, 'aap_workspace_template_query_v1.') > 0
           OR pg_catalog.strpos(definition, 'aap_workspace_project_query_v1.') > 0
           OR pg_catalog.strpos(definition, 'aap_workspace_directory_query_v1.') > 0
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_matviews
        WHERE pg_catalog.strpos(definition, 'aap_workspace_template_query_v1.') > 0
           OR pg_catalog.strpos(definition, 'aap_workspace_project_query_v1.') > 0
           OR pg_catalog.strpos(definition, 'aap_workspace_directory_query_v1.') > 0
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace query namespace has an unexpected view dependent';
    END IF;
    FOREACH v_role IN ARRAY v_legacy_roles LOOP
        SELECT role.oid INTO v_legacy
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = v_role;
        CONTINUE WHEN v_legacy IS NULL;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_roles AS role
            WHERE role.oid = v_legacy AND role.rolcanlogin
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55006',
                MESSAGE = 'Disable legacy Workspace query admission before namespace cutover',
                DETAIL = v_role;
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
                DETAIL = v_role;
        END IF;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname = ANY (v_sources)
              AND namespace.nspowner = v_legacy
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_proc AS function
            WHERE function.oid = ANY (v_oids)
              AND function.proowner = v_legacy
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Legacy Workspace query role owns query objects',
                DETAIL = v_role;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_stat_activity AS activity
        WHERE activity.datid = (
            SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()
        )
          AND activity.pid <> pg_catalog.pg_backend_pid()
          AND COALESCE(activity.backend_type, 'client backend') = 'client backend'
          AND activity.usesysid IS NOT NULL
          AND (
              EXISTS (
                  SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                  WHERE namespace.nspname = ANY (v_sources)
                    AND pg_catalog.has_schema_privilege(
                        activity.usesysid, namespace.oid, 'USAGE'
                    )
              )
              OR EXISTS (
                  SELECT 1
                  FROM pg_catalog.pg_proc AS function
                  JOIN pg_catalog.pg_namespace AS namespace
                    ON namespace.oid = function.pronamespace
                  WHERE namespace.nspname = ANY (v_sources)
                    AND pg_catalog.has_function_privilege(
                        activity.usesysid, function.oid, 'EXECUTE'
                    )
              )
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55006',
            MESSAGE = 'Drain Workspace query callers before namespace cutover';
    END IF;
    EXECUTE pg_catalog.format(
        'ALTER SCHEMA %I RENAME TO %I', v_sources[1], v_destinations[1]
    );
    EXECUTE pg_catalog.format(
        'ALTER SCHEMA %I RENAME TO %I', v_sources[2], v_destinations[2]
    );
    EXECUTE pg_catalog.format(
        'ALTER SCHEMA %I RENAME TO %I', v_sources[3], v_destinations[3]
    );
    FOR v_schema_index IN 1..pg_catalog.cardinality(v_destinations) LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM PUBLIC', v_destinations[v_schema_index]
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM PUBLIC',
            v_destinations[v_schema_index]
        );
    END LOOP;
    FOREACH v_role IN ARRAY v_legacy_roles LOOP
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = v_role
        ) THEN
            FOR v_schema_index IN 1..pg_catalog.cardinality(v_destinations) LOOP
                EXECUTE pg_catalog.format(
                    'REVOKE ALL ON SCHEMA %I FROM %I',
                    v_destinations[v_schema_index], v_role
                );
                EXECUTE pg_catalog.format(
                    'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
                    v_destinations[v_schema_index], v_role
                );
            END LOOP;
        END IF;
    END LOOP;
    FOR v_index IN 1..pg_catalog.cardinality(v_signatures) LOOP
        SELECT function.proowner, COALESCE(function.proacl::text, '')
        INTO v_owner, v_acl
        FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_oids[v_index];
        v_before_owners[v_index] := v_owner;
        v_before_acls[v_index] := v_acl;
    END LOOP;
    FOR v_index IN 1..pg_catalog.cardinality(v_signatures) LOOP
        v_definition := pg_catalog.pg_get_functiondef(v_oids[v_index]);
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
        v_new_signature := v_signatures[v_index];
        v_new_signature := pg_catalog.replace(
            v_new_signature,
            'aap_workspace_template_query_v1.',
            'yoao_workspace_template_query_v1.'
        );
        v_new_signature := pg_catalog.replace(
            v_new_signature,
            'aap_workspace_project_query_v1.',
            'yoao_workspace_project_query_v1.'
        );
        v_new_signature := pg_catalog.replace(
            v_new_signature,
            'aap_workspace_directory_query_v1.',
            'yoao_workspace_directory_query_v1.'
        );
        v_new_function := pg_catalog.to_regprocedure(v_new_signature)::oid;
        IF v_new_function IS DISTINCT FROM v_oids[v_index] THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query rewrite changed a function identity',
                DETAIL = v_signatures[v_index];
        END IF;
        SELECT function.proowner, COALESCE(function.proacl::text, '')
        INTO v_owner, v_acl
        FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_oids[v_index];
        IF v_owner IS DISTINCT FROM v_before_owners[v_index]
           OR v_acl IS DISTINCT FROM v_before_acls[v_index]
           OR NOT EXISTS (
               SELECT FROM pg_catalog.pg_proc AS function
               WHERE function.oid = v_oids[v_index]
                 AND function.prokind = 'f'
                 AND function.prosecdef
                 AND COALESCE(function.proconfig, ARRAY[]::text[])
                     @> ARRAY['search_path=pg_catalog, pg_temp']::text[]
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query rewrite changed owner, grants or identity',
                DETAIL = v_signatures[v_index];
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        WHERE pg_catalog.strpos(function.prosrc, 'aap_workspace_template_query_v1.') > 0
           OR pg_catalog.strpos(function.prosrc, 'aap_workspace_project_query_v1.') > 0
           OR pg_catalog.strpos(function.prosrc, 'aap_workspace_directory_query_v1.') > 0
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace query namespace still has legacy function references';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_views
        WHERE pg_catalog.strpos(definition, 'aap_workspace_template_query_v1.') > 0
           OR pg_catalog.strpos(definition, 'aap_workspace_project_query_v1.') > 0
           OR pg_catalog.strpos(definition, 'aap_workspace_directory_query_v1.') > 0
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_matviews
        WHERE pg_catalog.strpos(definition, 'aap_workspace_template_query_v1.') > 0
           OR pg_catalog.strpos(definition, 'aap_workspace_project_query_v1.') > 0
           OR pg_catalog.strpos(definition, 'aap_workspace_directory_query_v1.') > 0
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Workspace query namespace still has legacy view references';
    END IF;
    FOR v_schema_index IN 1..pg_catalog.cardinality(v_destinations) LOOP
        IF pg_catalog.to_regnamespace(v_sources[v_schema_index]) IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query legacy namespace still exists',
                DETAIL = v_sources[v_schema_index];
        END IF;
        SELECT pg_catalog.count(*) INTO v_count
        FROM pg_catalog.pg_proc AS function
        WHERE function.pronamespace =
              pg_catalog.to_regnamespace(v_destinations[v_schema_index]);
        IF v_count <> v_expected_counts[v_schema_index] THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace query destination namespace has an unexpected function count',
                DETAIL = v_destinations[v_schema_index];
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
                namespace.nspacl,
                pg_catalog.acldefault('n', namespace.nspowner)
            )) AS acl
            WHERE namespace.nspname = v_destinations[v_schema_index]
              AND acl.grantee = 0
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
                function.proacl,
                pg_catalog.acldefault('f', function.proowner)
            )) AS acl
            WHERE function.pronamespace =
                  pg_catalog.to_regnamespace(v_destinations[v_schema_index])
              AND acl.grantee = 0
              AND acl.privilege_type = 'EXECUTE'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Workspace query destination still grants PUBLIC',
                DETAIL = v_destinations[v_schema_index];
        END IF;
    END LOOP;
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
$cutover$;
