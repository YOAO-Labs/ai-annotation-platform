\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.grant_section_applied', '0', true);

\if :{?grant_configuration}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'yoao.workspace_configuration_role', :'workspace_configuration_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.workspace_configuration_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.workspace_configuration_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_workspace_configuration_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 3 THEN
        RAISE EXCEPTION 'Unexpected Workspace configuration role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace configuration roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    client_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_configuration_role')
    );
    policy_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_configuration_v1.configuration_policy()'
    )::oid;
    step_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])'
    )::oid;
    guide_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_configuration_v1.guide_assets_step(jsonb, text, jsonb)'
    )::oid;
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR client_oid IS NULL
       OR policy_oid IS NULL OR step_oid IS NULL OR guide_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Workspace configuration owner or LOGIN capability';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_v1.lock_accounts_share(uuid[],boolean)'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = policy_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = guide_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = guide_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype THEN
        RAISE EXCEPTION 'A Workspace configuration RPC has the wrong owner or return type';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_workspace_configuration_v1', 'USAGE'
    ) OR NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_identity_v1', 'USAGE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid,
        pg_catalog.to_regprocedure('yoao_identity_v1.lock_accounts_share(uuid[],boolean)'),
        'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'owner_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'updated_at', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'owner_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'updated_at', 'UPDATE'
    ) THEN
        RAISE EXCEPTION 'Workspace owner lacks a required configuration capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_configuration_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_configuration_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.workspace_configuration_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_workspace_configuration_v1
    TO :"workspace_configuration_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_configuration_v1.guide_assets_step(jsonb, text, jsonb),
    yoao_workspace_configuration_v1.configuration_policy(),
    yoao_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])
    TO :"workspace_configuration_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_configuration_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_workspace_configuration_v1.configuration_policy()',
        'yoao_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])',
        'yoao_workspace_configuration_v1.guide_assets_step(jsonb, text, jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace configuration LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_workspace_configuration_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_workspace_configuration_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS relation
            ON relation.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND (pg_catalog.has_table_privilege(
                     role_oid, relation.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                 ) OR pg_catalog.has_any_column_privilege(
                     role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                 ))
      ) OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS sequence
            ON sequence.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND sequence.relkind = 'S'
            AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid, current_database(), 'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Workspace configuration LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_consumer}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.workspace_consumer_role', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('yoao.workspace_consumer_role');
    v_workspace name := current_setting('yoao.workspace_owner');
    v_allowed oid[] := ARRAY[
        'yoao_workspace_configuration_v1.configuration_policy()'::regprocedure::oid,
        'yoao_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])'::regprocedure::oid,
        'yoao_workspace_directory_query_v1.directory_query_policy()'::regprocedure::oid,
        'yoao_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)'::regprocedure::oid,
        'yoao_workspace_membership_v1.apply_membership_command(jsonb)'::regprocedure::oid,
        'yoao_workspace_membership_v1.membership_policy()'::regprocedure::oid,
        'yoao_workspace_membership_v1.prepare_membership_command(jsonb)'::regprocedure::oid,
        'yoao_workspace_owner_transfer_v1.owner_transfer_policy()'::regprocedure::oid,
        'yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)'::regprocedure::oid,
        'yoao_workspace_project_query_v1.project_query_policy()'::regprocedure::oid,
        'yoao_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamp with time zone,timestamp with time zone,bigint,integer)'::regprocedure::oid,
        'yoao_workspace_project_query_v1.project_search_member_ids(uuid,text)'::regprocedure::oid,
        'yoao_workspace_project_query_v1.project_search_scope(uuid,text)'::regprocedure::oid,
        'yoao_workspace_template_query_v1.template_query_policy()'::regprocedure::oid,
        'yoao_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text)'::regprocedure::oid,
        'yoao_workspace_v1.actor_project_memberships(uuid)'::regprocedure::oid,
        'yoao_workspace_v1.clear_primary_backend_bindings(uuid)'::regprocedure::oid,
        'yoao_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)'::regprocedure::oid,
        'yoao_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)'::regprocedure::oid,
        'yoao_workspace_v1.lock_project_manager(uuid,uuid)'::regprocedure::oid,
        'yoao_workspace_v1.lock_project_member_reference(uuid,uuid,boolean)'::regprocedure::oid,
        'yoao_workspace_v1.lock_project_reference(uuid)'::regprocedure::oid,
        'yoao_workspace_v1.lock_project_reference_nowait(uuid)'::regprocedure::oid,
        'yoao_workspace_v1.lock_project_references_for_update(uuid[])'::regprocedure::oid,
        'yoao_workspace_v1.lock_project_row(uuid,boolean)'::regprocedure::oid,
        'yoao_workspace_v1.preview_dataset_unlink(uuid,uuid,uuid)'::regprocedure::oid,
        'yoao_workspace_v1.project_access_step(uuid,uuid,text)'::regprocedure::oid,
        'yoao_workspace_v1.project_member_rows(uuid,uuid[],boolean)'::regprocedure::oid,
        'yoao_workspace_v1.read_import_project(text)'::regprocedure::oid,
        'yoao_workspace_v1.read_project_row(uuid)'::regprocedure::oid,
        'yoao_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)'::regprocedure::oid,
        'yoao_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)'::regprocedure::oid
    ];
    v_relation record;
    v_schema record;
    v_function record;
    v_columns text;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR v_workspace=current_user
       OR v_consumer IN (current_user, v_workspace)
       OR pg_has_role(v_consumer,current_user,'MEMBER')
       OR pg_has_role(v_consumer,v_workspace,'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Workspace consumer deployment ownership';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_workspace
          AND NOT (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Workspace owner must be a non-admin NOLOGIN role';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_consumer AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members
        WHERE member=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
    ) THEN
        RAISE EXCEPTION 'Workspace consumer must be a non-admin LOGIN without memberships';
    END IF;
    IF (SELECT count(*) FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner
        WHERE p.oid=ANY(v_allowed) AND owner.rolname=v_workspace AND p.prosecdef)
        <> cardinality(v_allowed) THEN
        RAISE EXCEPTION 'Workspace consumer capabilities have unexpected ownership';
    END IF;
    FOR v_relation IN
        SELECT c.oid,c.relname,c.relkind,c.relowner,c.reloptions,
            ARRAY(SELECT a.attname::text FROM pg_attribute a
                  WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
                  ORDER BY a.attnum) AS columns
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='yoao_workspace_v1'
          AND c.relname IN ('project_facts','project_member_facts')
    LOOP
        IF v_relation.relkind <> 'v'
           OR v_relation.relowner<>(SELECT oid FROM pg_roles WHERE rolname=v_workspace)
           OR NOT COALESCE(v_relation.reloptions @> ARRAY['security_barrier=true'],false)
           OR v_relation.columns IS DISTINCT FROM (CASE v_relation.relname
                WHEN 'project_facts' THEN ARRAY['id','organization_id','display_id','name','type_label','type_key','data_type','owner_id','status','ai_enabled','ml_backend_pool_id','default_variants','preannotate_pipeline','label_config','tool_bindings','sampling','maximum_annotations','show_overlap_first','iou_dedup_threshold','box_threshold','text_threshold','ai_interactive_enabled','raster_mask_native_editing_enabled','mask_qc_config','point_cloud_quality_config','rendering_config','annotation_guide','guide_assets','model_version','task_lock_ttl_seconds','video_sampling','video_collaboration','scene_mode','prefer_same_scene_continuation','scene_continuation_window_min','due_date','created_at','updated_at','guide_assets_revision']
                ELSE ARRAY['id','project_id','user_id','role','assigned_by','assigned_at','version','updated_at','weekly_target'] END) THEN
            RAISE EXCEPTION 'Workspace read relation does not match its fixed contract';
        END IF;
    END LOOP;
    IF to_regclass('yoao_workspace_v1.project_facts') IS NULL
       OR to_regclass('yoao_workspace_v1.project_member_facts') IS NULL THEN
        RAISE EXCEPTION 'Workspace read relations are missing';
    END IF;

    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM %I',current_user,v_consumer);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I',current_user,v_consumer);

    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname,c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE (c.relkind IN ('r','p','v','m','f') AND (
            n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND (
                c.relname IN ('projects','project_members','project_templates',
                    'project_deletion_operations','dataset_unlink_operations')
                OR left(c.relname,10)='workspace_'
            ))
        )) OR (c.relkind='S' AND (
            n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND c.relname IN (
                'display_seq_projects','display_seq_project_templates'
            ))
        ))
    LOOP
        IF v_relation.relkind='S' THEN
            EXECUTE format('REVOKE ALL ON SEQUENCE %I.%I FROM %I',
                v_relation.nspname,v_relation.relname,v_consumer);
            CONTINUE;
        END IF;
        EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM %I',
            v_relation.nspname,v_relation.relname,v_consumer);
        SELECT string_agg(quote_ident(attname),',' ORDER BY attnum)
        INTO v_columns FROM pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns,v_relation.nspname,v_relation.relname,v_consumer);
        END IF;
    END LOOP;
    FOR v_schema IN SELECT nspname FROM pg_namespace
        WHERE nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
    LOOP
        EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',v_schema.nspname,v_consumer);
        IF v_schema.nspname = 'yoao_workspace_fixture_v1' THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',v_schema.nspname,v_consumer);
        END IF;
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I',v_schema.nspname,v_consumer);
    END LOOP;
    EXECUTE format('REVOKE EXECUTE ON FUNCTION yoao_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean) FROM %I', v_consumer);
    FOR v_schema IN
        SELECT DISTINCT n.nspname FROM pg_namespace n JOIN pg_proc p ON p.pronamespace=n.oid
        WHERE p.oid=ANY(v_allowed)
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I',v_schema.nspname,v_consumer);
    END LOOP;
    EXECUTE format('GRANT SELECT ON yoao_workspace_v1.project_facts, yoao_workspace_v1.project_member_facts TO %I',v_consumer);
    FOR v_function IN SELECT oid::regprocedure AS signature FROM pg_proc WHERE oid=ANY(v_allowed)
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',v_function.signature,v_consumer);
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
          AND has_function_privilege(v_consumer,p.oid,'EXECUTE') AND NOT p.oid=ANY(v_allowed)
    ) OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f') AND (
            n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND (
                c.relname IN ('projects','project_members','project_templates',
                    'project_deletion_operations','dataset_unlink_operations')
                OR left(c.relname,10)='workspace_'
            ))
        ) AND (
            has_table_privilege(v_consumer,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR has_any_column_privilege(v_consumer,c.oid,'INSERT,UPDATE,REFERENCES')
            OR (c.oid NOT IN ('yoao_workspace_v1.project_facts'::regclass,
                             'yoao_workspace_v1.project_member_facts'::regclass)
                AND (has_table_privilege(v_consumer,c.oid,'SELECT')
                     OR has_any_column_privilege(v_consumer,c.oid,'SELECT')))
        )
    ) OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='S' AND (
            n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND c.relname IN ('display_seq_projects','display_seq_project_templates'))
        ) AND CASE WHEN c.relkind='S' THEN
            has_sequence_privilege(v_consumer,c.oid,'USAGE,SELECT,UPDATE')
            ELSE false END
    ) OR EXISTS (
        SELECT 1 FROM pg_namespace n
        WHERE n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
          AND has_schema_privilege(v_consumer,n.oid,'CREATE')
    ) THEN
        RAISE EXCEPTION 'Workspace consumer retains private data or command privileges';
    END IF;
