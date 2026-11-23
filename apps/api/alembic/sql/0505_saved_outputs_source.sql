CREATE TABLE yoao_assets_v1.saved_output_admissions (
    operation_id uuid PRIMARY KEY REFERENCES public.storage_operations(id) ON DELETE CASCADE,
    family text NOT NULL CHECK (family IN ('archive', 'bug')),
    owner_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    report_id uuid,
    logical_key text,
    CHECK ((family = 'bug') = (report_id IS NOT NULL AND logical_key IS NOT NULL))
);
-- aap-statement-break
CREATE TABLE yoao_assets_v1.saved_output_writes (
    family text NOT NULL CHECK (family IN ('avatar', 'bug', 'archive', 'zip', 'member')),
    subject_id text NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 73),
    operation_id uuid NOT NULL UNIQUE,
    facts jsonb NOT NULL,
    receipt jsonb,
    PRIMARY KEY (family, subject_id)
);
-- aap-statement-break
REVOKE ALL ON yoao_assets_v1.saved_output_admissions, yoao_assets_v1.saved_output_writes FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._saved_output_gate(p_operation uuid, p_location uuid, p_purpose text)
RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT FROM public.storage_operations AS operation
        JOIN public.storage_maintenance_gates AS gate USING (location_id)
        WHERE operation.id = p_operation AND operation.location_id = p_location
          AND operation.purpose = p_purpose AND gate.state = 'active' AND gate.revision = operation.revision
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._saved_output_origin(p_location uuid, p_purpose text)
RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'location_id', location.id, 'config_revision', location.config_revision,
        'connection_revision', connection.revision, 'bucket', connection.config->>'bucket',
        'endpoint_url', CASE WHEN strpos(connection.config->>'endpoint', '://') > 0
                             THEN rtrim(connection.config->>'endpoint', '/')
                             ELSE (CASE WHEN connection.config->'use_ssl' = 'true'::jsonb
                                        THEN 'https://' ELSE 'http://' END) || (connection.config->>'endpoint') END,
        'base', regexp_replace(coalesce(connection.config->>'base_prefix', ''), '/$', '')
    )
    FROM public.storage_locations location
    JOIN public.storage_connections connection ON connection.id = location.connection_id
    WHERE location.id = p_location AND location.id <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
      AND location.kind = 's3' AND location.enabled AND connection.kind = 's3'
      AND connection.scope = 'global' AND connection.project_id IS NULL
      AND p_purpose IN ('avatars','bug-reports','audit-archive','export');
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_output_origin(p_ref jsonb, p_purpose text, p_suffix text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_origin jsonb;
    v_address text := coalesce(p_ref->>'object_key', p_ref->>'key_prefix');
    v_base text;
BEGIN
    IF p_ref->>'purpose' IS DISTINCT FROM p_purpose OR p_ref->'schema' IS DISTINCT FROM '2'::jsonb
       OR coalesce(length(p_suffix), 0) = 0 OR octet_length(v_address) > 1024 THEN RETURN NULL; END IF;
    v_origin := yoao_assets_v1._saved_output_origin((p_ref->>'location_id')::uuid, p_purpose);
    IF v_origin IS NULL OR v_origin->>'bucket' IS DISTINCT FROM p_ref->>'bucket'
       OR v_origin->>'endpoint_url' IS DISTINCT FROM p_ref->>'endpoint_url' THEN RETURN NULL; END IF;
    v_base := CASE WHEN v_origin->>'base' = '' THEN '' ELSE (v_origin->>'base') || '/' END;
    IF v_address = v_base || '_aap_managed/' || p_purpose || '/' || p_suffix
       OR (p_purpose = 'export' AND v_address = v_base || '_yoao_managed/export/' || p_suffix) THEN
        RETURN v_origin - 'base' - 'endpoint_url';
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.saved_archive_output_facts(p_id uuid, p_action text, p_actor uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object('id', archive.id, 'year', archive.year, 'month', archive.month,
        'storage_ref', archive.storage_ref, 'sha256', archive.sha256, 'size_bytes', archive.size_bytes)
    FROM public.audit_archive_outputs archive
    WHERE archive.id = p_id AND CASE p_action
        WHEN 'write' THEN archive.state = 'pending' AND EXISTS (
            SELECT FROM yoao_jobs_v1.read_job(archive.job_id) job
            WHERE job.kind = 'audit_archive' AND job.status = 'running'
              AND coalesce(job.payload->'cancel_requested', 'false'::jsonb) IN ('false'::jsonb, 'null'::jsonb)
        )
        WHEN 'read' THEN archive.state = 'ready' AND archive.published_at IS NOT NULL AND EXISTS (
            SELECT FROM yoao_identity_v1.read_actor_facts(p_actor) actor
            WHERE actor.is_active AND actor.role = 'super_admin'
        )
        ELSE false END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.admit_saved_output_read(
    p_operation uuid, p_family text, p_id uuid, p_actor uuid, p_report uuid, p_key text
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_facts jsonb;
    v_label text;
BEGIN
    IF p_family = 'archive' AND p_report IS NULL AND p_key IS NULL THEN
        v_facts := yoao_audit_v1.saved_archive_output_facts(p_id, 'read', p_actor);
        v_label := 'audit_archive_read_';
    ELSIF p_family = 'bug' AND p_report IS NOT NULL AND length(p_key) <= 128 THEN
        v_facts := yoao_diagnostics_v1.lock_report_attachment(p_actor, p_report, p_key)->'receipt';
        v_label := 'bug_attachment_read_';
    ELSE RETURN false;
    END IF;
    IF v_facts->>'id' IS DISTINCT FROM p_id::text OR NOT yoao_assets_v1._saved_output_gate(
        p_operation, (v_facts->'storage_ref'->>'location_id')::uuid, v_label || replace(p_id::text, '-', '')
    ) THEN RETURN false; END IF;
    INSERT INTO yoao_assets_v1.saved_output_admissions (operation_id, family, owner_id, actor_id, report_id, logical_key)
    VALUES (p_operation, p_family, p_id, p_actor, p_report, p_key);
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_avatar_subject(p_token text, p_operation uuid, p_action text)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object('token', upload.token, 'storage_ref', upload.storage_ref,
        'sha256', upload.sha256, 'size_bytes', upload.size_bytes, 'version_id', upload.object_version_id)
    FROM public.avatar_uploads upload
    WHERE upload.token = p_token AND yoao_assets_v1._saved_output_gate(
        p_operation, (upload.storage_ref->>'location_id')::uuid,
        (CASE p_action WHEN 'write' THEN 'avatar_upload_' WHEN 'read' THEN 'avatar_read_'
                       WHEN 'delete' THEN 'avatar_retirement_' END) || p_token
    ) AND CASE p_action
        WHEN 'write' THEN upload.state = 'pending' AND upload.object_version_id IS NULL
          AND EXISTS (SELECT FROM yoao_identity_v1.read_actor_facts(upload.owner_id) actor WHERE actor.is_active)
        WHEN 'read' THEN upload.state = 'ready'
        WHEN 'delete' THEN upload.state = 'retired' AND NOT EXISTS (
            SELECT FROM yoao_identity_v1.account_profiles profile WHERE profile.avatar_ref = 'upload:' || p_token
        ) AND NOT EXISTS (
            SELECT FROM public.storage_operations operation WHERE operation.purpose = 'avatar_read_' || p_token
        )
        ELSE false END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_bug_subject(p_id uuid, p_operation uuid, p_action text, p_actor uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_facts jsonb;
    v_admission yoao_assets_v1.saved_output_admissions;
BEGIN
    IF p_action = 'write' THEN
        v_facts := yoao_diagnostics_v1.lock_pending_bug_attachment(p_actor, p_id);
        IF (v_facts->>'expires_at')::timestamptz <= clock_timestamp()
           OR v_facts->>'object_version_id' IS NOT NULL
           OR NOT EXISTS (SELECT FROM yoao_identity_v1.read_actor_facts(p_actor) actor WHERE actor.is_active)
        THEN RETURN NULL; END IF;
    ELSIF p_action = 'read' THEN
        SELECT * INTO v_admission FROM yoao_assets_v1.saved_output_admissions
        WHERE operation_id = p_operation AND family = 'bug' AND owner_id = p_id;
        IF NOT FOUND THEN RETURN NULL; END IF;
        v_facts := yoao_diagnostics_v1.lock_report_attachment(
            v_admission.actor_id, v_admission.report_id, v_admission.logical_key
        )->'receipt';
    ELSIF p_action = 'delete' THEN
        v_facts := yoao_diagnostics_v1.lock_retired_bug_attachment(p_id);
    ELSE RETURN NULL;
    END IF;
    IF v_facts->>'id' = p_id::text AND yoao_assets_v1._saved_output_gate(
        p_operation, (v_facts->'storage_ref'->>'location_id')::uuid,
        'bug_attachment_' || p_action || '_' || replace(p_id::text, '-', '')
    ) THEN RETURN v_facts || jsonb_build_object('version_id', v_facts->>'object_version_id'); END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_archive_subject(p_id uuid, p_operation uuid, p_action text)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT facts.value || CASE WHEN p_action = 'read' THEN coalesce(write.receipt, '{}'::jsonb) ELSE '{}'::jsonb END
    FROM yoao_audit_v1.saved_archive_output_facts(p_id, p_action, (
        SELECT actor_id FROM yoao_assets_v1.saved_output_admissions
        WHERE operation_id = p_operation AND family = 'archive' AND owner_id = p_id
    )) facts(value)
    LEFT JOIN yoao_assets_v1.saved_output_writes write ON write.family = 'archive' AND write.subject_id = p_id::text
        AND write.facts->'storage_ref' = facts.value->'storage_ref'
    WHERE yoao_assets_v1._saved_output_gate(p_operation, (facts.value->'storage_ref'->>'location_id')::uuid,
        'audit_archive_' || p_action || '_' || replace(p_id::text, '-', ''));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.begin_saved_output_write(p_operation uuid, p_family text, p_subject text, p_facts jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    INSERT INTO yoao_assets_v1.saved_output_writes(operation_id, family, subject_id, facts)
    SELECT operation.id, p_family, p_subject, p_facts
    FROM public.storage_operations operation
    WHERE operation.id = p_operation AND yoao_assets_v1._saved_output_gate(
        operation.id, (p_facts->'storage_ref'->>'location_id')::uuid, operation.purpose
    ) ON CONFLICT DO NOTHING;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.finish_saved_output_write(p_operation uuid, p_family text, p_subject text, p_receipt jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    UPDATE yoao_assets_v1.saved_output_writes write SET receipt = p_receipt
    FROM public.storage_operations operation
    WHERE write.operation_id = p_operation AND write.family = p_family AND write.subject_id = p_subject
      AND write.receipt IS NULL AND operation.id = p_operation
      AND jsonb_typeof(p_receipt) = 'object' AND length(p_receipt->>'etag') BETWEEN 1 AND 1024
      AND (p_receipt->'version_id' = 'null'::jsonb OR length(p_receipt->>'version_id') BETWEEN 1 AND 1024)
      AND yoao_assets_v1._saved_output_gate(
          operation.id, (write.facts->'storage_ref'->>'location_id')::uuid, operation.purpose
      );
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_output_write_status(p_operation uuid, p_family text, p_subject text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT write.receipt FROM yoao_assets_v1.saved_output_writes write
    JOIN public.storage_operations operation ON operation.id = write.operation_id
    WHERE write.operation_id = p_operation AND write.family = p_family AND write.subject_id = p_subject
      AND yoao_assets_v1._saved_output_gate(
          operation.id, (write.facts->'storage_ref'->>'location_id')::uuid, operation.purpose
      );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._guard_saved_output_write()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_row jsonb := to_jsonb(NEW);
    v_kind text := CASE TG_TABLE_NAME WHEN 'avatar_uploads' THEN 'avatar'
                       WHEN 'bug_attachment_uploads' THEN 'bug' ELSE 'archive' END;
    v_version text := v_row->>'object_version_id';
BEGIN
    IF NEW.storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
       OR pg_has_role(session_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'USAGE')
       OR (NEW.state = 'pending' AND v_version IS NULL)
       OR (OLD.state <> 'pending' AND v_version IS NOT DISTINCT FROM to_jsonb(OLD)->>'object_version_id')
    THEN RETURN NEW; END IF;
    IF NOT EXISTS (
        SELECT FROM yoao_assets_v1.saved_output_writes write
        WHERE write.family = v_kind AND write.subject_id = coalesce(v_row->>'token', v_row->>'id')
          AND write.facts->'storage_ref' = NEW.storage_ref AND write.facts->>'sha256' = NEW.sha256
          AND write.facts->'size_bytes' = to_jsonb(NEW.size_bytes) AND write.receipt IS NOT NULL
          AND (v_kind = 'archive' OR write.receipt->>'version_id' IS NOT DISTINCT FROM v_version)
    ) THEN RAISE EXCEPTION 'registered upload has no matching Source write receipt' USING ERRCODE = '23514'; END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
CREATE TRIGGER trg_avatar_source_write BEFORE UPDATE OF state, object_version_id ON public.avatar_uploads
FOR EACH ROW EXECUTE FUNCTION yoao_assets_v1._guard_saved_output_write();
-- aap-statement-break
CREATE TRIGGER trg_bug_attachment_source_write BEFORE UPDATE OF state, object_version_id ON public.bug_attachment_uploads
FOR EACH ROW EXECUTE FUNCTION yoao_assets_v1._guard_saved_output_write();
-- aap-statement-break
CREATE TRIGGER trg_audit_archive_source_write BEFORE UPDATE OF state ON public.audit_archive_outputs
FOR EACH ROW EXECUTE FUNCTION yoao_assets_v1._guard_saved_output_write();
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.saved_archive_output_facts(uuid,text,uuid),
    yoao_assets_v1._saved_output_gate(uuid,uuid,text), yoao_assets_v1._saved_output_origin(uuid,text),
    yoao_assets_v1.saved_output_origin(jsonb,text,text), yoao_assets_v1.admit_saved_output_read(uuid,text,uuid,uuid,uuid,text),
    yoao_assets_v1.saved_avatar_subject(text,uuid,text), yoao_assets_v1.saved_bug_subject(uuid,uuid,text,uuid),
    yoao_assets_v1.saved_archive_subject(uuid,uuid,text), yoao_assets_v1.begin_saved_output_write(uuid,text,text,jsonb),
    yoao_assets_v1.finish_saved_output_write(uuid,text,text,jsonb), yoao_assets_v1.saved_output_write_status(uuid,text,text),
    yoao_assets_v1._guard_saved_output_write() FROM PUBLIC;
