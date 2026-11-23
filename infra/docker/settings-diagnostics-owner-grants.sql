GRANT USAGE ON SCHEMA yoao_settings_v1 TO :"settings_owner";
REVOKE UPDATE ON TABLE public.system_settings FROM :"settings_owner";
GRANT SELECT (key, value_type, value_json, updated_at, updated_by, runtime_revision),
    INSERT (key, value_type, value_json, updated_at, updated_by),
    UPDATE (value_type, value_json, updated_at, updated_by), DELETE
    ON public.system_settings TO :"settings_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_settings_v1 TO :"settings_owner";
ALTER TABLE yoao_settings_v1.smtp_test_attempts OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1._rekey_override() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1._setting_type(text) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1._override_stamp() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1._super_admin(uuid,boolean) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.runtime_knob(text) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.connector_override() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.import_limits() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.management_snapshot(uuid,boolean) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.apply_settings_edit(uuid,text,jsonb,text[],text)
    OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.queue_smtp_test(uuid) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.read_smtp_test(uuid,uuid) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.claim_smtp_test() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.finish_smtp_test(uuid,uuid,text,text,text,integer)
    OWNER TO :"settings_owner";
REVOKE CREATE ON SCHEMA yoao_settings_v1 FROM :"settings_owner";

GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"settings_owner", :"diagnostics_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid),
    yoao_identity_v1.lock_accounts_share(uuid[],boolean) TO :"settings_owner", :"diagnostics_owner";
GRANT SELECT (id, name, role) ON yoao_identity_v1.account_profiles TO :"diagnostics_owner";

GRANT USAGE ON SCHEMA public TO :"diagnostics_owner", :"settings_owner";
GRANT SELECT, DELETE ON public.bug_reports, public.bug_comments, public.bug_attachment_uploads
    TO :"diagnostics_owner";
GRANT INSERT (id, display_id, reporter_id, user_role, route, project_id, task_id, title, description,
    severity, status, browser_ua, viewport, recent_api_calls, recent_console_errors, screenshot_url, attachments),
    UPDATE (project_id, task_id, title, description, severity, status, duplicate_of_id, assigned_to_id,
        resolution, fixed_in_version, triaged_at, fixed_at, reopen_count, last_reopened_at)
    ON public.bug_reports TO :"diagnostics_owner";
GRANT INSERT (id, bug_report_id, author_id, body) ON public.bug_comments TO :"diagnostics_owner";
GRANT INSERT (id, owner_id, sha256, size_bytes, mime_type, file_name, created_at, expires_at, storage_ref, state),
    UPDATE (bound_report_id, object_version_id, state, last_attempted_at, retired_at)
    ON public.bug_attachment_uploads TO :"diagnostics_owner";
GRANT USAGE ON SEQUENCE public.display_seq_bug_reports TO :"diagnostics_owner";
GRANT SELECT (purpose) ON public.storage_operations TO :"diagnostics_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1, yoao_labeling_v1 TO :"diagnostics_owner";
GRANT SELECT (id, project_id, dataset_item_id) ON yoao_labeling_v1.task_rows TO :"diagnostics_owner";
GRANT SELECT (project_id, item_id) ON yoao_assets_v1.project_dataset_item_sources TO :"diagnostics_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_diagnostics_v1 TO :"diagnostics_owner";
DO $ownership$
DECLARE
    v_signature text;
    v_function regprocedure;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_diagnostics_v1._actor_role(uuid,boolean)',
        'yoao_diagnostics_v1._bug_projection(public.bug_reports)',
        'yoao_diagnostics_v1._comment_projection(public.bug_comments,text,text)',
        'yoao_diagnostics_v1._bug_attachment_key(uuid,uuid,text)',
        'yoao_diagnostics_v1._bug_attachment_public(public.bug_attachment_uploads)',
        'yoao_diagnostics_v1._bug_attachment_claim(jsonb)',
        'yoao_diagnostics_v1._bug_attachment_id(text)',
        'yoao_diagnostics_v1._bug_attachment_receipt(public.bug_attachment_uploads)',
        'yoao_diagnostics_v1._comment_context(public.bug_reports)',
        'yoao_diagnostics_v1._bug_attachment_cleanup_blocked(public.bug_attachment_uploads)',
        'yoao_diagnostics_v1.create_bug_report(uuid,jsonb)',
        'yoao_diagnostics_v1.confirm_bug_attachment_binding(uuid,uuid)',
        'yoao_diagnostics_v1.list_bug_reports(uuid,boolean,text,text,text,integer,integer)',
        'yoao_diagnostics_v1.read_bug_report(uuid,uuid)',
        'yoao_diagnostics_v1.patch_bug_report(uuid,uuid,jsonb)',
        'yoao_diagnostics_v1.prepare_bug_comment(uuid,uuid)',
        'yoao_diagnostics_v1.add_bug_comment(uuid,uuid,text)',
        'yoao_diagnostics_v1.bug_cluster_candidates(uuid,uuid)',
        'yoao_diagnostics_v1.bug_report_markdown_comments(uuid,uuid[])',
        'yoao_diagnostics_v1.delete_bug_report(uuid,uuid)',
        'yoao_diagnostics_v1.detach_orphan_bug_tasks(uuid,uuid[])',
        'yoao_diagnostics_v1.reserve_bug_attachment(uuid,uuid,text,integer,text,text,timestamptz,timestamptz,jsonb)',
        'yoao_diagnostics_v1.lock_pending_bug_attachment(uuid,uuid)',
        'yoao_diagnostics_v1.record_bug_attachment_version(uuid,uuid,jsonb,text)',
        'yoao_diagnostics_v1.publish_bug_attachment(uuid,uuid,jsonb,text)',
        'yoao_diagnostics_v1.lock_report_attachment(uuid,uuid,text)',
        'yoao_diagnostics_v1.bug_attachment_cleanup_candidates(integer)',
        'yoao_diagnostics_v1.prepare_bug_attachment_cleanup(uuid)',
        'yoao_diagnostics_v1.lock_retired_bug_attachment(uuid)',
        'yoao_diagnostics_v1.complete_bug_attachment_cleanup(uuid,jsonb,text)',
        'yoao_diagnostics_v1.bug_attachment_history(uuid)',
        'yoao_diagnostics_v1.bug_attachment_location_referenced(uuid)',
        'yoao_diagnostics_v1.bug_attachment_maintenance_facts()'
    ] LOOP
        v_function := v_signature::regprocedure;
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I', v_function,
                                  pg_catalog.current_setting('yoao.diagnostics_owner'));
    END LOOP;
END
$ownership$;
REVOKE CREATE ON SCHEMA yoao_diagnostics_v1 FROM :"diagnostics_owner";
GRANT USAGE ON SCHEMA yoao_diagnostics_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_diagnostics_v1.bug_attachment_location_referenced(uuid),
    yoao_diagnostics_v1.bug_attachment_maintenance_facts(),
    yoao_diagnostics_v1.lock_pending_bug_attachment(uuid,uuid),
    yoao_diagnostics_v1.lock_report_attachment(uuid,uuid,text),
    yoao_diagnostics_v1.lock_retired_bug_attachment(uuid)
    TO :"assets_owner";