END;
$profile$;
\endif

\if :{?grant_dataset_unlink}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'yoao.workspace_dataset_unlink_role', :'workspace_dataset_unlink_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.workspace_dataset_unlink_role');
    role_names text[] := ARRAY[
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.workspace_dataset_unlink_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_workspace_dataset_unlink_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 2 THEN
        RAISE EXCEPTION 'Unexpected Workspace dataset-unlink role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace dataset-unlink roles';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES
            ('yoao_workspace_v1.preview_dataset_unlink(uuid,uuid,uuid)'),
            ('yoao_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)')
        ) AS expected(signature)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_proc AS function
            WHERE function.oid = pg_catalog.to_regprocedure(expected.signature)
              AND function.proowner = owner_oid
              AND function.prorettype = 'jsonb'::pg_catalog.regtype
              AND function.prosecdef
              AND function.provolatile = 'v'
              AND function.proparallel = 'u'
              AND function.proconfig = ARRAY['search_path=pg_catalog, pg_temp']
        )
    ) THEN
        RAISE EXCEPTION 'Wrong Workspace dataset-unlink coordinator owner or shape';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(owner_oid, 'yoao_workspace_v1', 'USAGE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'id', 'SELECT')
       OR NOT pg_catalog.has_any_column_privilege(owner_oid, 'public.projects', 'UPDATE')
       OR EXISTS (
           SELECT 1 FROM (VALUES
               ('operation_id'), ('actor_id'), ('dataset_id'), ('project_id'),
               ('request'), ('result'), ('created_at')
           ) AS receipt_column(name)
           WHERE NOT pg_catalog.has_column_privilege(
               owner_oid, 'public.dataset_unlink_operations',
               receipt_column.name, 'SELECT'
           )
       )
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.dataset_unlink_operations', 'operation_id', 'INSERT') THEN
        RAISE EXCEPTION 'Workspace owner lacks required dataset-unlink capabilities';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_dataset_unlink_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_dataset_unlink_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.workspace_dataset_unlink_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_workspace_v1
    TO :"workspace_dataset_unlink_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.preview_dataset_unlink(uuid,uuid,uuid),
    yoao_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)
    TO :"workspace_dataset_unlink_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_dataset_unlink_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid)',
        'yoao_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace dataset-unlink LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_workspace_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_workspace_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS relation
            ON relation.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND (pg_catalog.has_table_privilege(
                     role_oid, relation.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                 ) OR pg_catalog.has_any_column_privilege(
                     role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                 ))
      ) OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS sequence
            ON sequence.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND sequence.relkind = 'S'
            AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid, current_database(), 'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Workspace dataset-unlink LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_directory_query}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'yoao.workspace_directory_query_role', :'workspace_directory_query_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.workspace_directory_query_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.workspace_directory_query_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_workspace_directory_query_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 3 THEN
        RAISE EXCEPTION 'Unexpected Workspace Directory query role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace Directory query roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    client_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_directory_query_role')
    );
    identity_function oid := pg_catalog.to_regprocedure(
        'yoao_identity_v1.workspace_directory_superadmins()'
    )::oid;
    account_function oid := pg_catalog.to_regprocedure(
        'yoao_identity_v1.read_workspace_membership_accounts(uuid[])'
    )::oid;
    project_query_function oid := pg_catalog.to_regprocedure(
        'yoao_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamp with time zone, timestamp with time zone, bigint, integer)'
    )::oid;
    policy_function oid := pg_catalog.to_regprocedure(
        'yoao_workspace_directory_query_v1.directory_query_policy()'
    )::oid;
    step_function oid := pg_catalog.to_regprocedure(
        'yoao_workspace_directory_query_v1.directory_query_step(jsonb, text, jsonb)'
    )::oid;
    helper_function oid := pg_catalog.to_regprocedure(
        'yoao_workspace_directory_query_v1._has_exact_keys(jsonb, text[])'
    )::oid;
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR client_oid IS NULL
       OR identity_function IS NULL OR account_function IS NULL
       OR project_query_function IS NULL OR policy_function IS NULL
       OR step_function IS NULL OR helper_function IS NULL THEN
        RAISE EXCEPTION 'Missing Workspace Directory query owner or LOGIN capability';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = identity_function) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_function) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_function) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = helper_function) IS DISTINCT FROM workspace_oid
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = identity_function) IS DISTINCT FROM 'record'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_function) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_function) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype THEN
        RAISE EXCEPTION 'A Workspace Directory query function has the wrong owner or return type';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_workspace_directory_query_v1', 'USAGE'
    ) OR NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_workspace_project_query_v1', 'USAGE'
    ) OR NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_identity_v1', 'USAGE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, project_query_function, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, account_function, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, identity_function, 'EXECUTE'
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.unnest(ARRAY[
            'id', 'project_id', 'user_id', 'role', 'assigned_by',
            'assigned_at', 'version', 'updated_at', 'weekly_target'
        ]::text[]) AS column_name(name)
        WHERE NOT pg_catalog.has_column_privilege(
            workspace_oid, 'public.project_members', column_name.name, 'SELECT'
        )
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.unnest(ARRAY[
            'id', 'name', 'email', 'role', 'is_active'
        ]::text[]) AS column_name(name)
        WHERE NOT pg_catalog.has_column_privilege(
            identity_oid, 'public.users', column_name.name, 'SELECT'
        )
    ) THEN
        RAISE EXCEPTION 'Workspace or Identity owner lacks a Directory query capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_directory_query_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_directory_query_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.workspace_directory_query_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_workspace_directory_query_v1
    TO :"workspace_directory_query_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_directory_query_v1.directory_query_policy(),
    yoao_workspace_directory_query_v1.directory_query_step(jsonb, text, jsonb)
    TO :"workspace_directory_query_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_directory_query_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_workspace_directory_query_v1.directory_query_policy()',
        'yoao_workspace_directory_query_v1.directory_query_step(jsonb, text, jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace Directory query LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_workspace_directory_query_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_workspace_directory_query_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'public', 'CREATE'
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(
                   role_oid, relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               ) OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence
          ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) OR pg_catalog.has_database_privilege(
        role_oid, current_database(), 'CREATE,TEMP'
    ) THEN
        RAISE EXCEPTION 'Workspace Directory query LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_e2e_fixture}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.e2e_fixture_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('yoao.e2e_fixture_operator');
    v_workspace name := current_setting('yoao.workspace_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR (right(lower(current_database()), 5) <> '_test'
           AND right(lower(current_database()), 4) <> '_e2e')
       OR v_consumer IN (current_user, v_workspace, 'identity_api_consumer')
       OR pg_has_role(v_consumer, v_workspace, 'MEMBER')
       OR pg_has_role(v_consumer, current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Workspace E2E fixture operator ownership';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_consumer AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members
        WHERE member=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
           OR (roleid=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
               AND member<>(SELECT oid FROM pg_roles
                            WHERE rolname=current_setting('yoao.migration_role')))
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_workspace AND NOT rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'E2E fixture operator requires non-admin, non-inheriting LOGIN';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM unnest(ARRAY['public.projects','public.project_templates']) AS private(name)
        WHERE has_table_privilege(v_consumer, private.name,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR has_any_column_privilege(v_consumer, private.name,
                  'SELECT,INSERT,UPDATE,REFERENCES')
    ) THEN
        RAISE EXCEPTION 'E2E fixture operator must use fixed Workspace capabilities';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class AS relation
        JOIN pg_roles AS owner ON owner.oid=relation.relowner
        WHERE relation.oid='yoao_workspace_fixture_v1.templates'::regclass
          AND relation.relkind='v'
          AND owner.rolname=v_workspace
          AND COALESCE(relation.reloptions @> ARRAY['security_barrier=true'], false)
          AND ARRAY(
              SELECT attribute.attname::text
              FROM pg_attribute AS attribute
              WHERE attribute.attrelid=relation.oid
                AND attribute.attnum>0
                AND NOT attribute.attisdropped
              ORDER BY attribute.attnum
          )=ARRAY['id','display_id']
    ) THEN
        RAISE EXCEPTION 'Unexpected Workspace E2E template read relation';
    END IF;

    EXECUTE format('GRANT USAGE ON SCHEMA yoao_workspace_fixture_v1 TO %I', v_consumer);
    EXECUTE format(
        'GRANT SELECT ON yoao_workspace_fixture_v1.templates TO %I', v_consumer
    );
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_workspace_fixture_v1.create_project(jsonb)',
        'yoao_workspace_fixture_v1.create_templates(jsonb)',
        'yoao_workspace_fixture_v1.guard_project_cleanup(uuid[])',
        'yoao_workspace_fixture_v1.delete_projects(uuid[])',
        'yoao_workspace_fixture_v1.delete_templates(boolean,text[])'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_proc AS function
            JOIN pg_roles AS owner ON owner.oid=function.proowner
            WHERE function.oid=v_signature::regprocedure
              AND function.prosecdef
              AND function.provolatile='v'
              AND owner.rolname=v_workspace
        ) THEN
            RAISE EXCEPTION 'Unexpected Workspace E2E fixture function owner or security mode';
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', v_signature, v_consumer);
    END LOOP;
