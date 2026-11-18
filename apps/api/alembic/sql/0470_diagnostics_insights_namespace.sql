-- Move the Diagnostics and Insights owner capabilities to their current YOAO
-- namespaces. Both namespaces contain only SECURITY DEFINER functions owned by
-- their module owner roles; the Workspace project-deletion and dataset-unlink
-- functions are the only SQL callers. Stop admission and drain every writer
-- that can enter either caller before this transaction.
--
-- The three capability bodies and the two caller bodies are drift checked
-- against immutable prose digests. Each caller replacement changes only the
-- allowlisted schema-qualified references; the post-cutover digest is derived
-- from the same frozen prose and verified again after replacement.

DO $cutover$
DECLARE
    v_diagnostics oid := pg_catalog.to_regnamespace('aap_diagnostics_v1');
    v_insights oid := pg_catalog.to_regnamespace('aap_insights_v1');
    v_functions text[];
    v_expected_dependents text[] := ARRAY[
        'aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)',
        'aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)'
    ]::text[];
    v_dependents text[];
    v_item record;
    v_signature text;
    v_function oid;
    v_row pg_catalog.pg_proc%ROWTYPE;
    v_acl text[];
    v_schema_oid oid;
    v_schema_owner oid;
    v_body_hash text;
    v_new_body_hash text;
    v_state jsonb := '{}'::jsonb;
