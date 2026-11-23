DO $ownership$
DECLARE
    v_owner pg_catalog.name;
    v_owner_oid pg_catalog.oid;
    v_public_create boolean;
    v_insights_create boolean;
    v_relation record;
    v_relation_oid pg_catalog.oid;
    v_function text;
    v_function_oid pg_catalog.oid;
    v_grantee record;
BEGIN
    SELECT owner.oid, owner.rolname INTO STRICT v_owner_oid, v_owner
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_roles AS owner ON owner.oid = function.proowner
    WHERE function.oid = 'yoao_insights_v1.clear_project_task_views(uuid)'::pg_catalog.regprocedure;

    IF v_owner <> CURRENT_USER AND (
        EXISTS (
            SELECT FROM pg_catalog.pg_roles AS role
            WHERE role.oid = v_owner_oid AND (role.rolcanlogin OR role.rolsuper OR role.rolcreatedb
                OR role.rolcreaterole OR role.rolreplication OR role.rolbypassrls)
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = v_owner_oid OR (membership.roleid = v_owner_oid
                AND membership.member <> CURRENT_USER::pg_catalog.regrole)
        )
    ) THEN
        RAISE EXCEPTION 'The existing Insights owner is not a confined NOLOGIN role';
    END IF;

    v_public_create := pg_catalog.has_schema_privilege(v_owner_oid, 'public', 'CREATE');
    v_insights_create := pg_catalog.has_schema_privilege(v_owner_oid, 'yoao_insights_v1', 'CREATE');
    IF NOT v_public_create THEN
        EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA public TO %I', v_owner);
    END IF;
    IF NOT v_insights_create THEN
        EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA yoao_insights_v1 TO %I', v_owner);
    END IF;

    FOR v_relation IN SELECT * FROM (VALUES
        ('MATERIALIZED VIEW', 'public.mv_user_perf_daily'),
        ('TABLE', 'public.project_task_views'),
        ('TABLE', 'public.dataset_saved_views')
    ) AS storage(kind, qualified_name) LOOP
        v_relation_oid := v_relation.qualified_name::pg_catalog.regclass;
        IF (SELECT relowner FROM pg_catalog.pg_class WHERE oid = v_relation_oid) <> v_owner_oid THEN
            EXECUTE pg_catalog.format('ALTER %s %s OWNER TO %I',
                v_relation.kind, v_relation.qualified_name, v_owner);
        END IF;
        FOR v_grantee IN
            SELECT DISTINCT CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(acl.grantee)) END AS role_sql,
                acl.column_name
            FROM (
                SELECT entry.grantee, NULL::pg_catalog.name AS column_name
                FROM pg_catalog.pg_class AS relation
                CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS entry
                WHERE relation.oid = v_relation_oid
                UNION ALL
                SELECT entry.grantee, attribute.attname
                FROM pg_catalog.pg_attribute AS attribute
                CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS entry
                WHERE attribute.attrelid = v_relation_oid AND NOT attribute.attisdropped
            ) AS acl
            WHERE acl.grantee <> v_owner_oid
        LOOP
            IF v_grantee.column_name IS NULL THEN
                EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %s FROM %s CASCADE',
                    v_relation.qualified_name, v_grantee.role_sql);
            ELSE
                EXECUTE pg_catalog.format('REVOKE ALL (%I) ON TABLE %s FROM %s CASCADE',
                    v_grantee.column_name, v_relation.qualified_name, v_grantee.role_sql);
            END IF;
        END LOOP;
    END LOOP;

    FOREACH v_function IN ARRAY ARRAY[
        'yoao_insights_v1.refresh_user_perf_daily()',
        'yoao_insights_v1._task_view_project_scope(uuid,uuid,boolean)',
        'yoao_insights_v1._task_view_snapshot(public.project_task_views)',
        'yoao_insights_v1._lock_writable_task_view(uuid,uuid,uuid,boolean)',
        'yoao_insights_v1.list_saved_task_views(uuid,uuid,text)',
        'yoao_insights_v1.read_saved_task_view(uuid,uuid,uuid)',
        'yoao_insights_v1.create_saved_task_view(uuid,uuid,text,text,text,jsonb,jsonb,jsonb)',
        'yoao_insights_v1.update_saved_task_view(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb)',
        'yoao_insights_v1.delete_saved_task_view(uuid,uuid,uuid)',
        'yoao_insights_v1._dataset_view_snapshot(public.dataset_saved_views)',
        'yoao_insights_v1.list_personal_dataset_views(uuid,uuid)',
        'yoao_insights_v1.has_personal_dataset_view(uuid,uuid,uuid)',
        'yoao_insights_v1.prepare_personal_dataset_view(uuid,uuid)',
        'yoao_insights_v1.create_personal_dataset_view(uuid,uuid,text,jsonb)',
        'yoao_insights_v1.update_personal_dataset_view(uuid,uuid,uuid,text,jsonb)',
        'yoao_insights_v1.delete_personal_dataset_view(uuid,uuid,uuid)'
    ] LOOP
        v_function_oid := v_function::pg_catalog.regprocedure;
        IF v_owner <> CURRENT_USER THEN
            EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I', v_function, v_owner);
        END IF;
        FOR v_grantee IN
            SELECT DISTINCT CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(acl.grantee)) END AS role_sql
            FROM pg_catalog.pg_proc AS function
            CROSS JOIN LATERAL pg_catalog.aclexplode(function.proacl) AS acl
            WHERE function.oid = v_function_oid AND acl.grantee <> v_owner_oid
        LOOP
            EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM %s CASCADE',
                v_function, v_grantee.role_sql);
        END LOOP;
    END LOOP;

    EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA public, yoao_insights_v1, yoao_labeling_v1, yoao_workspace_v1, yoao_assets_v1 TO %I', v_owner);
    EXECUTE pg_catalog.format('GRANT TEMPORARY ON DATABASE %I TO %I', pg_catalog.current_database(), v_owner);
    EXECUTE pg_catalog.format('GRANT SELECT (user_id, project_id, kind, started_at, duration_ms, annotation_count, was_rejected) ON yoao_labeling_v1.task_event_facts TO %I', v_owner);
    EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION yoao_workspace_v1.read_project_visibility(uuid,uuid), yoao_workspace_v1.lock_media_project_access(uuid,uuid), yoao_assets_v1.lock_dataset_reference(uuid) TO %I', v_owner);
    IF v_owner <> CURRENT_USER THEN
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION yoao_insights_v1.refresh_user_perf_daily() TO %I', CURRENT_USER);
    END IF;

    IF NOT v_insights_create THEN
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA yoao_insights_v1 FROM %I', v_owner);
    END IF;
    IF NOT v_public_create THEN
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA public FROM %I', v_owner);
    END IF;
END;
$ownership$;