END $profile$;
\endif

\if :{?grant_membership}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.workspace_membership_role', :'workspace_membership_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.workspace_membership_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.labeling_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.workspace_membership_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_workspace_membership_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 5 THEN
        RAISE EXCEPTION 'Unexpected Workspace membership role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace membership roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    labeling_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.labeling_owner')
    );
    audit_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.audit_owner')
    );
    client_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_membership_role')
    );
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR labeling_oid IS NULL
       OR audit_oid IS NULL OR client_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Workspace membership code owner or LOGIN';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_workspace_membership_v1.membership_policy()'
        )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_membership_v1.prepare_membership_command(jsonb)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_membership_v1.apply_membership_command(jsonb)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_v1.read_workspace_membership_accounts(uuid[])'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_workspace_membership_event(text,jsonb,uuid,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.read_membership_work(uuid,uuid,uuid,uuid)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.handoff_membership_annotation(uuid,uuid,uuid)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.handoff_membership_reviewer(uuid,uuid,uuid)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.release_membership_task_locks(uuid,uuid)'
           )::oid) IS DISTINCT FROM labeling_oid THEN
        RAISE EXCEPTION 'A Workspace membership capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_workspace_membership_v1', 'USAGE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_identity_v1.lock_accounts_update(uuid[],boolean)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_identity_v1.read_workspace_membership_accounts(uuid[])'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_workspace_membership_event(text,jsonb,uuid,jsonb,jsonb)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_audit_v1.valid_request_context(jsonb)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.read_membership_work(uuid,uuid,uuid,uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.handoff_membership_annotation(uuid,uuid,uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.handoff_membership_reviewer(uuid,uuid,uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.release_membership_task_locks(uuid,uuid)'
        )::oid, 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Workspace owner lacks a required typed capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_membership_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_membership_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.workspace_membership_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_workspace_membership_v1
    TO :"workspace_membership_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_membership_v1.membership_policy(),
    yoao_workspace_membership_v1.prepare_membership_command(jsonb),
    yoao_workspace_membership_v1.apply_membership_command(jsonb)
    TO :"workspace_membership_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_membership_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_workspace_membership_v1.apply_membership_command(jsonb)',
        'yoao_workspace_membership_v1.membership_policy()',
        'yoao_workspace_membership_v1.prepare_membership_command(jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace membership LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_workspace_membership_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(
                   role_oid, relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               ) OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
      ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence
          ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid, current_database(), 'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Workspace membership LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_owner_transfer}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'yoao.workspace_owner_transfer_role', :'workspace_owner_transfer_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.workspace_owner_transfer_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.workspace_owner_transfer_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_workspace_owner_transfer_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 3 THEN
        RAISE EXCEPTION 'Unexpected Workspace owner-transfer role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace owner-transfer roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    client_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner_transfer_role')
    );
    policy_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_owner_transfer_v1.owner_transfer_policy()'
    )::oid;
    transfer_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)'
    )::oid;
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR client_oid IS NULL
       OR policy_oid IS NULL OR transfer_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Workspace owner-transfer owner or LOGIN capability';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_v1.lock_accounts_update(uuid[],boolean)'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = policy_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = transfer_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = transfer_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype THEN
        RAISE EXCEPTION 'A Workspace owner-transfer RPC has the wrong owner or return type';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_workspace_owner_transfer_v1', 'USAGE'
    ) OR NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_identity_v1', 'USAGE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid,
        pg_catalog.to_regprocedure('yoao_identity_v1.lock_accounts_update(uuid[],boolean)'),
        'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'owner_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'updated_at', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'owner_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'updated_at', 'UPDATE'
    ) OR EXISTS (
        SELECT 1 FROM (VALUES
            ('operation_id'), ('actor_id'), ('project_id'), ('new_owner_id'),
            ('request_hash'), ('result')
        ) AS receipt_column(name)
        WHERE NOT pg_catalog.has_column_privilege(
            workspace_oid,
            'public.workspace_owner_transfer_operation_receipts',
            receipt_column.name,
            'SELECT'
        ) OR NOT pg_catalog.has_column_privilege(
            workspace_oid,
            'public.workspace_owner_transfer_operation_receipts',
            receipt_column.name,
            'INSERT'
        )
    ) THEN
        RAISE EXCEPTION 'Workspace owner lacks a required owner-transfer capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_owner_transfer_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_owner_transfer_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.workspace_owner_transfer_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_workspace_owner_transfer_v1
    TO :"workspace_owner_transfer_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_owner_transfer_v1.owner_transfer_policy(),
    yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)
    TO :"workspace_owner_transfer_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner_transfer_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_workspace_owner_transfer_v1.owner_transfer_policy()',
        'yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace owner-transfer LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_workspace_owner_transfer_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_workspace_owner_transfer_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS relation
            ON relation.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND (pg_catalog.has_table_privilege(
                     role_oid, relation.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                 ) OR pg_catalog.has_any_column_privilege(
                     role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                 ))
      ) OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS sequence
            ON sequence.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND sequence.relkind = 'S'
            AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid, current_database(), 'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Workspace owner-transfer LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_project_deletion}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'yoao.workspace_project_deletion_role', :'workspace_project_deletion_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.workspace_project_deletion_role');
    role_names text[] := ARRAY[
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.workspace_project_deletion_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_workspace_project_deletion_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 2 THEN
        RAISE EXCEPTION 'Unexpected Workspace project-deletion role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace project-deletion roles';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    coordinator_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)'
    );
