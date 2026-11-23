ALTER TABLE public.system_settings
    ADD COLUMN runtime_revision uuid NOT NULL DEFAULT pg_catalog.gen_random_uuid();
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1._rekey_override()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF (NEW.key, NEW.value_type, NEW.value_json::text, NEW.updated_at, NEW.updated_by)
        IS DISTINCT FROM
       (OLD.key, OLD.value_type, OLD.value_json::text, OLD.updated_at, OLD.updated_by) THEN
        NEW.runtime_revision := pg_catalog.gen_random_uuid();
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
CREATE TRIGGER settings_runtime_revision BEFORE UPDATE ON public.system_settings
FOR EACH ROW EXECUTE FUNCTION yoao_settings_v1._rekey_override();
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1._setting_type(p_key text)
RETURNS text
LANGUAGE sql IMMUTABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN p_key = 'allow_open_registration' THEN 'bool'
        WHEN p_key IN ('frontend_base_url', 'smtp_host', 'smtp_user', 'smtp_password', 'smtp_from') THEN 'str'
        WHEN p_key IN ('invitation_ttl_days', 'smtp_port', 'max_invitations_per_day',
            'offline_threshold_minutes', 'dataset_import_max_files', 'dataset_import_max_total_bytes',
            'task_create_sync_threshold', 'video_chunk_warmup_lookahead') THEN 'int'
        WHEN p_key = 'connector_host_allowlist' THEN 'json'
    END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1._override_stamp()
RETURNS text
LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(COALESCE(
        pg_catalog.jsonb_agg(pg_catalog.jsonb_build_array(s.key, s.runtime_revision)
                             ORDER BY s.key)::text, '[]'), 'UTF8')), 'hex')
    FROM public.system_settings AS s
    WHERE yoao_settings_v1._setting_type(s.key) IS NOT NULL;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1._super_admin(p_actor uuid, p_lock boolean)