BEGIN
    IF v_diagnostics IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42704',
            MESSAGE = 'Diagnostics source namespace is missing';
    END IF;
    IF v_insights IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42704',
            MESSAGE = 'Insights source namespace is missing';
    END IF;
    IF pg_catalog.to_regnamespace('yoao_diagnostics_v1') IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42P06',
            MESSAGE = 'Diagnostics destination namespace already exists';
    END IF;
    IF pg_catalog.to_regnamespace('yoao_insights_v1') IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42P06',
            MESSAGE = 'Insights destination namespace already exists';
    END IF;

    SELECT pg_catalog.array_agg(
        function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY function.proname COLLATE "C"
    ) INTO v_functions
    FROM pg_catalog.pg_proc AS function
    WHERE function.pronamespace = v_diagnostics;
    IF v_functions IS DISTINCT FROM ARRAY[
        'detach_dataset_task_references(uuid, uuid)',
        'detach_project_references(uuid)'
    ]::text[] THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics source namespace is not the owned function surface';
    END IF;

    SELECT pg_catalog.array_agg(
        function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY function.proname COLLATE "C"
    ) INTO v_functions
    FROM pg_catalog.pg_proc AS function
    WHERE function.pronamespace = v_insights;
    IF v_functions IS DISTINCT FROM ARRAY[
        'clear_project_task_views(uuid)'
    ]::text[] THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Insights source namespace is not the owned function surface';
    END IF;

    IF EXISTS (
        SELECT FROM pg_catalog.pg_class
        WHERE relnamespace IN (v_diagnostics, v_insights)
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_type
        WHERE typnamespace IN (v_diagnostics, v_insights)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics or Insights namespace contains a non-function object';
    END IF;

    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.pronamespace IN (v_diagnostics, v_insights)
          AND (
              function.prokind <> 'f'
              OR NOT function.prosecdef
              OR function.provolatile <> 'v'
              OR function.proparallel <> 'u'
              OR function.proconfig IS DISTINCT FROM
                 ARRAY['search_path=pg_catalog, pg_temp']::text[]
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics or Insights namespace is not a fixed owner capability';
    END IF;

    FOREACH v_signature IN ARRAY ARRAY[
        'aap_diagnostics_v1.detach_dataset_task_references(uuid, uuid)',
        'aap_diagnostics_v1.detach_project_references(uuid)',
        'aap_insights_v1.clear_project_task_views(uuid)'
    ]::text[] LOOP
        v_function := pg_catalog.to_regprocedure(v_signature);
        IF v_function IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Diagnostics or Insights capability is missing: ' ||
                    v_signature;
        END IF;
        SELECT * INTO v_row FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_function;
        v_body_hash := pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(v_row.prosrc, 'UTF8')
            ),
            'hex'
        );
        IF v_body_hash IS DISTINCT FROM (CASE v_signature
            WHEN 'aap_diagnostics_v1.detach_dataset_task_references(uuid, uuid)'
                THEN '5cea043d2fdcc088ee1d1842bb94a21044075271b01249082f9fca8db0068e22'
            WHEN 'aap_diagnostics_v1.detach_project_references(uuid)'
                THEN '31aa9c50715a42faed14a878409e2cbed17694177992a8216b5b22985c9d5abf'
            ELSE
                'bf382b1f3653dfeffd04f09a8787f3a9861dc116fd63fe23f1a3d92f17eb20f7'
        END) THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Diagnostics or Insights capability body drifted: ' ||
                    v_signature;
        END IF;
        v_acl := ARRAY(
            SELECT pg_catalog.format(
                '%s:%s:%s',
                COALESCE(grantee.rolname, 'PUBLIC'),
                entry.privilege_type,
                entry.is_grantable
            )
            FROM pg_catalog.aclexplode(
                COALESCE(
                    v_row.proacl,
                    pg_catalog.acldefault('f', v_row.proowner)
                )
            ) AS entry
            LEFT JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = entry.grantee
            WHERE entry.grantee <> 0
            ORDER BY 1
        );
        v_state := v_state || pg_catalog.jsonb_build_object(
            'capability:' || v_signature,
            pg_catalog.jsonb_build_object(
                'oid', v_function,
                'owner', v_row.proowner,
                'acl', pg_catalog.to_jsonb(v_acl),
                'security_definer', v_row.prosecdef,
                'config', pg_catalog.to_jsonb(v_row.proconfig),
                'volatility', v_row.provolatile,
                'parallel', v_row.proparallel,
                'kind', v_row.prokind,
                'is_strict', v_row.proisstrict,
                'language', v_row.prolang,
                'body_sha256', v_body_hash
            )
        );
    END LOOP;

    SELECT pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname COLLATE "C",
                 function.proname COLLATE "C"
    ) INTO v_dependents
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE function.pronamespace NOT IN (v_diagnostics, v_insights)
      AND (
          function.prosrc LIKE '%aap_diagnostics_v1.%'
          OR function.prosrc LIKE '%aap_insights_v1.%'
      );
    IF v_dependents IS DISTINCT FROM v_expected_dependents THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics or Insights namespace has unexpected callers',
            DETAIL = COALESCE(
                pg_catalog.array_to_string(v_dependents, ', '), 'none'
            );
    END IF;

    FOREACH v_signature IN ARRAY v_expected_dependents LOOP
        v_function := pg_catalog.to_regprocedure(v_signature);
        IF v_function IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Workspace capability is missing: ' || v_signature;
        END IF;
        SELECT * INTO v_row FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_function;
        IF v_row.prokind <> 'f'
           OR NOT v_row.prosecdef
           OR v_row.provolatile <> 'v'
           OR v_row.proparallel <> 'u'
           OR v_row.proconfig IS DISTINCT FROM
              ARRAY['search_path=pg_catalog, pg_temp']::text[] THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace caller is not a fixed owner capability: ' ||
                    v_signature;
        END IF;
        v_body_hash := pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(v_row.prosrc, 'UTF8')
            ),
            'hex'
        );
        IF v_signature = 'aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)'
        THEN
            IF v_body_hash IS DISTINCT FROM
               '85794d8f70ed17bd40e7520ce1f93af89ed44ea912d9faf3b4cdaa2afdbbd4ad'
               OR (
                   pg_catalog.length(v_row.prosrc) -
                   pg_catalog.length(
                       pg_catalog.replace(
                           v_row.prosrc, 'aap_diagnostics_v1.', ''
                       )
                   )
               ) / pg_catalog.length('aap_diagnostics_v1.') <> 1
               OR (
                   pg_catalog.length(v_row.prosrc) -
                   pg_catalog.length(
                       pg_catalog.replace(
                           v_row.prosrc, 'aap_insights_v1.', ''
                       )
                   )
               ) / pg_catalog.length('aap_insights_v1.') <> 1
               OR pg_catalog.strpos(v_row.prosrc, 'yoao_diagnostics_v1.') <> 0
               OR pg_catalog.strpos(v_row.prosrc, 'yoao_insights_v1.') <> 0 THEN
                RAISE EXCEPTION USING ERRCODE = '55000',
                    MESSAGE = 'Workspace project-deletion body drifted before cutover';
            END IF;
            v_new_body_hash :=
                'bbe50a16726ebb954b9594d4c923b72a995d61158a1bb8613d0203a4df51bc82';
        ELSE
            IF v_body_hash IS DISTINCT FROM
               '9f9a140bf2ec88b784d36273c7ba2f31f8b4b035db5a4ebac3ed21cc544798f2'
               OR (
                   pg_catalog.length(v_row.prosrc) -
                   pg_catalog.length(
                       pg_catalog.replace(
                           v_row.prosrc, 'aap_diagnostics_v1.', ''
                       )
                   )
               ) / pg_catalog.length('aap_diagnostics_v1.') <> 1
               OR pg_catalog.strpos(v_row.prosrc, 'aap_insights_v1.') <> 0
               OR pg_catalog.strpos(v_row.prosrc, 'yoao_diagnostics_v1.') <> 0
               OR pg_catalog.strpos(v_row.prosrc, 'yoao_insights_v1.') <> 0 THEN
                RAISE EXCEPTION USING ERRCODE = '55000',
                    MESSAGE = 'Workspace dataset-unlink body drifted before cutover';
            END IF;
            v_new_body_hash :=
                'ffe8c2a35e60d832c4f0907d8cef9a74c69dc0e96c488bb7169cb267e38a97c4';
        END IF;
        IF pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(
                    pg_catalog.replace(
                        pg_catalog.replace(
                            v_row.prosrc,
                            'aap_diagnostics_v1.',
                            'yoao_diagnostics_v1.'
                        ),
                        'aap_insights_v1.',
                        'yoao_insights_v1.'
                    ),
                    'UTF8'
                )
            ),
            'hex'
        ) IS DISTINCT FROM v_new_body_hash THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace caller replacement derivation drifted: ' ||
                    v_signature;
        END IF;
        v_acl := ARRAY(
            SELECT pg_catalog.format(
                '%s:%s:%s',
                COALESCE(grantee.rolname, 'PUBLIC'),
                entry.privilege_type,
                entry.is_grantable
            )
            FROM pg_catalog.aclexplode(
                COALESCE(
                    v_row.proacl,
                    pg_catalog.acldefault('f', v_row.proowner)
                )
            ) AS entry
            LEFT JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = entry.grantee
            WHERE entry.grantee <> 0
            ORDER BY 1
        );
        v_state := v_state || pg_catalog.jsonb_build_object(
            'caller:' || v_signature,
            pg_catalog.jsonb_build_object(
                'oid', v_function,
                'owner', v_row.proowner,
                'acl', pg_catalog.to_jsonb(v_acl),
                'security_definer', v_row.prosecdef,
                'config', pg_catalog.to_jsonb(v_row.proconfig),
                'volatility', v_row.provolatile,
                'parallel', v_row.proparallel,
                'kind', v_row.prokind,
                'is_strict', v_row.proisstrict,
                'language', v_row.prolang,
                'body_sha256', v_body_hash,
                'new_body_sha256', v_new_body_hash
            )
        );
    END LOOP;

    FOR v_item IN
        SELECT * FROM (
            VALUES
                ('aap_diagnostics_v1'::text),
                ('aap_insights_v1'::text)
        ) AS item(schema_name)
    LOOP
        SELECT namespace.oid,
               namespace.nspowner,
               ARRAY(
                   SELECT pg_catalog.format(
                       '%s:%s:%s',
                       COALESCE(grantee.rolname, 'PUBLIC'),
                       entry.privilege_type,
                       entry.is_grantable
                   )
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           namespace.nspacl,
                           pg_catalog.acldefault('n', namespace.nspowner)
                       )
                   ) AS entry
                   LEFT JOIN pg_catalog.pg_roles AS grantee
                     ON grantee.oid = entry.grantee
                   WHERE entry.grantee <> 0
                   ORDER BY 1
               )
        INTO v_schema_oid, v_schema_owner, v_acl
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = v_item.schema_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Owner capability schema is missing: ' ||
                    v_item.schema_name;
        END IF;
        v_state := v_state || pg_catalog.jsonb_build_object(
            'schema:' || v_item.schema_name,
            pg_catalog.jsonb_build_object(
                'oid', v_schema_oid,
                'owner', v_schema_owner,
                'acl', pg_catalog.to_jsonb(v_acl)
            )
        );
    END LOOP;

    IF EXISTS (
        SELECT FROM pg_catalog.pg_stat_activity AS activity
        WHERE activity.datid = (
            SELECT db.oid FROM pg_catalog.pg_database AS db
            WHERE db.datname = pg_catalog.current_database()
        )
          AND activity.pid <> pg_catalog.pg_backend_pid()
          AND COALESCE(activity.backend_type, 'client backend') = 'client backend'
          AND activity.usesysid IS NOT NULL
          AND (
              pg_catalog.has_schema_privilege(
                  activity.usesysid, v_diagnostics, 'USAGE'
              )
              OR pg_catalog.has_schema_privilege(
                  activity.usesysid, v_insights, 'USAGE'
              )
              OR pg_catalog.has_function_privilege(
                  activity.usesysid,
                  'aap_diagnostics_v1.detach_dataset_task_references(uuid,uuid)',
                  'EXECUTE'
              )
              OR pg_catalog.has_function_privilege(
                  activity.usesysid,
                  'aap_diagnostics_v1.detach_project_references(uuid)',
                  'EXECUTE'
              )
              OR pg_catalog.has_function_privilege(
                  activity.usesysid,
                  'aap_insights_v1.clear_project_task_views(uuid)',
                  'EXECUTE'
              )
              OR pg_catalog.has_function_privilege(
                  activity.usesysid,
                  'aap_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)',
                  'EXECUTE'
              )
              OR pg_catalog.has_function_privilege(
                  activity.usesysid,
                  'aap_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)',
                  'EXECUTE'
              )
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55006',
            MESSAGE = 'Drain Diagnostics, Insights and Workspace deletion writers before namespace cutover';
    END IF;

    PERFORM pg_catalog.set_config(
        'yoao.n5_diagnostics_insights_pre_state', v_state::text, true
    );
END;
$cutover$;
-- aap-statement-break
ALTER SCHEMA aap_diagnostics_v1 RENAME TO yoao_diagnostics_v1;
-- aap-statement-break
ALTER SCHEMA aap_insights_v1 RENAME TO yoao_insights_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA yoao_diagnostics_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON SCHEMA yoao_insights_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_diagnostics_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_insights_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.unlink_dataset(
        uuid, uuid, uuid, uuid, text, jsonb
    ) FROM PUBLIC;
-- aap-statement-break
DO $patch$
DECLARE
    v_state jsonb := pg_catalog.current_setting(
        'yoao.n5_diagnostics_insights_pre_state', true
    )::jsonb;
    v_signature text;
    v_expected jsonb;
    v_function oid;
    v_row pg_catalog.pg_proc%ROWTYPE;
    v_definition text;
    v_acl text[];
    v_body_hash text;
BEGIN
    IF v_state IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics and Insights pre-cutover state is missing';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)',
        'aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)'
    ]::text[] LOOP
        v_expected := v_state -> ('caller:' || v_signature);
        IF v_expected IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace caller state is missing: ' || v_signature;
        END IF;
        v_function := pg_catalog.to_regprocedure(v_signature);
        IF v_function IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Workspace caller is missing: ' || v_signature;
        END IF;
        SELECT * INTO v_row FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_function;
        v_body_hash := pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(v_row.prosrc, 'UTF8')
            ),
            'hex'
        );
        IF v_body_hash IS DISTINCT FROM (v_expected ->> 'body_sha256')
           OR v_function IS DISTINCT FROM
              (v_expected ->> 'oid')::pg_catalog.oid
           OR v_row.proowner IS DISTINCT FROM
              (v_expected ->> 'owner')::pg_catalog.oid
           OR v_row.prosecdef IS DISTINCT FROM
              (v_expected ->> 'security_definer')::boolean
           OR pg_catalog.to_jsonb(v_row.proconfig) IS DISTINCT FROM
              (v_expected -> 'config')
           OR v_row.provolatile::text IS DISTINCT FROM
              (v_expected ->> 'volatility')
           OR v_row.proparallel::text IS DISTINCT FROM
              (v_expected ->> 'parallel')
           OR v_row.prokind::text IS DISTINCT FROM
              (v_expected ->> 'kind')
           OR v_row.proisstrict IS DISTINCT FROM
              (v_expected ->> 'is_strict')::boolean
           OR v_row.prolang IS DISTINCT FROM
              (v_expected ->> 'language')::pg_catalog.oid THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace caller drifted before cutover: ' ||
                    v_signature;
        END IF;
        v_acl := ARRAY(
            SELECT pg_catalog.format(
                '%s:%s:%s',
                COALESCE(grantee.rolname, 'PUBLIC'),
                entry.privilege_type,
                entry.is_grantable
            )
            FROM pg_catalog.aclexplode(
                COALESCE(
                    v_row.proacl,
                    pg_catalog.acldefault('f', v_row.proowner)
                )
            ) AS entry
            LEFT JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = entry.grantee
            WHERE entry.grantee <> 0
            ORDER BY 1
        );
        IF pg_catalog.to_jsonb(v_acl) IS DISTINCT FROM (v_expected -> 'acl') THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Workspace caller ACL drifted before cutover: ' ||
                    v_signature;
        END IF;

        v_definition := pg_catalog.pg_get_functiondef(v_function);
        IF v_definition IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Workspace caller definition is missing: ' ||
                    v_signature;
        END IF;
        IF v_signature = 'aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)'
        THEN
            IF (
                pg_catalog.length(v_definition) -
                pg_catalog.length(
                    pg_catalog.replace(
                        v_definition, 'aap_diagnostics_v1.', ''
                    )
                )
            ) / pg_catalog.length('aap_diagnostics_v1.') <> 1
               OR (
                   pg_catalog.length(v_definition) -
                   pg_catalog.length(
                       pg_catalog.replace(
                           v_definition, 'aap_insights_v1.', ''
                       )
                   )
               ) / pg_catalog.length('aap_insights_v1.') <> 1
               OR pg_catalog.strpos(v_definition, 'yoao_diagnostics_v1.') <> 0
               OR pg_catalog.strpos(v_definition, 'yoao_insights_v1.') <> 0 THEN
                RAISE EXCEPTION USING ERRCODE = '55000',
                    MESSAGE = 'Workspace project-deletion definition drifted: ' ||
                        v_signature;
            END IF;
            v_definition := pg_catalog.replace(
                v_definition, 'aap_diagnostics_v1.', 'yoao_diagnostics_v1.'
            );
            v_definition := pg_catalog.replace(
                v_definition, 'aap_insights_v1.', 'yoao_insights_v1.'
            );
        ELSE
            IF (
                pg_catalog.length(v_definition) -
                pg_catalog.length(
                    pg_catalog.replace(
                        v_definition, 'aap_diagnostics_v1.', ''
                    )
                )
            ) / pg_catalog.length('aap_diagnostics_v1.') <> 1
               OR pg_catalog.strpos(v_definition, 'aap_insights_v1.') <> 0
               OR pg_catalog.strpos(v_definition, 'yoao_diagnostics_v1.') <> 0
               OR pg_catalog.strpos(v_definition, 'yoao_insights_v1.') <> 0 THEN
                RAISE EXCEPTION USING ERRCODE = '55000',
                    MESSAGE = 'Workspace dataset-unlink definition drifted: ' ||
                        v_signature;
            END IF;
            v_definition := pg_catalog.replace(
                v_definition, 'aap_diagnostics_v1.', 'yoao_diagnostics_v1.'
            );
        END IF;
        IF pg_catalog.strpos(v_definition, 'aap_diagnostics_v1.') <> 0
           OR pg_catalog.strpos(v_definition, 'aap_insights_v1.') <> 0 THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace caller still references a legacy namespace: ' ||
                    v_signature;
        END IF;

        EXECUTE v_definition;

        SELECT * INTO v_row FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_function;
        v_body_hash := pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(v_row.prosrc, 'UTF8')
            ),
            'hex'
        );
        IF v_body_hash IS DISTINCT FROM (v_expected ->> 'new_body_sha256')
           OR pg_catalog.strpos(v_row.prosrc, 'aap_diagnostics_v1.') <> 0
           OR pg_catalog.strpos(v_row.prosrc, 'aap_insights_v1.') <> 0 THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace caller patch result is invalid: ' ||
                    v_signature;
        END IF;
        IF v_row.proowner IS DISTINCT FROM
              (v_expected ->> 'owner')::pg_catalog.oid
           OR v_row.prosecdef IS DISTINCT FROM
              (v_expected ->> 'security_definer')::boolean
           OR pg_catalog.to_jsonb(v_row.proconfig) IS DISTINCT FROM
              (v_expected -> 'config')
           OR v_row.provolatile::text IS DISTINCT FROM
              (v_expected ->> 'volatility')
           OR v_row.proparallel::text IS DISTINCT FROM
              (v_expected ->> 'parallel')
           OR v_row.prokind::text IS DISTINCT FROM
              (v_expected ->> 'kind')
           OR v_row.proisstrict IS DISTINCT FROM
              (v_expected ->> 'is_strict')::boolean
           OR v_row.prolang IS DISTINCT FROM
              (v_expected ->> 'language')::pg_catalog.oid THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Workspace caller security changed during cutover: ' ||
                    v_signature;
        END IF;
        v_acl := ARRAY(
            SELECT pg_catalog.format(
                '%s:%s:%s',
                COALESCE(grantee.rolname, 'PUBLIC'),
                entry.privilege_type,
                entry.is_grantable
            )
            FROM pg_catalog.aclexplode(
                COALESCE(
                    v_row.proacl,
                    pg_catalog.acldefault('f', v_row.proowner)
                )
            ) AS entry
            LEFT JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = entry.grantee
            WHERE entry.grantee <> 0
            ORDER BY 1
        );
        IF pg_catalog.to_jsonb(v_acl) IS DISTINCT FROM (v_expected -> 'acl') THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Workspace caller ACL changed during cutover: ' ||
                    v_signature;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.aclexplode(
                COALESCE(
                    v_row.proacl,
                    pg_catalog.acldefault('f', v_row.proowner)
                )
            ) AS entry
            WHERE entry.grantee = 0
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Workspace caller retains a PUBLIC grant: ' ||
                    v_signature;
        END IF;
    END LOOP;