BEGIN
    IF coordinator_oid IS NULL OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        WHERE function.oid = coordinator_oid
          AND function.proowner = owner_oid
          AND function.prorettype = 'jsonb'::pg_catalog.regtype
          AND function.prosecdef
    ) THEN
        RAISE EXCEPTION 'Wrong Workspace project-deletion coordinator owner or shape';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(owner_oid, 'yoao_workspace_v1', 'USAGE')
       OR NOT pg_catalog.has_table_privilege(owner_oid, 'public.projects', 'DELETE')
       OR EXISTS (
           SELECT 1 FROM (VALUES
               ('operation_id'), ('actor_id'), ('project_id'),
               ('request'), ('result'), ('created_at')
           ) AS receipt_column(name)
           WHERE NOT pg_catalog.has_column_privilege(
               owner_oid, 'public.project_deletion_operations',
               receipt_column.name, 'SELECT'
           )
       )
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.project_deletion_operations', 'operation_id', 'INSERT') THEN
        RAISE EXCEPTION 'Workspace owner lacks required project-deletion capabilities';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_project_deletion_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_project_deletion_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.workspace_project_deletion_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_workspace_v1
    TO :"workspace_project_deletion_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)
    TO :"workspace_project_deletion_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_project_deletion_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace project-deletion LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_workspace_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_workspace_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS relation
            ON relation.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND (pg_catalog.has_table_privilege(
                     role_oid, relation.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                 ) OR pg_catalog.has_any_column_privilege(
                     role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                 ))
      ) OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS sequence
            ON sequence.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND sequence.relkind = 'S'
            AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid, current_database(), 'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Workspace project-deletion LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_project_maintenance}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'yoao.workspace_project_maintenance_role', :'workspace_project_maintenance_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.workspace_project_maintenance_role');
    role_names text[] := ARRAY[
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.workspace_project_maintenance_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_workspace_project_maintenance_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 2 THEN
        RAISE EXCEPTION 'Unexpected Workspace project-maintenance role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace project-maintenance roles';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    signature text;
    function_oid oid;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'yoao_workspace_v1.read_import_project(text)',
        'yoao_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)',
        'yoao_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)',
        'yoao_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)',
        'yoao_workspace_v1.write_seed_members(jsonb)',
        'yoao_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)',
        'yoao_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)',
        'yoao_workspace_v1.write_seed_project_projection(uuid,jsonb)',
        'yoao_workspace_v1.pin_seed_member_assignment(uuid,timestamptz)'
    ] LOOP
        function_oid := pg_catalog.to_regprocedure(signature);
        IF function_oid IS NULL OR NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_proc AS function
            WHERE function.oid = function_oid
              AND function.proowner = owner_oid
              AND function.prorettype = 'jsonb'::pg_catalog.regtype
              AND function.prosecdef
        ) THEN
            RAISE EXCEPTION 'Wrong Workspace project-maintenance capability owner or shape';
        END IF;
    END LOOP;
    IF NOT pg_catalog.has_schema_privilege(owner_oid, 'yoao_workspace_v1', 'USAGE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'display_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'point_cloud_quality_config', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'point_cloud_quality_config', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'updated_at', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'updated_at', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'created_at', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'ml_backend_pool_id', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'preannotate_pipeline', 'UPDATE') THEN
        RAISE EXCEPTION 'Workspace owner lacks required project-maintenance capabilities';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_project_maintenance_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_project_maintenance_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.workspace_project_maintenance_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_workspace_v1
    TO :"workspace_project_maintenance_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.read_import_project(text),
    yoao_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb),
    yoao_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb),
    yoao_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb),
    yoao_workspace_v1.write_seed_members(jsonb),
    yoao_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)
    TO :"workspace_project_maintenance_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_project_maintenance_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_workspace_v1.create_import_project(uuid, text, text, uuid, jsonb)',
        'yoao_workspace_v1.create_point_cloud_seed_project(uuid, text, text, uuid, jsonb)',
        'yoao_workspace_v1.read_import_project(text)',
        'yoao_workspace_v1.write_nuscenes_seed_defaults(uuid, text, jsonb)',
        'yoao_workspace_v1.write_point_cloud_quality_config(uuid, jsonb)',
        'yoao_workspace_v1.write_seed_members(jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace project-maintenance LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_workspace_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_workspace_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS relation
            ON relation.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND (pg_catalog.has_table_privilege(
                     role_oid, relation.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                 ) OR pg_catalog.has_any_column_privilege(
                     role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                 ))
      ) OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS sequence
            ON sequence.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND sequence.relkind = 'S'
            AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid, current_database(), 'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Workspace project-maintenance LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_project_query}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'yoao.workspace_project_query_role', :'workspace_project_query_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.workspace_project_query_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.workspace_project_query_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_workspace_project_query_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 3 THEN
        RAISE EXCEPTION 'Unexpected Workspace Project query role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace Project query roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    client_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_project_query_role')
    );
    policy_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_project_query_v1.project_query_policy()'
    )::oid;
    step_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamp with time zone, timestamp with time zone, bigint, integer)'
    )::oid;
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR client_oid IS NULL
       OR policy_oid IS NULL OR step_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Workspace Project query owner or LOGIN capability';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_v1.read_actor_facts(uuid)'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = policy_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype THEN
        RAISE EXCEPTION 'A Workspace Project query RPC has the wrong owner or return type';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_workspace_project_query_v1', 'USAGE'
    ) OR NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_identity_v1', 'USAGE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid,
        pg_catalog.to_regprocedure('yoao_identity_v1.read_actor_facts(uuid)'),
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid,
        pg_catalog.to_regprocedure('yoao_identity_v1.read_workspace_membership_accounts(uuid[])'),
        'EXECUTE'
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.unnest(ARRAY[
            'id', 'organization_id', 'display_id', 'name', 'type_label', 'type_key',
            'data_type', 'owner_id', 'status', 'ai_enabled', 'ml_backend_pool_id',
            'default_variants', 'preannotate_pipeline', 'label_config', 'tool_bindings',
            'sampling', 'maximum_annotations', 'show_overlap_first',
            'iou_dedup_threshold', 'box_threshold', 'text_threshold',
            'ai_interactive_enabled', 'raster_mask_native_editing_enabled',
            'mask_qc_config', 'point_cloud_quality_config', 'rendering_config',
            'annotation_guide', 'guide_assets', 'model_version', 'task_lock_ttl_seconds',
            'video_sampling', 'video_collaboration', 'scene_mode',
            'prefer_same_scene_continuation', 'scene_continuation_window_min',
            'due_date', 'created_at', 'updated_at'
        ]::text[]) AS column_name(name)
        WHERE NOT pg_catalog.has_column_privilege(
            workspace_oid, 'public.projects', column_name.name, 'SELECT'
        )
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.workspace_configuration_operation_receipts', 'operation_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.workspace_configuration_operation_receipts', 'actor_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.workspace_configuration_operation_receipts', 'operation', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.workspace_configuration_operation_receipts', 'result', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.workspace_configuration_operation_receipts', 'target_id', 'SELECT'
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.unnest(ARRAY[
            'operation_id', 'actor_id', 'project_id', 'new_owner_id', 'result'
        ]::text[]) AS field(name)
        WHERE NOT pg_catalog.has_column_privilege(
            workspace_oid, 'public.workspace_owner_transfer_operation_receipts', field.name, 'SELECT'
        )
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.unnest(ARRAY[
            'total_tasks', 'completed_tasks', 'review_tasks', 'in_progress_tasks', 'batch_summary'
        ]::text[]) AS field(name)
        WHERE pg_catalog.has_column_privilege(workspace_oid, 'public.projects', field.name, 'SELECT')
    ) THEN
        RAISE EXCEPTION 'Workspace owner lacks a required Project-query capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_project_query_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_project_query_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.workspace_project_query_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_workspace_project_query_v1
    TO :"workspace_project_query_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_project_query_v1.project_query_policy(),
    yoao_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamp with time zone, timestamp with time zone, bigint, integer)
    TO :"workspace_project_query_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_project_query_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_workspace_project_query_v1.project_query_policy()',
        'yoao_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamp with time zone, timestamp with time zone, bigint, integer)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace Project query LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_workspace_project_query_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_workspace_project_query_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS relation
            ON relation.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND (pg_catalog.has_table_privilege(
                     role_oid, relation.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                 ) OR pg_catalog.has_any_column_privilege(
                     role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                 ))
      ) OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS sequence
            ON sequence.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND sequence.relkind = 'S'
            AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid, current_database(), 'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Workspace Project query LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_role_audit}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.audit_consumer', :'consumer_role', true);

