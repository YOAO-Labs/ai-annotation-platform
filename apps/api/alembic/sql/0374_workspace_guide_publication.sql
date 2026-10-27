-- Publish authenticated Guide uploads through the Workspace owner.
--
-- Main's guide feature owns immutable upload facts, storage receipts and
-- byte I/O in the API. The Project list and its revision are Workspace state,
-- so this forward owner command is the only publication writer of
-- `projects.guide_assets`, `projects.guide_assets_revision` and
-- `project_guide_assets`. Project deletion is the separate retirement path:
-- `aap_workspace_v1.delete_project` removes that project's reference rows and
-- `aap_workspace_v1.retire_project_uploads(uuid)` retires its last-reference
-- uploads while removing the Project row. Upload rows are created `pending`
-- by the caller; publication alone may advance them to `ready`, and only an
-- unlink/apply/deletion may retire an unreferenced owner.
--
-- The SECURITY DEFINER owner must be able to SELECT/UPDATE
-- public.projects(guide_assets, guide_assets_revision, annotation_guide,
-- updated_at), SELECT public.project_members, SELECT/UPDATE
-- public.guide_asset_uploads, SELECT/INSERT/DELETE public.project_guide_assets
-- and EXECUTE aap_assets_v1.lock_publication_storage_gate(uuid, bigint). The
-- migration role owns the guide objects, so bo0374 needs no private gate table
-- privilege for Workspace; the Workspace configuration LOGIN additionally
-- needs EXECUTE on
-- `aap_workspace_configuration_v1.guide_assets_step(jsonb, text, jsonb)`.
--
-- The immutable 0269 `configuration_step` bytes stay frozen. This artifact
-- forward-replaces the canonical function with a clean body that keeps every
-- non-guide operation and action unchanged, removes the retired presign guide
-- operations/actions, and removes `guide_assets` from the generic project
-- insert/update column surface. The guide list, revision and references are
-- writable only through `guide_assets_step` above.
--
-- Downgrade restores the original canonical `configuration_step` body from
-- `0269_workspace_guide_assets.sql`, then drops `guide_assets_step` and its
-- five private helpers. The bo0374 grants keep the existing canonical
-- function ownership plus the new `guide_assets_step`; no other schema or
-- retained wrapper is introduced.

CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1._guide_entry_matches(
    p_entry jsonb,
    p_project_id uuid,
    p_upload_id uuid,
    p_file_name text,
    p_mime_type text,
    p_size_bytes integer,
    p_created_at timestamptz
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
DECLARE
    v_uploaded_at timestamptz;
BEGIN
    IF p_entry IS NULL OR p_project_id IS NULL OR p_upload_id IS NULL
       OR p_file_name IS NULL OR p_mime_type IS NULL OR p_size_bytes IS NULL
       OR p_created_at IS NULL THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_entry) IS DISTINCT FROM 'object'
       OR NOT aap_workspace_configuration_v1._has_exact_keys(
           p_entry,
           ARRAY['key', 'original_name', 'content_type', 'size', 'uploaded_at']::text[]
       )
    THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_entry->'key') IS DISTINCT FROM 'string'
       OR p_entry->>'key' IS DISTINCT FROM
           'projects/' || p_project_id::text || '/guide/' || p_upload_id::text
       OR pg_catalog.jsonb_typeof(p_entry->'original_name') IS DISTINCT FROM 'string'
       OR p_entry->>'original_name' IS DISTINCT FROM p_file_name
       OR pg_catalog.jsonb_typeof(p_entry->'content_type') IS DISTINCT FROM 'string'
       OR p_entry->>'content_type' IS DISTINCT FROM p_mime_type
       OR pg_catalog.jsonb_typeof(p_entry->'size') IS DISTINCT FROM 'number'
       OR (p_entry->>'size') COLLATE "C" !~ '^(0|[1-9][0-9]*)$'
       OR (p_entry->>'size')::bigint IS DISTINCT FROM p_size_bytes::bigint
       OR pg_catalog.jsonb_typeof(p_entry->'uploaded_at') IS DISTINCT FROM 'string'
    THEN
        RETURN false;
    END IF;
    BEGIN
        v_uploaded_at := (p_entry->>'uploaded_at')::timestamptz;
    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
        RETURN false;
    END;
    RETURN v_uploaded_at IS NOT DISTINCT FROM p_created_at;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_configuration_v1._guide_entry_matches(
        jsonb, uuid, uuid, text, text, integer, timestamptz
    ) FROM PUBLIC;
-- aap-statement-break

-- Lock and read exactly the Project fields guide publication needs. `wait`
-- bounds ordinary image publication to the same one-second wait the retired
-- service used; list mutations keep the fast NOWAIT conflict response.
CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1._guide_lock_project(
    p_project_id uuid,
    p_mode text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
DECLARE
    v_row jsonb;
    v_prior text;
BEGIN
    IF p_project_id IS NULL OR p_mode IS NULL
       OR p_mode NOT IN ('wait', 'nowait', 'share_nowait') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid guide-assets Project lock request';
    END IF;

    IF p_mode = 'wait' THEN
        v_prior := pg_catalog.current_setting('lock_timeout');
        PERFORM pg_catalog.set_config('lock_timeout', '1s', true);
        BEGIN
            SELECT pg_catalog.jsonb_build_object(
                'id', project.id,
                'owner_id', project.owner_id,
                'organization_id', project.organization_id,
                'annotation_guide', project.annotation_guide,
                'guide_assets', project.guide_assets,
                'guide_assets_revision', project.guide_assets_revision
            )
            INTO v_row
            FROM public.projects AS project
            WHERE project.id = p_project_id
            FOR UPDATE OF project;
        EXCEPTION WHEN lock_not_available OR deadlock_detected
                     OR serialization_failure THEN
            PERFORM pg_catalog.set_config('lock_timeout', v_prior, true);
            RETURN pg_catalog.jsonb_build_object('ok', false, 'code', 'busy');
        END;
        PERFORM pg_catalog.set_config('lock_timeout', v_prior, true);
    ELSIF p_mode = 'nowait' THEN
        BEGIN
            SELECT pg_catalog.jsonb_build_object(
                'id', project.id,
                'owner_id', project.owner_id,
                'organization_id', project.organization_id,
                'annotation_guide', project.annotation_guide,
                'guide_assets', project.guide_assets,
                'guide_assets_revision', project.guide_assets_revision
            )
            INTO v_row
            FROM public.projects AS project
            WHERE project.id = p_project_id
            FOR UPDATE OF project NOWAIT;
        EXCEPTION WHEN lock_not_available OR deadlock_detected
                     OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'code', 'busy');
        END;
    ELSE
        BEGIN
            SELECT pg_catalog.jsonb_build_object(
                'id', project.id,
                'owner_id', project.owner_id,
                'organization_id', project.organization_id,
                'annotation_guide', project.annotation_guide,
                'guide_assets', project.guide_assets,
                'guide_assets_revision', project.guide_assets_revision
            )
            INTO v_row
            FROM public.projects AS project
            WHERE project.id = p_project_id
            FOR SHARE OF project NOWAIT;
        EXCEPTION WHEN lock_not_available OR deadlock_detected
                     OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'code', 'busy');
        END;
    END IF;

    IF v_row IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'code', 'missing');
    END IF;
    RETURN pg_catalog.jsonb_build_object('ok', true, 'row', v_row);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_configuration_v1._guide_lock_project(uuid, text)
    FROM PUBLIC;
-- aap-statement-break