RETURNS boolean
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
BEGIN
    IF p_actor IS NULL THEN RETURN false; END IF;
    IF p_lock THEN
        v_actor := yoao_identity_v1.lock_accounts_share(ARRAY[p_actor], false) #> '{accounts,0}';
        RETURN COALESCE(v_actor->>'role' = 'super_admin'
                     AND v_actor->>'is_active' = 'true', false);
    END IF;
    RETURN EXISTS (
        SELECT FROM yoao_identity_v1.read_actor_facts(p_actor) AS a
        WHERE a.is_active AND a.platform_role = 'super_admin'
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1.runtime_knob(p_key text)
RETURNS jsonb
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.value_json FROM public.system_settings AS s
    WHERE s.key = p_key AND s.key IN ('offline_threshold_minutes',
        'task_create_sync_threshold', 'video_chunk_warmup_lookahead');
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1.connector_override()
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.value_json FROM public.system_settings AS s
    WHERE s.key = 'connector_host_allowlist';
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1.import_limits()
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'stamp', yoao_settings_v1._override_stamp(),
        'overrides', COALESCE(pg_catalog.jsonb_object_agg(s.key, s.value_json), '{}'::jsonb)
    )
    FROM public.system_settings AS s
    WHERE s.key IN ('dataset_import_max_files', 'dataset_import_max_total_bytes');
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1.management_snapshot(p_actor uuid, p_edit boolean)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_result jsonb;
BEGIN
    IF NOT yoao_settings_v1._super_admin(p_actor, p_edit) THEN RETURN NULL; END IF;
    IF p_edit THEN
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('aap.system_settings'));
        PERFORM 1 FROM public.system_settings AS s
        WHERE yoao_settings_v1._setting_type(s.key) IS NOT NULL
        ORDER BY s.key FOR UPDATE;
    END IF;
    SELECT pg_catalog.jsonb_build_object(
        'stamp', yoao_settings_v1._override_stamp(),
        'overrides', COALESCE(pg_catalog.jsonb_object_agg(s.key, pg_catalog.jsonb_build_object(
            'value', CASE WHEN s.key <> 'smtp_password' THEN s.value_json
                WHEN pg_catalog.jsonb_typeof(s.value_json) = 'string'
                THEN pg_catalog.to_jsonb(s.value_json #>> '{}' <> '') END,
            'is_override', NULLIF(s.value_json, 'null'::jsonb) IS NOT NULL,
            'updated_at', s.updated_at, 'updated_by', s.updated_by
        )), '{}'::jsonb)
    ) INTO v_result
    FROM public.system_settings AS s
    WHERE yoao_settings_v1._setting_type(s.key) IS NOT NULL;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1.apply_settings_edit(
    p_actor uuid, p_stamp text, p_set jsonb, p_reset text[], p_deployment_password text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key text;
    v_value jsonb;
    v_old jsonb;
    v_type text;
    v_old_password text;
    v_changed jsonb := '{}'::jsonb;
BEGIN
    IF NOT yoao_settings_v1._super_admin(p_actor, true) THEN RETURN NULL; END IF;
    IF pg_catalog.jsonb_typeof(p_set) IS DISTINCT FROM 'object' OR p_reset IS NULL
       OR (p_set <> '{}'::jsonb AND pg_catalog.cardinality(p_reset) <> 0) THEN
        RAISE EXCEPTION 'invalid settings edit' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('aap.system_settings'));
    IF p_stamp IS DISTINCT FROM yoao_settings_v1._override_stamp() THEN
        RETURN pg_catalog.jsonb_build_object('conflict', true);
    END IF;
    FOR v_key, v_value IN SELECT key, value FROM pg_catalog.jsonb_each(p_set) LOOP
        v_type := yoao_settings_v1._setting_type(v_key);
        IF v_type IS NULL OR pg_catalog.jsonb_typeof(v_value) <> CASE v_type
                WHEN 'int' THEN 'number' WHEN 'str' THEN 'string'
                WHEN 'bool' THEN 'boolean' ELSE 'array' END
           OR (v_type = 'int' AND v_value::text !~ '^(0|[1-9][0-9]*)$') THEN
            RAISE EXCEPTION 'invalid settings override' USING ERRCODE = '22023';
        END IF;
        IF v_key = 'smtp_password' THEN
            SELECT s.value_json INTO v_old FROM public.system_settings AS s WHERE s.key = v_key;
            v_old_password := CASE WHEN pg_catalog.jsonb_typeof(v_old) = 'string'
                THEN v_old #>> '{}' ELSE p_deployment_password END;
        END IF;
        INSERT INTO public.system_settings AS s (key, value_type, value_json, updated_by, updated_at)
        VALUES (v_key, v_type, v_value, p_actor, pg_catalog.now())
        ON CONFLICT (key) DO UPDATE SET value_type = EXCLUDED.value_type,
            value_json = EXCLUDED.value_json, updated_by = EXCLUDED.updated_by,
            updated_at = EXCLUDED.updated_at
        WHERE s.value_json::text IS DISTINCT FROM EXCLUDED.value_json::text;
        IF FOUND THEN
            v_changed := v_changed || pg_catalog.jsonb_build_object(v_key,
                v_key <> 'smtp_password' OR v_old_password IS DISTINCT FROM (v_value #>> '{}'));
        END IF;
    END LOOP;
    FOREACH v_key IN ARRAY p_reset LOOP
        IF yoao_settings_v1._setting_type(v_key) IS NULL THEN
            RAISE EXCEPTION 'invalid settings reset' USING ERRCODE = '22023';
        END IF;
        DELETE FROM public.system_settings AS s WHERE s.key = v_key RETURNING s.value_json INTO v_old;
        IF FOUND THEN
            v_changed := v_changed || pg_catalog.jsonb_build_object(v_key,
                v_key <> 'smtp_password' OR (pg_catalog.jsonb_typeof(v_old) = 'string'
                    AND (v_old #>> '{}') IS DISTINCT FROM p_deployment_password) IS TRUE);
        END IF;
    END LOOP;
    RETURN pg_catalog.jsonb_build_object('changes', v_changed);
END;
$function$;
-- aap-statement-break
CREATE TABLE yoao_settings_v1.smtp_test_attempts (
    id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    actor_id uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    lease_id uuid,
    recipient text,
    result jsonb
);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_settings_v1.smtp_test_attempts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1.queue_smtp_test(p_actor uuid)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_id uuid;
BEGIN
    IF NOT yoao_settings_v1._super_admin(p_actor, true) THEN RETURN NULL; END IF;
    INSERT INTO yoao_settings_v1.smtp_test_attempts (actor_id, expires_at)
    VALUES (p_actor, pg_catalog.clock_timestamp() + INTERVAL '90 seconds')
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1.read_smtp_test(p_actor uuid, p_attempt uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT t.result FROM yoao_settings_v1.smtp_test_attempts AS t
    JOIN yoao_identity_v1.read_actor_facts(p_actor) AS a ON a.actor_id = t.actor_id
    WHERE t.id = p_attempt AND t.actor_id = p_actor AND a.is_active
      AND a.platform_role = 'super_admin' AND t.expires_at > pg_catalog.statement_timestamp();
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1.claim_smtp_test()
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_attempt yoao_settings_v1.smtp_test_attempts;
    v_actor jsonb;
    v_address text;
    v_lease uuid := pg_catalog.gen_random_uuid();
BEGIN
    DELETE FROM yoao_settings_v1.smtp_test_attempts WHERE expires_at <= pg_catalog.clock_timestamp();
    SELECT t.* INTO v_attempt FROM yoao_settings_v1.smtp_test_attempts AS t
    WHERE t.lease_id IS NULL AND t.result IS NULL
      AND t.expires_at > pg_catalog.clock_timestamp() + INTERVAL '30 seconds'
    ORDER BY t.expires_at, t.id LIMIT 1 FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN RETURN NULL; END IF;
    v_actor := yoao_identity_v1.lock_accounts_share(ARRAY[v_attempt.actor_id], false) #> '{accounts,0}';
    v_address := v_actor->>'email';
    IF v_actor->>'role' IS DISTINCT FROM 'super_admin' OR v_actor->>'is_active' IS DISTINCT FROM 'true'
       OR COALESCE(v_address, '') = '' THEN
        UPDATE yoao_settings_v1.smtp_test_attempts SET result = '{"reason":"not_authorized"}'::jsonb
        WHERE id = v_attempt.id;
        RETURN NULL;
    END IF;
    UPDATE yoao_settings_v1.smtp_test_attempts SET lease_id = v_lease, recipient = v_address
    WHERE id = v_attempt.id;
    RETURN pg_catalog.jsonb_build_object('id', v_attempt.id, 'lease_id', v_lease, 'recipient', v_address);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_settings_v1.finish_smtp_test(
    p_attempt uuid, p_lease uuid, p_reason text, p_sender text, p_host text, p_port integer
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_reason IS NULL OR p_reason NOT IN ('sent', 'not_configured', 'message_invalid',
        'tls_failed', 'auth_failed', 'recipient_rejected', 'sender_rejected', 'data_rejected',
        'connect_failed', 'transport_failed') OR (p_reason = 'sent' AND (
        COALESCE(p_sender, '') = '' OR COALESCE(p_host, '') = '' OR p_port IS NULL
        OR p_port NOT BETWEEN 1 AND 65535)) THEN
        RAISE EXCEPTION 'invalid SMTP diagnostic receipt' USING ERRCODE = '22023';
    END IF;
    UPDATE yoao_settings_v1.smtp_test_attempts AS t
    SET result = CASE WHEN p_reason = 'sent' THEN pg_catalog.jsonb_build_object(
        'reason', p_reason, 'to', t.recipient, 'from', p_sender, 'host', p_host, 'port', p_port)
        ELSE pg_catalog.jsonb_build_object('reason', p_reason) END
    WHERE t.id = p_attempt AND t.lease_id = p_lease AND t.result IS NULL
      AND t.expires_at > pg_catalog.clock_timestamp();
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_settings_v1._rekey_override(), yoao_settings_v1._setting_type(text),
    yoao_settings_v1._override_stamp(), yoao_settings_v1._super_admin(uuid, boolean),
    yoao_settings_v1.runtime_knob(text), yoao_settings_v1.connector_override(),
    yoao_settings_v1.import_limits(), yoao_settings_v1.management_snapshot(uuid, boolean),
    yoao_settings_v1.apply_settings_edit(uuid, text, jsonb, text[], text),
    yoao_settings_v1.queue_smtp_test(uuid), yoao_settings_v1.read_smtp_test(uuid, uuid),
    yoao_settings_v1.claim_smtp_test(),
    yoao_settings_v1.finish_smtp_test(uuid, uuid, text, text, text, integer)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._actor_role(p_actor uuid, p_lock boolean)
RETURNS text
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_role text;
BEGIN
    IF p_actor IS NULL THEN RETURN NULL; END IF;
    IF p_lock THEN
        v_actor := yoao_identity_v1.lock_accounts_share(ARRAY[p_actor], false) #> '{accounts,0}';
        IF v_actor->>'is_active' = 'true' THEN RETURN v_actor->>'role'; END IF;
        RETURN NULL;
    END IF;
    SELECT a.platform_role INTO v_role FROM yoao_identity_v1.read_actor_facts(p_actor) AS a
    WHERE a.is_active;
    RETURN v_role;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._bug_projection(p_bug public.bug_reports)
RETURNS jsonb
LANGUAGE sql STABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_bug.id, 'display_id', p_bug.display_id, 'reporter_id', p_bug.reporter_id,
        'route', p_bug.route, 'user_role', p_bug.user_role, 'project_id', p_bug.project_id,
        'task_id', p_bug.task_id, 'title', p_bug.title, 'description', p_bug.description,
        'severity', p_bug.severity, 'status', p_bug.status, 'duplicate_of_id', p_bug.duplicate_of_id,
        'browser_ua', p_bug.browser_ua, 'viewport', p_bug.viewport,
        'recent_api_calls', p_bug.recent_api_calls, 'recent_console_errors', p_bug.recent_console_errors,
        'screenshot_url', p_bug.screenshot_url, 'attachments', p_bug.attachments,
        'resolution', p_bug.resolution, 'fixed_in_version', p_bug.fixed_in_version,
        'assigned_to_id', p_bug.assigned_to_id, 'created_at', p_bug.created_at,
        'triaged_at', p_bug.triaged_at, 'fixed_at', p_bug.fixed_at,
        'reopen_count', p_bug.reopen_count, 'last_reopened_at', p_bug.last_reopened_at
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._comment_projection(
    p_comment public.bug_comments, p_name text, p_role text
)
RETURNS jsonb
LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_comment.id, 'bug_report_id', p_comment.bug_report_id,
        'author_id', p_comment.author_id, 'body', p_comment.body, 'created_at', p_comment.created_at,
        'author_name', COALESCE(p_name, ''), 'author_role', COALESCE(p_role, '')
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._bug_attachment_key(p_owner uuid, p_upload uuid, p_mime text)
RETURNS text
LANGUAGE sql IMMUTABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT 'bug-report-attachments/' || p_owner::text || '/' || p_upload::text ||
        CASE p_mime WHEN 'image/png' THEN '.png' WHEN 'image/jpeg' THEN '.jpg'
            WHEN 'image/webp' THEN '.webp' END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._bug_attachment_public(p_upload public.bug_attachment_uploads)
RETURNS jsonb
LANGUAGE sql IMMUTABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'storageKey', yoao_diagnostics_v1._bug_attachment_key(p_upload.owner_id, p_upload.id, p_upload.mime_type),
        'fileName', p_upload.file_name, 'mimeType', p_upload.mime_type, 'size', p_upload.size_bytes
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._bug_attachment_claim(p_claim jsonb)
RETURNS jsonb
LANGUAGE sql IMMUTABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'storageKey', COALESCE(p_claim->'storageKey', p_claim->'storage_key'),
        'fileName', COALESCE(p_claim->'fileName', p_claim->'file_name'),
        'mimeType', COALESCE(p_claim->'mimeType', p_claim->'mime_type'),
        'size', p_claim->'size'
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._bug_attachment_id(p_key text)
RETURNS uuid
LANGUAGE sql IMMUTABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_key ~
        '^bug-report-attachments/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}[.](png|jpg|webp)$'
        THEN pg_catalog.split_part(pg_catalog.split_part(p_key, '/', 3), '.', 1)::uuid END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._bug_attachment_receipt(p_upload public.bug_attachment_uploads)
RETURNS jsonb
LANGUAGE sql STABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_upload.id, 'owner_id', p_upload.owner_id, 'storage_ref', p_upload.storage_ref,
        'object_version_id', p_upload.object_version_id, 'sha256', p_upload.sha256,
        'size_bytes', p_upload.size_bytes, 'mime_type', p_upload.mime_type, 'file_name', p_upload.file_name,
        'created_at', p_upload.created_at, 'expires_at', p_upload.expires_at,
        'bound_report_id', p_upload.bound_report_id
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.create_bug_report(p_actor uuid, p_input jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text := yoao_diagnostics_v1._actor_role(p_actor, true);
    v_bug public.bug_reports;
    v_upload public.bug_attachment_uploads;
    v_supplied jsonb := COALESCE(p_input->'attachments', '[]'::jsonb);
    v_alias text := NULLIF(p_input->>'screenshot_url', '');
    v_keys text[];
    v_uploads uuid[] := ARRAY[]::uuid[];
    v_key text;
    v_actual jsonb;
    v_ordered jsonb := '[]'::jsonb;
    v_positions jsonb := '{}'::jsonb;
BEGIN
    IF v_role IS NULL THEN RETURN '{"error":"authority"}'::jsonb; END IF;
    IF pg_catalog.jsonb_typeof(p_input) IS DISTINCT FROM 'object'
       OR pg_catalog.char_length(COALESCE(p_input->>'title', '')) NOT BETWEEN 1 AND 500
       OR pg_catalog.char_length(COALESCE(p_input->>'description', '')) NOT BETWEEN 1 AND 20000
       OR COALESCE(p_input->>'severity', 'medium') NOT IN ('low', 'medium', 'high', 'critical')
       OR pg_catalog.jsonb_typeof(v_supplied) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'invalid bug report' USING ERRCODE = '22023';
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(COALESCE(e.item->>'storageKey', e.item->>'storage_key') ORDER BY e.n),
                    ARRAY[]::text[])
    INTO v_keys FROM pg_catalog.jsonb_array_elements(v_supplied) WITH ORDINALITY AS e(item, n);
    IF pg_catalog.cardinality(v_keys) > 5 OR pg_catalog.cardinality(v_keys) <>
        (SELECT pg_catalog.count(DISTINCT k) FROM pg_catalog.unnest(v_keys) AS keys(k)) THEN
        RETURN '{"error":"bug_attachment_duplicate_or_limit"}'::jsonb;
    END IF;
    IF v_alias IS NOT NULL THEN
        IF pg_catalog.cardinality(v_keys) = 0 THEN v_keys := ARRAY[v_alias];
        ELSIF v_alias IS DISTINCT FROM v_keys[1] THEN
            RETURN '{"error":"bug_attachment_screenshot_mismatch"}'::jsonb;
        END IF;
    END IF;
    FOR v_key IN SELECT k FROM pg_catalog.unnest(v_keys) AS keys(k) ORDER BY k LOOP
        SELECT u.* INTO v_upload FROM public.bug_attachment_uploads AS u
        WHERE u.id = yoao_diagnostics_v1._bug_attachment_id(v_key)
          AND yoao_diagnostics_v1._bug_attachment_key(u.owner_id, u.id, u.mime_type) = v_key
        FOR UPDATE;
        IF NOT FOUND THEN RETURN '{"error":"bug_attachment_unknown_owner"}'::jsonb; END IF;
        IF v_upload.owner_id <> p_actor THEN RETURN '{"error":"bug_attachment_foreign_owner"}'::jsonb; END IF;
        IF v_upload.expires_at <= pg_catalog.clock_timestamp() THEN
            RETURN '{"error":"bug_attachment_expired"}'::jsonb;
        END IF;
        IF v_upload.state <> 'ready' THEN RETURN '{"error":"bug_attachment_not_ready"}'::jsonb; END IF;
        IF v_upload.bound_report_id IS NOT NULL THEN
            RETURN '{"error":"bug_attachment_already_bound"}'::jsonb;
        END IF;
        v_actual := yoao_diagnostics_v1._bug_attachment_public(v_upload);
        IF v_supplied <> '[]'::jsonb AND v_actual IS DISTINCT FROM
           yoao_diagnostics_v1._bug_attachment_claim(v_supplied->(pg_catalog.array_position(v_keys, v_key) - 1)) THEN
            RETURN '{"error":"bug_attachment_receipt_mismatch"}'::jsonb;
        END IF;
        v_positions := v_positions || pg_catalog.jsonb_build_object(v_key, v_actual);
        v_uploads := pg_catalog.array_append(v_uploads, v_upload.id);
    END LOOP;
    FOREACH v_key IN ARRAY v_keys LOOP
        v_ordered := v_ordered || pg_catalog.jsonb_build_array(v_positions->v_key);
    END LOOP;
    INSERT INTO public.bug_reports (
        id, display_id, reporter_id, user_role, route, project_id, task_id, title, description,
        severity, status, browser_ua, viewport, recent_api_calls, recent_console_errors, screenshot_url, attachments
    ) VALUES (
        pg_catalog.gen_random_uuid(), 'B-' || pg_catalog.nextval('public.display_seq_bug_reports'::pg_catalog.regclass),
        p_actor, v_role, COALESCE(p_input->>'route', ''),
        (p_input->>'project_id')::uuid, (p_input->>'task_id')::uuid,
        p_input->>'title', p_input->>'description', COALESCE(p_input->>'severity', 'medium'), 'new',
        p_input->>'browser_ua', p_input->>'viewport', p_input->'recent_api_calls',
        p_input->'recent_console_errors', v_keys[1], v_ordered
    ) RETURNING * INTO v_bug;
    UPDATE public.bug_attachment_uploads AS u SET bound_report_id = v_bug.id
    WHERE u.id = ANY(v_uploads);
    RETURN pg_catalog.jsonb_build_object('report', yoao_diagnostics_v1._bug_projection(v_bug));
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.confirm_bug_attachment_binding(p_actor uuid, p_report uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT NOT EXISTS (
        SELECT FROM public.bug_attachment_uploads AS u
        WHERE u.owner_id = p_actor AND u.bound_report_id = p_report
          AND (u.state <> 'ready' OR u.expires_at <= pg_catalog.statement_timestamp())
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.list_bug_reports(
    p_actor uuid, p_mine boolean, p_status text, p_severity text, p_route text,
    p_limit integer, p_offset integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text := yoao_diagnostics_v1._actor_role(p_actor, false);
    v_result jsonb;
BEGIN
    IF v_role IS NULL OR p_mine IS NULL OR (NOT p_mine AND v_role NOT IN ('super_admin', 'project_admin')) THEN
        RETURN '{"error":"authority"}'::jsonb;
    END IF;
    WITH matching AS NOT MATERIALIZED (
        SELECT b AS report, b.id, b.created_at FROM public.bug_reports AS b
        WHERE (NOT p_mine OR b.reporter_id = p_actor)
          AND (NULLIF(p_status, '') IS NULL OR b.status = p_status)
          AND (NULLIF(p_severity, '') IS NULL OR b.severity = p_severity)
          AND (NULLIF(p_route, '') IS NULL OR b.route ILIKE '%' || p_route || '%')
    ), page AS (
        SELECT m.* FROM matching AS m ORDER BY m.created_at DESC, m.id
        LIMIT LEAST(200, GREATEST(p_limit, 1)) OFFSET GREATEST(p_offset, 0)
    )
    SELECT pg_catalog.jsonb_build_object(
        'reports', COALESCE((SELECT pg_catalog.jsonb_agg(
            yoao_diagnostics_v1._bug_projection(p.report) ORDER BY p.created_at DESC, p.id) FROM page AS p), '[]'::jsonb),
        'total', (SELECT pg_catalog.count(*) FROM matching)
    ) INTO v_result;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.read_bug_report(p_actor uuid, p_report uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text := yoao_diagnostics_v1._actor_role(p_actor, false);
    v_bug public.bug_reports;
    v_comments jsonb;
BEGIN
    SELECT b.* INTO v_bug FROM public.bug_reports AS b WHERE b.id = p_report;
    IF NOT FOUND THEN RETURN '{"error":"missing"}'::jsonb; END IF;
    IF v_role IS NULL OR (v_role NOT IN ('super_admin', 'project_admin') AND v_bug.reporter_id <> p_actor) THEN
        RETURN '{"error":"authority"}'::jsonb;
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(
        yoao_diagnostics_v1._comment_projection(c, author.name, author.role)
        ORDER BY c.created_at), '[]'::jsonb)
    INTO v_comments FROM public.bug_comments AS c
    JOIN yoao_identity_v1.account_profiles AS author ON author.id = c.author_id
    WHERE c.bug_report_id = p_report;
    RETURN pg_catalog.jsonb_build_object('report', yoao_diagnostics_v1._bug_projection(v_bug), 'comments', v_comments);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.patch_bug_report(p_actor uuid, p_report uuid, p_patch jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text := yoao_diagnostics_v1._actor_role(p_actor, true);
    v_bug public.bug_reports;
    v_old_status text;
    v_status text := p_patch->>'status';
BEGIN
    SELECT b.* INTO v_bug FROM public.bug_reports AS b WHERE b.id = p_report FOR UPDATE;
    IF NOT FOUND THEN RETURN '{"error":"missing"}'::jsonb; END IF;
    IF v_role IS NULL OR (v_role NOT IN ('super_admin', 'project_admin') AND v_bug.reporter_id <> p_actor) THEN
        RETURN '{"error":"authority"}'::jsonb;
    END IF;
    IF v_role NOT IN ('super_admin', 'project_admin')
       AND (p_patch - ARRAY['title', 'description', 'severity']) <> '{}'::jsonb THEN
        RETURN '{"error":"moderation"}'::jsonb;
    END IF;
    IF pg_catalog.jsonb_typeof(p_patch) IS DISTINCT FROM 'object'
       OR (p_patch - ARRAY['title', 'description', 'severity', 'status', 'duplicate_of_id',
                           'assigned_to_id', 'fixed_in_version', 'resolution']) <> '{}'::jsonb
       OR (p_patch ? 'title' AND pg_catalog.char_length(COALESCE(p_patch->>'title', '')) NOT BETWEEN 1 AND 500)
       OR (p_patch ? 'description' AND pg_catalog.char_length(COALESCE(p_patch->>'description', '')) NOT BETWEEN 1 AND 20000)
       OR (p_patch ? 'severity' AND COALESCE(p_patch->>'severity', '') NOT IN ('low', 'medium', 'high', 'critical'))
       OR (v_status IS NOT NULL AND v_status NOT IN ('new', 'triaged', 'in_progress', 'fixed', 'wont_fix', 'duplicate')) THEN
        RAISE EXCEPTION 'invalid bug report patch' USING ERRCODE = '22023';
    END IF;
    v_old_status := v_bug.status;
    IF (p_patch - 'status') <> '{}'::jsonb OR (v_status IS NOT NULL AND v_status <> v_old_status) THEN
        UPDATE public.bug_reports AS b SET
            title = COALESCE(p_patch->>'title', b.title),
            description = COALESCE(p_patch->>'description', b.description),
            severity = COALESCE(p_patch->>'severity', b.severity),
            status = COALESCE(v_status, b.status),
            triaged_at = CASE WHEN v_status = 'triaged' AND v_status <> v_old_status
                THEN pg_catalog.clock_timestamp() ELSE b.triaged_at END,
            fixed_at = CASE WHEN v_status = 'fixed' AND v_status <> v_old_status
                THEN pg_catalog.clock_timestamp() ELSE b.fixed_at END,
            duplicate_of_id = CASE WHEN p_patch ? 'duplicate_of_id' THEN (p_patch->>'duplicate_of_id')::uuid ELSE b.duplicate_of_id END,
            assigned_to_id = CASE WHEN p_patch ? 'assigned_to_id' THEN (p_patch->>'assigned_to_id')::uuid ELSE b.assigned_to_id END,
            fixed_in_version = CASE WHEN p_patch ? 'fixed_in_version' THEN p_patch->>'fixed_in_version' ELSE b.fixed_in_version END,
            resolution = CASE WHEN p_patch ? 'resolution' THEN p_patch->>'resolution' ELSE b.resolution END
        WHERE b.id = p_report RETURNING b.* INTO v_bug;
    END IF;
    RETURN pg_catalog.jsonb_build_object('report', yoao_diagnostics_v1._bug_projection(v_bug), 'old_status', v_old_status);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._comment_context(p_bug public.bug_reports)
RETURNS jsonb
LANGUAGE sql STABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_bug.id, 'reporter_id', p_bug.reporter_id, 'display_id', p_bug.display_id,
        'assigned_to_id', p_bug.assigned_to_id, 'title', p_bug.title,
        'reopen_count', p_bug.reopen_count, 'status', p_bug.status
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.prepare_bug_comment(p_actor uuid, p_report uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text := yoao_diagnostics_v1._actor_role(p_actor, true);
    v_bug public.bug_reports;
BEGIN
    SELECT b.* INTO v_bug FROM public.bug_reports AS b WHERE b.id = p_report FOR UPDATE;
    IF NOT FOUND THEN RETURN '{"error":"missing"}'::jsonb; END IF;
    IF v_role IS NULL OR (v_role NOT IN ('super_admin', 'project_admin') AND v_bug.reporter_id <> p_actor) THEN
        RETURN '{"error":"authority"}'::jsonb;
    END IF;
    RETURN yoao_diagnostics_v1._comment_context(v_bug);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.add_bug_comment(p_actor uuid, p_report uuid, p_body text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text := yoao_diagnostics_v1._actor_role(p_actor, true);
    v_bug public.bug_reports;
    v_comment public.bug_comments;
    v_name text;
    v_reopened boolean;
BEGIN
    SELECT b.* INTO v_bug FROM public.bug_reports AS b WHERE b.id = p_report FOR UPDATE;
    IF NOT FOUND THEN RETURN '{"error":"missing"}'::jsonb; END IF;
    IF v_role IS NULL OR (v_role NOT IN ('super_admin', 'project_admin') AND v_bug.reporter_id <> p_actor) THEN
        RETURN '{"error":"authority"}'::jsonb;
    END IF;
    IF pg_catalog.char_length(COALESCE(p_body, '')) NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION 'invalid bug comment' USING ERRCODE = '22023';
    END IF;
    v_reopened := p_actor = v_bug.reporter_id AND v_bug.status IN ('fixed', 'wont_fix', 'duplicate');
    IF v_reopened THEN
        UPDATE public.bug_reports AS b
        SET status = 'triaged', reopen_count = COALESCE(b.reopen_count, 0) + 1,
            last_reopened_at = pg_catalog.clock_timestamp(), triaged_at = pg_catalog.clock_timestamp()
        WHERE b.id = p_report RETURNING b.* INTO v_bug;
    END IF;
    INSERT INTO public.bug_comments (id, bug_report_id, author_id, body)
    VALUES (pg_catalog.gen_random_uuid(), p_report, p_actor, p_body) RETURNING * INTO v_comment;
    SELECT a.name INTO v_name FROM yoao_identity_v1.account_profiles AS a WHERE a.id = p_actor;
    RETURN pg_catalog.jsonb_build_object(
        'report', yoao_diagnostics_v1._comment_context(v_bug), 'reopened', v_reopened,
        'comment', yoao_diagnostics_v1._comment_projection(v_comment, v_name, v_role)
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.bug_cluster_candidates(p_actor uuid, p_report uuid)
RETURNS TABLE(id uuid, title text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT candidate.id, candidate.title::text FROM public.bug_reports AS anchor
    JOIN public.bug_reports AS candidate ON candidate.route = anchor.route AND candidate.id <> anchor.id
    WHERE anchor.id = p_report AND candidate.status IN ('new', 'triaged', 'in_progress')
      AND EXISTS (SELECT FROM yoao_identity_v1.read_actor_facts(p_actor) AS a
                  WHERE a.is_active AND a.platform_role IN ('super_admin', 'project_admin'));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.delete_bug_report(p_actor uuid, p_report uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text := yoao_diagnostics_v1._actor_role(p_actor, true);
    v_bug public.bug_reports;
    v_retired uuid[];
BEGIN
    SELECT b.* INTO v_bug FROM public.bug_reports AS b WHERE b.id = p_report FOR UPDATE;
    IF NOT FOUND THEN RETURN '{"error":"missing"}'::jsonb; END IF;
    IF v_role IS NULL OR (v_role NOT IN ('super_admin', 'project_admin') AND v_bug.reporter_id <> p_actor) THEN
        RETURN '{"error":"authority"}'::jsonb;
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(locked.id ORDER BY locked.id), ARRAY[]::uuid[]) INTO v_retired
    FROM (
        SELECT u.id FROM public.bug_attachment_uploads AS u
        WHERE u.bound_report_id = p_report AND u.owner_id = v_bug.reporter_id
          AND u.state IN ('ready', 'retired')
        ORDER BY u.id FOR UPDATE
    ) AS locked;
    UPDATE public.bug_attachment_uploads AS u
    SET state = 'retired', retired_at = pg_catalog.clock_timestamp()
    WHERE u.id = ANY(v_retired) AND u.state = 'ready';
    DELETE FROM public.bug_comments AS c WHERE c.bug_report_id = p_report;
    DELETE FROM public.bug_reports AS b WHERE b.id = p_report;
    RETURN pg_catalog.jsonb_build_object('id', p_report, 'display_id', v_bug.display_id, 'retired', v_retired);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.detach_orphan_bug_tasks(p_project uuid, p_tasks uuid[])
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.bug_reports AS b SET task_id = NULL
    FROM yoao_labeling_v1.task_rows AS t
    WHERE b.task_id = t.id AND t.id = ANY(p_tasks) AND t.project_id = p_project
      AND NOT EXISTS (
          SELECT FROM yoao_assets_v1.project_dataset_item_sources AS s
          WHERE s.project_id = t.project_id AND s.item_id = t.dataset_item_id
      );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.reserve_bug_attachment(
    p_actor uuid, p_id uuid, p_sha256 text, p_bytes integer, p_mime text, p_file text,
    p_created timestamptz, p_expires timestamptz, p_ref jsonb
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF yoao_diagnostics_v1._actor_role(p_actor, true) IS NULL THEN RETURN false; END IF;
    INSERT INTO public.bug_attachment_uploads (
        id, owner_id, sha256, size_bytes, mime_type, file_name, created_at, expires_at, storage_ref, state
    ) VALUES (p_id, p_actor, p_sha256, p_bytes, p_mime, p_file, p_created, p_expires, p_ref, 'pending');
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.lock_pending_bug_attachment(p_actor uuid, p_id uuid)
RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT yoao_diagnostics_v1._bug_attachment_receipt(u)
    FROM public.bug_attachment_uploads AS u
    WHERE u.id = p_id AND u.owner_id = p_actor AND u.state = 'pending'
    FOR UPDATE;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.record_bug_attachment_version(
    p_actor uuid, p_id uuid, p_ref jsonb, p_version text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_upload public.bug_attachment_uploads;
BEGIN
    IF p_version IS NULL THEN RETURN NULL; END IF;
    UPDATE public.bug_attachment_uploads AS u SET object_version_id = p_version
    WHERE u.id = p_id AND u.owner_id = p_actor AND u.state = 'pending'
      AND u.storage_ref = p_ref AND u.object_version_id IS NULL
    RETURNING u.* INTO v_upload;
    RETURN yoao_diagnostics_v1._bug_attachment_receipt(v_upload);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.publish_bug_attachment(
    p_actor uuid, p_id uuid, p_ref jsonb, p_version text
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF yoao_diagnostics_v1._actor_role(p_actor, true) IS NULL THEN RETURN false; END IF;
    UPDATE public.bug_attachment_uploads AS u SET state = 'ready'
    WHERE u.id = p_id AND u.owner_id = p_actor AND u.state = 'pending'
      AND u.storage_ref = p_ref AND u.object_version_id IS NOT DISTINCT FROM p_version
      AND u.expires_at > pg_catalog.clock_timestamp();
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.lock_report_attachment(p_actor uuid, p_report uuid, p_key text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_role text := yoao_diagnostics_v1._actor_role(p_actor, true);
    v_bug public.bug_reports;
    v_upload public.bug_attachment_uploads;
    v_item jsonb;
    v_expected jsonb;
BEGIN
    SELECT b.* INTO v_bug FROM public.bug_reports AS b WHERE b.id = p_report FOR SHARE;
    IF NOT FOUND THEN RETURN '{"error":"bug_report_missing"}'::jsonb; END IF;
    IF v_role IS NULL OR (v_role NOT IN ('super_admin', 'project_admin') AND v_bug.reporter_id <> p_actor) THEN
        RETURN '{"error":"bug_attachment_access_denied"}'::jsonb;
    END IF;
    IF v_bug.screenshot_url IS DISTINCT FROM p_key AND NOT EXISTS (
        SELECT FROM pg_catalog.jsonb_array_elements(v_bug.attachments) AS e(item)
        WHERE COALESCE(e.item->>'storageKey', e.item->>'storage_key') = p_key
    ) THEN RETURN '{"error":"bug_attachment_unregistered_key"}'::jsonb; END IF;
    SELECT u.* INTO v_upload FROM public.bug_attachment_uploads AS u
    WHERE u.id = yoao_diagnostics_v1._bug_attachment_id(p_key)
      AND yoao_diagnostics_v1._bug_attachment_key(u.owner_id, u.id, u.mime_type) = p_key
    FOR UPDATE;
    IF NOT FOUND THEN RETURN '{"error":"bug_attachment_unknown_owner"}'::jsonb; END IF;
    IF v_upload.expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN '{"error":"bug_attachment_expired"}'::jsonb;
    END IF;
    IF v_upload.state <> 'ready' THEN RETURN '{"error":"bug_attachment_not_ready"}'::jsonb; END IF;
    IF v_upload.bound_report_id IS DISTINCT FROM p_report OR v_upload.owner_id <> v_bug.reporter_id THEN
        RETURN '{"error":"bug_attachment_binding_mismatch"}'::jsonb;
    END IF;
    v_expected := yoao_diagnostics_v1._bug_attachment_public(v_upload);
    FOR v_item IN
        SELECT e.item FROM pg_catalog.jsonb_array_elements(v_bug.attachments) AS e(item)
        WHERE COALESCE(e.item->>'storageKey', e.item->>'storage_key') = p_key
    LOOP
        IF yoao_diagnostics_v1._bug_attachment_claim(v_item) IS DISTINCT FROM v_expected THEN
            RETURN '{"error":"bug_attachment_receipt_mismatch"}'::jsonb;
        END IF;
    END LOOP;
    RETURN pg_catalog.jsonb_build_object('receipt', yoao_diagnostics_v1._bug_attachment_receipt(v_upload));
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1._bug_attachment_cleanup_blocked(p_upload public.bug_attachment_uploads)
RETURNS boolean
LANGUAGE sql VOLATILE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.storage_operations AS op
        WHERE op.purpose = 'bug_attachment_read_' || pg_catalog.replace(p_upload.id::text, '-', '')
    ) OR (
        p_upload.expires_at > pg_catalog.clock_timestamp() AND EXISTS (
            SELECT FROM public.bug_reports AS b WHERE b.id = p_upload.bound_report_id
        )
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.bug_attachment_cleanup_candidates(p_limit integer)
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT u.id FROM public.bug_attachment_uploads AS u
    WHERE u.state = 'retired' OR (u.state = 'ready' AND u.expires_at <= pg_catalog.statement_timestamp())
    ORDER BY u.last_attempted_at NULLS FIRST, u.created_at, u.id
    LIMIT LEAST(100, GREATEST(1, p_limit));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.prepare_bug_attachment_cleanup(p_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_upload public.bug_attachment_uploads;
BEGIN
    SELECT u.* INTO v_upload FROM public.bug_attachment_uploads AS u
    WHERE u.id = p_id AND (u.state = 'retired' OR
        (u.state = 'ready' AND u.expires_at <= pg_catalog.clock_timestamp()))
    FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN RETURN NULL; END IF;
    UPDATE public.bug_attachment_uploads AS u SET last_attempted_at = pg_catalog.clock_timestamp()
    WHERE u.id = p_id;
    IF yoao_diagnostics_v1._bug_attachment_cleanup_blocked(v_upload) THEN RETURN NULL; END IF;
    IF v_upload.state = 'ready' THEN
        UPDATE public.bug_attachment_uploads AS u SET state = 'retired', retired_at = pg_catalog.clock_timestamp()
        WHERE u.id = p_id;
    END IF;
    RETURN yoao_diagnostics_v1._bug_attachment_receipt(v_upload);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.lock_retired_bug_attachment(p_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_upload public.bug_attachment_uploads;
BEGIN
    SELECT u.* INTO v_upload FROM public.bug_attachment_uploads AS u
    WHERE u.id = p_id AND u.state = 'retired' FOR UPDATE;
    IF NOT FOUND OR yoao_diagnostics_v1._bug_attachment_cleanup_blocked(v_upload) THEN RETURN NULL; END IF;
    RETURN yoao_diagnostics_v1._bug_attachment_receipt(v_upload);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.complete_bug_attachment_cleanup(p_id uuid, p_ref jsonb, p_version text)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_upload public.bug_attachment_uploads;
BEGIN
    SELECT u.* INTO v_upload FROM public.bug_attachment_uploads AS u
    WHERE u.id = p_id AND u.state = 'retired' AND u.storage_ref = p_ref
      AND u.object_version_id IS NOT DISTINCT FROM p_version FOR UPDATE;
    IF NOT FOUND OR yoao_diagnostics_v1._bug_attachment_cleanup_blocked(v_upload) THEN RETURN false; END IF;
    DELETE FROM public.bug_attachment_uploads AS u WHERE u.id = p_id;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.bug_attachment_history(p_actor uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'pending', pg_catalog.count(*) FILTER (WHERE u.state = 'pending'),
        'ready', pg_catalog.count(*) FILTER (WHERE u.state = 'ready'),
        'retired', pg_catalog.count(*) FILTER (WHERE u.state = 'retired')
    ) FROM public.bug_attachment_uploads AS u
    HAVING EXISTS (
        SELECT FROM yoao_identity_v1.read_actor_facts(p_actor) AS a
        WHERE a.is_active AND a.platform_role = 'super_admin'
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.bug_attachment_location_referenced(p_location uuid)
RETURNS boolean
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.bug_attachment_uploads AS u WHERE u.storage_ref->>'location_id' = p_location::text
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.bug_attachment_maintenance_facts()
RETURNS TABLE(id uuid, owner_id uuid, bound_report_id uuid, state text, storage_ref jsonb, object_version_id text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT u.id, u.owner_id, u.bound_report_id, u.state::text, u.storage_ref, u.object_version_id::text
    FROM public.bug_attachment_uploads AS u;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_diagnostics_v1._actor_role(uuid, boolean),
    yoao_diagnostics_v1._bug_projection(public.bug_reports),
    yoao_diagnostics_v1._comment_projection(public.bug_comments, text, text),
    yoao_diagnostics_v1._bug_attachment_key(uuid, uuid, text),
    yoao_diagnostics_v1._bug_attachment_public(public.bug_attachment_uploads),
    yoao_diagnostics_v1._bug_attachment_claim(jsonb),
    yoao_diagnostics_v1._bug_attachment_id(text),
    yoao_diagnostics_v1._bug_attachment_receipt(public.bug_attachment_uploads),
    yoao_diagnostics_v1._comment_context(public.bug_reports),
    yoao_diagnostics_v1._bug_attachment_cleanup_blocked(public.bug_attachment_uploads),
    yoao_diagnostics_v1.create_bug_report(uuid, jsonb),
    yoao_diagnostics_v1.confirm_bug_attachment_binding(uuid, uuid),
    yoao_diagnostics_v1.list_bug_reports(uuid, boolean, text, text, text, integer, integer),
    yoao_diagnostics_v1.read_bug_report(uuid, uuid),
    yoao_diagnostics_v1.patch_bug_report(uuid, uuid, jsonb),
    yoao_diagnostics_v1.prepare_bug_comment(uuid, uuid),
    yoao_diagnostics_v1.add_bug_comment(uuid, uuid, text),
    yoao_diagnostics_v1.bug_cluster_candidates(uuid, uuid),
    yoao_diagnostics_v1.delete_bug_report(uuid, uuid),
    yoao_diagnostics_v1.detach_orphan_bug_tasks(uuid, uuid[]),
    yoao_diagnostics_v1.reserve_bug_attachment(uuid, uuid, text, integer, text, text, timestamptz, timestamptz, jsonb),
    yoao_diagnostics_v1.lock_pending_bug_attachment(uuid, uuid),
    yoao_diagnostics_v1.record_bug_attachment_version(uuid, uuid, jsonb, text),
    yoao_diagnostics_v1.publish_bug_attachment(uuid, uuid, jsonb, text),
    yoao_diagnostics_v1.lock_report_attachment(uuid, uuid, text),
    yoao_diagnostics_v1.bug_attachment_cleanup_candidates(integer),
    yoao_diagnostics_v1.prepare_bug_attachment_cleanup(uuid),
    yoao_diagnostics_v1.lock_retired_bug_attachment(uuid),
    yoao_diagnostics_v1.complete_bug_attachment_cleanup(uuid, jsonb, text),
    yoao_diagnostics_v1.bug_attachment_history(uuid),
    yoao_diagnostics_v1.bug_attachment_location_referenced(uuid),
    yoao_diagnostics_v1.bug_attachment_maintenance_facts()
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.bug_report_markdown_comments(p_actor uuid, p_reports uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT yoao_diagnostics_v1._comment_projection(c, '', '')
    FROM public.bug_comments AS c
    WHERE pg_catalog.cardinality(p_reports) BETWEEN 1 AND 50 AND c.bug_report_id = ANY(p_reports)
      AND EXISTS (SELECT FROM yoao_identity_v1.read_actor_facts(p_actor) AS a
                  WHERE a.is_active AND a.platform_role IN ('super_admin', 'project_admin'))
    ORDER BY c.bug_report_id, c.created_at;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_diagnostics_v1.bug_report_markdown_comments(uuid, uuid[]) FROM PUBLIC;