DO $preflight$
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
END
$preflight$;

CREATE TEMP VIEW _aap_workspace_role_audit_project_ref
WITH (security_barrier = true) AS
SELECT project.id, project.owner_id, project.status
FROM public.projects AS project;

CREATE TEMP VIEW _aap_workspace_role_audit_member_ref
WITH (security_barrier = true) AS
SELECT member.id, member.project_id, member.user_id, member.role
FROM public.project_members AS member;

CREATE TEMP VIEW _aap_workspace_role_audit_capabilities_ref
WITH (security_barrier = true) AS
SELECT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.project_members'::pg_catalog.regclass
      AND attribute.attname::text = 'version'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS member_version,
EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.project_members'::pg_catalog.regclass
      AND attribute.attname::text = 'updated_at'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS member_updated_at;

DO $install$
DECLARE
    v_migration name := current_setting('yoao.migration_role');
    v_workspace name := current_setting('yoao.workspace_owner');
    v_consumer name := current_setting('yoao.audit_consumer');
    v_migration_oid oid;
    v_workspace_oid oid;
    v_consumer_oid oid;
    v_projects_oid oid;
    v_members_oid oid;
    v_schema_oid oid;
    v_view_oid oid;
    v_reference_oids oid[] := ARRAY[
        pg_catalog.to_regclass('pg_temp._aap_workspace_role_audit_project_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_workspace_role_audit_member_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_workspace_role_audit_capabilities_ref')::oid
    ];
    v_view_names text[] := ARRAY[
        'project_facts', 'project_member_facts', 'schema_capabilities'
    ];
    v_view_name text;
    v_all_project_columns text;
    v_all_member_columns text;
    v_view_columns text;
    v_columns text[];
    v_column_types text[];
    v_expected_columns text[];
    v_expected_types text[];
    v_view_definition text;
    v_granted_create boolean := false;
    v_index integer;
