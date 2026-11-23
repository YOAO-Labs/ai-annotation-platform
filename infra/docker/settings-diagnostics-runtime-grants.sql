GRANT USAGE ON SCHEMA yoao_settings_v1, yoao_diagnostics_v1 TO :"application_role";
REVOKE EXECUTE ON FUNCTION yoao_settings_v1.mail_delivery_policy(jsonb),
    yoao_settings_v1.claim_smtp_test(), yoao_settings_v1.finish_smtp_test(uuid,uuid,text,text,text,integer)
    FROM :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_settings_v1.runtime_knob(text),
    yoao_settings_v1.connector_override(),
    yoao_settings_v1.import_limits(),
    yoao_settings_v1.management_snapshot(uuid,boolean),
    yoao_settings_v1.apply_settings_edit(uuid,text,jsonb,text[],text),
    yoao_settings_v1.queue_smtp_test(uuid),
    yoao_settings_v1.read_smtp_test(uuid,uuid),
    yoao_diagnostics_v1.create_bug_report(uuid,jsonb),
    yoao_diagnostics_v1.confirm_bug_attachment_binding(uuid,uuid),
    yoao_diagnostics_v1.list_bug_reports(uuid,boolean,text,text,text,integer,integer),
    yoao_diagnostics_v1.read_bug_report(uuid,uuid),
    yoao_diagnostics_v1.patch_bug_report(uuid,uuid,jsonb),
    yoao_diagnostics_v1.prepare_bug_comment(uuid,uuid),
    yoao_diagnostics_v1.add_bug_comment(uuid,uuid,text),
    yoao_diagnostics_v1.bug_cluster_candidates(uuid,uuid),
    yoao_diagnostics_v1.bug_report_markdown_comments(uuid,uuid[]),
    yoao_diagnostics_v1.delete_bug_report(uuid,uuid),
    yoao_diagnostics_v1.detach_orphan_bug_tasks(uuid,uuid[]),
    yoao_diagnostics_v1.reserve_bug_attachment(uuid,uuid,text,integer,text,text,timestamptz,timestamptz,jsonb),
    yoao_diagnostics_v1.lock_pending_bug_attachment(uuid,uuid),
    yoao_diagnostics_v1.record_bug_attachment_version(uuid,uuid,jsonb,text),
    yoao_diagnostics_v1.publish_bug_attachment(uuid,uuid,jsonb,text),
    yoao_diagnostics_v1.lock_report_attachment(uuid,uuid,text),
    yoao_diagnostics_v1.bug_attachment_cleanup_candidates(integer),
    yoao_diagnostics_v1.prepare_bug_attachment_cleanup(uuid),
    yoao_diagnostics_v1.lock_retired_bug_attachment(uuid),
    yoao_diagnostics_v1.complete_bug_attachment_cleanup(uuid,jsonb,text),
    yoao_diagnostics_v1.bug_attachment_history(uuid)
TO :"application_role";
