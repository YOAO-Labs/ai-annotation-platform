-- Restore the frozen pre-0381 trigger definitions for a clean downgrade:
-- unqualified relation names, caller search_path and invoker security.
--
-- Fail-closed rollback: function ACLs are intentionally NOT rewritten here.
-- CREATE OR REPLACE keeps each guard's existing owner and ACL, so a deployment
-- that already revoked PUBLIC EXECUTE stays revoked, and one that granted it
-- keeps its own grant. Trigger execution never checks consumer EXECUTE, so a
-- revoked ACL cannot disable the restored guards. Function OIDs, owners,
-- trigger bindings and table grants are untouched.

CREATE OR REPLACE FUNCTION rotate_ml_backend_authentication()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.auth_revision IS DISTINCT FROM OLD.auth_revision THEN
        RAISE EXCEPTION 'backend authentication revision is database managed'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'ck_ml_backend_auth_revision_managed';
    END IF;
    IF OLD.auth_method IS DISTINCT FROM NEW.auth_method
       OR OLD.auth_token IS DISTINCT FROM NEW.auth_token THEN
        -- The earlier resource/global barrier trigger keeps lock ordering intact.
        -- Proof commits lock this membership while rechecking their registry
        -- snapshot; hold the same row lock without changing its epoch or state.
        IF OLD.gpu_resource_id IS NOT NULL THEN
            PERFORM 1 FROM gpu_backend_memberships
            WHERE backend_registry_id = OLD.id
              AND gpu_resource_id = OLD.gpu_resource_id
              AND state IN ('pending', 'active')
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'current GPU membership is missing'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_gpu_backend_membership_current_missing';
            END IF;
        END IF;
        NEW.auth_revision := OLD.auth_revision + 1;
        NEW.state := 'disconnected';
        NEW.health_meta := NULL;
        NEW.last_checked_at := NULL;
        NEW.error_message := NULL;
    END IF;
    RETURN NEW;