BEGIN
    IF v_migration !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_workspace !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_consumer !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_workspace = v_migration
       OR v_consumer IN (
            v_migration, v_workspace, 'workspace_api_consumer',
            'identity_api_consumer'
       ) THEN
        RAISE EXCEPTION 'Invalid or overlapping Workspace audit roles';
    END IF;
    IF pg_catalog.array_position(v_reference_oids, NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Failed to create Workspace audit reference views';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(20261023, 302);

    SELECT role.oid INTO v_migration_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_migration;
    SELECT role.oid INTO v_workspace_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_workspace;
    SELECT role.oid INTO v_consumer_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_consumer;
    IF v_migration_oid IS NULL OR v_workspace_oid IS NULL OR v_consumer_oid IS NULL THEN
        RAISE EXCEPTION 'Migration, Workspace owner, and audit consumer roles must exist';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_workspace_oid
          AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_consumer_oid
          AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Workspace owner must be NOLOGIN and audit consumer a restricted LOGIN';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = v_consumer_oid
           OR membership.roleid = v_consumer_oid
           OR membership.member = v_workspace_oid
           OR (membership.roleid = v_workspace_oid
               AND membership.member <> v_migration_oid)
    ) THEN
        RAISE EXCEPTION 'Audit consumer or Workspace owner has an unsafe role membership';
    END IF;
    IF NOT pg_catalog.pg_has_role(v_migration_oid, v_workspace_oid, 'MEMBER')
       AND NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles AS role
           WHERE role.oid = v_migration_oid AND role.rolsuper
       ) THEN
        RAISE EXCEPTION 'Migration role cannot transfer views to the Workspace owner';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('projects', 'id', 'uuid'),
            ('projects', 'owner_id', 'uuid'),
            ('projects', 'status', 'character varying(30)'),
            ('project_members', 'id', 'uuid'),
            ('project_members', 'project_id', 'uuid'),
            ('project_members', 'user_id', 'uuid'),
            ('project_members', 'role', 'character varying(30)')
        ) AS expected(table_name, column_name, type_name)
        LEFT JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.nspname = 'public'
        LEFT JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid
         AND relation.relname = expected.table_name
        LEFT JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = relation.oid
         AND attribute.attname = expected.column_name
         AND attribute.attnum > 0 AND NOT attribute.attisdropped
        WHERE relation.oid IS NULL OR relation.relkind <> 'r'
           OR relation.relowner <> v_migration_oid
           OR attribute.attnum IS NULL
           OR pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
                IS DISTINCT FROM expected.type_name
    ) THEN
        RAISE EXCEPTION 'Historical Project or ProjectMember source schema is incompatible';
    END IF;
    SELECT relation.oid INTO v_projects_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'projects';
    SELECT relation.oid INTO v_members_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'project_members';
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid
         AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_projects_oid
          AND primary_key.indisprimary AND primary_key.indnatts = 1
          AND primary_key.indkey[0] = attribute.attnum
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid
         AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_members_oid
          AND primary_key.indisprimary AND primary_key.indnatts = 1
          AND primary_key.indkey[0] = attribute.attnum
    ) THEN
        RAISE EXCEPTION 'Historical Project and ProjectMember IDs must remain primary keys';
    END IF;

    IF NOT pg_catalog.has_schema_privilege(v_workspace_oid, 'public', 'USAGE') THEN
        EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA public TO %I', v_workspace);
    END IF;
    EXECUTE pg_catalog.format(
        'GRANT SELECT (id, owner_id, status) ON TABLE public.projects TO %I',
        v_workspace
    );
    EXECUTE pg_catalog.format(
        'GRANT SELECT (id, project_id, user_id, role) '
        'ON TABLE public.project_members TO %I',
        v_workspace
    );

    SELECT namespace.oid INTO v_schema_oid
    FROM pg_catalog.pg_namespace AS namespace
    WHERE namespace.nspname = 'yoao_workspace_audit_v1';
    IF v_schema_oid IS NULL THEN
        EXECUTE pg_catalog.format(
            'CREATE SCHEMA yoao_workspace_audit_v1 AUTHORIZATION %I', v_workspace
        );
        SELECT namespace.oid INTO v_schema_oid
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_workspace_audit_v1';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.oid = v_schema_oid
          AND namespace.nspowner = v_workspace_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace = v_schema_oid
          AND relation.relname <> ALL (v_view_names)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS routine
        WHERE routine.pronamespace = v_schema_oid
    ) THEN
        RAISE EXCEPTION 'yoao_workspace_audit_v1 has a foreign owner or unexpected object';
    END IF;

    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('yoao_workspace_audit_v1.%I', v_view_name)
        );
        IF v_view_oid IS NULL THEN
            IF NOT pg_catalog.has_schema_privilege(
                   v_migration_oid, v_schema_oid, 'CREATE'
               ) AND NOT v_granted_create THEN
                EXECUTE pg_catalog.format(
                    'GRANT CREATE ON SCHEMA yoao_workspace_audit_v1 TO %I',
                    v_migration
                );
                v_granted_create := true;
            END IF;
            v_view_definition := pg_catalog.pg_get_viewdef(
                v_reference_oids[v_index], true
            );
            EXECUTE pg_catalog.format(
                'CREATE VIEW yoao_workspace_audit_v1.%I '
                'WITH (security_barrier = true) AS %s',
                v_view_name, v_view_definition
            );
            EXECUTE pg_catalog.format(
                'ALTER VIEW yoao_workspace_audit_v1.%I OWNER TO %I',
                v_view_name, v_workspace
            );
            v_view_oid := pg_catalog.to_regclass(
                pg_catalog.format('yoao_workspace_audit_v1.%I', v_view_name)
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS relation
            WHERE relation.oid = v_view_oid AND relation.relkind = 'v'
              AND relation.relowner = v_workspace_oid
              AND COALESCE(relation.reloptions, ARRAY[]::text[])
                  @> ARRAY['security_barrier=true']
        ) THEN
            RAISE EXCEPTION '%.% has an unexpected owner, kind, or security option',
                'yoao_workspace_audit_v1', v_view_name;
        END IF;
        SELECT pg_catalog.array_agg(attribute.attname::text ORDER BY attribute.attnum),
               pg_catalog.array_agg(
                   pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
                   ORDER BY attribute.attnum
               )
        INTO v_columns, v_column_types
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_view_oid
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        CASE v_index
            WHEN 1 THEN
                v_expected_columns := ARRAY['id', 'owner_id', 'status'];
                v_expected_types := ARRAY[
                    'uuid', 'uuid', 'character varying(30)'
                ];
            WHEN 2 THEN
                v_expected_columns := ARRAY[
                    'id', 'project_id', 'user_id', 'role'
                ];
                v_expected_types := ARRAY[
                    'uuid', 'uuid', 'uuid', 'character varying(30)'
                ];
            ELSE
                v_expected_columns := ARRAY[
                    'member_version', 'member_updated_at'
                ];
                v_expected_types := ARRAY['boolean', 'boolean'];
        END CASE;
        IF v_columns IS DISTINCT FROM v_expected_columns
           OR v_column_types IS DISTINCT FROM v_expected_types
           OR pg_catalog.pg_get_viewdef(v_view_oid, true) IS DISTINCT FROM
              pg_catalog.pg_get_viewdef(v_reference_oids[v_index], true) THEN
            RAISE EXCEPTION '%.% has an unexpected definition or shape',
                'yoao_workspace_audit_v1', v_view_name;
        END IF;
    END LOOP;
    IF v_granted_create THEN
        EXECUTE pg_catalog.format(
            'REVOKE CREATE ON SCHEMA yoao_workspace_audit_v1 FROM %I', v_migration
        );
    END IF;

    EXECUTE 'REVOKE ALL ON SCHEMA yoao_workspace_audit_v1 FROM PUBLIC';
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON SCHEMA yoao_workspace_audit_v1 FROM %I', v_consumer
    );
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                 ORDER BY attribute.attnum)
    INTO v_all_project_columns
    FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = v_projects_oid
      AND attribute.attnum > 0 AND NOT attribute.attisdropped;
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                 ORDER BY attribute.attnum)
    INTO v_all_member_columns
    FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = v_members_oid
      AND attribute.attnum > 0 AND NOT attribute.attisdropped;
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON TABLE public.projects FROM %I', v_consumer
    );
    EXECUTE pg_catalog.format(
        'REVOKE ALL (%s) ON TABLE public.projects FROM %I',
        v_all_project_columns, v_consumer
    );
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON TABLE public.project_members FROM %I', v_consumer
    );
    EXECUTE pg_catalog.format(
        'REVOKE ALL (%s) ON TABLE public.project_members FROM %I',
        v_all_member_columns, v_consumer
    );

    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('yoao_workspace_audit_v1.%I', v_view_name)
        );
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                     ORDER BY attribute.attnum)
        INTO v_view_columns
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_view_oid
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE yoao_workspace_audit_v1.%I FROM PUBLIC',
            v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE yoao_workspace_audit_v1.%I FROM PUBLIC',
            v_view_columns, v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE yoao_workspace_audit_v1.%I FROM %I',
            v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE yoao_workspace_audit_v1.%I FROM %I',
            v_view_columns, v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'GRANT SELECT ON TABLE yoao_workspace_audit_v1.%I TO %I',
            v_view_name, v_consumer
        );
    END LOOP;
    EXECUTE pg_catalog.format(
        'GRANT USAGE ON SCHEMA yoao_workspace_audit_v1 TO %I', v_consumer
    );

    IF pg_catalog.has_table_privilege(
           v_consumer_oid, v_projects_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_projects_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.projects access';
    END IF;
    IF pg_catalog.has_table_privilege(
           v_consumer_oid, v_members_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_members_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.project_members access';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
           v_consumer_oid, v_schema_oid, 'USAGE'
       ) OR pg_catalog.has_schema_privilege(
           v_consumer_oid, v_schema_oid, 'CREATE'
       ) THEN
        RAISE EXCEPTION 'Audit consumer has an unexpected Workspace audit schema privilege';
    END IF;
    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('yoao_workspace_audit_v1.%I', v_view_name)
        );
        IF NOT pg_catalog.has_table_privilege(
               v_consumer_oid, v_view_oid, 'SELECT'
           ) OR pg_catalog.has_table_privilege(
               v_consumer_oid, v_view_oid,
               'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
           ) OR pg_catalog.has_any_column_privilege(
               v_consumer_oid, v_view_oid, 'INSERT,UPDATE,REFERENCES'
           ) THEN
            RAISE EXCEPTION 'Audit consumer has an unexpected Workspace audit view privilege';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS relation
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    relation.relacl,
                    pg_catalog.acldefault('r', relation.relowner)
                )
            ) AS privilege
            WHERE relation.oid = v_view_oid AND privilege.grantee = 0
        ) THEN
            RAISE EXCEPTION 'PUBLIC retains access to a Workspace audit relation';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
                namespace.nspacl,
                pg_catalog.acldefault('n', namespace.nspowner)
            )
        ) AS privilege
        WHERE namespace.oid = v_schema_oid AND privilege.grantee = 0
    ) THEN
        RAISE EXCEPTION 'PUBLIC retains access to yoao_workspace_audit_v1';
    END IF;