-- Re-authenticate the caller and require a current project manager. The
-- membership row is share-locked exactly like configuration authority.
CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1._guide_authority(
    p_actor_id uuid,
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
DECLARE
    v_actor jsonb;
    v_project jsonb;
    v_access jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'status', 403, 'code', 'guide_asset_authority_revoked'
        );
    END IF;
    BEGIN
        v_actor := aap_workspace_configuration_v1._configuration_actor(
            p_actor_id, true, 'account_busy'
        );
    EXCEPTION WHEN lock_not_available OR deadlock_detected
                 OR serialization_failure THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'status', 409, 'code', 'guide_asset_busy'
        );
    END;
    IF v_actor IS NULL
       OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'status', 403, 'code', 'guide_asset_authority_revoked'
        );
    END IF;

    v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
        p_project_id, 'none'
    );
    IF v_project IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'status', 404, 'code', 'project_missing'
        );
    END IF;
    BEGIN
        v_access := aap_workspace_configuration_v1._configuration_project_access(
            v_actor, v_project, true
        );
    EXCEPTION WHEN lock_not_available OR deadlock_detected
                 OR serialization_failure THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'status', 409, 'code', 'guide_asset_busy'
        );
    END;
    IF v_access->>'code' = 'project_missing' THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'status', 404, 'code', 'project_missing'
        );
    ELSIF v_access->>'code' IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'status', 403, 'code', 'guide_asset_manager_required'
        );
    ELSIF COALESCE((v_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'status', 403, 'code', 'guide_asset_manager_required'
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object('ok', true);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_configuration_v1._guide_authority(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- Re-acquire the Assets-owned publication admission gate. The Assets owner
-- holds FOR SHARE NOWAIT until this owner transaction commits; Workspace never
-- reads the private gate table. A NULL expected revision asks the Assets owner
-- to lock and report the current active gate (used by cross-project copy).
CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1._guide_storage_gate(
    p_location_id uuid,
    p_expected_revision bigint
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_gate jsonb;
BEGIN
    IF p_location_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'storage_maintenance'
        );
    END IF;
    v_gate := aap_assets_v1.lock_publication_storage_gate(
        p_location_id, p_expected_revision
    );
    IF v_gate IS NULL
       OR pg_catalog.jsonb_typeof(v_gate) IS DISTINCT FROM 'object'
       OR v_gate->>'ok' IS DISTINCT FROM 'true' THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false,
            'code', COALESCE(
                NULLIF(v_gate->>'code', ''), 'storage_maintenance'
            )
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object('ok', true);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_configuration_v1._guide_storage_gate(uuid, bigint)
    FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1._guide_failure(
    p_request jsonb,
    p_action text,
    p_status integer,
    p_code text
)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_code = 'project_missing' THEN
        RETURN aap_workspace_configuration_v1._step_failure(
            p_request, p_action, 404, pg_catalog.to_jsonb('项目不存在'::text)
        );
    END IF;
    RETURN aap_workspace_configuration_v1._step_failure(
        p_request,
        p_action,
        p_status,
        pg_catalog.jsonb_build_object('code', p_code)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_configuration_v1._guide_failure(jsonb, text, integer, text)
    FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1.guide_assets_step(
    p_request jsonb,
    p_action text,
    p_args jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_operation text;
    v_actor_id uuid;
    v_target_id uuid;
    v_selectors jsonb;
    v_uuid_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_authority jsonb;
    v_locked jsonb;
    v_project jsonb;
    v_assets jsonb;
    v_entry jsonb;
    v_entry_ascii text;
    v_key text;
    v_upload public.guide_asset_uploads%ROWTYPE;
    v_receipt jsonb;
    v_storage_ref jsonb;
    v_location_id uuid;
    v_maintenance_revision bigint;
    v_gate jsonb;
    v_expected_revision integer;
    v_revision integer;
    v_owner_ids uuid[];
    v_ref record;
    v_supplied jsonb;
    v_supplied_entry jsonb;
    v_matches boolean;
    v_source_id uuid;
    v_snapshot jsonb;
    v_source_row jsonb;
    v_source_assets jsonb;
    v_current_refs uuid[];
    v_snapshot_refs uuid[];
    v_locations uuid[];
    v_location uuid;
BEGIN
    IF p_request IS NULL OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-configuration/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'actor_id') IS DISTINCT FROM 'string'
       OR (p_request->>'actor_id') !~* v_uuid_pattern
       OR pg_catalog.jsonb_typeof(p_request->'selectors') IS DISTINCT FROM 'object'
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace guide-assets request';
    END IF;
    IF p_action IS NULL OR p_action NOT IN (
        'guide_publish', 'guide_unlink', 'guide_assets_apply', 'guide_assets_copy'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace guide-assets action';
    END IF;
    IF p_args IS NULL OR pg_catalog.jsonb_typeof(p_args) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace guide-assets arguments';
    END IF;

    v_operation := p_request->>'operation';
    v_actor_id := (p_request->>'actor_id')::uuid;
    v_selectors := p_request->'selectors';
    IF pg_catalog.jsonb_typeof(p_request->'target_id') NOT IN ('null', 'string')
       OR (
           pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
           AND (p_request->>'target_id') !~* v_uuid_pattern
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace guide-assets target';
    END IF;
    v_target_id := CASE
        WHEN pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
        THEN (p_request->>'target_id')::uuid
        ELSE NULL
    END;

    IF (p_action = 'guide_publish' AND v_operation IS DISTINCT FROM 'guide_asset_publish')
       OR (p_action = 'guide_unlink' AND v_operation IS DISTINCT FROM 'guide_asset_unlink')
       OR (p_action = 'guide_assets_apply' AND v_operation IS DISTINCT FROM 'update_project')
       OR (p_action = 'guide_assets_copy' AND v_operation IS DISTINCT FROM 'create_project')
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'guide-assets action does not match operation';
    END IF;

    IF p_action IN ('guide_publish', 'guide_unlink') THEN
        IF v_target_id IS NULL OR v_selectors IS DISTINCT FROM
            pg_catalog.jsonb_build_object(
                'source_project_id', NULL,
                'template_id', NULL,
                'ml_backend_source_id', NULL,
                'ml_backend_id', NULL,
                'ml_backend_present', false,
                'scope', NULL,
                'scope_present', false,
                'organization_id', NULL,
                'organization_present', false
            )
        THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid guide-assets selectors';
        END IF;
    ELSIF p_action = 'guide_assets_apply' THEN
        IF v_target_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'guide-assets apply requires a target';
        END IF;
    ELSE
        IF v_target_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'guide-assets copy requires a null target';
        END IF;
    END IF;

    IF p_action = 'guide_publish' THEN
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            p_args, ARRAY['receipt', 'entry_ascii']::text[]
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid guide-assets publish arguments';
        END IF;
    ELSIF p_action = 'guide_unlink' THEN
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            p_args, ARRAY['key']::text[]
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid guide-assets unlink arguments';
        END IF;
    ELSIF p_action = 'guide_assets_apply' THEN
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            p_args, ARRAY['expected_revision', 'guide_assets_ascii']::text[]
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid guide-assets apply arguments';
        END IF;
    ELSE
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            p_args,
            ARRAY['source_project_id', 'target_project_id', 'source_snapshot']::text[]
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid guide-assets copy arguments';
        END IF;
    END IF;

    v_authority := aap_workspace_configuration_v1._guide_authority(
        v_actor_id,
        CASE WHEN p_action = 'guide_assets_copy'
             THEN (p_args->>'target_project_id')::uuid
             ELSE v_target_id
        END
    );
    IF v_authority->>'ok' IS DISTINCT FROM 'true' THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action,
            (v_authority->>'status')::integer,
            v_authority->>'code'
        );
    END IF;

    -- Publish one verified, still-pending upload and append its reference and
    -- list entry in the same transaction.
    IF p_action = 'guide_publish' THEN
        v_receipt := p_args->'receipt';
        IF pg_catalog.jsonb_typeof(v_receipt) IS DISTINCT FROM 'object'
           OR NOT aap_workspace_configuration_v1._has_exact_keys(
               v_receipt,
               ARRAY['upload_id', 'sha256', 'size_bytes',
                     'maintenance_revision', 'storage_ref']::text[]
           )
           OR pg_catalog.jsonb_typeof(v_receipt->'upload_id') IS DISTINCT FROM 'string'
           OR (v_receipt->>'upload_id') !~* v_uuid_pattern
           OR pg_catalog.jsonb_typeof(v_receipt->'sha256') IS DISTINCT FROM 'string'
           OR (v_receipt->>'sha256') COLLATE "C" !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_receipt->'size_bytes') IS DISTINCT FROM 'number'
           OR (v_receipt->>'size_bytes') COLLATE "C" !~ '^[1-9][0-9]{0,7}$'
           OR pg_catalog.jsonb_typeof(v_receipt->'maintenance_revision')
               IS DISTINCT FROM 'number'
           OR (v_receipt->>'maintenance_revision') COLLATE "C"
               !~ '^[1-9][0-9]{0,9}$'
           OR pg_catalog.jsonb_typeof(v_receipt->'storage_ref')
               IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_args->'entry_ascii') IS DISTINCT FROM 'string'
        THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END IF;
        v_entry_ascii := p_args->>'entry_ascii';
        IF pg_catalog.octet_length(pg_catalog.convert_to(v_entry_ascii, 'UTF8'))
               <> pg_catalog.char_length(v_entry_ascii) THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END IF;
        v_storage_ref := v_receipt->'storage_ref';
        IF pg_catalog.jsonb_typeof(v_storage_ref->'location_id')
               IS DISTINCT FROM 'string'
           OR (v_storage_ref->>'location_id') !~* v_uuid_pattern THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END IF;
        v_location_id := (v_storage_ref->>'location_id')::uuid;
        v_maintenance_revision := (v_receipt->>'maintenance_revision')::bigint;
        BEGIN
            v_entry := v_entry_ascii::jsonb;
        EXCEPTION WHEN invalid_text_representation OR untranslatable_character THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END;

        v_locked := aap_workspace_configuration_v1._guide_lock_project(
            v_target_id, 'wait'
        );
        IF v_locked->>'ok' IS DISTINCT FROM 'true' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action,
                CASE WHEN v_locked->>'code' = 'missing' THEN 404 ELSE 409 END,
                CASE WHEN v_locked->>'code' = 'missing'
                     THEN 'project_missing' ELSE 'guide_asset_busy' END
            );
        END IF;
        v_project := v_locked->'row';
        v_assets := COALESCE(v_project->'guide_assets', '[]'::jsonb);
        IF pg_catalog.jsonb_typeof(v_assets) IS DISTINCT FROM 'array' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END IF;

        BEGIN
            SELECT upload.*
            INTO v_upload
            FROM public.guide_asset_uploads AS upload
            WHERE upload.id = (v_receipt->>'upload_id')::uuid
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR deadlock_detected
                     OR serialization_failure THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_busy'
            );
        END;
        IF NOT FOUND THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END IF;

        IF v_upload.project_id IS DISTINCT FROM v_target_id
           OR v_upload.owner_id IS DISTINCT FROM v_actor_id
           OR v_upload.sha256 IS DISTINCT FROM (v_receipt->>'sha256')
           OR v_upload.size_bytes IS DISTINCT FROM (v_receipt->>'size_bytes')::bigint
           OR v_upload.storage_ref IS DISTINCT FROM v_storage_ref
           OR NOT aap_workspace_configuration_v1._guide_entry_matches(
               v_entry, v_target_id, v_upload.id, v_upload.file_name,
               v_upload.mime_type, v_upload.size_bytes, v_upload.created_at
           )
        THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END IF;

        IF v_upload.state = 'ready' THEN
            IF EXISTS (
                SELECT 1 FROM public.project_guide_assets AS reference
                WHERE reference.project_id = v_target_id
                  AND reference.upload_id = v_upload.id
            ) AND EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(v_assets) AS item(value)
                WHERE item.value = v_entry
            ) THEN
                RETURN aap_workspace_configuration_v1._step_success(
                    p_request, p_action,
                    pg_catalog.jsonb_build_object('entry', v_entry)
                );
            END IF;
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        ELSIF v_upload.state IS DISTINCT FROM 'pending' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END IF;

        v_gate := aap_workspace_configuration_v1._guide_storage_gate(
            v_location_id, v_maintenance_revision
        );
        IF v_gate->>'ok' IS DISTINCT FROM 'true' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action,
                CASE WHEN v_gate->>'code' = 'storage_maintenance_busy'
                     THEN 409 ELSE 409 END,
                v_gate->>'code'
            );
        END IF;

        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_assets) AS item(value)
            WHERE item.value->>'key' IS NOT DISTINCT FROM v_entry->>'key'
        ) THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END IF;

        UPDATE public.guide_asset_uploads AS upload
        SET state = 'ready'
        WHERE upload.id = v_upload.id
          AND upload.state = 'pending';
        IF NOT FOUND THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END IF;
        BEGIN
            INSERT INTO public.project_guide_assets (project_id, upload_id)
            VALUES (v_target_id, v_upload.id);
        EXCEPTION WHEN unique_violation OR foreign_key_violation
                     OR check_violation THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END;
        UPDATE public.projects AS project
        SET guide_assets = v_assets || pg_catalog.jsonb_build_array(v_entry),
            updated_at = pg_catalog.clock_timestamp()
        WHERE project.id = v_target_id
        RETURNING project.guide_assets_revision INTO v_revision;
        IF NOT FOUND THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'entry', v_entry, 'guide_assets_revision', v_revision
            )
        );
    END IF;

    -- Remove one displayed key, unlink exactly its current references and
    -- retire only owners that have no remaining project reference.
    IF p_action = 'guide_unlink' THEN
        v_key := p_args->>'key';
        IF pg_catalog.jsonb_typeof(p_args->'key') IS DISTINCT FROM 'string'
           OR v_key IS NULL OR v_key = '' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_missing'
            );
        END IF;
        v_locked := aap_workspace_configuration_v1._guide_lock_project(
            v_target_id, 'wait'
        );
        IF v_locked->>'ok' IS DISTINCT FROM 'true' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action,
                CASE WHEN v_locked->>'code' = 'missing' THEN 404 ELSE 409 END,
                CASE WHEN v_locked->>'code' = 'missing'
                     THEN 'project_missing' ELSE 'guide_asset_busy' END
            );
        END IF;
        v_assets := COALESCE(v_locked->'row'->'guide_assets', '[]'::jsonb);
        IF pg_catalog.jsonb_typeof(v_assets) IS DISTINCT FROM 'array' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_assets) AS item(value)
            WHERE item.value->>'key' IS NOT DISTINCT FROM v_key
        ) THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 404, 'guide_asset_missing'
            );
        END IF;

        SELECT COALESCE(pg_catalog.array_agg(reference.upload_id), ARRAY[]::uuid[])
        INTO v_owner_ids
        FROM public.project_guide_assets AS reference
        JOIN public.guide_asset_uploads AS owner
          ON owner.id = reference.upload_id
        WHERE reference.project_id = v_target_id
          AND 'projects/' || owner.project_id::text || '/guide/' || owner.id::text = v_key;

        IF pg_catalog.array_length(v_owner_ids, 1) IS NOT NULL THEN
            BEGIN
                PERFORM owner.id
                FROM public.guide_asset_uploads AS owner
                WHERE owner.id = ANY(v_owner_ids)
                ORDER BY owner.id
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR deadlock_detected
                         OR serialization_failure THEN
                RETURN aap_workspace_configuration_v1._guide_failure(
                    p_request, p_action, 409, 'guide_asset_busy'
                );
            END;
            DELETE FROM public.project_guide_assets AS reference
            WHERE reference.project_id = v_target_id
              AND reference.upload_id = ANY(v_owner_ids);
            UPDATE public.guide_asset_uploads AS owner
            SET state = 'retired', retired_at = pg_catalog.clock_timestamp()
            WHERE owner.id = ANY(v_owner_ids)
              AND owner.state = 'ready'
              AND NOT EXISTS (
                  SELECT 1 FROM public.project_guide_assets AS remaining
                  WHERE remaining.upload_id = owner.id
              );
        END IF;

        UPDATE public.projects AS project
        SET guide_assets = COALESCE((
                SELECT pg_catalog.jsonb_agg(item.value ORDER BY item.ordinality)
                FROM pg_catalog.jsonb_array_elements(v_assets)
                    WITH ORDINALITY AS item(value, ordinality)
                WHERE item.value->>'key' IS DISTINCT FROM v_key
            ), '[]'::jsonb),
            updated_at = pg_catalog.clock_timestamp()
        WHERE project.id = v_target_id;
        IF NOT FOUND THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('deleted', v_key)
        );
    END IF;

    -- Explicit list update: only the exact current entries may be retained,
    -- reordered or removed, and only against the current revision.
    IF p_action = 'guide_assets_apply' THEN
        -- Retain the one-second bound for the whole guide-bearing request so a
        -- concurrent pool/binding lock cannot turn an explicit list update into
        -- an unbounded wait.
        PERFORM pg_catalog.set_config('lock_timeout', '1s', true);
        IF pg_catalog.jsonb_typeof(p_args->'expected_revision') IS DISTINCT FROM 'number'
           OR (p_args->>'expected_revision') COLLATE "C" !~ '^[1-9][0-9]{0,8}$'
           OR pg_catalog.jsonb_typeof(p_args->'guide_assets_ascii')
               IS DISTINCT FROM 'string'
        THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_revision_conflict'
            );
        END IF;
        v_expected_revision := (p_args->>'expected_revision')::integer;
        BEGIN
            v_supplied := (p_args->>'guide_assets_ascii')::jsonb;
        EXCEPTION WHEN invalid_text_representation OR untranslatable_character THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END;
        IF pg_catalog.jsonb_typeof(v_supplied) IS DISTINCT FROM 'array' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_supplied) AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(item.value->'key') IS DISTINCT FROM 'string'
               OR item.value->>'key' = ''
        ) THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_supplied) AS item(value)
            GROUP BY item.value->>'key'
            HAVING pg_catalog.count(*) > 1
        ) THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_duplicate_key'
            );
        END IF;

        v_locked := aap_workspace_configuration_v1._guide_lock_project(
            v_target_id, 'nowait'
        );
        IF v_locked->>'ok' IS DISTINCT FROM 'true' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action,
                CASE WHEN v_locked->>'code' = 'missing' THEN 404 ELSE 409 END,
                CASE WHEN v_locked->>'code' = 'missing'
                     THEN 'project_missing' ELSE 'guide_asset_busy' END
            );
        END IF;
        v_project := v_locked->'row';
        IF (v_project->>'guide_assets_revision')::integer
               IS DISTINCT FROM v_expected_revision THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_revision_conflict'
            );
        END IF;
        v_assets := COALESCE(v_project->'guide_assets', '[]'::jsonb);
        IF pg_catalog.jsonb_typeof(v_assets) IS DISTINCT FROM 'array' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_supplied) AS supplied(value)
            WHERE NOT EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(v_assets) AS current_value(value)
                WHERE current_value.value->>'key' IS NOT DISTINCT FROM
                          supplied.value->>'key'
                  AND current_value.value::text = supplied.value::text
            )
        ) THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END IF;

        SELECT COALESCE(pg_catalog.array_agg(reference.upload_id), ARRAY[]::uuid[])
        INTO v_owner_ids
        FROM public.project_guide_assets AS reference
        WHERE reference.project_id = v_target_id;
        IF pg_catalog.array_length(v_owner_ids, 1) IS NOT NULL THEN
            BEGIN
                PERFORM owner.id
                FROM public.guide_asset_uploads AS owner
                WHERE owner.id = ANY(v_owner_ids)
                ORDER BY owner.id
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR deadlock_detected
                         OR serialization_failure THEN
                RETURN aap_workspace_configuration_v1._guide_failure(
                    p_request, p_action, 409, 'guide_asset_busy'
                );
            END;
        END IF;

        FOR v_ref IN
            SELECT reference.upload_id AS upload_id,
                   owner.id AS owner_id,
                   owner.project_id AS owner_project_id,
                   owner.state AS owner_state,
                   owner.file_name AS file_name,
                   owner.mime_type AS mime_type,
                   owner.size_bytes AS size_bytes,
                   owner.created_at AS created_at
            FROM public.project_guide_assets AS reference
            JOIN public.guide_asset_uploads AS owner
              ON owner.id = reference.upload_id
            WHERE reference.project_id = v_target_id
            ORDER BY reference.upload_id
        LOOP
            v_key := 'projects/' || v_ref.owner_project_id::text
                || '/guide/' || v_ref.owner_id::text;
            v_matches := false;
            FOR v_supplied_entry IN
                SELECT item.value
                FROM pg_catalog.jsonb_array_elements(v_supplied) AS item(value)
                WHERE item.value->>'key' IS NOT DISTINCT FROM v_key
            LOOP
                v_matches := true;
                IF v_ref.owner_state IS DISTINCT FROM 'ready'
                   OR NOT aap_workspace_configuration_v1._guide_entry_matches(
                       v_supplied_entry, v_target_id, v_ref.owner_id,
                       v_ref.file_name, v_ref.mime_type,
                       v_ref.size_bytes, v_ref.created_at
                   ) THEN
                    RETURN aap_workspace_configuration_v1._guide_failure(
                        p_request, p_action, 409, 'guide_asset_receipt_mismatch'
                    );
                END IF;
            END LOOP;
            IF NOT v_matches THEN
                DELETE FROM public.project_guide_assets AS reference
                WHERE reference.project_id = v_target_id
                  AND reference.upload_id = v_ref.upload_id;
            END IF;
        END LOOP;

        UPDATE public.guide_asset_uploads AS owner
        SET state = 'retired', retired_at = pg_catalog.clock_timestamp()
        WHERE owner.id = ANY(v_owner_ids)
          AND owner.state = 'ready'
          AND NOT EXISTS (
              SELECT 1 FROM public.project_guide_assets AS remaining
              WHERE remaining.upload_id = owner.id
          );

        UPDATE public.projects AS project
        SET guide_assets = v_supplied,
            updated_at = pg_catalog.clock_timestamp()
        WHERE project.id = v_target_id
        RETURNING project.guide_assets_revision INTO v_revision;
        IF NOT FOUND THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_changed'
            );
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('guide_assets_revision', v_revision)
        );
    END IF;

    -- Copy the source list and every current reference after the caller has
    -- locked both Projects and checked the source did not change during setup.
    v_source_id := (p_args->>'source_project_id')::uuid;
    v_target_id := (p_args->>'target_project_id')::uuid;
    v_snapshot := p_args->'source_snapshot';
    IF v_source_id IS NULL OR v_target_id IS NULL OR v_source_id = v_target_id
       OR pg_catalog.jsonb_typeof(v_snapshot) IS DISTINCT FROM 'object'
       OR NOT aap_workspace_configuration_v1._has_exact_keys(
           v_snapshot,
           ARRAY['owner_id', 'annotation_guide', 'guide_assets',
                 'guide_assets_revision', 'upload_ids']::text[]
       )
       OR pg_catalog.jsonb_typeof(v_snapshot->'guide_assets')
           IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_snapshot->'upload_ids')
           IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_snapshot->'guide_assets_revision')
           IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_snapshot->'annotation_guide')
           NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(v_snapshot->'owner_id') IS DISTINCT FROM 'string'
       OR (v_snapshot->>'owner_id') !~* v_uuid_pattern
       OR (v_snapshot->>'guide_assets_revision') COLLATE "C"
           !~ '^[1-9][0-9]{0,8}$'
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_array_elements(v_snapshot->'upload_ids')
               AS item(value)
           WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
              OR (item.value #>> '{}') !~* v_uuid_pattern
       )
    THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action, 409, 'guide_asset_source_changed'
        );
    END IF;

    v_locked := aap_workspace_configuration_v1._guide_lock_project(
        v_source_id, 'share_nowait'
    );
    IF v_locked->>'ok' IS DISTINCT FROM 'true' THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action,
            CASE WHEN v_locked->>'code' = 'missing' THEN 404 ELSE 409 END,
            CASE WHEN v_locked->>'code' = 'missing'
                 THEN 'project_missing' ELSE 'guide_asset_busy' END
        );
    END IF;
    v_source_row := v_locked->'row';
    -- Recheck visibility against the locked source before comparing facts.
    BEGIN
        v_authority := aap_workspace_configuration_v1._configuration_project_access(
            aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            ),
            v_source_row,
            false
        );
    EXCEPTION WHEN lock_not_available OR deadlock_detected
                 OR serialization_failure THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action, 409, 'guide_asset_busy'
        );
    END;
    IF v_authority->>'code' = 'project_missing' THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action, 404, 'project_missing'
        );
    ELSIF v_authority->>'code' IS NOT NULL THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action, 403, 'guide_asset_manager_required'
        );
    END IF;
    IF (v_source_row->>'owner_id') IS DISTINCT FROM (v_snapshot->>'owner_id')
       OR (v_source_row->'annotation_guide') IS DISTINCT FROM
            (v_snapshot->'annotation_guide')
       OR (v_source_row->'guide_assets') IS DISTINCT FROM
            (v_snapshot->'guide_assets')
       OR (v_source_row->>'guide_assets_revision')::integer IS DISTINCT FROM
            (v_snapshot->>'guide_assets_revision')::integer
    THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action, 409, 'guide_asset_source_changed'
        );
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(reference.upload_id
            ORDER BY reference.upload_id), ARRAY[]::uuid[])
    INTO v_current_refs
    FROM public.project_guide_assets AS reference
    WHERE reference.project_id = v_source_id;
    SELECT COALESCE(pg_catalog.array_agg((item.value #>> '{}')::uuid
            ORDER BY (item.value #>> '{}')::uuid), ARRAY[]::uuid[])
    INTO v_snapshot_refs
    FROM pg_catalog.jsonb_array_elements(v_snapshot->'upload_ids') AS item(value)
    WHERE pg_catalog.jsonb_typeof(item.value) = 'string';
    IF v_current_refs IS DISTINCT FROM v_snapshot_refs THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action, 409, 'guide_asset_source_changed'
        );
    END IF;

    v_locked := aap_workspace_configuration_v1._guide_lock_project(
        v_target_id, 'nowait'
    );
    IF v_locked->>'ok' IS DISTINCT FROM 'true' THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action,
            CASE WHEN v_locked->>'code' = 'missing' THEN 404 ELSE 409 END,
            CASE WHEN v_locked->>'code' = 'missing'
                 THEN 'project_missing' ELSE 'guide_asset_busy' END
        );
    END IF;
    v_project := v_locked->'row';
    v_assets := COALESCE(v_project->'guide_assets', '[]'::jsonb);
    IF v_assets IS DISTINCT FROM '[]'::jsonb
       OR EXISTS (
           SELECT 1 FROM public.project_guide_assets AS reference
           WHERE reference.project_id = v_target_id
       )
    THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action, 409, 'guide_asset_changed'
        );
    END IF;

    v_source_assets := COALESCE(v_source_row->'guide_assets', '[]'::jsonb);
    IF pg_catalog.jsonb_typeof(v_source_assets) IS DISTINCT FROM 'array' THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action, 409, 'guide_asset_receipt_mismatch'
        );
    END IF;
    v_locations := ARRAY[]::uuid[];
    FOR v_ref IN
        SELECT owner.*
        FROM public.project_guide_assets AS reference
        JOIN public.guide_asset_uploads AS owner
          ON owner.id = reference.upload_id
        WHERE reference.project_id = v_source_id
        ORDER BY reference.upload_id
    LOOP
        v_key := 'projects/' || v_ref.project_id::text || '/guide/' || v_ref.id::text;
        IF v_ref.state IS DISTINCT FROM 'ready'
           OR (
               SELECT pg_catalog.count(*)
               FROM pg_catalog.jsonb_array_elements(v_source_assets) AS item(value)
               WHERE item.value->>'key' IS NOT DISTINCT FROM v_key
           ) <> 1
           OR NOT EXISTS (
               SELECT 1
               FROM pg_catalog.jsonb_array_elements(v_source_assets) AS item(value)
               WHERE item.value->>'key' IS NOT DISTINCT FROM v_key
                 AND aap_workspace_configuration_v1._guide_entry_matches(
                     item.value, v_source_id, v_ref.id, v_ref.file_name,
                     v_ref.mime_type, v_ref.size_bytes, v_ref.created_at
                 )
           )
        THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, 'guide_asset_receipt_mismatch'
            );
        END IF;
        v_locations := pg_catalog.array_append(
            v_locations, (v_ref.storage_ref->>'location_id')::uuid
        );
    END LOOP;

    FOR v_ref IN
        SELECT reference.upload_id AS id
        FROM public.project_guide_assets AS reference
        WHERE reference.project_id = v_source_id
        ORDER BY reference.upload_id
    LOOP
        INSERT INTO public.project_guide_assets (project_id, upload_id)
        VALUES (v_target_id, v_ref.id);
    END LOOP;

    FOR v_location IN
        SELECT DISTINCT location_id
        FROM pg_catalog.unnest(v_locations) AS location_id
        ORDER BY location_id
    LOOP
        v_gate := aap_workspace_configuration_v1._guide_storage_gate(
            v_location, NULL
        );
        IF v_gate->>'ok' IS DISTINCT FROM 'true' THEN
            RETURN aap_workspace_configuration_v1._guide_failure(
                p_request, p_action, 409, v_gate->>'code'
            );
        END IF;
    END LOOP;

    UPDATE public.projects AS project
    SET guide_assets = v_source_assets,
        annotation_guide = v_source_row->>'annotation_guide',
        updated_at = pg_catalog.clock_timestamp()
    WHERE project.id = v_target_id
    RETURNING project.guide_assets_revision INTO v_revision;
    IF NOT FOUND THEN
        RETURN aap_workspace_configuration_v1._guide_failure(
            p_request, p_action, 409, 'guide_asset_changed'
        );
    END IF;
    RETURN aap_workspace_configuration_v1._step_success(
        p_request, p_action,
        pg_catalog.jsonb_build_object('guide_assets_revision', v_revision)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_configuration_v1.guide_assets_step(jsonb, text, jsonb)
    FROM PUBLIC;
-- aap-statement-break

-- Forward canonical configuration entry point. The frozen 0269 bytes stay
-- untouched; this body keeps every non-guide operation and action unchanged
-- while dropping the retired presign guide operations/actions and the
-- `guide_assets` columns from the generic project insert/update surface. The
-- guide list, its revision and references are writable only through
-- `guide_assets_step` above.
CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1.configuration_step(
    p_request jsonb,
    p_action text,
    p_args jsonb,
    p_column_names text[],
    p_column_values text[]
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_action text;
    v_operation_id uuid;
    v_actor_id uuid;
    v_target_id uuid;
    v_selectors jsonb;
    v_request_hash text;
    v_txid bigint;
    v_actor jsonb;
    v_project jsonb;
    v_source_project jsonb;
    v_template jsonb;
    v_project_access jsonb;
    v_source_project_access jsonb;
    v_facts jsonb;
    v_replay jsonb;
    v_receipt public.workspace_configuration_operation_receipts%ROWTYPE;
    v_state public.workspace_configuration_operation_states%ROWTYPE;
    v_state_found boolean;
    v_active boolean;
    v_authorized boolean;
    v_is_creator boolean;
    v_scope text;
    v_organization_id uuid;
    v_id uuid;
    v_resource_id uuid;
    v_display_id text;
    v_registry_id uuid;
    v_pool_id uuid;
    v_project_id uuid;
    v_dataset_id uuid;
    v_item_ids uuid[];
    v_dataset_ids uuid[];
    v_media_ids uuid[];
    v_dataset_reply jsonb;
    v_media_reply jsonb;
    v_backend_reply jsonb;
    v_support_reply jsonb;
    v_summary jsonb;
    v_summaries jsonb;
    v_output jsonb;
    v_result jsonb;
    v_detail jsonb;
    v_headers jsonb;
    v_rows integer;
    v_name text;
    v_value text;
    v_cast text;
    v_table text;
    v_sql text;
    v_columns_sql text;
    v_values_sql text;
    v_assignments_sql text;
    v_column_count integer;
    v_index integer;
    v_action_json jsonb;
    v_source_pool_id uuid;
    v_found boolean;
    v_enabled_pool_id uuid;
    v_authority_actor jsonb;
    v_authority_project jsonb;
    v_authority_source_project jsonb;
    v_authority_template jsonb;
    v_authority_project_access jsonb;
    v_authority_source_access jsonb;
    v_autoflush boolean;
    v_lock_busy boolean;
    v_current_pool_id uuid;
    v_frozen_template jsonb;
    v_frozen_visible boolean;
    v_frozen_editable boolean;
    v_state_columns text[];
    v_class_rename_row_json text;
    v_class_rename_member_rows_json text;
    v_class_rename_old_name text;
    v_class_rename_new_name text;
    v_class_rename_tool_bindings_ascii text;
    v_class_rename_tool_bindings jsonb;
    v_uuid_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF p_action IS NULL OR p_action NOT IN (
        'begin', 'backend', 'source_pool', 'video_annotations',
        'video_trackers', 'video_items', 'video_media', 'task_count',
        'lock_config', 'datasets', 'dataset_scenes', 'authority', 'allocate',
        'insert', 'enable_backend', 'binding', 'update', 'delete', 'finish',
        'class_rename_prepare', 'class_rename_member',
        'class_rename_lock', 'class_rename_apply'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration action';
    END IF;
    v_action := p_action;
    IF NOT aap_workspace_configuration_v1._has_exact_keys(
        p_request,
        ARRAY[
            'schema', 'operation_id', 'operation', 'actor_id', 'target_id',
            'selectors', 'payload_ascii'
        ]::text[]
    ) OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
      OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-configuration/v1'
      OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
      OR (p_request->>'operation_id') !~* v_uuid_pattern
      OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
      OR p_request->>'operation' NOT IN (
          'create_project', 'update_project', 'create_template',
          'update_template', 'delete_template', 'duplicate_template',
          'rename_project_class'
      )
      OR pg_catalog.jsonb_typeof(p_request->'actor_id') IS DISTINCT FROM 'string'
      OR (p_request->>'actor_id') !~* v_uuid_pattern
      OR pg_catalog.jsonb_typeof(p_request->'target_id') NOT IN ('null', 'string')
      OR (
          pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
          AND (p_request->>'target_id') !~* v_uuid_pattern
      )
      OR pg_catalog.jsonb_typeof(p_request->'payload_ascii') IS DISTINCT FROM 'string'
      OR pg_catalog.octet_length(pg_catalog.convert_to(
          p_request->>'payload_ascii', 'UTF8'
      )) <> pg_catalog.char_length(p_request->>'payload_ascii')
      OR NOT aap_workspace_configuration_v1._has_exact_keys(
          p_request->'selectors',
          ARRAY[
              'source_project_id', 'template_id', 'ml_backend_source_id',
              'ml_backend_id', 'ml_backend_present', 'scope', 'scope_present',
              'organization_id', 'organization_present'
          ]::text[]
      ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration request';
    END IF;

    v_operation := p_request->>'operation';
    v_operation_id := (p_request->>'operation_id')::uuid;
    v_actor_id := (p_request->>'actor_id')::uuid;
    v_target_id := CASE
        WHEN pg_catalog.jsonb_typeof(p_request->'target_id') = 'string'
        THEN (p_request->>'target_id')::uuid
        ELSE NULL
    END;
    v_selectors := p_request->'selectors';
    IF (
        pg_catalog.jsonb_typeof(v_selectors->'source_project_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'source_project_id') = 'string'
            AND (v_selectors->>'source_project_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'template_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'template_id') = 'string'
            AND (v_selectors->>'template_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'ml_backend_source_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'ml_backend_source_id') = 'string'
            AND (v_selectors->>'ml_backend_source_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'ml_backend_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'ml_backend_id') = 'string'
            AND (v_selectors->>'ml_backend_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'ml_backend_present') IS DISTINCT FROM 'boolean'
        OR pg_catalog.jsonb_typeof(v_selectors->'scope') NOT IN ('null', 'string')
        OR pg_catalog.jsonb_typeof(v_selectors->'scope_present') IS DISTINCT FROM 'boolean'
        OR pg_catalog.jsonb_typeof(v_selectors->'organization_id') NOT IN ('null', 'string')
        OR (
            pg_catalog.jsonb_typeof(v_selectors->'organization_id') = 'string'
            AND (v_selectors->>'organization_id') !~* v_uuid_pattern
        )
        OR pg_catalog.jsonb_typeof(v_selectors->'organization_present') IS DISTINCT FROM 'boolean'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration selectors';
    END IF;
    IF (v_operation IN ('create_project', 'create_template') AND v_target_id IS NOT NULL)
       OR (v_operation NOT IN ('create_project', 'create_template') AND v_target_id IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration target binding';
    END IF;
    IF v_operation = 'rename_project_class' AND (
        v_selectors->'source_project_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'template_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'ml_backend_source_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'ml_backend_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'ml_backend_present' IS DISTINCT FROM 'false'::jsonb
        OR v_selectors->'scope' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'scope_present' IS DISTINCT FROM 'false'::jsonb
        OR v_selectors->'organization_id' IS DISTINCT FROM 'null'::jsonb
        OR v_selectors->'organization_present' IS DISTINCT FROM 'false'::jsonb
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid class rename selectors';
    END IF;
    IF p_args IS NULL OR pg_catalog.jsonb_typeof(p_args) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration step arguments';
    END IF;
    IF p_action = 'begin' AND NOT aap_workspace_configuration_v1._has_exact_keys(
        p_args, ARRAY[]::text[]
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid begin arguments';
    ELSIF p_action IN ('backend', 'enable_backend')
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['registry_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend arguments';
    ELSIF p_action = 'source_pool'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['pool_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid source-pool arguments';
    ELSIF p_action = 'class_rename_apply'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['old_name', 'new_name', 'tool_bindings_ascii']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid class rename apply arguments';
    ELSIF p_action IN (
        'video_annotations', 'video_trackers', 'video_items', 'task_count',
        'datasets', 'allocate', 'insert', 'delete', 'finish',
        'class_rename_prepare', 'class_rename_member', 'class_rename_lock'
    ) AND NOT aap_workspace_configuration_v1._has_exact_keys(
        p_args, ARRAY[]::text[]
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid empty arguments';
    ELSIF p_action = 'video_media'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['item_ids']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid video-media arguments';
    ELSIF p_action = 'dataset_scenes'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['dataset_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset-scenes arguments';
    ELSIF p_action = 'lock_config'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['kind']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lock-config arguments';
    ELSIF p_action = 'authority'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['autoflush']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid authority arguments';
    ELSIF p_action = 'binding'
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY['pool_id']::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid binding arguments';
    ELSIF p_action IN ('insert', 'update')
       AND NOT aap_workspace_configuration_v1._has_exact_keys(
           p_args, ARRAY[]::text[]
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid write arguments';
    ELSIF p_action IN ('update', 'insert')
       AND (p_column_names IS NULL OR p_column_values IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'missing configuration columns';
    END IF;

    IF p_action IN ('backend', 'enable_backend') THEN
        IF pg_catalog.jsonb_typeof(p_args->'registry_id') IS DISTINCT FROM 'string'
           OR (p_args->>'registry_id') !~* v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend selector';
        END IF;
    ELSIF p_action = 'source_pool' THEN
        IF pg_catalog.jsonb_typeof(p_args->'pool_id') IS DISTINCT FROM 'string'
           OR (p_args->>'pool_id') !~* v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid source-pool selector';
        END IF;
    ELSIF p_action = 'dataset_scenes' THEN
        IF pg_catalog.jsonb_typeof(p_args->'dataset_id') IS DISTINCT FROM 'string'
           OR (p_args->>'dataset_id') !~* v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset selector';
        END IF;
    ELSIF p_action = 'lock_config' THEN
        IF pg_catalog.jsonb_typeof(p_args->'kind') IS DISTINCT FROM 'string'
           OR p_args->>'kind' NOT IN ('mask_qc', 'point_cloud') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project config lock kind';
        END IF;
    ELSIF p_action = 'authority' THEN
        IF pg_catalog.jsonb_typeof(p_args->'autoflush') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid authority autoflush';
        END IF;
    ELSIF p_action = 'binding' THEN
        IF pg_catalog.jsonb_typeof(p_args->'pool_id') NOT IN ('null', 'string')
           OR (pg_catalog.jsonb_typeof(p_args->'pool_id') = 'string'
               AND (p_args->>'pool_id') !~* v_uuid_pattern) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid binding pool';
        END IF;
    ELSIF p_action = 'video_media' THEN
        IF pg_catalog.jsonb_typeof(p_args->'item_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_args->'item_ids') > 500
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.jsonb_array_elements(p_args->'item_ids') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* v_uuid_pattern
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid video-media item ids';
        END IF;
    END IF;
    IF p_action = 'class_rename_apply' THEN
        IF pg_catalog.jsonb_typeof(p_args->'old_name') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_args->'new_name') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(
               p_args->'tool_bindings_ascii'
           ) IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid class rename apply values';
        END IF;
        v_class_rename_old_name := p_args->>'old_name';
        v_class_rename_new_name := p_args->>'new_name';
        v_class_rename_tool_bindings_ascii := p_args->>'tool_bindings_ascii';
        IF v_class_rename_old_name = ''
           OR v_class_rename_new_name = ''
           OR v_class_rename_old_name IS NOT DISTINCT FROM v_class_rename_new_name
           OR pg_catalog.octet_length(pg_catalog.convert_to(
               v_class_rename_tool_bindings_ascii, 'UTF8'
           )) <> pg_catalog.char_length(v_class_rename_tool_bindings_ascii) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid class rename apply values';
        END IF;
    END IF;

    IF p_action = 'update' OR p_action = 'insert' THEN
        v_column_count := pg_catalog.cardinality(p_column_names);
        IF v_column_count IS DISTINCT FROM pg_catalog.cardinality(p_column_values)
           OR pg_catalog.array_position(p_column_names, NULL::text) IS NOT NULL
           OR (
               v_column_count > 0
               AND pg_catalog.array_ndims(p_column_names) IS DISTINCT FROM 1
           ) OR (
               v_column_count > 0
               AND pg_catalog.array_ndims(p_column_values) IS DISTINCT FROM 1
           ) OR EXISTS (
               SELECT 1
               FROM pg_catalog.unnest(p_column_names) AS field(name)
               GROUP BY field.name
               HAVING pg_catalog.count(*) > 1
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid configuration column arrays';
        END IF;
    ELSIF pg_catalog.cardinality(COALESCE(p_column_names, ARRAY[]::text[])) <> 0
       OR pg_catalog.cardinality(COALESCE(p_column_values, ARRAY[]::text[])) <> 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unexpected configuration columns';
    END IF;

    IF p_action IN (
        'class_rename_prepare', 'class_rename_member',
        'class_rename_lock', 'class_rename_apply'
    ) AND (
        p_column_names IS NULL
        OR pg_catalog.cardinality(p_column_names) IS DISTINCT FROM 0
        OR p_column_values IS NULL
        OR pg_catalog.cardinality(p_column_values) IS DISTINCT FROM 0
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'class rename actions do not accept generic columns';
    END IF;


    IF v_operation = 'rename_project_class' THEN
        IF p_action NOT IN (
            'class_rename_prepare', 'class_rename_member',
            'class_rename_lock', 'class_rename_apply'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace class rename action';
        END IF;

        IF p_action = 'class_rename_prepare' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL
               OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;
            IF v_actor->>'role' IS NULL
               OR v_actor->>'role' NOT IN (
                   'super_admin', 'project_admin', 'employee', 'viewer'
               ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;

            v_class_rename_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'key_share'
                );
            v_facts := pg_catalog.jsonb_build_object(
                'actor', pg_catalog.jsonb_build_object(
                    'actor_id', (v_actor->>'id')::uuid,
                    'platform_role', v_actor->'role',
                    'is_active', v_actor->'is_active'
                ),
                'row_json', CASE
                    WHEN v_class_rename_row_json IS NULL THEN NULL
                    ELSE pg_catalog.to_jsonb(v_class_rename_row_json)
                END
            );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, v_facts
            );
        ELSIF p_action = 'class_rename_member' THEN
            v_class_rename_member_rows_json :=
                aap_workspace_configuration_v1._class_rename_member_rows(
                    v_target_id, v_actor_id
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object(
                    'rows_json', v_class_rename_member_rows_json
                )
            );
        ELSIF p_action = 'class_rename_lock' THEN
            v_class_rename_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'update'
                );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request,
                p_action,
                pg_catalog.jsonb_build_object(
                    'row_json', CASE
                        WHEN v_class_rename_row_json IS NULL THEN NULL
                        ELSE pg_catalog.to_jsonb(v_class_rename_row_json)
                    END
                )
            );
        ELSE
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL
               OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;
            IF v_actor->>'role' IS NULL
               OR v_actor->>'role' NOT IN (
                   'super_admin', 'project_admin', 'employee', 'viewer'
               ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer'),
                    false
                );
            END IF;

            v_class_rename_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'key_share'
                );
            IF v_class_rename_row_json IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text),
                    NULL,
                    false
                );
            END IF;
            v_project := v_class_rename_row_json::jsonb;
            v_project_access :=
                aap_workspace_configuration_v1._configuration_project_access(
                    v_actor, v_project, true
                );
            IF v_project_access->>'code' = 'project_missing' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text),
                    NULL,
                    false
                );
            ELSIF v_project_access->>'code' = 'invalid_account_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text),
                    NULL,
                    false
                );
            ELSIF v_project_access->>'code' = 'invalid_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text),
                    NULL,
                    false
                );
            ELSIF v_project_access->>'code' = 'incompatible_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text),
                    NULL,
                    false
                );
            ELSIF COALESCE(
                (v_project_access->>'is_manager')::boolean, false
            ) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text),
                    NULL,
                    false
                );
            END IF;

            v_class_rename_row_json :=
                aap_workspace_configuration_v1._class_rename_project_row(
                    v_target_id, 'update'
                );
            IF v_class_rename_row_json IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text),
                    NULL,
                    false
                );
            END IF;

            BEGIN
                v_class_rename_tool_bindings :=
                    v_class_rename_tool_bindings_ascii::jsonb;
            EXCEPTION WHEN invalid_text_representation
                         OR untranslatable_character THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'invalid class rename tool bindings';
            END;
            IF pg_catalog.jsonb_typeof(
                v_class_rename_tool_bindings
            ) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'class rename tool bindings must be an object';
            END IF;

            PERFORM aap_labeling_v1.rename_project_annotation_class(
                v_target_id,
                v_class_rename_old_name,
                v_class_rename_new_name
            );
            UPDATE public.projects
            SET tool_bindings = v_class_rename_tool_bindings,
                updated_at = pg_catalog.clock_timestamp()
            WHERE id = v_target_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Project class rename did not update one project';
            END IF;

            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, '{}'::jsonb
            );
        END IF;
    ELSIF p_action IN (
        'class_rename_prepare', 'class_rename_member',
        'class_rename_lock', 'class_rename_apply'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'class rename action does not match operation';
    END IF;

    v_request_hash := pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(p_request::text, 'UTF8')),
        'hex'
    );
    v_txid := pg_catalog.txid_current();

    IF p_action = 'begin' THEN
        SELECT state.*
        INTO v_state
        FROM public.workspace_configuration_operation_states AS state
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        v_state_found := FOUND;
        IF v_state_found THEN
            IF v_state.request_hash IS DISTINCT FROM v_request_hash
               OR v_state.request IS DISTINCT FROM p_request THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 409,
                    pg_catalog.jsonb_build_object('reason', 'operation_conflict')
                );
            END IF;
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, v_state.facts
            );
        END IF;

        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap-workspace-configuration/v1:' || v_operation_id::text, 0
            )
        ) THEN
            RETURN aap_workspace_configuration_v1._step_failure(
                p_request, p_action, 409,
                pg_catalog.jsonb_build_object('reason', 'operation_busy')
            );
        END IF;

        SELECT receipt.*
        INTO v_receipt
        FROM public.workspace_configuration_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.target_id IS DISTINCT FROM v_target_id
               OR v_receipt.request_hash IS DISTINCT FROM v_request_hash
               OR v_receipt.selectors IS DISTINCT FROM v_selectors THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 409,
                    pg_catalog.jsonb_build_object('reason', 'operation_conflict')
                );
            END IF;
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;

            IF v_operation = 'delete_template' THEN
                v_frozen_template := pg_catalog.jsonb_build_object(
                    'scope', v_receipt.frozen_scope,
                    'organization_id', v_receipt.frozen_organization_id,
                    'created_by', v_receipt.frozen_creator_id
                );
                v_frozen_visible := aap_workspace_configuration_v1._configuration_template_visible(
                    v_actor, v_frozen_template
                );
                v_frozen_editable := v_actor->>'role' = 'super_admin'
                    OR v_receipt.frozen_creator_id = v_actor_id;
                IF NOT v_frozen_visible THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                IF NOT v_frozen_editable THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('仅模板创建者或超级管理员可删除'::text)
                    );
                END IF;
            ELSIF v_operation IN ('create_project', 'update_project') THEN
                v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    (v_receipt.result->>'resource_id')::uuid,
                    'key_share'
                );
                IF v_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_project_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_actor, v_project, true
                );
                IF v_project_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_project_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_project_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_project_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                ELSIF COALESCE((v_project_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                    );
                END IF;
            ELSE
                v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_receipt.result->>'resource_id')::uuid,
                    true
                );
                IF v_template IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                IF NOT aap_workspace_configuration_v1._configuration_template_visible(
                    v_actor, v_template
                ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                IF v_actor->>'role' <> 'super_admin'
                   AND v_template->>'created_by' IS DISTINCT FROM v_actor_id::text THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('仅模板创建者或超级管理员可编辑'::text)
                    );
                END IF;
            END IF;
            v_facts := pg_catalog.jsonb_build_object(
                'actor', v_actor,
                'project', v_project,
                'source_project', NULL,
                'template', v_template,
                'replay', v_receipt.result,
                'project_access', v_project_access,
                'source_project_access', NULL,
                'is_manager', CASE
                    WHEN v_project_access IS NULL THEN NULL
                    ELSE v_project_access->'is_manager'
                END
            );
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action, v_facts
            );
        END IF;

        IF v_operation = 'create_project' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, false, NULL
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            IF v_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('需要角色权限: super_admin或project_admin'::text)
                );
            END IF;
            IF v_selectors->>'source_project_id' IS NOT NULL THEN
                v_source_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    (v_selectors->>'source_project_id')::uuid, 'none'
                );
                IF v_source_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_source_project_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_actor, v_source_project, false
                );
                IF v_source_project_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_source_project_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_source_project_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_source_project_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                END IF;
            END IF;
            IF v_selectors->>'template_id' IS NOT NULL THEN
                v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_selectors->>'template_id')::uuid, false
                );
                IF v_template IS NULL OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                    v_actor, v_template
                ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
            END IF;
            v_is_creator := true;
            v_authorized := false;
        ELSIF v_operation = 'update_project' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, false, NULL
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_target_id, 'none'
            );
            IF v_project IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            v_project_access := aap_workspace_configuration_v1._configuration_project_access(
                v_actor, v_project, false
            );
            IF v_project_access->>'code' = 'project_missing' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            ELSIF v_project_access->>'code' = 'invalid_account_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                );
            ELSIF v_project_access->>'code' = 'invalid_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                );
            ELSIF v_project_access->>'code' = 'incompatible_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                );
            ELSIF COALESCE((v_project_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                );
            END IF;
            v_is_creator := true;
            v_authorized := false;
        ELSIF v_operation = 'create_template' THEN
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, false, NULL
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('无效的认证凭据'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            IF v_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目管理员或超级管理员可创建模板'::text)
                );
            END IF;
            v_scope := CASE
                WHEN (v_selectors->>'scope_present')::boolean
                THEN v_selectors->>'scope'
                ELSE 'private'
            END;
            v_organization_id := CASE
                WHEN pg_catalog.jsonb_typeof(v_selectors->'organization_id') = 'string'
                THEN (v_selectors->>'organization_id')::uuid
                ELSE NULL
            END;
            IF v_scope = 'public' AND v_actor->>'role' <> 'super_admin' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅超级管理员可创建公共模板'::text)
                );
            END IF;
            IF v_scope = 'organization' AND v_organization_id IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 400,
                    pg_catalog.to_jsonb('organization scope 必须指定 organization_id'::text)
                );
            END IF;
            v_authorized := false;
            v_is_creator := true;
        ELSE
            v_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_actor IS NULL OR COALESCE((v_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            IF v_operation = 'duplicate_template'
               AND v_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目管理员或超级管理员可克隆模板'::text)
                );
            END IF;
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_target_id, false
            );
            IF v_template IS NULL OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                v_actor, v_template
            ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('模板不存在'::text)
                );
            END IF;
            IF v_operation IN ('update_template', 'delete_template')
               AND v_actor->>'role' <> 'super_admin'
               AND v_template->>'created_by' IS DISTINCT FROM v_actor_id::text THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb(
                        CASE WHEN v_operation = 'delete_template'
                            THEN '仅模板创建者或超级管理员可删除'
                            ELSE '仅模板创建者或超级管理员可编辑'
                        END::text
                    )
                );
            END IF;
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_target_id, true
            );
            IF v_template IS NULL OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                v_actor, v_template
            ) THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('模板不存在'::text)
                );
            END IF;
            IF v_operation IN ('update_template', 'delete_template')
               AND v_actor->>'role' <> 'super_admin'
               AND v_template->>'created_by' IS DISTINCT FROM v_actor_id::text THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb(
                        CASE WHEN v_operation = 'delete_template'
                            THEN '仅模板创建者或超级管理员可删除'
                            ELSE '仅模板创建者或超级管理员可编辑'
                        END::text
                    )
                );
            END IF;
            v_authorized := true;
            v_is_creator := true;
        END IF;

        v_facts := pg_catalog.jsonb_build_object(
            'actor', v_actor,
            'project', v_project,
            'source_project', v_source_project,
            'template', v_template,
            'replay', NULL,
            'project_access', v_project_access,
            'source_project_access', v_source_project_access,
            'is_manager', COALESCE(v_project_access->'is_manager',
                CASE WHEN v_operation IN ('create_project', 'create_template')
                     OR v_operation = 'duplicate_template' THEN 'true'::jsonb
                     ELSE NULL END)
        );
        INSERT INTO public.workspace_configuration_operation_states (
            transaction_id, operation_id, request_hash, request, facts,
            authorized, source_project_id, target_id, source_backend_id,
            template_id
        ) VALUES (
            v_txid, v_operation_id, v_request_hash, p_request, v_facts,
            v_authorized,
            CASE WHEN v_selectors->>'source_project_id' IS NOT NULL
                 THEN (v_selectors->>'source_project_id')::uuid ELSE NULL END,
            v_target_id,
            CASE WHEN v_selectors->>'ml_backend_source_id' IS NOT NULL
                 THEN (v_selectors->>'ml_backend_source_id')::uuid ELSE NULL END,
            CASE
                WHEN v_operation IN ('update_template', 'delete_template', 'duplicate_template')
                    THEN v_target_id
                WHEN v_selectors->>'template_id' IS NOT NULL
                    THEN (v_selectors->>'template_id')::uuid
                ELSE NULL
            END
        );
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_facts
        );
    END IF;

    SELECT state.*
    INTO v_state
    FROM public.workspace_configuration_operation_states AS state
    WHERE state.transaction_id = v_txid
      AND state.operation_id = v_operation_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Workspace configuration operation has not begun';
    END IF;
    IF v_state.request_hash IS DISTINCT FROM v_request_hash
       OR v_state.request IS DISTINCT FROM p_request THEN
        RETURN aap_workspace_configuration_v1._step_failure(
            p_request, p_action, 409,
            pg_catalog.jsonb_build_object('reason', 'operation_conflict')
        );
    END IF;

    v_actor := NULLIF(v_state.facts->'actor', 'null'::jsonb);
    v_project := NULLIF(v_state.facts->'project', 'null'::jsonb);
    v_source_project := NULLIF(v_state.facts->'source_project', 'null'::jsonb);
    v_template := NULLIF(v_state.facts->'template', 'null'::jsonb);
    v_authorized := v_state.authorized;
    v_selectors := p_request->'selectors';

    IF p_action = 'authority' THEN
        IF v_operation NOT IN ('create_project', 'update_project', 'create_template') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'authority not allowed for operation';
        END IF;
        v_autoflush := (p_args->>'autoflush')::boolean;
        IF v_operation = 'create_project'
           AND v_state.source_project_id IS NULL
           AND v_state.binding_pending
           AND v_autoflush IS TRUE THEN
            IF v_state.resource_id IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace project binding has no inserted project';
            END IF;
            SELECT project.ml_backend_pool_id
            INTO v_current_pool_id
            FROM public.projects AS project
            WHERE project.id = v_state.resource_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace project binding target disappeared';
            END IF;
            IF v_current_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                INSERT INTO public.workspace_configuration_operation_attempts (
                    transaction_id, operation_id, request_hash
                ) VALUES (v_txid, v_operation_id, v_request_hash)
                ON CONFLICT (transaction_id, operation_id) DO NOTHING;
                UPDATE public.projects AS project
                SET ml_backend_pool_id = v_state.primary_pool_id,
                    updated_at = pg_catalog.now()
                WHERE project.id = v_state.resource_id;
                GET DIAGNOSTICS v_rows = ROW_COUNT;
                IF v_rows <> 1 THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'Workspace primary backend flush did not update one project';
                END IF;
            END IF;
            UPDATE public.workspace_configuration_operation_states AS state
            SET binding_pending = false
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
            v_state.binding_pending := false;
        END IF;

        v_authority_actor := NULL;
        v_authority_project := NULL;
        v_authority_source_project := NULL;
        v_authority_template := NULL;
        v_authority_project_access := NULL;
        v_authority_source_access := NULL;

        IF v_operation = 'create_project' THEN
            IF v_state.source_project_id IS NOT NULL THEN
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
                v_authority_source_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    v_state.source_project_id, 'key_share'
                );
                IF v_authority_source_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_authority_source_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_authority_actor, v_authority_source_project, true
                );
                IF v_authority_source_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                END IF;
                IF v_authority_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('需要角色权限: super_admin或project_admin'::text)
                    );
                END IF;
            ELSE
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
                IF v_authority_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('需要角色权限: super_admin或project_admin'::text)
                    );
                END IF;
            END IF;
            IF v_selectors->>'template_id' IS NOT NULL THEN
                v_authority_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_selectors->>'template_id')::uuid, false
                );
                IF v_authority_template IS NULL
                   OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                       v_authority_actor, v_authority_template
                   ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                v_authority_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                    (v_selectors->>'template_id')::uuid, true
                );
                IF v_authority_template IS NULL
                   OR NOT aap_workspace_configuration_v1._configuration_template_visible(
                       v_authority_actor, v_authority_template
                   ) THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
            END IF;
            IF v_state.resource_id IS NOT NULL THEN
                v_authority_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    v_state.resource_id, 'none'
                );
            END IF;
        ELSIF v_operation = 'create_template' THEN
            IF v_state.source_project_id IS NOT NULL THEN
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
                v_authority_source_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                    v_state.source_project_id, 'key_share'
                );
                IF v_authority_source_project IS NULL THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                END IF;
                v_authority_source_access := aap_workspace_configuration_v1._configuration_project_access(
                    v_authority_actor, v_authority_source_project, true
                );
                IF v_authority_source_access->>'code' = 'project_missing' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('项目不存在'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_account_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'invalid_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                    );
                ELSIF v_authority_source_access->>'code' = 'incompatible_project_role' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 403,
                        pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                    );
                END IF;
            ELSE
                v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                    v_actor_id, true, 'account_busy'
                );
                IF v_authority_actor IS NULL
                   OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 401,
                        pg_catalog.to_jsonb('账号已停用或不存在'::text),
                        pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                    );
                END IF;
            END IF;
            IF v_authority_actor->>'role' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目管理员或超级管理员可创建模板'::text)
                );
            END IF;
            v_scope := CASE
                WHEN (v_selectors->>'scope_present')::boolean
                THEN v_selectors->>'scope'
                ELSE 'private'
            END;
            IF v_scope = 'public' AND v_authority_actor->>'role' <> 'super_admin' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅超级管理员可创建公共模板'::text)
                );
            END IF;
        ELSE
            v_authority_actor := aap_workspace_configuration_v1._configuration_actor(
                v_actor_id, true, 'account_busy'
            );
            IF v_authority_actor IS NULL
               OR COALESCE((v_authority_actor->>'is_active')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 401,
                    pg_catalog.to_jsonb('账号已停用或不存在'::text),
                    pg_catalog.jsonb_build_object('WWW-Authenticate', 'Bearer')
                );
            END IF;
            v_authority_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_target_id, 'key_share'
            );
            IF v_authority_project IS NULL THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            v_authority_project_access := aap_workspace_configuration_v1._configuration_project_access(
                v_authority_actor, v_authority_project, true
            );
            IF v_authority_project_access->>'code' = 'project_missing' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            ELSIF v_authority_project_access->>'code' = 'invalid_account_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号角色无效，访问被拒绝'::text)
                );
            ELSIF v_authority_project_access->>'code' = 'invalid_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('项目职责无效，访问被拒绝'::text)
                );
            ELSIF v_authority_project_access->>'code' = 'incompatible_project_role' THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('账号平台角色与项目职责不兼容'::text)
                );
            ELSIF COALESCE((v_authority_project_access->>'is_manager')::boolean, false) IS NOT TRUE THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 403,
                    pg_catalog.to_jsonb('仅项目负责人或超级管理员可执行'::text)
                );
            END IF;
        END IF;

        v_facts := pg_catalog.jsonb_build_object(
            'actor', v_authority_actor,
            'project', v_authority_project,
            'source_project', v_authority_source_project,
            'template', v_authority_template,
            'replay', NULL,
            'project_access', v_authority_project_access,
            'source_project_access', v_authority_source_access,
            'is_manager', COALESCE(
                v_authority_project_access->'is_manager',
                CASE WHEN v_operation IN ('create_project', 'create_template')
                     THEN 'true'::jsonb ELSE NULL END
            )
        );
        UPDATE public.workspace_configuration_operation_states AS state
        SET facts = v_facts,
            authorized = true,
            completed_actions = CASE
                WHEN 'authority' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'authority')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_facts
        );
    ELSIF p_action = 'allocate' THEN
        IF v_operation NOT IN ('create_project', 'create_template', 'duplicate_template') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'allocation not allowed';
        END IF;
        IF NOT v_authorized AND NOT (
            v_operation = 'create_project'
            AND v_state.source_backend_id IS NOT NULL
            AND v_state.source_backend_ready
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace configuration allocation requires authority';
        END IF;
        IF v_state.resource_id IS NOT NULL THEN
            RETURN aap_workspace_configuration_v1._step_success(
                p_request, p_action,
                pg_catalog.jsonb_build_object(
                    'resource_id', v_state.resource_id,
                    'display_id', v_state.display_id
                )
            );
        END IF;
        v_resource_id := pg_catalog.gen_random_uuid();
        IF v_operation = 'create_project' THEN
            v_display_id := 'P-' || pg_catalog.nextval(
                'public.display_seq_projects'::pg_catalog.regclass
            )::text;
        ELSE
            v_display_id := 'PT-' || pg_catalog.nextval(
                'public.display_seq_project_templates'::pg_catalog.regclass
            )::text;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET resource_id = v_resource_id,
            display_id = v_display_id,
            completed_actions = CASE
                WHEN 'allocate' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'allocate')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'resource_id', v_resource_id,
                'display_id', v_display_id
            )
        );
    ELSIF p_action = 'insert' THEN
        IF v_operation NOT IN ('create_project', 'create_template', 'duplicate_template')
           OR v_state.resource_id IS NULL
           OR (
               NOT v_authorized
               AND NOT (
                   v_operation = 'create_project'
                   AND v_state.source_backend_id IS NOT NULL
                   AND v_state.source_backend_ready
               )
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace configuration insert is not admitted';
        END IF;
        INSERT INTO public.workspace_configuration_operation_attempts (
            transaction_id, operation_id, request_hash
        ) VALUES (v_txid, v_operation_id, v_request_hash)
        ON CONFLICT (transaction_id, operation_id) DO NOTHING;
        v_columns_sql := 'id, display_id';
        v_values_sql := pg_catalog.quote_nullable(v_state.resource_id::text) || '::uuid, '
            || pg_catalog.quote_nullable(v_state.display_id) || '::text';
        IF v_operation = 'create_project' THEN
            v_table := 'public.projects';
            v_columns_sql := v_columns_sql || ', owner_id';
            v_values_sql := v_values_sql || ', ' || pg_catalog.quote_nullable(v_actor_id::text) || '::uuid';
        ELSE
            v_table := 'public.project_templates';
            v_columns_sql := v_columns_sql || ', created_by, source_project_id';
            v_values_sql := v_values_sql || ', '
                || pg_catalog.quote_nullable(v_actor_id::text) || '::uuid, '
                || pg_catalog.quote_nullable(CASE
                    WHEN v_operation = 'duplicate_template'
                    THEN v_template->>'source_project_id'
                    WHEN v_state.source_project_id IS NOT NULL
                    THEN v_state.source_project_id::text
                    ELSE NULL
                END) || '::uuid';
        END IF;
        FOR v_index IN 1..COALESCE(v_column_count, 0) LOOP
            v_name := p_column_names[v_index];
            v_value := p_column_values[v_index];
            IF v_operation = 'create_project' THEN
                IF v_name NOT IN (
                    'name', 'type_label', 'type_key', 'data_type', 'tool_bindings',
                    'ai_enabled', 'label_config', 'sampling', 'maximum_annotations',
                    'show_overlap_first', 'iou_dedup_threshold', 'box_threshold',
                    'text_threshold', 'rendering_config', 'annotation_guide',
                    'due_date', 'ai_interactive_enabled',
                    'raster_mask_native_editing_enabled', 'video_sampling',
                    'video_collaboration', 'scene_mode', 'prefer_same_scene_continuation'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'project insert column is not allowed';
                END IF;
            ELSE
                IF v_name NOT IN (
                    'name', 'description', 'type_label', 'type_key', 'data_type',
                    'tool_bindings', 'label_config', 'ai_enabled', 'sampling',
                    'maximum_annotations', 'show_overlap_first', 'iou_dedup_threshold',
                    'box_threshold', 'text_threshold', 'rendering_config',
                    'annotation_guide', 'scope', 'organization_id'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'template insert column is not allowed';
                END IF;
            END IF;
            IF v_name IN (
                'tool_bindings', 'label_config', 'rendering_config',
                'video_sampling', 'video_collaboration',
                'default_variants', 'preannotate_pipeline', 'mask_qc_config',
                'point_cloud_quality_config'
            ) THEN
                v_cast := '::jsonb';
                IF v_value IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'JSON configuration values must use JSON text';
                END IF;
            ELSIF v_name = 'due_date' THEN
                v_cast := '::date';
            ELSIF v_name = 'organization_id' THEN
                v_cast := '::uuid';
            ELSIF v_name IN ('ai_enabled', 'show_overlap_first', 'scene_mode',
                             'prefer_same_scene_continuation',
                             'ai_interactive_enabled',
                             'raster_mask_native_editing_enabled') THEN
                v_cast := '::boolean';
            ELSIF v_name = 'maximum_annotations' THEN
                v_cast := '::integer';
            ELSIF v_name IN ('iou_dedup_threshold', 'box_threshold', 'text_threshold') THEN
                IF v_operation = 'create_project' AND v_name IN ('box_threshold', 'text_threshold') THEN
                    v_cast := '::real';
                ELSE
                    v_cast := '::double precision';
                END IF;
            ELSE
                v_cast := '::text';
            END IF;
            v_columns_sql := v_columns_sql || ', ' || pg_catalog.quote_ident(v_name);
            v_values_sql := v_values_sql || ', '
                || pg_catalog.quote_nullable(v_value) || v_cast;
        END LOOP;
        IF v_operation IN ('create_template', 'duplicate_template')
           AND NOT ('name' = ANY(COALESCE(p_column_names, ARRAY[]::text[]))) THEN
            RAISE EXCEPTION USING ERRCODE = '23502',
                MESSAGE = 'null value in column "name" of relation "project_templates" violates not-null constraint',
                SCHEMA = 'public', TABLE = 'project_templates', COLUMN = 'name';
        END IF;
        v_sql := 'INSERT INTO ' || v_table || ' (' || v_columns_sql || ') VALUES ('
            || v_values_sql || ')';
        EXECUTE v_sql;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'insert' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'insert')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        IF v_operation = 'create_project' THEN
            v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_state.resource_id, 'none'
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_state.resource_id,
                'project', v_project
            );
        ELSE
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_state.resource_id, false
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_state.resource_id,
                'template', v_template
            );
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_output
        );
    END IF;

    IF p_action = 'enable_backend' THEN
        IF v_operation NOT IN ('create_project', 'update_project') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'backend enablement not allowed';
        END IF;
        v_registry_id := (p_args->>'registry_id')::uuid;
        IF v_registry_id IS DISTINCT FROM NULLIF(v_selectors->>'ml_backend_id', '')::uuid
           AND v_registry_id IS DISTINCT FROM NULLIF(
               v_selectors->>'ml_backend_source_id', ''
           )::uuid
           AND v_registry_id IS DISTINCT FROM v_state.source_backend_id THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped backend enablement';
        END IF;
        IF v_state.resource_id IS NULL AND v_operation = 'create_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'backend requires an inserted project';
        END IF;
        IF v_operation = 'create_project'
           AND NOT v_authorized
           AND NOT (
               v_state.source_backend_id = v_registry_id
               AND v_state.source_backend_ready
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'pre-authority backend enablement requires a checked source backend';
        END IF;
        v_project_id := CASE
            WHEN v_operation = 'create_project' THEN v_state.resource_id
            ELSE v_target_id
        END;
        v_backend_reply := aap_inference_v1.enable_workspace_project_backend(
            v_project_id, v_registry_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_backend_reply,
            ARRAY['schema', 'project_id', 'registry_id', 'pool_id']::text[]
        ) OR v_backend_reply->>'schema' IS DISTINCT FROM
            'aap-inference-workspace-configuration/v1'
          OR v_backend_reply->>'project_id' IS DISTINCT FROM v_project_id::text
          OR v_backend_reply->>'registry_id' IS DISTINCT FROM v_registry_id::text
          OR pg_catalog.jsonb_typeof(v_backend_reply->'pool_id') NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Inference enable reply';
        END IF;
        v_enabled_pool_id := CASE
            WHEN pg_catalog.jsonb_typeof(v_backend_reply->'pool_id') = 'string'
            THEN (v_backend_reply->>'pool_id')::uuid
            ELSE NULL
        END;
        IF v_enabled_pool_id IS NOT NULL THEN
            INSERT INTO public.workspace_configuration_operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (v_txid, v_operation_id, v_request_hash)
            ON CONFLICT (transaction_id, operation_id) DO NOTHING;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET primary_pool_id = v_enabled_pool_id,
            completed_actions = CASE
                WHEN 'enable_backend' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'enable_backend')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('pool_id', v_backend_reply->'pool_id')
        );
    ELSIF p_action = 'binding' THEN
        IF v_operation NOT IN ('create_project', 'update_project') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'primary binding not allowed';
        END IF;
        v_pool_id := CASE
            WHEN pg_catalog.jsonb_typeof(p_args->'pool_id') = 'string'
            THEN (p_args->>'pool_id')::uuid
            ELSE NULL
        END;
        IF v_operation = 'create_project' THEN
            IF v_state.resource_id IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding requires an inserted project';
            END IF;
            IF v_state.source_backend_id IS NOT NULL
               AND v_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding pool does not match enabled source backend';
            END IF;
            IF v_state.source_backend_id IS NULL
               AND v_selectors->>'ml_backend_id' IS NOT NULL
               AND 'enable_backend' = ANY(v_state.completed_actions)
               AND v_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding pool does not match enabled backend';
            END IF;
        ELSE
            IF (v_selectors->>'ml_backend_present')::boolean IS DISTINCT FROM true THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'primary binding was not requested';
            END IF;
            IF v_selectors->>'ml_backend_id' IS NULL AND v_pool_id IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'cleared primary binding must be null';
            END IF;
            IF v_selectors->>'ml_backend_id' IS NOT NULL
               AND 'enable_backend' = ANY(v_state.completed_actions)
               AND v_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'binding pool does not match enabled backend';
            END IF;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET primary_pool_id = v_pool_id,
            binding_pending = true,
            completed_actions = CASE
                WHEN 'binding' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'binding')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, '{}'::jsonb
        );
    ELSIF p_action = 'update' THEN
        IF v_operation NOT IN ('update_project', 'update_template') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'configuration update not allowed';
        END IF;
        IF v_operation = 'update_project' AND NOT v_authorized THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project update lacks final authority';
        END IF;
        IF v_operation = 'update_template' AND NOT v_authorized THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'template update lacks authority';
        END IF;
        v_assignments_sql := '';
        FOR v_index IN 1..COALESCE(v_column_count, 0) LOOP
            v_name := p_column_names[v_index];
            v_value := p_column_values[v_index];
            IF v_operation = 'update_project' THEN
                IF v_name NOT IN (
                    'name', 'type_label', 'type_key', 'data_type', 'status',
                    'tool_bindings', 'ai_enabled', 'due_date', 'sampling',
                    'maximum_annotations', 'show_overlap_first',
                    'iou_dedup_threshold', 'box_threshold', 'text_threshold',
                    'ai_interactive_enabled', 'raster_mask_native_editing_enabled',
                    'mask_qc_config', 'point_cloud_quality_config',
                    'rendering_config', 'annotation_guide',
                    'video_sampling', 'video_collaboration', 'scene_mode',
                    'prefer_same_scene_continuation', 'scene_continuation_window_min',
                    'default_variants', 'preannotate_pipeline'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'project update column is not allowed';
                END IF;
            ELSE
                IF v_name NOT IN (
                    'name', 'description', 'type_label', 'type_key', 'data_type',
                    'tool_bindings', 'label_config', 'ai_enabled', 'sampling',
                    'maximum_annotations', 'show_overlap_first',
                    'iou_dedup_threshold', 'box_threshold', 'text_threshold',
                    'rendering_config', 'annotation_guide', 'scope', 'organization_id'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'template update column is not allowed';
                END IF;
            END IF;
            IF v_name IN (
                'tool_bindings', 'label_config', 'rendering_config',
                'video_sampling', 'video_collaboration',
                'default_variants', 'preannotate_pipeline', 'mask_qc_config',
                'point_cloud_quality_config'
            ) THEN
                v_cast := '::jsonb';
                IF v_value IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = '22023',
                        MESSAGE = 'JSON configuration values must use JSON text';
                END IF;
            ELSIF v_name = 'due_date' THEN
                v_cast := '::date';
            ELSIF v_name = 'organization_id' THEN
                v_cast := '::uuid';
            ELSIF v_name IN (
                'ai_enabled', 'show_overlap_first', 'scene_mode',
                'prefer_same_scene_continuation', 'ai_interactive_enabled',
                'raster_mask_native_editing_enabled'
            ) THEN
                v_cast := '::boolean';
            ELSIF v_name IN ('maximum_annotations', 'scene_continuation_window_min') THEN
                v_cast := '::integer';
            ELSIF v_name IN ('iou_dedup_threshold', 'box_threshold', 'text_threshold') THEN
                IF v_operation = 'update_project'
                   AND v_name IN ('box_threshold', 'text_threshold') THEN
                    v_cast := '::real';
                ELSE
                    v_cast := '::double precision';
                END IF;
            ELSE
                v_cast := '::text';
            END IF;
            IF v_assignments_sql <> '' THEN
                v_assignments_sql := v_assignments_sql || ', ';
            END IF;
            v_assignments_sql := v_assignments_sql
                || pg_catalog.quote_ident(v_name) || ' = '
                || pg_catalog.quote_nullable(v_value) || v_cast;
        END LOOP;

        IF v_operation = 'update_project' AND v_state.binding_pending THEN
            SELECT project.ml_backend_pool_id
            INTO v_current_pool_id
            FROM public.projects AS project
            WHERE project.id = v_target_id;
            IF NOT FOUND THEN
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
            IF v_current_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                IF v_assignments_sql <> '' THEN
                    v_assignments_sql := v_assignments_sql || ', ';
                END IF;
                v_assignments_sql := v_assignments_sql
                    || 'ml_backend_pool_id = '
                    || pg_catalog.quote_nullable(v_state.primary_pool_id::text)
                    || '::uuid';
            END IF;
        END IF;

        IF v_assignments_sql <> '' THEN
            INSERT INTO public.workspace_configuration_operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (v_txid, v_operation_id, v_request_hash)
            ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            v_table := CASE
                WHEN v_operation = 'update_project' THEN 'public.projects'
                ELSE 'public.project_templates'
            END;
            v_project_id := CASE
                WHEN v_operation = 'update_project' THEN v_target_id
                ELSE v_target_id
            END;
            v_sql := 'UPDATE ' || v_table || ' SET ' || v_assignments_sql
                || ', updated_at = pg_catalog.now() WHERE id = '
                || pg_catalog.quote_nullable(v_project_id::text) || '::uuid';
            EXECUTE v_sql;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                IF v_operation = 'update_template' THEN
                    RETURN aap_workspace_configuration_v1._step_failure(
                        p_request, p_action, 404,
                        pg_catalog.to_jsonb('模板不存在'::text)
                    );
                END IF;
                RETURN aap_workspace_configuration_v1._step_failure(
                    p_request, p_action, 404,
                    pg_catalog.to_jsonb('项目不存在'::text)
                );
            END IF;
        END IF;
        IF v_operation = 'update_project' AND v_state.binding_pending THEN
            UPDATE public.workspace_configuration_operation_states AS state
            SET binding_pending = false
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'update' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'update')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        IF v_operation = 'update_project' THEN
            v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
                v_target_id, 'none'
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_target_id,
                'project', v_project
            );
        ELSE
            v_template := aap_workspace_configuration_v1._configuration_template_snapshot(
                v_target_id, false
            );
            v_output := pg_catalog.jsonb_build_object(
                'resource_id', v_target_id,
                'template', v_template
            );
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_output
        );
    ELSIF p_action = 'delete' THEN
        IF v_operation <> 'delete_template' OR NOT v_authorized THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'template deletion is not admitted';
        END IF;
        INSERT INTO public.workspace_configuration_operation_attempts (
            transaction_id, operation_id, request_hash
        ) VALUES (v_txid, v_operation_id, v_request_hash)
        ON CONFLICT (transaction_id, operation_id) DO NOTHING;
        DELETE FROM public.project_templates AS template
        WHERE template.id = v_target_id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 THEN
            RETURN aap_workspace_configuration_v1._step_failure(
                p_request, p_action, 404,
                pg_catalog.to_jsonb('模板不存在'::text)
            );
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'delete' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'delete')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('resource_id', v_target_id)
        );
    ELSIF p_action = 'finish' THEN
        IF (
            v_operation = 'create_project'
            AND (NOT v_authorized OR NOT ('insert' = ANY(v_state.completed_actions)))
        ) OR (
            v_operation IN ('create_template', 'duplicate_template')
            AND (NOT v_authorized OR NOT ('insert' = ANY(v_state.completed_actions)))
        ) OR (
            v_operation IN ('update_project', 'update_template')
            AND NOT ('update' = ANY(v_state.completed_actions))
        ) OR (
            v_operation = 'delete_template'
            AND NOT ('delete' = ANY(v_state.completed_actions))
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace configuration operation is incomplete';
        END IF;

        IF v_operation IN ('create_project', 'update_project')
           AND v_state.binding_pending THEN
            v_project_id := CASE
                WHEN v_operation = 'create_project' THEN v_state.resource_id
                ELSE v_target_id
            END;
            SELECT project.ml_backend_pool_id
            INTO v_current_pool_id
            FROM public.projects AS project
            WHERE project.id = v_project_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace configuration binding target disappeared';
            END IF;
            IF v_current_pool_id IS DISTINCT FROM v_state.primary_pool_id THEN
                INSERT INTO public.workspace_configuration_operation_attempts (
                    transaction_id, operation_id, request_hash
                ) VALUES (v_txid, v_operation_id, v_request_hash)
                ON CONFLICT (transaction_id, operation_id) DO NOTHING;
                UPDATE public.projects AS project
                SET ml_backend_pool_id = v_state.primary_pool_id,
                    updated_at = pg_catalog.now()
                WHERE project.id = v_project_id;
                GET DIAGNOSTICS v_rows = ROW_COUNT;
                IF v_rows <> 1 THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'Workspace configuration binding did not update one project';
                END IF;
            END IF;
        END IF;

        IF v_operation = 'create_project'
           AND v_state.template_id IS NOT NULL THEN
            INSERT INTO public.workspace_configuration_operation_attempts (
                transaction_id, operation_id, request_hash
            ) VALUES (v_txid, v_operation_id, v_request_hash)
            ON CONFLICT (transaction_id, operation_id) DO NOTHING;
            UPDATE public.project_templates AS template
            SET usage_count = template.usage_count + 1,
                updated_at = pg_catalog.now()
            WHERE template.id = v_state.template_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            IF v_rows <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'Workspace template usage increment did not update one template';
            END IF;
        END IF;

        v_resource_id := CASE
            WHEN v_operation IN ('create_project', 'create_template', 'duplicate_template')
            THEN v_state.resource_id
            ELSE v_target_id
        END;
        v_id := CASE
            WHEN v_operation = 'create_project'
            THEN v_state.template_id
            ELSE NULL
        END;
        v_result := pg_catalog.jsonb_build_object(
            'resource_id', v_resource_id,
            'template_id', v_id
        );
        IF pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'object'
           OR v_result->>'resource_id' IS DISTINCT FROM v_resource_id::text
           OR (
               v_id IS NULL AND v_result->'template_id' IS DISTINCT FROM 'null'::jsonb
           ) OR (
               v_id IS NOT NULL AND v_result->>'template_id' IS DISTINCT FROM v_id::text
           ) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration finish result';
        END IF;
        INSERT INTO public.workspace_configuration_operation_receipts (
            operation_id, actor_id, operation, target_id, request_hash,
            selectors, result, frozen_scope, frozen_organization_id,
            frozen_creator_id
        ) VALUES (
            v_operation_id, v_actor_id, v_operation, v_target_id,
            v_request_hash, v_selectors, v_result,
            CASE WHEN v_operation = 'delete_template' THEN v_template->>'scope' ELSE NULL END,
            CASE WHEN v_operation = 'delete_template'
                 AND pg_catalog.jsonb_typeof(v_template->'organization_id') = 'string'
                 THEN (v_template->>'organization_id')::uuid ELSE NULL END,
            CASE WHEN v_operation = 'delete_template'
                 AND pg_catalog.jsonb_typeof(v_template->'created_by') = 'string'
                 THEN (v_template->>'created_by')::uuid ELSE NULL END
        );
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Workspace configuration receipt was not recorded';
        END IF;
        DELETE FROM public.workspace_configuration_operation_attempts AS attempt
        WHERE attempt.transaction_id = v_txid
          AND attempt.operation_id = v_operation_id;
        DELETE FROM public.workspace_configuration_operation_states AS state
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_result
        );
    END IF;

    IF p_action = 'backend' THEN
        IF v_operation NOT IN ('create_project', 'update_project') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'backend lookup not allowed';
        END IF;
        v_registry_id := (p_args->>'registry_id')::uuid;
        IF v_registry_id IS DISTINCT FROM NULLIF(v_selectors->>'ml_backend_id', '')::uuid
           AND v_registry_id IS DISTINCT FROM NULLIF(
               v_selectors->>'ml_backend_source_id', ''
           )::uuid
           AND v_registry_id IS DISTINCT FROM v_state.source_backend_id THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped backend lookup';
        END IF;
        v_backend_reply := aap_inference_v1.workspace_configuration_backend(
            v_registry_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_backend_reply, ARRAY['schema', 'registry_id', 'backend']::text[]
        ) OR v_backend_reply->>'schema' IS DISTINCT FROM
            'aap-inference-workspace-configuration/v1'
          OR v_backend_reply->>'registry_id' IS DISTINCT FROM v_registry_id::text
          OR pg_catalog.jsonb_typeof(v_backend_reply->'backend') NOT IN ('null', 'object') THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Inference backend reply';
        END IF;
        IF v_state.source_backend_id = v_registry_id
           AND pg_catalog.jsonb_typeof(v_backend_reply->'backend') = 'object' THEN
            UPDATE public.workspace_configuration_operation_states AS state
            SET source_backend_ready = true,
                completed_actions = CASE
                    WHEN 'backend' = ANY(state.completed_actions) THEN state.completed_actions
                    ELSE pg_catalog.array_append(state.completed_actions, 'backend')
                END
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
        ELSE
            UPDATE public.workspace_configuration_operation_states AS state
            SET completed_actions = CASE
                    WHEN 'backend' = ANY(state.completed_actions) THEN state.completed_actions
                    ELSE pg_catalog.array_append(state.completed_actions, 'backend')
                END
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
        END IF;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('backend', v_backend_reply->'backend')
        );
    ELSIF p_action = 'source_pool' THEN
        IF v_operation <> 'create_project'
           OR v_source_project IS NULL
           OR (v_source_project->>'ml_backend_pool_id') IS DISTINCT FROM p_args->>'pool_id' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped source-pool lookup';
        END IF;
        v_pool_id := (p_args->>'pool_id')::uuid;
        v_support_reply := aap_inference_v1.workspace_configuration_source_pool(
            v_pool_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_support_reply,
            ARRAY['schema', 'pool_id', 'legacy_registry_id', 'found']::text[]
        ) OR v_support_reply->>'schema' IS DISTINCT FROM
            'aap-inference-workspace-configuration/v1'
          OR v_support_reply->>'pool_id' IS DISTINCT FROM v_pool_id::text
          OR pg_catalog.jsonb_typeof(v_support_reply->'found') IS DISTINCT FROM 'boolean'
          OR pg_catalog.jsonb_typeof(v_support_reply->'legacy_registry_id') NOT IN ('null', 'string') THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Inference source-pool reply';
        END IF;
        v_registry_id := CASE
            WHEN pg_catalog.jsonb_typeof(v_support_reply->'legacy_registry_id') = 'string'
            THEN (v_support_reply->>'legacy_registry_id')::uuid
            ELSE NULL
        END;
        UPDATE public.workspace_configuration_operation_states AS state
        SET source_backend_id = v_registry_id,
            source_backend_ready = false,
            completed_actions = CASE
                WHEN 'source_pool' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'source_pool')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'found', v_support_reply->'found',
                'legacy_registry_id', v_support_reply->'legacy_registry_id'
            )
        );
    ELSIF p_action IN ('video_annotations', 'video_trackers', 'video_items', 'video_media') THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'video facts require a project update';
        END IF;
        IF p_action = 'video_annotations' THEN
            v_support_reply := aap_labeling_v1.workspace_configuration_annotation_count(
                v_target_id
            );
            IF NOT aap_workspace_configuration_v1._has_exact_keys(
                v_support_reply, ARRAY['schema', 'project_id', 'annotation_count']::text[]
            ) OR v_support_reply->>'schema' IS DISTINCT FROM
                'aap-labeling-workspace-configuration/v1'
              OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration annotation facts';
            END IF;
            v_output := pg_catalog.jsonb_build_object(
                'annotation_count', v_support_reply->'annotation_count'
            );
        ELSIF p_action = 'video_trackers' THEN
            v_support_reply := aap_labeling_v1.workspace_configuration_tracker_count(
                v_target_id
            );
            IF NOT aap_workspace_configuration_v1._has_exact_keys(
                v_support_reply, ARRAY['schema', 'project_id', 'active_tracker_count']::text[]
            ) OR v_support_reply->>'schema' IS DISTINCT FROM
                'aap-labeling-workspace-configuration/v1'
              OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration tracker facts';
            END IF;
            v_output := pg_catalog.jsonb_build_object(
                'active_tracker_count', v_support_reply->'active_tracker_count'
            );
        ELSIF p_action = 'video_items' THEN
            v_support_reply := aap_labeling_v1.workspace_configuration_video_items(
                v_target_id
            );
            IF NOT aap_workspace_configuration_v1._has_exact_keys(
                v_support_reply, ARRAY['schema', 'project_id', 'dataset_item_ids']::text[]
            ) OR v_support_reply->>'schema' IS DISTINCT FROM
                'aap-labeling-workspace-configuration/v1'
              OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text
              OR pg_catalog.jsonb_typeof(v_support_reply->'dataset_item_ids') IS DISTINCT FROM 'array'
              OR EXISTS (
                  SELECT 1
                  FROM pg_catalog.jsonb_array_elements(v_support_reply->'dataset_item_ids') AS item(value)
                  WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                     OR item.value #>> '{}' !~* v_uuid_pattern
              ) THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration video item facts';
            END IF;
            SELECT COALESCE(
                pg_catalog.array_agg(DISTINCT (item.value #>> '{}')::uuid),
                ARRAY[]::uuid[]
            )
            INTO v_media_ids
            FROM pg_catalog.jsonb_array_elements(v_support_reply->'dataset_item_ids') AS item(value);
            UPDATE public.workspace_configuration_operation_states AS state
            SET media_item_ids = v_media_ids,
                completed_actions = CASE
                    WHEN 'video_items' = ANY(state.completed_actions) THEN state.completed_actions
                    ELSE pg_catalog.array_append(state.completed_actions, 'video_items')
                END
            WHERE state.transaction_id = v_txid
              AND state.operation_id = v_operation_id;
            v_output := pg_catalog.jsonb_build_object(
                'dataset_item_ids', v_support_reply->'dataset_item_ids'
            );
        ELSE
            SELECT COALESCE(
                pg_catalog.array_agg(item.value::uuid ORDER BY item.ordinality),
                ARRAY[]::uuid[]
            )
            INTO v_item_ids
            FROM pg_catalog.jsonb_array_elements_text(p_args->'item_ids')
                WITH ORDINALITY AS item(value, ordinality);
            IF NOT v_state.media_item_ids @> v_item_ids THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'video media ids were not read from the selected project';
            END IF;
            v_media_reply := aap_assets_v1.read_media_summaries(v_item_ids);
            IF v_media_reply->>'schema' IS DISTINCT FROM 'aap-assets-media/v1'
               OR v_media_reply->'ok' IS DISTINCT FROM 'true'::jsonb
               OR pg_catalog.jsonb_typeof(v_media_reply->'data') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_media_reply #> '{data,items}') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION USING ERRCODE = 'XX000',
                    MESSAGE = 'invalid Workspace configuration Assets media reply';
            END IF;
            v_summaries := '{}'::jsonb;
            FOR v_summary IN
                SELECT item.value
                FROM pg_catalog.jsonb_array_elements(v_media_reply #> '{data,items}') AS item(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_summary->'dataset_item_id') IS DISTINCT FROM 'string'
                   OR (v_summary->>'dataset_item_id') !~* v_uuid_pattern
                   OR NOT v_item_ids @> ARRAY[(v_summary->>'dataset_item_id')::uuid]::uuid[] THEN
                    RAISE EXCEPTION USING ERRCODE = 'XX000',
                        MESSAGE = 'invalid Workspace configuration Assets media item';
                END IF;
                IF v_summary->>'file_type' = 'video' THEN
                    v_summaries := v_summaries || pg_catalog.jsonb_build_object(
                        v_summary->>'dataset_item_id',
                        CASE
                            WHEN v_summary->>'source_drifted' = 'false'
                             AND v_summary->'source_generation' = v_summary->'materialized_generation'
                            THEN v_summary->'metadata'
                            ELSE 'null'::jsonb
                        END
                    );
                END IF;
            END LOOP;
            v_output := pg_catalog.jsonb_build_object('summaries', v_summaries);
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN p_action = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, p_action)
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action, v_output
        );
    ELSIF p_action = 'task_count' THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'task count requires a project update';
        END IF;
        v_support_reply := aap_labeling_v1.workspace_configuration_task_count(
            v_target_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_support_reply, ARRAY['schema', 'project_id', 'total_tasks']::text[]
        ) OR v_support_reply->>'schema' IS DISTINCT FROM
            'aap-labeling-workspace-configuration/v1'
          OR v_support_reply->>'project_id' IS DISTINCT FROM v_target_id::text THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration task-count facts';
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'task_count' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'task_count')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('total_tasks', v_support_reply->'total_tasks')
        );
    ELSIF p_action = 'datasets' THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'dataset read requires a project update';
        END IF;
        v_dataset_reply := aap_assets_v1.workspace_configuration_dataset_rows(
            v_target_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_dataset_reply, ARRAY['schema', 'project_id', 'datasets']::text[]
        ) OR v_dataset_reply->>'schema' IS DISTINCT FROM
            'aap-assets-workspace-configuration/v1'
          OR v_dataset_reply->>'project_id' IS DISTINCT FROM v_target_id::text
          OR pg_catalog.jsonb_typeof(v_dataset_reply->'datasets') IS DISTINCT FROM 'array'
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.jsonb_array_elements(v_dataset_reply->'datasets') AS item(value)
              WHERE NOT aap_workspace_configuration_v1._has_exact_keys(
                  item.value, ARRAY['id', 'display_id', 'data_type']::text[]
              ) OR item.value->>'id' !~* v_uuid_pattern
                OR pg_catalog.jsonb_typeof(item.value->'display_id') IS DISTINCT FROM 'string'
                OR pg_catalog.jsonb_typeof(item.value->'data_type') IS DISTINCT FROM 'string'
          ) THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Assets dataset reply';
        END IF;
        SELECT COALESCE(
            pg_catalog.array_agg((item.value->>'id')::uuid ORDER BY item.ordinality),
            ARRAY[]::uuid[]
        )
        INTO v_dataset_ids
        FROM pg_catalog.jsonb_array_elements(v_dataset_reply->'datasets')
            WITH ORDINALITY AS item(value, ordinality);
        UPDATE public.workspace_configuration_operation_states AS state
        SET dataset_ids = v_dataset_ids,
            completed_actions = CASE
                WHEN 'datasets' = ANY(state.completed_actions) THEN state.completed_actions
                ELSE pg_catalog.array_append(state.completed_actions, 'datasets')
            END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('datasets', v_dataset_reply->'datasets')
        );
    ELSIF p_action = 'dataset_scenes' THEN
        v_dataset_id := (p_args->>'dataset_id')::uuid;
        IF v_operation <> 'update_project'
           OR NOT v_state.dataset_ids @> ARRAY[v_dataset_id]::uuid[] THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unscoped dataset scene read';
        END IF;
        v_support_reply := aap_assets_v1.workspace_configuration_dataset_scenes(
            v_dataset_id
        );
        IF NOT aap_workspace_configuration_v1._has_exact_keys(
            v_support_reply, ARRAY['schema', 'dataset_id', 'has_scenes']::text[]
        ) OR v_support_reply->>'schema' IS DISTINCT FROM
            'aap-assets-workspace-configuration/v1'
          OR v_support_reply->>'dataset_id' IS DISTINCT FROM v_dataset_id::text
          OR pg_catalog.jsonb_typeof(v_support_reply->'has_scenes') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Workspace configuration Assets scene reply';
        END IF;
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'dataset_scenes' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'dataset_scenes')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object('has_scenes', v_support_reply->'has_scenes')
        );
    ELSIF p_action = 'lock_config' THEN
        IF v_operation <> 'update_project' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project config lock requires update';
        END IF;
        BEGIN
            SELECT project.id INTO v_id
            FROM public.projects AS project
            WHERE project.id = v_target_id
            FOR UPDATE OF project NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING
                ERRCODE = SQLSTATE,
                MESSAGE = SQLERRM,
                HINT = 'aap.workspace.configuration.project_busy';
        END;
        IF NOT FOUND THEN
            RETURN aap_workspace_configuration_v1._step_failure(
                p_request, p_action, 404,
                pg_catalog.to_jsonb('项目不存在'::text)
            );
        END IF;
        v_project := aap_workspace_configuration_v1._configuration_project_snapshot(
            v_target_id, 'none'
        );
        UPDATE public.workspace_configuration_operation_states AS state
        SET completed_actions = CASE
            WHEN 'lock_config' = ANY(state.completed_actions) THEN state.completed_actions
            ELSE pg_catalog.array_append(state.completed_actions, 'lock_config')
        END
        WHERE state.transaction_id = v_txid
          AND state.operation_id = v_operation_id;
        RETURN aap_workspace_configuration_v1._step_success(
            p_request, p_action,
            pg_catalog.jsonb_build_object(
                'kind', p_args->'kind',
                'project', v_project
            )
        );
    END IF;

    RAISE EXCEPTION USING ERRCODE = '22023',
        MESSAGE = 'unhandled Workspace configuration action';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_configuration_v1.configuration_step(
    jsonb, text, jsonb, text[], text[]
) FROM PUBLIC;