END;
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION protect_reference_item() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM dataset_references WHERE dataset_item_id = OLD.id)
           AND (NEW.file_path IS DISTINCT FROM OLD.file_path OR NEW.dataset_id IS DISTINCT FROM OLD.dataset_id) THEN
            RAISE EXCEPTION 'Reference item binding is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION protect_reference_task() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM dataset_references WHERE dataset_item_id = OLD.dataset_item_id)
           AND (NEW.file_path IS DISTINCT FROM OLD.file_path OR NEW.dataset_item_id IS DISTINCT FROM OLD.dataset_item_id OR NEW.project_id IS DISTINCT FROM OLD.project_id) THEN
            RAISE EXCEPTION 'Reference task binding is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION protect_directory_item() RETURNS trigger LANGUAGE plpgsql AS $$    BEGIN
      IF EXISTS (SELECT 1 FROM dataset_source_revisions WHERE dataset_item_id=OLD.id)
         AND (NEW.file_path IS DISTINCT FROM OLD.file_path OR NEW.dataset_id IS DISTINCT FROM OLD.dataset_id) THEN
        RAISE EXCEPTION 'Directory item binding is immutable' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION protect_directory_asset() RETURNS trigger LANGUAGE plpgsql AS $$    BEGIN
      IF EXISTS (SELECT 1 FROM dataset_source_revisions WHERE asset_id=OLD.id)
         AND (NEW.dataset_item_id IS DISTINCT FROM OLD.dataset_item_id
           OR NEW.checksum_algorithm IS DISTINCT FROM OLD.checksum_algorithm
           OR NEW.checksum_value IS DISTINCT FROM OLD.checksum_value
           OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
           OR NEW.verification_state IS DISTINCT FROM OLD.verification_state) THEN
        RAISE EXCEPTION 'Directory asset identity is immutable' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION protect_source_head() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' THEN
        IF NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.path_digest IS DISTINCT FROM OLD.path_digest THEN
          RAISE EXCEPTION 'Source head locator is immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.current_revision_id IS DISTINCT FROM OLD.current_revision_id AND NOT EXISTS (
          SELECT 1 FROM dataset_source_revisions WHERE id=NEW.current_revision_id
            AND previous_revision_id=OLD.current_revision_id) THEN
          RAISE EXCEPTION 'Source revision predecessor changed' USING ERRCODE='23514';
        END IF;
      END IF;
      IF NOT EXISTS (SELECT 1 FROM dataset_source_revisions r
        WHERE r.id=NEW.current_revision_id AND r.source_id=NEW.source_id
          AND r.path_digest=NEW.path_digest AND r.run_id=NEW.run_id
          AND r.dataset_item_id=NEW.dataset_item_id AND r.asset_id=NEW.asset_id
          AND r.content_sha256=NEW.content_sha256 AND r.ownership_mode=NEW.ownership_mode
          AND r.owns_item=NEW.owns_item) THEN
        RAISE EXCEPTION 'Source head must match its accepted revision' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION protect_relocated_revision() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE p dataset_source_revisions;
    BEGIN
      IF NEW.previous_revision_id IS NOT NULL THEN
        SELECT * INTO p FROM dataset_source_revisions WHERE id=NEW.previous_revision_id;
        IF p.source_id IS DISTINCT FROM NEW.source_id THEN
          RAISE EXCEPTION 'Revision predecessor source differs' USING ERRCODE='23514';
        END IF;
        IF p.path_digest IS DISTINCT FROM NEW.path_digest AND
          (p.item_id_at_accept,p.asset_id_at_accept,p.content_sha256,p.ownership_mode,p.owns_item)
          IS DISTINCT FROM
          (NEW.item_id_at_accept,NEW.asset_id_at_accept,NEW.content_sha256,NEW.ownership_mode,NEW.owns_item) THEN
          RAISE EXCEPTION 'Relocation must preserve business identity' USING ERRCODE='23514';
        END IF;
      END IF;
      RETURN NEW;
    END; $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_managed_original_creation_attachment() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='UPDATE' AND OLD.creation_id IS DISTINCT FROM NEW.creation_id
                AND (TG_TABLE_NAME='managed_original_retirements' OR OLD.creation_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Managed original creation link is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_managed_creation_link';
            END IF;
            IF TG_TABLE_NAME='managed_original_retirements' THEN
                RETURN NEW;
            END IF;
            IF TG_OP='UPDATE' AND OLD.creation_id IS NOT NULL THEN
                IF ROW(NEW.asset_id,NEW.location_id,NEW.object_key,NEW.role)
                    IS DISTINCT FROM ROW(OLD.asset_id,OLD.location_id,OLD.object_key,OLD.role) THEN
                    RAISE EXCEPTION 'Managed original creation link is immutable'
                        USING ERRCODE='23514', CONSTRAINT='ck_managed_creation_link';
                END IF;
                -- The link is historical creation provenance. A controlled
                -- offline bucket remap preserves that source fact; current
                -- applicability is revalidated by attach/configuration pins.
                RETURN NEW;
            END IF;
            IF NEW.creation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM managed_original_creations c
                JOIN dataset_assets a ON a.id=NEW.asset_id
                JOIN dataset_items i ON i.id=a.dataset_item_id
                WHERE c.id=NEW.creation_id AND c.status='verified'
                    AND c.dataset_id=i.dataset_id AND c.location_id=NEW.location_id
                    AND c.bucket=NEW.bucket AND c.object_key=NEW.object_key
                    AND NEW.role='managed_original' AND a.verification_state='verified'
                    AND a.checksum_algorithm='sha256' AND a.checksum_value=c.sha256
                    AND a.size_bytes=c.size_bytes
            ) THEN
                RAISE EXCEPTION 'Managed original creation attachment does not match'
                    USING ERRCODE='23514', CONSTRAINT='ck_managed_creation_link';
            END IF;
            RETURN NEW;
        END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_guide_asset_facts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.mime_type IS DISTINCT FROM OLD.mime_type
               OR NEW.file_name IS DISTINCT FROM OLD.file_name
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'Guide image identity and content are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_guide_asset_facts_immutable';
            END IF;
            IF NOT (NEW.state = OLD.state
                OR (OLD.state='pending' AND NEW.state IN ('ready','retired'))
                OR (OLD.state='ready' AND NEW.state='retired'))
            THEN
                RAISE EXCEPTION 'Guide image state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_guide_asset_state_forward';
            END IF;
            IF NEW.state='retired' AND OLD.state<>'retired'
               AND EXISTS (SELECT 1 FROM project_guide_assets WHERE upload_id=OLD.id)
            THEN
                RAISE EXCEPTION 'Referenced guide images cannot retire'
                    USING ERRCODE='23514', CONSTRAINT='ck_guide_asset_referenced_ready';
            END IF;
            IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at
            THEN
                RAISE EXCEPTION 'Guide image retirement is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_guide_asset_retirement_immutable';
            END IF;
            RETURN NEW;
        END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_project_guide_asset() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE owner_state text;
        BEGIN
            IF TG_OP='UPDATE' THEN
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'Project guide reference facts are immutable'
                        USING ERRCODE='23514', CONSTRAINT='ck_project_guide_asset_immutable';
                END IF;
                RETURN NEW;
            END IF;
            -- Keep direct inserts in the same project-before-owner order as services.
            PERFORM 1 FROM projects WHERE id=NEW.project_id FOR KEY SHARE NOWAIT;
            SELECT state INTO owner_state FROM guide_asset_uploads
                WHERE id=NEW.upload_id FOR UPDATE NOWAIT;
            IF owner_state IS DISTINCT FROM 'ready' THEN
                RAISE EXCEPTION 'Project guide references require a ready owner'
                    USING ERRCODE='23514', CONSTRAINT='ck_project_guide_asset_ready';
            END IF;
            RETURN NEW;
        END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_raster_object_facts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.id,NEW.logical_key,NEW.canonical_sha256,NEW.canonical_size_bytes,
                   NEW.height,NEW.width,NEW.runs,NEW.storage_encoding,
                   NEW.stored_sha256,NEW.stored_size_bytes,NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id,OLD.logical_key,OLD.canonical_sha256,OLD.canonical_size_bytes,
                   OLD.height,OLD.width,OLD.runs,OLD.storage_encoding,
                   OLD.stored_sha256,OLD.stored_size_bytes,OLD.created_at)
            THEN
                RAISE EXCEPTION 'RLE object identity and content are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_facts_immutable';
            END IF;
            IF NOT (NEW.state=OLD.state
                OR (OLD.state='pending' AND NEW.state IN ('ready','retired'))
                OR (OLD.state='ready' AND NEW.state='retired'))
            THEN
                RAISE EXCEPTION 'RLE object state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_state_forward';
            END IF;
            IF NEW.published_at IS DISTINCT FROM OLD.published_at
               AND NOT (OLD.state='pending' AND NEW.state='ready'
                        AND OLD.published_at IS NULL AND NEW.published_at IS NOT NULL)
            THEN
                RAISE EXCEPTION 'RLE object publication is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_publication_immutable';
            END IF;
            IF NEW.last_used_at IS DISTINCT FROM OLD.last_used_at
               AND (NEW.state <> 'ready' OR NEW.last_used_at IS NULL
                    OR (OLD.last_used_at IS NOT NULL AND NEW.last_used_at < OLD.last_used_at))
            THEN
                RAISE EXCEPTION 'RLE object use clock must advance on ready objects'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_last_used_monotonic';
            END IF;
            IF OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at
            THEN
                RAISE EXCEPTION 'RLE object retirement is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_retirement_immutable';
            END IF;
            IF NEW.state='retired' AND OLD.state<>'retired'
               AND EXISTS (SELECT 1 FROM raster_mask_publications WHERE object_id=OLD.id)
            THEN
                RAISE EXCEPTION 'Guarded RLE objects cannot retire'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_object_publication_guard';
            END IF;
            RETURN NEW;
        END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_raster_publication() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE owner_state text;
        BEGIN
            IF TG_OP='UPDATE' THEN
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'RLE publication facts are immutable'
                        USING ERRCODE='23514', CONSTRAINT='ck_raster_publication_immutable';
                END IF;
                RETURN NEW;
            END IF;
            SELECT state INTO owner_state FROM raster_mask_objects
                WHERE id=NEW.object_id FOR UPDATE NOWAIT;
            IF owner_state IS NULL OR owner_state NOT IN ('pending','ready') THEN
                RAISE EXCEPTION 'RLE publications require pending or ready owner'
                    USING ERRCODE='23514', CONSTRAINT='ck_raster_publication_owner';
            END IF;
            RETURN NEW;
        END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_task_original() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE relation_owner oid;
        BEGIN
            IF TG_OP='DELETE' THEN
                RAISE EXCEPTION 'Task original responsibility cannot be deleted'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_immutable';
            END IF;
            IF TG_OP='INSERT' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM managed_original_creations c
                    WHERE c.id=NEW.creation_id AND c.id=NEW.id
                      AND c.owner_kind='task' AND c.dataset_id IS NULL
                      AND c.task_id=NEW.task_id AND c.project_id=NEW.project_id
                      AND c.status='verified' AND c.origin='direct'
                      AND c.location_id=NEW.location_id
                      AND c.storage_identity=NEW.storage_identity AND c.bucket=NEW.bucket
                      AND c.object_key=NEW.object_key AND c.sha256=NEW.sha256
                      AND c.size_bytes=NEW.size_bytes AND c.etag=NEW.etag
                      AND c.version_id IS NOT DISTINCT FROM NEW.version_id
                      AND c.completed_at=NEW.created_at
                      AND (CASE WHEN c.configuration->>'schema'='2' THEN c.configuration->'original_ref' ELSE NULL END) IS NOT DISTINCT FROM NEW.storage_ref
                ) THEN
                    RAISE EXCEPTION 'Task original requires matching verified creation'
                        USING ERRCODE='23514', CONSTRAINT='ck_task_original_creation';
                END IF;
                RETURN NEW;
            END IF;
            IF ROW(NEW.id,NEW.creation_id,NEW.task_id,NEW.project_id,NEW.location_id,
                   NEW.storage_ref,NEW.object_key,NEW.sha256,NEW.size_bytes,NEW.etag,NEW.version_id,NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id,OLD.creation_id,OLD.task_id,OLD.project_id,OLD.location_id,
                   OLD.storage_ref,OLD.object_key,OLD.sha256,OLD.size_bytes,OLD.etag,OLD.version_id,OLD.created_at)
            THEN
                RAISE EXCEPTION 'Task original facts are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_immutable';
            END IF;
            IF ROW(NEW.bucket,NEW.storage_identity) IS DISTINCT FROM ROW(OLD.bucket,OLD.storage_identity) THEN
                SELECT relowner INTO relation_owner FROM pg_class WHERE oid=TG_RELID;
                IF OLD.storage_ref IS NULL AND current_setting('aap.restore_training_storage',true)='on'
                   AND pg_has_role(current_user,relation_owner,'MEMBER')
                   AND NEW.bucket IS DISTINCT FROM OLD.bucket
                   AND NEW.storage_identity IS DISTINCT FROM OLD.storage_identity
                THEN RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Task original location is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_location_immutable';
            END IF;
            RETURN NEW;
        END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_task_original_attachment() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='UPDATE' AND OLD.original_id IS NOT NULL
               AND NEW.original_id IS DISTINCT FROM OLD.original_id THEN
                RAISE EXCEPTION 'Task original link cannot be rebound'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_attachment';
            END IF;
            IF NEW.original_id IS NOT NULL AND (
                NEW.dataset_item_id IS NOT NULL
                OR EXISTS (SELECT 1 FROM task_dataset_item_links WHERE task_id=NEW.id)
                OR NOT EXISTS (SELECT 1 FROM task_originals o
                    WHERE o.id=NEW.original_id AND o.task_id=NEW.id
                      AND o.project_id=NEW.project_id AND o.object_key=NEW.file_path)
            ) THEN
                RAISE EXCEPTION 'Task original attachment does not match standalone task'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_attachment';
            END IF;
            RETURN NEW;
        END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_task_original_dataset_link() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE current_original uuid;
        BEGIN
            -- Serialize both directions of the standalone/multifile boundary.
            -- Never wait behind a caller that may already own a link row.
            SELECT original_id INTO current_original FROM tasks
                WHERE id=NEW.task_id FOR UPDATE NOWAIT;
            IF current_original IS NOT NULL THEN
                RAISE EXCEPTION 'Standalone original cannot acquire dataset links'
                    USING ERRCODE='23514', CONSTRAINT='ck_task_original_dataset_link';
            END IF;
            RETURN NEW;
        END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION check_export_media_pair() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            bundle export_media_bundles%ROWTYPE;
            output export_outputs%ROWTYPE;
            first_ready boolean := false;
        BEGIN
            IF TG_TABLE_NAME='export_outputs' THEN
                SELECT * INTO output FROM export_outputs WHERE id=NEW.id;
                IF NOT FOUND OR output.media_bundle_id IS NULL THEN RETURN NULL; END IF;
                SELECT * INTO bundle FROM export_media_bundles WHERE id=output.media_bundle_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Export ZIP has no media owner'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_pair';
                END IF;
                IF TG_OP='INSERT' THEN first_ready := NEW.state='ready';
                ELSE first_ready := OLD.state<>'ready' AND NEW.state='ready'; END IF;
            ELSE
                SELECT * INTO bundle FROM export_media_bundles WHERE id=NEW.id;
                IF NOT FOUND THEN RETURN NULL; END IF;
                IF TG_OP='UPDATE' THEN first_ready := OLD.state<>'ready' AND NEW.state='ready'; END IF;
                SELECT * INTO output FROM export_outputs WHERE media_bundle_id=bundle.id;
                IF NOT FOUND THEN
                    IF NOT first_ready THEN RETURN NULL; END IF;
                    RAISE EXCEPTION 'Published export media has no matching ZIP'
                        USING ERRCODE='23514', CONSTRAINT='ck_export_media_pair';
                END IF;
            END IF;
            IF output.id IS DISTINCT FROM bundle.zip_output_id
                OR output.project_id IS DISTINCT FROM bundle.project_id
                OR output.job_id IS DISTINCT FROM bundle.job_id
                OR output.expires_at IS DISTINCT FROM bundle.expires_at
                OR output.media IS DISTINCT FROM 'image'
                OR (first_ready AND (output.state<>'ready' OR bundle.state<>'ready' OR bundle.published_at IS NULL))
            THEN
                RAISE EXCEPTION 'Export ZIP and media must describe the same delivery'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_pair';
            END IF;
            IF first_ready AND (bundle.lease_expires_at <= clock_timestamp()
                OR bundle.expires_at <= clock_timestamp())
            THEN
                RAISE EXCEPTION 'Expired export media cannot publish'
                    USING ERRCODE='23514', CONSTRAINT='ck_export_media_publication_expired';
            END IF;
            RETURN NULL;
        END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_auxiliary_original() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE relation_owner oid;
    BEGIN
        IF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'Auxiliary original responsibility cannot be deleted' USING ERRCODE='23514';
        END IF;
        IF TG_OP='INSERT' THEN
            IF NOT EXISTS (SELECT 1 FROM managed_original_creations c
                WHERE c.id=NEW.id AND c.id=NEW.creation_id AND c.owner_kind='dataset_auxiliary'
                AND c.dataset_id=NEW.dataset_id AND c.status='verified'
                AND c.location_id=NEW.location_id AND c.storage_identity=NEW.storage_identity
                AND c.bucket=NEW.bucket AND c.object_key=NEW.object_key
                AND c.sha256=NEW.sha256 AND c.size_bytes=NEW.size_bytes AND c.etag=NEW.etag
                AND c.version_id IS NOT DISTINCT FROM NEW.version_id AND c.completed_at=NEW.created_at
                AND c.authority->'auxiliary'->>'kind'=NEW.kind
                AND c.authority->'auxiliary'->'source_contract'=NEW.source_contract) THEN
                RAISE EXCEPTION 'Auxiliary original requires matching verified creation' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END IF;
        IF ROW(NEW.id,NEW.creation_id,NEW.dataset_id,NEW.kind,NEW.source_contract,NEW.location_id,
               NEW.object_key,NEW.sha256,NEW.size_bytes,NEW.etag,NEW.version_id,NEW.created_at)
           IS DISTINCT FROM ROW(OLD.id,OLD.creation_id,OLD.dataset_id,OLD.kind,OLD.source_contract,OLD.location_id,
               OLD.object_key,OLD.sha256,OLD.size_bytes,OLD.etag,OLD.version_id,OLD.created_at) THEN
            RAISE EXCEPTION 'Auxiliary original facts are immutable' USING ERRCODE='23514';
        END IF;
        IF ROW(NEW.bucket,NEW.storage_identity) IS DISTINCT FROM ROW(OLD.bucket,OLD.storage_identity) THEN
            SELECT relowner INTO relation_owner FROM pg_class WHERE oid=TG_RELID;
            IF current_setting('aap.restore_training_storage',true)='on'
                AND pg_has_role(current_user,relation_owner,'MEMBER')
                AND NEW.bucket IS DISTINCT FROM OLD.bucket AND NEW.storage_identity IS DISTINCT FROM OLD.storage_identity THEN RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Auxiliary original location is immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_auxiliary_pointer() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE pointer uuid; old_pointer uuid; expected_kind text; expected_contract jsonb; source jsonb;
    BEGIN
        IF TG_TABLE_NAME='dataset_items' THEN
            pointer := NEW.nuscenes_raw_original_id;
            expected_kind := 'raw_lidar';
            source := NEW.metadata->'nuscenes_export';
            expected_contract := jsonb_build_object('sample',source->'sample','sample_data',source->'sample_data',
                'calibrated_sensor',source->'calibrated_sensor','sensor',source->'sensor','ego_pose',source->'ego_pose');
            IF TG_OP='UPDATE' THEN old_pointer := OLD.nuscenes_raw_original_id; END IF;
            IF pointer IS NOT NULL AND NEW.file_type <> 'point_cloud' THEN
                RAISE EXCEPTION 'Raw original requires primary point cloud' USING ERRCODE='23514';
            END IF;
        ELSE
            pointer := NEW.nuscenes_map_original_id;
            expected_kind := 'map';
            expected_contract := NEW.source_metadata->'nuscenes_export'->'map';
            IF TG_OP='UPDATE' THEN old_pointer := OLD.nuscenes_map_original_id; END IF;
        END IF;
        IF old_pointer IS NOT NULL AND pointer IS DISTINCT FROM old_pointer THEN
            RAISE EXCEPTION 'Auxiliary pointer cannot be rebound' USING ERRCODE='23514';
        END IF;
        IF pointer IS NOT NULL AND TG_TABLE_NAME='dataset_items' THEN
            IF NOT EXISTS (SELECT 1 FROM scenes s WHERE s.id=NEW.scene_id AND s.dataset_id=NEW.dataset_id
                AND s.source_format='nuscenes' AND source->'sample'->>'scene_token'=s.source_metadata->'nuscenes_export'->'scene'->>'token') THEN
                RAISE EXCEPTION 'Raw original scene mismatch' USING ERRCODE='23514';
            END IF;
        END IF;
        IF pointer IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dataset_auxiliary_originals o
            WHERE o.id=pointer AND o.dataset_id=NEW.dataset_id AND o.kind=expected_kind
              AND o.source_contract=expected_contract) THEN
            RAISE EXCEPTION 'Auxiliary pointer contract mismatch' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION reject_auxiliary_creation_asset() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.creation_id IS NOT NULL AND EXISTS (SELECT 1 FROM managed_original_creations c
            WHERE c.id=NEW.creation_id AND c.owner_kind <> 'dataset') THEN
            RAISE EXCEPTION 'Dataset asset requires dataset creation' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION guard_prediction_input_owner() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        member record; previous record; entry jsonb; total bigint:=0; count_objects integer:=0;
        relation_owner oid; restoring boolean:=false;
    BEGIN
        IF TG_OP='DELETE' THEN
            IF OLD.contract_version NOT IN (2,3) OR OLD.state<>'deleted' OR EXISTS (
                SELECT 1 FROM storage_operations WHERE purpose='prediction_input_' || replace(OLD.id::text,'-','')
            ) THEN
                RAISE EXCEPTION 'Prediction input responsibility is not deleted' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_delete';
            END IF;
            RETURN OLD;
        END IF;
        IF TG_OP='UPDATE' THEN
            SELECT relowner INTO relation_owner FROM pg_class WHERE oid=TG_RELID;
            restoring:=current_setting('aap.restore_training_storage',true)='on'
                AND pg_has_role(current_user,relation_owner,'MEMBER')
                AND NEW.bucket IS DISTINCT FROM OLD.bucket
                AND NEW.storage_identity IS DISTINCT FROM OLD.storage_identity
                AND (to_jsonb(NEW)-ARRAY['bucket','storage_identity','storage_ref']::text[])
                    = (to_jsonb(OLD)-ARRAY['bucket','storage_identity','storage_ref']::text[])
                AND ((OLD.contract_version=1 AND NEW.storage_ref IS NULL)
                    OR (NEW.storage_ref IS NOT NULL AND OLD.storage_ref IS NOT NULL
                        AND NEW.storage_ref-ARRAY['bucket','storage_identity']::text[]
                            =OLD.storage_ref-ARRAY['bucket','storage_identity']::text[]));
            IF restoring THEN RETURN NEW; END IF;
            IF ROW(NEW.id,NEW.owner_id,NEW.owner_kind,NEW.task_id,NEW.contract_version,NEW.bucket,
                NEW.storage_identity,NEW.storage_ref,NEW.maintenance_revision,NEW.source_ref,
                NEW.created_at,NEW.expires_at,NEW.retain_until)
               IS DISTINCT FROM ROW(OLD.id,OLD.owner_id,OLD.owner_kind,OLD.task_id,OLD.contract_version,OLD.bucket,
                OLD.storage_identity,OLD.storage_ref,OLD.maintenance_revision,OLD.source_ref,
                OLD.created_at,OLD.expires_at,OLD.retain_until) THEN
                RAISE EXCEPTION 'Prediction input facts are immutable' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_immutable';
            END IF;
            IF OLD.state='deleted' AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Deleted prediction input is terminal' USING ERRCODE='23514';
            END IF;
            IF (OLD.state='cleanup_pending' AND NEW.state NOT IN ('cleanup_pending','deleted'))
                OR (OLD.state='active' AND NEW.state NOT IN ('active','cleanup_pending'))
                OR (OLD.uncertain_write AND NOT NEW.uncertain_write)
                OR (OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at) THEN
                RAISE EXCEPTION 'Prediction input state cannot reverse' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_transition';
            END IF;
        END IF;
        IF NEW.contract_version=1 THEN RETURN NEW; END IF;
        FOR member IN SELECT key,value FROM jsonb_each(NEW.inventory->'objects') LOOP
            entry:=member.value;
            IF NEW.contract_version=3 THEN
                IF NOT COALESCE((jsonb_typeof(entry)='object'
                    AND entry ?& ARRAY['sha256','size_bytes','media_type','state','etag','version_id','write_nonce','container','probe']
                    AND entry-ARRAY['sha256','size_bytes','media_type','state','etag','version_id','write_nonce','container','probe']::text[]='{}'::jsonb
                    AND jsonb_typeof(entry->'sha256')='string' AND entry->>'sha256' ~ '^[0-9a-f]{64}$'
                    AND jsonb_typeof(entry->'size_bytes')='number'
                    AND (CASE WHEN entry->>'size_bytes' ~ '^[1-9][0-9]{0,9}$' THEN (entry->>'size_bytes')::bigint<=5368709120 ELSE false END)
                    AND entry->>'media_type'='application/octet-stream'
                    AND entry->>'state' IN ('planned','verified')
                    AND jsonb_typeof(entry->'write_nonce')='string'
                    AND entry->>'write_nonce' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                    AND entry->>'container' IN ('mov','matroska','avi','asf','flv')
                    AND member.key='video/' || (entry->>'sha256') || (CASE entry->>'container' WHEN 'mov' THEN '.mov' WHEN 'matroska' THEN '.mkv' WHEN 'avi' THEN '.avi' WHEN 'asf' THEN '.asf' WHEN 'flv' THEN '.flv' END)
                    AND jsonb_typeof(entry->'probe')='object'
                    AND entry->'probe' ?& ARRAY['probe_contract','video']
                    AND (entry->'probe')-ARRAY['probe_contract','video']::text[]='{}'::jsonb
                    AND entry->'probe'->'probe_contract'='1'::jsonb AND entry->'probe'->>'probe_contract'='1'
                    AND jsonb_typeof(entry->'probe'->'video')='object'
                    AND entry->'probe'->'video' ?& ARRAY['width','height','fps','frame_count','duration_ms','codec']
                    AND (entry->'probe'->'video')-ARRAY['width','height','fps','frame_count','duration_ms','codec']::text[]='{}'::jsonb
                    AND jsonb_typeof(entry->'probe'->'video'->'width')='number'
                    AND jsonb_typeof(entry->'probe'->'video'->'height')='number'
                    AND (CASE WHEN entry->'probe'->'video'->>'width' ~ '^[1-9][0-9]{0,9}$' THEN (entry->'probe'->'video'->>'width')::bigint<=2147483647 ELSE false END)
                    AND (CASE WHEN entry->'probe'->'video'->>'height' ~ '^[1-9][0-9]{0,9}$' THEN (entry->'probe'->'video'->>'height')::bigint<=2147483647 ELSE false END)
                    AND jsonb_typeof(entry->'probe'->'video'->'frame_count')='number'
                    AND (CASE WHEN entry->'probe'->'video'->>'frame_count' ~ '^[1-9][0-9]{0,6}$' THEN (entry->'probe'->'video'->>'frame_count')::bigint<=1000000 ELSE false END)
                    AND (entry->'probe'->'video'->'duration_ms'='null'::jsonb OR (jsonb_typeof(entry->'probe'->'video'->'duration_ms')='number' AND (CASE WHEN entry->'probe'->'video'->>'duration_ms' ~ '^(0|[1-9][0-9]{0,18})$' THEN (entry->'probe'->'video'->>'duration_ms')::numeric<=9223372036854775807 ELSE false END)))
                    AND jsonb_typeof(entry->'probe'->'video'->'fps')='number'
                    AND (CASE WHEN jsonb_typeof(entry->'probe'->'video'->'fps')='number' THEN (entry->'probe'->'video'->>'fps')::numeric>0 ELSE false END)
                    AND jsonb_typeof(entry->'probe'->'video'->'codec')='string'
                    AND length(entry->'probe'->'video'->>'codec') BETWEEN 1 AND 128
                    AND octet_length((entry->'probe')::text)<=4096),false) THEN
                    RAISE EXCEPTION 'Invalid video prediction input object' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_inventory';
                END IF;
            ELSE
            IF NOT COALESCE((jsonb_typeof(entry)='object'
                AND entry ?& ARRAY['sha256','size_bytes','media_type','state','etag','version_id','write_nonce']
                AND entry-ARRAY['sha256','size_bytes','media_type','state','etag','version_id','write_nonce']::text[]='{}'::jsonb
                AND jsonb_typeof(entry->'sha256')='string' AND entry->>'sha256' ~ '^[0-9a-f]{64}$'
                AND jsonb_typeof(entry->'size_bytes')='number' AND entry->>'size_bytes' ~ '^[1-9][0-9]{0,7}$'
                AND (CASE WHEN entry->>'size_bytes' ~ '^[1-9][0-9]{0,7}$'
                    THEN (entry->>'size_bytes')::bigint<=67108864 ELSE false END)
                AND entry->>'media_type' IN ('image/jpeg','image/png','image/webp')
                AND entry->>'state' IN ('planned','verified')
                AND jsonb_typeof(entry->'write_nonce')='string'
                AND entry->>'write_nonce' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                AND member.key ~ '^(original/[0-9a-f]{64}[.](jpg|png|webp)|crops/(0|[1-9][0-9]{0,9})-[0-9a-f]{64}[.]jpg)$'
                AND right(member.key,64+length(CASE entry->>'media_type' WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/png' THEN '.png' ELSE '.webp' END))
                    =(entry->>'sha256') || (CASE entry->>'media_type' WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/png' THEN '.png' ELSE '.webp' END)),false) THEN
                RAISE EXCEPTION 'Invalid prediction input object' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_inventory';
            END IF;
            END IF;
            IF NOT COALESCE(((entry->>'state'='planned' AND entry->'etag'='null'::jsonb AND entry->'version_id'='null'::jsonb)
                OR (entry->>'state'='verified' AND jsonb_typeof(entry->'etag')='string'
                    AND octet_length(entry->>'etag') BETWEEN 1 AND 1024
                    AND length(entry->>'etag')=octet_length(entry->>'etag')
                    AND entry->>'etag' ~ '^[!-~]+$'
                    AND (entry->'version_id'='null'::jsonb OR (jsonb_typeof(entry->'version_id')='string'
                        AND octet_length(entry->>'version_id') BETWEEN 1 AND 1024
                        AND length(entry->>'version_id')=octet_length(entry->>'version_id')
                        AND entry->>'version_id' ~ '^[!-~]+$')))),false) THEN
                RAISE EXCEPTION 'Invalid prediction verification receipt' USING ERRCODE='23514';
            END IF;
            count_objects:=count_objects+1;
            total:=total+(entry->>'size_bytes')::bigint;
            IF TG_OP='INSERT' AND entry->>'state'<>'planned' THEN
                RAISE EXCEPTION 'Objects must first be planned' USING ERRCODE='23514';
            END IF;
            IF TG_OP='UPDATE' AND NOT (OLD.inventory->'objects' ? member.key)
                AND entry->>'state'<>'planned' THEN
                RAISE EXCEPTION 'New objects must first be planned' USING ERRCODE='23514';
            END IF;
        END LOOP;
        IF count_objects<>NEW.object_count OR total<>NEW.size_bytes THEN
            RAISE EXCEPTION 'Prediction input totals do not match inventory' USING ERRCODE='23514';
        END IF;
        IF TG_OP='UPDATE' THEN
            FOR previous IN SELECT key,value FROM jsonb_each(OLD.inventory->'objects') LOOP
                entry:=NEW.inventory->'objects'->previous.key;
                IF entry IS NULL OR entry-ARRAY['state','etag','version_id']::text[]
                    <>previous.value-ARRAY['state','etag','version_id']::text[]
                    OR (previous.value->>'state'='verified' AND entry<>previous.value) THEN
                    RAISE EXCEPTION 'Prediction input objects are immutable' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_append_only';
                END IF;
            END LOOP;
            IF NEW.inventory IS DISTINCT FROM OLD.inventory AND (
                NEW.state<>'active' OR OLD.state<>'active' OR NEW.expires_at<=statement_timestamp()
            ) THEN
                RAISE EXCEPTION 'Retired prediction inventory is frozen' USING ERRCODE='23514';
            END IF;
        END IF;
        RETURN NEW;
    END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION protect_reference_replica() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.primary_asset_id IS DISTINCT FROM OLD.primary_asset_id
               OR NEW.replica_asset_id IS DISTINCT FROM OLD.replica_asset_id
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR (OLD.revoked_at IS NOT NULL AND NEW IS DISTINCT FROM OLD) THEN
                RAISE EXCEPTION 'Replica identity and revoked grants are immutable'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM dataset_assets a
            JOIN dataset_items ai ON ai.id = a.dataset_item_id
            JOIN dataset_references ar ON ar.dataset_item_id = ai.id
            JOIN dataset_assets b ON b.id = NEW.replica_asset_id
            JOIN dataset_items bi ON bi.id = b.dataset_item_id
            JOIN dataset_references br ON br.dataset_item_id = bi.id
            WHERE a.id = NEW.primary_asset_id AND ai.dataset_id = bi.dataset_id
              AND a.media_type = 'image' AND b.media_type = 'image'
              AND a.verification_state = 'verified' AND b.verification_state = 'verified'
              AND a.checksum_algorithm = 'sha256' AND b.checksum_algorithm = 'sha256'
              AND a.checksum_value = b.checksum_value AND a.size_bytes = b.size_bytes
        ) THEN
            RAISE EXCEPTION 'Replica requires same-dataset verified reference content'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END; $$;