END;
$patch$;
-- aap-statement-break
DO $verify$
DECLARE
    v_state jsonb := pg_catalog.current_setting(
        'yoao.n5_diagnostics_insights_pre_state', true
    )::jsonb;
    v_item record;
    v_expected jsonb;
    v_function oid;
    v_row pg_catalog.pg_proc%ROWTYPE;
    v_acl text[];
    v_functions text[];
    v_body_hash text;
    v_schema_oid oid;
    v_schema_owner oid;
BEGIN
    IF v_state IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics and Insights pre-cutover state is missing';
    END IF;
    IF pg_catalog.to_regnamespace('aap_diagnostics_v1') IS NOT NULL
       OR pg_catalog.to_regnamespace('aap_insights_v1') IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics or Insights legacy namespace still exists';
    END IF;
    IF pg_catalog.to_regnamespace('yoao_diagnostics_v1') IS NULL
       OR pg_catalog.to_regnamespace('yoao_insights_v1') IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42704',
            MESSAGE = 'Diagnostics or Insights destination namespace is missing';
    END IF;

    SELECT pg_catalog.array_agg(
        function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY function.proname COLLATE "C"
    ) INTO v_functions
    FROM pg_catalog.pg_proc AS function
    WHERE function.pronamespace =
          pg_catalog.to_regnamespace('yoao_diagnostics_v1');
    IF v_functions IS DISTINCT FROM ARRAY[
        'detach_dataset_task_references(uuid, uuid)',
        'detach_project_references(uuid)'
    ]::text[] THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics destination namespace is not the owned function surface';
    END IF;

    SELECT pg_catalog.array_agg(
        function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY function.proname COLLATE "C"
    ) INTO v_functions
    FROM pg_catalog.pg_proc AS function
    WHERE function.pronamespace =
          pg_catalog.to_regnamespace('yoao_insights_v1');
    IF v_functions IS DISTINCT FROM ARRAY[
        'clear_project_task_views(uuid)'
    ]::text[] THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Insights destination namespace is not the owned function surface';
    END IF;

    IF EXISTS (
        SELECT FROM pg_catalog.pg_class
        WHERE relnamespace IN (
            pg_catalog.to_regnamespace('yoao_diagnostics_v1'),
            pg_catalog.to_regnamespace('yoao_insights_v1')
        )
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_type
        WHERE typnamespace IN (
            pg_catalog.to_regnamespace('yoao_diagnostics_v1'),
            pg_catalog.to_regnamespace('yoao_insights_v1')
        )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics or Insights destination contains a non-function object';
    END IF;

    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.pronamespace IN (
            pg_catalog.to_regnamespace('yoao_diagnostics_v1'),
            pg_catalog.to_regnamespace('yoao_insights_v1')
        )
          AND (
              function.prokind <> 'f'
              OR NOT function.prosecdef
              OR function.provolatile <> 'v'
              OR function.proparallel <> 'u'
              OR function.proconfig IS DISTINCT FROM
                 ARRAY['search_path=pg_catalog, pg_temp']::text[]
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'Diagnostics or Insights destination is not a fixed owner capability';
    END IF;

    FOR v_item IN
        SELECT * FROM (
            VALUES
                ('aap_diagnostics_v1', 'yoao_diagnostics_v1'),
                ('aap_insights_v1', 'yoao_insights_v1')
        ) AS item(old_schema, new_schema)
    LOOP
        v_expected := v_state -> ('schema:' || v_item.old_schema);
        IF v_expected IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Owner capability schema state is missing: ' ||
                    v_item.old_schema;
        END IF;
        SELECT namespace.oid,
               namespace.nspowner,
               ARRAY(
                   SELECT pg_catalog.format(
                       '%s:%s:%s',
                       COALESCE(grantee.rolname, 'PUBLIC'),
                       entry.privilege_type,
                       entry.is_grantable
                   )
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           namespace.nspacl,
                           pg_catalog.acldefault('n', namespace.nspowner)
                       )
                   ) AS entry
                   LEFT JOIN pg_catalog.pg_roles AS grantee
                     ON grantee.oid = entry.grantee
                   WHERE entry.grantee <> 0
                   ORDER BY 1
               )
        INTO v_schema_oid, v_schema_owner, v_acl
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = v_item.new_schema;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Owner capability destination schema is missing: ' ||
                    v_item.new_schema;
        END IF;
        IF v_schema_oid IS DISTINCT FROM
              (v_expected ->> 'oid')::pg_catalog.oid
           OR v_schema_owner IS DISTINCT FROM
              (v_expected ->> 'owner')::pg_catalog.oid
           OR pg_catalog.to_jsonb(v_acl) IS DISTINCT FROM
              (v_expected -> 'acl') THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Owner capability schema security changed during cutover: ' ||
                    v_item.new_schema;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    namespace.nspacl,
                    pg_catalog.acldefault('n', namespace.nspowner)
                )
            ) AS entry
            WHERE namespace.nspname = v_item.new_schema
              AND entry.grantee = 0
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Owner capability schema retains a PUBLIC grant: ' ||
                    v_item.new_schema;
        END IF;
    END LOOP;

    FOR v_item IN
        SELECT * FROM (
            VALUES
                (
                    'aap_insights_v1.clear_project_task_views(uuid)',
                    'yoao_insights_v1.clear_project_task_views(uuid)',
                    'capability'
                ),
                (
                    'aap_diagnostics_v1.detach_project_references(uuid)',
                    'yoao_diagnostics_v1.detach_project_references(uuid)',
                    'capability'
                ),
                (
                    'aap_diagnostics_v1.detach_dataset_task_references(uuid, uuid)',
                    'yoao_diagnostics_v1.detach_dataset_task_references(uuid, uuid)',
                    'capability'
                ),
                (
                    'aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)',
                    'aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)',
                    'caller'
                ),
                (
                    'aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)',
                    'aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)',
                    'caller'
                )
        ) AS item(old_signature, new_signature, state_prefix)
    LOOP
        v_expected := v_state -> (
            v_item.state_prefix || ':' || v_item.old_signature
        );
        IF v_expected IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Owner capability state is missing: ' ||
                    v_item.old_signature;
        END IF;
        v_function := pg_catalog.to_regprocedure(v_item.new_signature);
        IF v_function IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'Owner capability is missing: ' ||
                    v_item.new_signature;
        END IF;
        SELECT * INTO v_row FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_function;
        v_body_hash := pg_catalog.encode(
            pg_catalog.sha256(
                pg_catalog.convert_to(v_row.prosrc, 'UTF8')
            ),
            'hex'
        );
        IF v_body_hash IS DISTINCT FROM COALESCE(
               v_expected ->> 'new_body_sha256',
               v_expected ->> 'body_sha256'
           )
           OR v_function IS DISTINCT FROM
              (v_expected ->> 'oid')::pg_catalog.oid
           OR pg_catalog.strpos(v_row.prosrc, 'aap_diagnostics_v1.') <> 0
           OR pg_catalog.strpos(v_row.prosrc, 'aap_insights_v1.') <> 0 THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Owner capability body changed during cutover: ' ||
                    v_item.new_signature;
        END IF;
        IF v_row.proowner IS DISTINCT FROM
              (v_expected ->> 'owner')::pg_catalog.oid
           OR v_row.prosecdef IS DISTINCT FROM
              (v_expected ->> 'security_definer')::boolean
           OR pg_catalog.to_jsonb(v_row.proconfig) IS DISTINCT FROM
              (v_expected -> 'config')
           OR v_row.provolatile::text IS DISTINCT FROM
              (v_expected ->> 'volatility')
           OR v_row.proparallel::text IS DISTINCT FROM
              (v_expected ->> 'parallel')
           OR v_row.prokind::text IS DISTINCT FROM
              (v_expected ->> 'kind')
           OR v_row.proisstrict IS DISTINCT FROM
              (v_expected ->> 'is_strict')::boolean
           OR v_row.prolang IS DISTINCT FROM
              (v_expected ->> 'language')::pg_catalog.oid THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'Owner capability security changed during cutover: ' ||
                    v_item.new_signature;
        END IF;
        v_acl := ARRAY(
            SELECT pg_catalog.format(
                '%s:%s:%s',
                COALESCE(grantee.rolname, 'PUBLIC'),
                entry.privilege_type,
                entry.is_grantable
            )
            FROM pg_catalog.aclexplode(
                COALESCE(
                    v_row.proacl,
                    pg_catalog.acldefault('f', v_row.proowner)
                )
            ) AS entry
            LEFT JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = entry.grantee
            WHERE entry.grantee <> 0
            ORDER BY 1
        );
        IF pg_catalog.to_jsonb(v_acl) IS DISTINCT FROM (v_expected -> 'acl') THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Owner capability ACL changed during cutover: ' ||
                    v_item.new_signature;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.aclexplode(
                COALESCE(
                    v_row.proacl,
                    pg_catalog.acldefault('f', v_row.proowner)
                )
            ) AS entry
            WHERE entry.grantee = 0
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Owner capability retains a PUBLIC grant: ' ||
                    v_item.new_signature;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS function
        WHERE function.prosrc LIKE '%aap_diagnostics_v1.%'
           OR function.prosrc LIKE '%aap_insights_v1.%'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'A live function still references a Diagnostics or Insights legacy namespace';
    END IF;
    PERFORM pg_catalog.set_config(
        'yoao.n5_diagnostics_insights_pre_state', '', true
    );
END;
$verify$;
