-- Apply after owner and legacy application grants to an ordinary API/worker LOGIN.
-- Explicit seed operators retain their own profile. No service/connection is added.
-- psql -v expected_database=... -v migration_role=... -v workspace_owner=...
--      -v consumer_role=... -f workspace-consumer-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.workspace_consumer_role', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('aap.workspace_consumer_role');
    v_workspace name := current_setting('aap.workspace_owner');
    v_allowed oid[] := ARRAY[
        'aap_workspace_configuration_v1.configuration_policy()'::regprocedure::oid,
        'aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])'::regprocedure::oid,
        'aap_workspace_directory_query_v1.directory_query_policy()'::regprocedure::oid,
        'aap_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)'::regprocedure::oid,
        'aap_workspace_membership_v1.apply_membership_command(jsonb)'::regprocedure::oid,
        'aap_workspace_membership_v1.membership_policy()'::regprocedure::oid,
        'aap_workspace_membership_v1.prepare_membership_command(jsonb)'::regprocedure::oid,
        'aap_workspace_owner_transfer_v1.owner_transfer_policy()'::regprocedure::oid,
        'aap_workspace_owner_transfer_v1.transfer_project_owner(jsonb)'::regprocedure::oid,
        'aap_workspace_project_query_v1.project_query_policy()'::regprocedure::oid,
        'aap_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamp with time zone,timestamp with time zone,bigint,integer)'::regprocedure::oid,
        'aap_workspace_project_query_v1.project_search_member_ids(uuid,text)'::regprocedure::oid,
        'aap_workspace_project_query_v1.project_search_scope(uuid,text)'::regprocedure::oid,
        'aap_workspace_template_query_v1.template_query_policy()'::regprocedure::oid,
        'aap_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text)'::regprocedure::oid,
        'aap_workspace_v1.actor_project_memberships(uuid)'::regprocedure::oid,
        'aap_workspace_v1.clear_primary_backend_bindings(uuid)'::regprocedure::oid,
        'aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)'::regprocedure::oid,
        'aap_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)'::regprocedure::oid,
        'aap_workspace_v1.lock_project_manager(uuid,uuid)'::regprocedure::oid,
        'aap_workspace_v1.lock_project_member_reference(uuid,uuid,boolean)'::regprocedure::oid,
        'aap_workspace_v1.lock_project_reference(uuid)'::regprocedure::oid,
        'aap_workspace_v1.lock_project_reference_nowait(uuid)'::regprocedure::oid,
        'aap_workspace_v1.lock_project_references_for_update(uuid[])'::regprocedure::oid,
        'aap_workspace_v1.lock_project_row(uuid,boolean)'::regprocedure::oid,
        'aap_workspace_v1.preview_dataset_unlink(uuid,uuid,uuid)'::regprocedure::oid,
        'aap_workspace_v1.project_access_step(uuid,uuid,text)'::regprocedure::oid,
        'aap_workspace_v1.project_member_rows(uuid,uuid[],boolean)'::regprocedure::oid,
        'aap_workspace_v1.read_import_project(text)'::regprocedure::oid,
        'aap_workspace_v1.read_project_row(uuid)'::regprocedure::oid,
        'aap_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)'::regprocedure::oid,
        'aap_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)'::regprocedure::oid
    ];
    v_relation record;
    v_schema record;
    v_function record;
    v_columns text;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
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
        WHERE n.nspname='aap_workspace_v1'
          AND c.relname IN ('project_facts','project_member_facts')
    LOOP
        IF v_relation.relkind <> 'v'
           OR v_relation.relowner<>(SELECT oid FROM pg_roles WHERE rolname=v_workspace)
           OR NOT COALESCE(v_relation.reloptions @> ARRAY['security_barrier=true'],false)
           OR v_relation.columns IS DISTINCT FROM (CASE v_relation.relname
                WHEN 'project_facts' THEN ARRAY['id','organization_id','display_id','name','type_label','type_key','data_type','owner_id','status','ai_enabled','ml_backend_pool_id','default_variants','preannotate_pipeline','label_config','tool_bindings','sampling','maximum_annotations','show_overlap_first','iou_dedup_threshold','box_threshold','text_threshold','ai_interactive_enabled','raster_mask_native_editing_enabled','mask_qc_config','point_cloud_quality_config','rendering_config','annotation_guide','guide_assets','model_version','task_lock_ttl_seconds','video_sampling','video_collaboration','scene_mode','prefer_same_scene_continuation','scene_continuation_window_min','due_date','created_at','updated_at']
                ELSE ARRAY['id','project_id','user_id','role','assigned_by','assigned_at','version','updated_at','weekly_target'] END) THEN
            RAISE EXCEPTION 'Workspace read relation does not match its fixed contract';
        END IF;
    END LOOP;
    IF to_regclass('aap_workspace_v1.project_facts') IS NULL
       OR to_regclass('aap_workspace_v1.project_member_facts') IS NULL THEN
        RAISE EXCEPTION 'Workspace read relations are missing';
    END IF;

    -- Broad legacy defaults must not reintroduce private access on later tables.
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM %I',current_user,v_consumer);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I',current_user,v_consumer);

    -- Revoke column ACLs as well as relation ACLs; NOINHERIT is insufficient.
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname,c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE (c.relkind IN ('r','p','v','m','f') AND (
            n.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND (
                c.relname IN ('projects','project_members','project_templates',
                    'project_deletion_operations','dataset_unlink_operations')
                OR left(c.relname,10)='workspace_'
            ))
        )) OR (c.relkind='S' AND (
            n.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$'
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
        WHERE nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$'
    LOOP
        EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',v_schema.nspname,v_consumer);
        IF v_schema.nspname = 'aap_workspace_fixture_v1' THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',v_schema.nspname,v_consumer);
        END IF;
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I',v_schema.nspname,v_consumer);
    END LOOP;
    EXECUTE format('REVOKE EXECUTE ON FUNCTION aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean) FROM %I', v_consumer);
    FOR v_schema IN
        SELECT DISTINCT n.nspname FROM pg_namespace n JOIN pg_proc p ON p.pronamespace=n.oid
        WHERE p.oid=ANY(v_allowed)
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I',v_schema.nspname,v_consumer);
    END LOOP;
    EXECUTE format('GRANT SELECT ON aap_workspace_v1.project_facts, aap_workspace_v1.project_member_facts TO %I',v_consumer);
    FOR v_function IN SELECT oid::regprocedure AS signature FROM pg_proc WHERE oid=ANY(v_allowed)
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',v_function.signature,v_consumer);
    END LOOP;

    -- Effective privilege checks also catch PUBLIC grants and owned objects.
    IF EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$'
          AND has_function_privilege(v_consumer,p.oid,'EXECUTE') AND NOT p.oid=ANY(v_allowed)
    ) OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f') AND (
            n.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND (
                c.relname IN ('projects','project_members','project_templates',
                    'project_deletion_operations','dataset_unlink_operations')
                OR left(c.relname,10)='workspace_'
            ))
        ) AND (
            has_table_privilege(v_consumer,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR has_any_column_privilege(v_consumer,c.oid,'INSERT,UPDATE,REFERENCES')
            OR (c.oid NOT IN ('aap_workspace_v1.project_facts'::regclass,
                             'aap_workspace_v1.project_member_facts'::regclass)
                AND (has_table_privilege(v_consumer,c.oid,'SELECT')
                     OR has_any_column_privilege(v_consumer,c.oid,'SELECT')))
        )
    ) OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='S' AND (
            n.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND c.relname IN ('display_seq_projects','display_seq_project_templates'))
        ) AND CASE WHEN c.relkind='S' THEN
            has_sequence_privilege(v_consumer,c.oid,'USAGE,SELECT,UPDATE')
            ELSE false END
    ) OR EXISTS (
        SELECT 1 FROM pg_namespace n
        WHERE n.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$'
          AND has_schema_privilege(v_consumer,n.oid,'CREATE')
    ) THEN
        RAISE EXCEPTION 'Workspace consumer retains private data or command privileges';
    END IF;
END;
$profile$;
COMMIT;