END
$install$;
DROP VIEW pg_temp._aap_workspace_role_audit_capabilities_ref;
DROP VIEW pg_temp._aap_workspace_role_audit_member_ref;
DROP VIEW pg_temp._aap_workspace_role_audit_project_ref;
\endif

\if :{?grant_scale_seed}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.assets_owner', :'assets_owner', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.scale_seed_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('yoao.scale_seed_operator');
    v_workspace name := current_setting('yoao.workspace_owner');
    v_assets name := current_setting('yoao.assets_owner');
    v_labeling name := current_setting('yoao.labeling_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
 THEN
        RAISE EXCEPTION 'Unexpected database or migration owner for scale seed grants';
    END IF;
    IF (
        SELECT pg_catalog.count(DISTINCT role_name)
        FROM pg_catalog.unnest(ARRAY[
            current_user::name, v_workspace, v_assets, v_labeling, v_consumer
        ]) AS role_name
    ) <> 5 THEN
        RAISE EXCEPTION 'Migration, module owners and scale seed operator must be distinct';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = v_consumer
          AND role.rolcanlogin
          AND NOT (
              role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
              OR role.rolreplication OR role.rolbypassrls
          )
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = v_consumer
        )
           OR (membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = v_consumer
        ) AND membership.member <> (SELECT role.oid FROM pg_catalog.pg_roles AS role WHERE role.rolname=current_user))
    ) THEN
        RAISE EXCEPTION 'Scale seed operator must be a non-admin LOGIN without inherited roles';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname IN (v_workspace, v_assets, v_labeling)
          AND (
              role.rolcanlogin
              OR role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
              OR role.rolreplication OR role.rolbypassrls
          )
    ) OR (
        SELECT pg_catalog.count(*)
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname IN (v_workspace, v_assets, v_labeling)
    ) <> 3 THEN
        RAISE EXCEPTION 'Workspace, Assets and Labeling owners must be non-admin NOLOGIN roles';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(ARRAY[
            'public.projects',
            'public.project_members',
            'public.project_templates'
        ]) AS private(relation_name)
        WHERE pg_catalog.has_table_privilege(
                  v_consumer, private.relation_name,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
           OR pg_catalog.has_any_column_privilege(
                  v_consumer, private.relation_name,
                  'SELECT,INSERT,UPDATE,REFERENCES'
              )
    ) THEN
        RAISE EXCEPTION 'Scale seed operator must not access private Workspace tables directly';
    END IF;

    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_workspace_v1.create_scale_seed_project(uuid,text)',
        'yoao_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid)',
        'yoao_workspace_v1.guard_scale_seed_cleanup(text)',
        'yoao_workspace_v1.delete_scale_seed_projects(text,uuid[])',
        'yoao_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean)',
        'yoao_labeling_v1.write_project_counter_projection(uuid,jsonb)',
        'yoao_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[])',
        'yoao_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid)',
        'yoao_labeling_v1.create_scale_seed_tasks(text,uuid,uuid)',
        'yoao_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_roles AS owner ON owner.oid = function.proowner
            WHERE function.oid = v_signature::regprocedure
              AND function.prosecdef
              AND owner.rolname = CASE
                  WHEN pg_catalog.split_part(v_signature, '.', 1)
                       = 'yoao_workspace_v1' THEN v_workspace
                  WHEN pg_catalog.split_part(v_signature, '.', 1)
                       = 'yoao_assets_v1' THEN v_assets
                  ELSE v_labeling
              END
              AND function.provolatile = 'v'
        ) THEN
            RAISE EXCEPTION 'Unexpected scale seed capability owner or security mode: %', v_signature;
        END IF;
    END LOOP;
