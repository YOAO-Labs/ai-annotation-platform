DO $ordinary_runtime_verify$
DECLARE
    runtime_role text := pg_catalog.current_setting('yoao.application_role');
    runtime_oid oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = runtime_role);
    creation_view oid := 'yoao_jobs_v1.async_job_creation'::pg_catalog.regclass;
    target record;
    signature text;
BEGIN
    FOR target IN
        WITH RECURSIVE scoped(oid) AS (
            SELECT c.oid FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE (n.nspname = 'public' OR pg_catalog.left(n.nspname,5) = 'yoao_'
                                      OR pg_catalog.left(n.nspname,4) = 'aap_')
              AND c.relkind IN ('r','p','m','f','v','S')
            UNION
            SELECT i.inhrelid FROM pg_catalog.pg_inherits AS i JOIN scoped ON scoped.oid = i.inhparent
        )
        SELECT c.oid, c.relkind, c.relacl, c.relowner FROM scoped
        JOIN pg_catalog.pg_class AS c ON c.oid = scoped.oid
    LOOP
        IF target.relowner = runtime_oid OR EXISTS (
            SELECT FROM pg_catalog.aclexplode(target.relacl) AS a
            WHERE a.grantee IN (0,runtime_oid) AND (
                a.grantee = 0 OR target.relkind <> 'v' OR a.is_grantable
                OR NOT (a.privilege_type = 'SELECT'
                        OR target.oid = creation_view AND a.privilege_type = 'INSERT')
            )
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_attribute AS attribute CROSS JOIN LATERAL
                pg_catalog.aclexplode(attribute.attacl) AS a
            WHERE attribute.attrelid = target.oid AND NOT attribute.attisdropped
              AND a.grantee IN (0,runtime_oid)
        ) THEN
            RAISE EXCEPTION 'Ordinary runtime retains private or writing relation privileges on %',
                target.oid::pg_catalog.regclass;
        END IF;
    END LOOP;

    IF (SELECT relkind FROM pg_catalog.pg_class WHERE oid = creation_view) <> 'v'
       OR NOT pg_catalog.has_table_privilege(runtime_oid, creation_view, 'SELECT')
       OR NOT pg_catalog.has_table_privilege(runtime_oid, creation_view, 'INSERT') THEN
        RAISE EXCEPTION 'Ordinary runtime is missing the Jobs creation view';
    END IF;
    IF (SELECT relowner FROM pg_catalog.pg_class
        WHERE oid = 'public.mv_user_perf_daily'::pg_catalog.regclass)
       IS DISTINCT FROM (SELECT proowner FROM pg_catalog.pg_proc
        WHERE oid = 'yoao_insights_v1.refresh_user_perf_daily()'::pg_catalog.regprocedure) THEN
        RAISE EXCEPTION 'Insights materialized view and refresh capability must share their trusted owner';
    END IF;
    FOREACH signature IN ARRAY ARRAY[
        'public.v_annotation_feedback_unified',
        'yoao_audit_v1.audit_events',
        'yoao_audit_v1.audit_daily_facts',
        'yoao_jobs_v1.notification_scope_facts',
        'yoao_identity_v1.account_profiles',
        'yoao_labeling_v1.task_rows',
        'yoao_workspace_v1.project_facts'
    ] LOOP
        IF NOT pg_catalog.has_table_privilege(runtime_oid, signature, 'SELECT') THEN
            RAISE EXCEPTION 'Ordinary runtime is missing the published view %', signature;
        END IF;
    END LOOP;

    FOREACH signature IN ARRAY ARRAY[
        'yoao_jobs_v1.claim_dispatches(jsonb)',
        'yoao_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)',
        'yoao_jobs_v1._directory_job_ids(uuid,uuid)',
        'yoao_jobs_v1.dataset_active_job_facts(uuid)',
        'yoao_jobs_v1.read_storage_fault_job(uuid)',
        'yoao_jobs_v1.settle_dataset_scan_job(uuid,text,text,timestamptz,text,jsonb)',
        'yoao_jobs_v1.transition_directory_registration_job(uuid,uuid,text,text,text,timestamptz,text,jsonb)',
        'yoao_notifications_v1._visible_inbox_rows(uuid,text)',
        'yoao_audit_v1._assert_platform_partition(date,bigint,bigint)',
        'yoao_audit_v1.maintenance_command(jsonb)',
        'yoao_audit_v1.refresh_audit_bi_mv()',
        'yoao_insights_v1.refresh_user_perf_daily()',
        'yoao_identity_mail_v2.claim_mail(jsonb)',
        'yoao_identity_mail_v2.finish_mail(jsonb)',
        'yoao_identity_mail_v2.renew_mail(jsonb)',
        'yoao_identity_recovery_v1.claim_mail(jsonb)',
        'yoao_identity_recovery_v1.finish_mail(jsonb)',
        'yoao_identity_recovery_v1.renew_mail(jsonb)',
        'yoao_settings_v1.mail_delivery_policy(jsonb)',
        'yoao_settings_v1.claim_smtp_test()',
        'yoao_settings_v1.finish_smtp_test(uuid,uuid,text,text,text,integer)',
        'yoao_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)',
        'yoao_inference_backend_endpoint_v1._endpoint_record(uuid)',
        'yoao_inference_backend_endpoint_v1._open_backend_endpoint_subject(text,uuid,uuid,text,text)',
        'yoao_inference_backend_endpoint_v1.open_health_observation_endpoint(text,uuid,uuid,text)',
        'yoao_inference_backend_endpoint_v1.open_setup_endpoint(text,uuid,uuid,text)',
        'yoao_inference_backend_endpoint_v1.open_catalog_fallback_endpoint(text,uuid,uuid,text)',
        'yoao_inference_backend_endpoint_v1.open_stats_endpoint(text,uuid,uuid,text)',
        'yoao_inference_backend_endpoint_v1.open_backend_read_endpoint(text,uuid,uuid,text)',
        'yoao_inference_backend_endpoint_v1.open_lifecycle_endpoint(text,uuid,uuid,text)',
        'yoao_inference_backend_endpoint_v1.open_execution_endpoint(text,uuid,uuid,text)',
        'yoao_inference_backend_endpoint_v1.open_gpu_control_endpoint(text,uuid,uuid,text)',
        'yoao_inference_backend_endpoint_v1.open_installation_terminal_endpoint(uuid,uuid,text,text,text)',
        'yoao_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid)',
        'yoao_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text)',
        'yoao_inference_registry_ingress_v1.registry_by_url(text)',
        'yoao_inference_registry_ingress_v1.registry_candidates(text)',
        'yoao_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text)',
        'yoao_inference_registry_ingress_v1.create_registry_plugin_legacy(uuid,text,text,text,boolean,text)',
        'yoao_inference_v1.workspace_configuration_backend(uuid)',
        'yoao_assets_v1.matches_avatar_reservation(text,jsonb)',
        'yoao_assets_v1.capture_storage_connection(uuid,uuid,text)',
        'yoao_assets_v1.begin_connection_probe(uuid,uuid,uuid,text[])',
        'yoao_assets_v1.finish_connection_probe(uuid,uuid,uuid,uuid,text[],jsonb,boolean)',
        'yoao_assets_v1.capture_reference_io(uuid,text)',
        'yoao_assets_v1.recheck_reference_io(uuid,text)',
        'yoao_assets_v1.capture_saved_binding_probe(uuid,uuid,boolean)',
        'yoao_assets_v1.saved_io_connector(uuid,integer,jsonb)',
        'yoao_assets_v1.saved_io_target(uuid,integer,integer,jsonb)',
        'yoao_assets_v1.saved_output_origin(jsonb,text,text)',
        'yoao_assets_v1.capture_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb)',
        'yoao_assets_v1.media_io_creation(uuid,uuid,uuid,text,boolean)',
        'yoao_assets_v1.migration_io_read(uuid,uuid,integer,text,text,integer,integer,boolean)',
        'yoao_assets_v1.begin_operator_auxiliary(jsonb)',
        'yoao_assets_v1.begin_saved_operator_original(jsonb)',
        'yoao_assets_v1.saved_operator_item_original(uuid,uuid)',
        'yoao_assets_v1.saved_operator_auxiliary_original(uuid,uuid)',
        'yoao_assets_v1.operator_auxiliary_material(uuid,uuid)',
        'yoao_assets_v1.operator_item_original_material(uuid,uuid)',
        'yoao_assets_v1.capture_native_pyramid_write_io(uuid,uuid,bigint,uuid,text)',
        'yoao_assets_v1.capture_native_pyramid_read_io(uuid,uuid,jsonb)',
        'yoao_assets_v1.capture_native_pyramid_retirement_io(uuid,uuid,uuid)',
        'public.retire_fixture_native_export_output(uuid)',
        'public.confirm_fixture_export_zip_absent(uuid)'
    ] LOOP
        IF pg_catalog.has_function_privilege(runtime_oid, pg_catalog.to_regprocedure(signature), 'EXECUTE') THEN
            RAISE EXCEPTION 'Ordinary runtime can execute private capability %', signature;
        END IF;
    END LOOP;
    FOREACH signature IN ARRAY ARRAY[
        'yoao_jobs_v1.prepare_dispatch(jsonb)',
        'yoao_jobs_v1.finish_dispatch(jsonb)',
        'yoao_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)',
        'yoao_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)',
        'yoao_jobs_v1.dataset_job_visible(uuid,uuid)',
        'yoao_notifications_v1.append_notification(jsonb)',
        'yoao_notifications_v1.append_job_terminal_notification(jsonb)',
        'yoao_notifications_v1.delivery_job_scopes(uuid[])',
        'yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer)',
        'yoao_audit_v1.append_events(jsonb)',
        'yoao_audit_v1.query_events(jsonb)',
        'yoao_audit_v1.platform_maintenance(text,date,bigint,bigint,text,uuid,timestamptz)',
        'yoao_inference_backend_endpoint_v1.capture_backend_endpoint_subject(uuid,uuid,text,text)',
        'yoao_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision)',
        'yoao_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)',
        'yoao_labeling_v1.create_uploading_task(uuid,uuid,text,text,text,uuid)',
        'public.prepare_image_export_scope(uuid,uuid,uuid,uuid,uuid[])',
        'public.image_export_scope_matches(uuid,uuid)'
    ] LOOP
        IF NOT pg_catalog.has_function_privilege(runtime_oid, signature, 'EXECUTE') THEN
            RAISE EXCEPTION 'Ordinary runtime is missing capability %', signature;
        END IF;
    END LOOP;
END;
$ordinary_runtime_verify$;