END $profile$;

GRANT USAGE ON SCHEMA yoao_workspace_v1, yoao_assets_v1, yoao_labeling_v1
    TO :"consumer_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.create_scale_seed_project(uuid,text),
    yoao_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid),
    yoao_workspace_v1.guard_scale_seed_cleanup(text),
    yoao_workspace_v1.delete_scale_seed_projects(text,uuid[]),
    yoao_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean),
    yoao_labeling_v1.write_project_counter_projection(uuid,jsonb),
    yoao_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[]),
    yoao_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid),
    yoao_labeling_v1.create_scale_seed_tasks(text,uuid,uuid),
    yoao_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)
    TO :"consumer_role";
GRANT SELECT (id, project_id, display_id)
    ON yoao_labeling_v1.batch_rows TO :"consumer_role";
\endif

\if :{?grant_seed_operator}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.seed_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('yoao.seed_operator');
    v_workspace name := current_setting('yoao.workspace_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR v_consumer IN (current_user, v_workspace, 'identity_api_consumer')
       OR pg_has_role(v_consumer, v_workspace, 'MEMBER')
       OR pg_has_role(v_consumer, current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Workspace seed operator ownership';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_consumer AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members
        WHERE member=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
           OR (roleid=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
               AND member<>(SELECT oid FROM pg_roles
                            WHERE rolname=current_setting('yoao.migration_role')))
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_workspace AND NOT rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Seed operator requires non-admin, non-inheriting LOGIN';
    END IF;
    IF has_table_privilege(v_consumer, 'public.projects',
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR has_any_column_privilege(v_consumer, 'public.projects',
        'SELECT,INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'Apply the ordinary Workspace consumer fence first';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)',
        'yoao_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)',
        'yoao_workspace_v1.write_seed_members(jsonb)',
        'yoao_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)',
        'yoao_workspace_v1.write_seed_project_projection(uuid,jsonb)',
        'yoao_workspace_v1.pin_seed_member_assignment(uuid,timestamptz)'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner
            WHERE p.oid=v_signature::regprocedure AND p.prosecdef
              AND owner.rolname=v_workspace
        ) THEN
            RAISE EXCEPTION 'Unexpected seed function owner or security mode';
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', v_signature, v_consumer);
    END LOOP;
END $profile$;
\endif

\if :{?grant_template_query}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'yoao.workspace_template_query_role', :'workspace_template_query_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.workspace_template_query_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.workspace_template_query_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_workspace_template_query_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 3 THEN
        RAISE EXCEPTION 'Unexpected Workspace template query role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace template query roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    client_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_template_query_role')
    );
    policy_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_template_query_v1.template_query_policy()'
    )::oid;
    step_oid oid := pg_catalog.to_regprocedure(
        'yoao_workspace_template_query_v1.template_query_step(jsonb, text, jsonb, text, text[], text)'
    )::oid;
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR client_oid IS NULL
       OR policy_oid IS NULL OR step_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Workspace template query owner or LOGIN capability';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_v1.workspace_template_query_org_ids(uuid)'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = policy_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype THEN
        RAISE EXCEPTION 'A Workspace template query RPC has the wrong owner or return type';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_workspace_template_query_v1', 'USAGE'
    ) OR NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'yoao_identity_v1', 'USAGE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid,
        pg_catalog.to_regprocedure('yoao_identity_v1.workspace_template_query_org_ids(uuid)'),
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid,
        pg_catalog.to_regprocedure('yoao_identity_v1.read_workspace_membership_accounts(uuid[])'),
        'EXECUTE'
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.unnest(ARRAY[
            'id', 'display_id', 'name', 'description', 'type_label', 'type_key',
            'data_type', 'tool_bindings', 'label_config', 'ai_enabled', 'sampling',
            'maximum_annotations', 'show_overlap_first', 'iou_dedup_threshold',
            'box_threshold', 'text_threshold', 'rendering_config',
            'annotation_guide', 'scope', 'organization_id', 'created_by',
            'source_project_id', 'usage_count', 'created_at', 'updated_at'
        ]::text[]) AS column_name(name)
        WHERE NOT pg_catalog.has_column_privilege(
            workspace_oid, 'public.project_templates', column_name.name, 'SELECT'
        )
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.workspace_configuration_operation_receipts', 'operation_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.workspace_configuration_operation_receipts', 'actor_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.workspace_configuration_operation_receipts', 'operation', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.workspace_configuration_operation_receipts', 'result', 'SELECT'
    ) THEN
        RAISE EXCEPTION 'Workspace owner lacks a required template-query capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_template_query_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_template_query_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.workspace_template_query_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_workspace_template_query_v1
    TO :"workspace_template_query_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_template_query_v1.template_query_policy(),
    yoao_workspace_template_query_v1.template_query_step(jsonb, text, jsonb, text, text[], text)
    TO :"workspace_template_query_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_template_query_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_workspace_template_query_v1.template_query_policy()',
        'yoao_workspace_template_query_v1.template_query_step(jsonb, text, jsonb, text, text[], text)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace template query LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_workspace_template_query_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_workspace_template_query_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS relation
            ON relation.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND (pg_catalog.has_table_privilege(
                     role_oid, relation.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                 ) OR pg_catalog.has_any_column_privilege(
                     role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                 ))
      ) OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS sequence
            ON sequence.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND sequence.relkind = 'S'
            AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid, current_database(), 'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Workspace template query LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

DO $$
BEGIN
    IF current_setting('yoao.grant_section_applied', true) IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION 'No capability section selected; pass -v grant_<capability>=1';
    END IF;
END $$;
COMMIT;
