-- Add source-scoped scene and auxiliary-retention fences before DatasetItem DML.

CREATE FUNCTION aap_assets_v1.dataset_item_scene_references(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS TABLE(scene_id uuid)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL OR p_item_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid dataset item scene reference request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    RETURN QUERY
    SELECT item.scene_id
    FROM public.dataset_items AS item
    WHERE item.dataset_id = p_dataset_id
      AND item.id = p_item_id
      AND item.scene_id IS NOT NULL
    ORDER BY item.scene_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.dataset_item_scene_references(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.guard_dataset_source_deletion(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    -- A direct primary source and every multimodal task link retain the item,
    -- including links whose role is not primary_lidar.
    IF EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.tasks AS task
          ON task.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.task_dataset_item_links AS item_link
          ON item_link.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.annotations AS annotation
          ON annotation.sensor_dataset_item_id = source_item.item_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    -- Deleting an Item may proceed when it has no item-owned history. Dataset
    -- deletion also removes Scenes, so retain all Scene/track/quality history.
    IF p_item_id IS NULL THEN
        IF EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.scene_tracks AS scene_track
              ON scene_track.scene_id = source_scene.scene_id
            LIMIT 1
        ) OR EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.scene_track_operations AS operation
              ON operation.scene_id = source_scene.scene_id
            LIMIT 1
        ) OR EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.point_cloud_quality_issues AS issue
              ON issue.scene_id = source_scene.scene_id
            LIMIT 1
        ) OR EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.point_cloud_quality_runs AS quality_run
              ON (quality_run.scope_json -> 'scene_ids') ? source_scene.scene_id::text
            LIMIT 1
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
            );
        END IF;
    END IF;

    -- A selected Item also retains history for its containing Scene. Resolve
    -- that relationship through Assets' narrow projection rather than reading
    -- dataset_items from the Labeling owner.
    IF p_item_id IS NOT NULL AND EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_scene_references(p_dataset_id, p_item_id)
            AS source_scene
        WHERE EXISTS (
            SELECT 1 FROM public.scene_tracks AS scene_track
            WHERE scene_track.scene_id = source_scene.scene_id
        ) OR EXISTS (
            SELECT 1 FROM public.scene_track_operations AS operation
            WHERE operation.scene_id = source_scene.scene_id
        ) OR EXISTS (
            SELECT 1 FROM public.point_cloud_quality_issues AS issue
            WHERE issue.scene_id = source_scene.scene_id
        ) OR EXISTS (
            SELECT 1 FROM public.point_cloud_quality_runs AS quality_run
            WHERE (quality_run.scope_json -> 'scene_ids') ? source_scene.scene_id::text
        )
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    -- Video segment/tracker records are retained evidence. DatasetItem FKs
    -- cascade, so reject deletion before those rows could disappear silently.
    IF EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.video_segments AS segment
          ON segment.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.video_tracker_jobs AS tracker_job
          ON tracker_job.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.video_chapters AS chapter
          ON chapter.dataset_item_id = source_item.item_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    IF p_item_id IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM public.task_creation_operations AS operation
            WHERE operation.dataset_id = p_dataset_id
              AND operation.state IN ('accepted', 'running')
              AND EXISTS (
                  SELECT 1
                  FROM aap_assets_v1.dataset_association_references(
                      p_dataset_id
                  ) AS association
                  WHERE association.association_id = operation.association_id
                    AND association.project_id = operation.project_id
              )
              AND aap_jobs_v1.task_creation_execution_active(
                  operation.id, operation.job_id
              ) IS DISTINCT FROM false
            LIMIT 1
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'code', 'creation_in_progress'
                )
            );
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-source-retention/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object()
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.guard_dataset_source_deletion(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_dataset_source_auxiliary_retention(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_image_input public.asset_accepted_image_inputs%ROWTYPE;
    v_video_input public.asset_accepted_video_inputs%ROWTYPE;
    v_video_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_manifest record;
    v_refs jsonb;
    v_job_reply jsonb;
    v_job jsonb;
BEGIN
    IF p_dataset_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    -- A pending/running job can still publish against its captured source.
    -- Backfills have no item FK, so scope them by their immutable selection.
    IF EXISTS (
        SELECT 1
        FROM public.asset_media_jobs AS job
        WHERE job.status IN ('pending', 'running')
          AND (
              job.dataset_item_id IN (
                  SELECT source_item.item_id
                  FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
                      AS source_item
              )
              OR (
                  job.kind = 'video_backfill'
                  AND (
                      pg_catalog.lower(job.spec #>> '{selection,dataset_id}')
                          = p_dataset_id::text
                      OR EXISTS (
                          SELECT 1
                          FROM aap_assets_v1.dataset_item_references(
                              p_dataset_id, p_item_id
                          ) AS source_item
                          CROSS JOIN LATERAL pg_catalog.jsonb_array_elements_text(
                              COALESCE(
                                  job.spec #> '{selection,dataset_item_ids}',
                                  '[]'::jsonb
                              )
                          ) AS selected(item_id)
                          WHERE pg_catalog.lower(selected.item_id)
                              = source_item.item_id::text
                      )
                  )
              )
          )
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    FOR v_image_input IN
        SELECT input.*
        FROM public.asset_accepted_image_inputs AS input
        JOIN aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item ON source_item.item_id = input.dataset_item_id
        ORDER BY input.input_id
    LOOP
        -- Labeling pins remain authoritative even when a receipt has expired.
        v_refs := aap_labeling_v1.accepted_image_input_referenced(
            v_image_input.input_id
        );
        IF v_refs IS NULL
           OR v_refs ->> 'schema' IS DISTINCT FROM
                'aap-labeling-accepted-input-retention/v1'
           OR v_refs -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_refs #>> '{data,input_id}' IS DISTINCT FROM
                v_image_input.input_id::text
           OR pg_catalog.jsonb_typeof(v_refs #> '{data,referenced}')
                IS DISTINCT FROM 'boolean' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        IF v_refs #> '{data,referenced}' = 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
            );
        END IF;
        IF v_image_input.status = 'expired' THEN
            CONTINUE;
        END IF;

        v_job_reply := aap_assets_v1._accepted_image_input_job_consistency(
            v_image_input.job_id,
            v_image_input.task_id,
            v_image_input.actor_id,
            v_image_input.project_id
        );
        IF v_job_reply IS NULL
           OR v_job_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-inputs/v1'
           OR v_job_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR pg_catalog.jsonb_typeof(v_job_reply -> 'data')
                IS DISTINCT FROM 'object'
           OR v_job_reply #>> '{data,job_id}' IS DISTINCT FROM
                v_image_input.job_id::text
           OR v_job_reply #>> '{data,task_id}' IS DISTINCT FROM
                v_image_input.task_id::text
           OR v_job_reply #>> '{data,actor_id}' IS DISTINCT FROM
                v_image_input.actor_id::text
           OR v_job_reply #>> '{data,project_id}' IS DISTINCT FROM
                v_image_input.project_id::text
           OR pg_catalog.jsonb_typeof(v_job_reply #> '{data,active}')
                IS DISTINCT FROM 'boolean' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        IF v_job_reply #> '{data,active}' = 'true'::jsonb
           OR (
                v_image_input.status = 'ready'
                AND (v_image_input.retire_after IS NULL
                     OR v_image_input.retire_after > v_now)
           ) OR (
                v_image_input.status = 'materializing'
                AND v_image_input.lease_expires_at > v_now
           ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
            );
        END IF;
    END LOOP;

    -- A preparing image manifest can contain members not yet backed by input
    -- rows. Retain those only while the authoritative Jobs projection is live.
    FOR v_manifest IN
        SELECT manifest.job_id, manifest.actor_id, manifest.project_id,
               manifest.selection, manifest.selection_digest, member.task_id
        FROM public.asset_accepted_image_input_manifest_members AS member
        JOIN public.asset_accepted_image_input_manifests AS manifest
          ON manifest.job_id = member.job_id
        JOIN aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item ON source_item.item_id = member.dataset_item_id
        WHERE manifest.status = 'preparing'
        ORDER BY manifest.job_id, member.ordinal
    LOOP
        v_job_reply := aap_assets_v1._accepted_image_input_manifest_job_scope(
            v_manifest.job_id, v_manifest.actor_id, v_manifest.project_id,
            v_manifest.task_id, v_manifest.selection,
            v_manifest.selection_digest, false
        );
        IF v_job_reply IS NULL
           OR v_job_reply ->> 'schema' IS DISTINCT FROM
                'aap-assets-input-manifest/v1'
           OR v_job_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR pg_catalog.jsonb_typeof(v_job_reply #> '{data,active}')
                IS DISTINCT FROM 'boolean' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        IF v_job_reply #> '{data,active}' = 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
            );
        END IF;
    END LOOP;

    FOR v_video_input IN
        SELECT input.*
        FROM public.asset_accepted_video_inputs AS input
        JOIN aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
          ON source_item.item_id =
             CASE
                 WHEN input.task ->> 'dataset_item_id' ~
                      '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                 THEN (input.task ->> 'dataset_item_id')::uuid
                 ELSE NULL::uuid
             END
        ORDER BY input.input_id
    LOOP
        v_refs := aap_labeling_v1.accepted_video_input_referenced(
            v_video_input.input_id
        );
        IF v_refs IS NULL
           OR v_refs ->> 'schema' IS DISTINCT FROM
                'aap-labeling-video-input-retention/v1'
           OR v_refs -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_refs #>> '{data,input_id}' IS DISTINCT FROM
                v_video_input.input_id::text
           OR pg_catalog.jsonb_typeof(v_refs #> '{data,referenced}')
                IS DISTINCT FROM 'boolean' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        IF v_refs #> '{data,referenced}' = 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
            );
        END IF;
        IF v_video_input.status = 'expired' THEN
            CONTINUE;
        END IF;

        SELECT manifest.* INTO v_video_manifest
        FROM public.asset_accepted_video_manifests AS manifest
        WHERE manifest.job_id = v_video_input.job_id;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        v_job_reply := aap_assets_v1._video_input_job(
            v_video_manifest, v_video_input.task_id, false
        );
        IF v_job_reply IS NULL
           OR aap_assets_v1._video_input_code(
                v_job_reply, 'aap-assets-video-input-execution/v1'
              ) IS NOT NULL
           OR pg_catalog.jsonb_typeof(v_job_reply #> '{data,job}')
                NOT IN ('object', 'null') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        v_job := v_job_reply #> '{data,job}';
        IF v_job <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(v_job -> 'active') IS DISTINCT FROM 'boolean' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        IF (v_job <> 'null'::jsonb AND v_job -> 'active' = 'true'::jsonb)
           OR (
                v_video_input.status = 'ready'
                AND (v_video_input.retire_after IS NULL
                     OR v_video_input.retire_after > v_now)
           ) OR (
                v_video_input.status = 'materializing'
                AND v_video_input.lease_expires_at > v_now
           ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
            );
        END IF;
    END LOOP;

    -- Video manifests can be preparing before a member has an input receipt.
    FOR v_manifest IN
        SELECT manifest.job_id, member.task_id
        FROM public.asset_accepted_video_members AS member
        JOIN public.asset_accepted_video_manifests AS manifest
          ON manifest.job_id = member.job_id
        JOIN aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
          ON source_item.item_id =
             CASE
                 WHEN member.task ->> 'dataset_item_id' ~
                      '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                 THEN (member.task ->> 'dataset_item_id')::uuid
                 ELSE NULL::uuid
             END
        WHERE manifest.status = 'preparing'
        ORDER BY manifest.job_id, member.ordinal
    LOOP
        SELECT manifest.* INTO v_video_manifest
        FROM public.asset_accepted_video_manifests AS manifest
        WHERE manifest.job_id = v_manifest.job_id;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        v_job_reply := aap_assets_v1._video_input_job(
            v_video_manifest, v_manifest.task_id, false
        );
        IF v_job_reply IS NULL
           OR aap_assets_v1._video_input_code(
                v_job_reply, 'aap-assets-video-input-execution/v1'
              ) IS NOT NULL
           OR pg_catalog.jsonb_typeof(v_job_reply #> '{data,job}')
                NOT IN ('object', 'null') THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        v_job := v_job_reply #> '{data,job}';
        IF v_job <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(v_job -> 'active') IS DISTINCT FROM 'boolean' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
        END IF;
        IF v_job <> 'null'::jsonb AND v_job -> 'active' = 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
            );
        END IF;
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-source-retention/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object()
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_busy')
        );
    WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_busy')
        );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_dataset_source_auxiliary_retention(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1._lock_dataset_source_deletion(
    p_dataset_id uuid, p_item_id uuid
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_id uuid; v_scene_id uuid; v_locked_scene_id uuid; v_pending boolean := false;
BEGIN
    IF p_dataset_id IS NULL THEN
        RETURN aap_assets_v1._source_delete_failure('invalid_request');
    END IF;
    IF p_item_id IS NOT NULL THEN
        -- Preserve cooperative append serialization before the stronger FK fence.
        v_id := aap_assets_v1.lock_dataset_reference(p_dataset_id);
        IF v_id IS NULL THEN
            RETURN aap_assets_v1._source_delete_failure('item_missing');
        END IF;
    END IF;
    SELECT dataset.id INTO v_id FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._source_delete_failure('dataset_missing');
    END IF;
    IF p_item_id IS NULL THEN
        PERFORM scene.id FROM public.scenes AS scene
        WHERE scene.dataset_id = p_dataset_id ORDER BY scene.id FOR UPDATE NOWAIT;
        IF EXISTS (
            SELECT 1 FROM public.dataset_items AS item
            JOIN public.scenes AS scene ON scene.id = item.scene_id
            WHERE scene.dataset_id = p_dataset_id
              AND item.dataset_id IS DISTINCT FROM p_dataset_id
        ) THEN
            RETURN aap_assets_v1._source_delete_failure('source_in_use');
        END IF;
        PERFORM item.id FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id ORDER BY item.id FOR UPDATE NOWAIT;
        PERFORM association.id FROM public.project_datasets AS association
        WHERE association.dataset_id = p_dataset_id
        ORDER BY association.id FOR UPDATE NOWAIT;
        PERFORM intent.item_id FROM public.dataset_upload_intents AS intent
        WHERE intent.dataset_id = p_dataset_id
        ORDER BY intent.item_id FOR UPDATE NOWAIT;
    ELSE
        -- Lock Scene before Item, matching the Dataset -> Scene -> Item order
        -- used by whole-dataset deletion and scene writers.
        SELECT item.scene_id INTO v_scene_id
        FROM public.dataset_items AS item
        WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id;
        IF FOUND THEN
            IF v_scene_id IS NOT NULL THEN
                PERFORM scene.id FROM public.scenes AS scene
                WHERE scene.id = v_scene_id FOR UPDATE NOWAIT;
                IF NOT FOUND THEN
                    RETURN aap_assets_v1._source_delete_failure('source_busy');
                END IF;
            END IF;
            SELECT item.id, item.scene_id INTO v_id, v_locked_scene_id
            FROM public.dataset_items AS item
            WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_assets_v1._source_delete_failure('item_missing');
            END IF;
            IF v_locked_scene_id IS DISTINCT FROM v_scene_id THEN
                RETURN aap_assets_v1._source_delete_failure('source_busy');
            END IF;
        ELSE
            SELECT intent.item_id INTO v_id FROM public.dataset_upload_intents AS intent
            WHERE intent.item_id = p_item_id AND intent.dataset_id = p_dataset_id
              AND intent.state = 'pending' FOR UPDATE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_assets_v1._source_delete_failure('item_missing');
            END IF;
            v_pending := true;
        END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-source-delete-scope/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object('pending_upload', v_pending)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._lock_dataset_source_deletion(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1._delete_dataset_source(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid, p_item_id uuid,
    p_bucket text, p_audit_context jsonb
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb; v_role text; v_request jsonb;
    v_saved public.dataset_source_deletion_operations%ROWTYPE;
    v_scope jsonb; v_guard jsonb; v_pending boolean; v_counts jsonb;
    v_association record; v_jobs bigint := 0; v_audit bigint; v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_bucket IS NULL OR pg_catalog.char_length(p_bucket) NOT BETWEEN 3 AND 63
       OR p_bucket !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
       OR p_bucket ~ '^[0-9]+(\.[0-9]+){3}$'
       OR aap_audit_v1.valid_request_context(p_audit_context) IS DISTINCT FROM true THEN
        RETURN aap_assets_v1._source_delete_failure('invalid_request');
    END IF;
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source deletion identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('schema', 'aap-dataset-source-delete/v1', 'ok', false, 'error', v_actor -> 'error');
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap-dataset-source-delete:' || p_operation_id::text, 0));
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id,
        'dataset_id', p_dataset_id, 'item_id', p_item_id, 'cleanup_bucket', p_bucket
    );
    SELECT receipt.* INTO v_saved FROM public.dataset_source_deletion_operations AS receipt
    WHERE receipt.operation_id = p_operation_id FOR UPDATE;
    IF FOUND THEN
        IF v_saved.request IS DISTINCT FROM v_request THEN
            RETURN aap_assets_v1._source_delete_failure('operation_conflict');
        END IF;
        RETURN v_saved.result;
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    IF v_role IS NULL OR v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN aap_assets_v1._source_delete_failure('invalid_account_role');
    END IF;
    IF v_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._source_delete_failure('catalog_manager_required');
    END IF;
    v_scope := aap_assets_v1._lock_dataset_source_deletion(p_dataset_id, p_item_id);
    IF v_scope -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_scope; END IF;
    IF v_scope ->> 'schema' IS DISTINCT FROM 'aap-dataset-source-delete-scope/v1'
       OR pg_catalog.jsonb_typeof(v_scope #> '{data,pending_upload}') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source deletion lock contract mismatch';
    END IF;
    v_pending := (v_scope #>> '{data,pending_upload}')::boolean;
    IF NOT v_pending THEN
        v_guard := aap_learning_v1.guard_dataset_source_deletion(p_dataset_id, p_item_id);
        IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source Learning contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('schema', 'aap-dataset-source-delete/v1', 'ok', false, 'error', v_guard -> 'error');
        END IF;
        v_guard := aap_labeling_v1.guard_dataset_source_deletion(p_dataset_id, p_item_id);
        IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source Labeling contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('schema', 'aap-dataset-source-delete/v1', 'ok', false, 'error', v_guard -> 'error');
        END IF;
    END IF;
    v_guard := aap_jobs_v1.guard_dataset_source_deletion(p_dataset_id);
    IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source Jobs contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('schema', 'aap-dataset-source-delete/v1', 'ok', false, 'error', v_guard -> 'error');
    END IF;
    v_guard := aap_assets_v1.guard_dataset_source_auxiliary_retention(
        p_dataset_id, p_item_id
    );
    IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source Assets retention contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('schema', 'aap-dataset-source-delete/v1', 'ok', false, 'error', v_guard -> 'error');
    END IF;
    IF p_item_id IS NULL THEN
        FOR v_association IN SELECT * FROM aap_assets_v1.dataset_association_references(p_dataset_id) LOOP
            v_jobs := v_jobs + aap_jobs_v1.cancel_dataset_task_creation(p_dataset_id, v_association.project_id, v_association.association_id);
        END LOOP;
        PERFORM aap_labeling_v1.detach_deleted_dataset_attribution(p_dataset_id);
    END IF;
    v_counts := aap_assets_v1._delete_dataset_source_records(p_operation_id, p_dataset_id, p_item_id, p_bucket);
    v_audit := aap_audit_v1.record_dataset_source_deletion(
        p_operation_id, p_actor_id, p_dataset_id, p_item_id,
        (v_counts ->> 'deleted_items')::bigint, (v_counts ->> 'deleted_scenes')::bigint,
        (v_counts ->> 'deleted_associations')::bigint, (v_counts ->> 'discarded_uploads')::bigint,
        v_jobs, (v_counts ->> 'queued_storage_deletions')::bigint, p_audit_context
    );
    IF v_audit IS NULL OR v_audit <= 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid source deletion audit receipt';
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-source-delete/v1', 'ok', true,
        'data', v_counts || pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id, 'actor_id', p_actor_id, 'dataset_id', p_dataset_id,
            'item_id', p_item_id, 'cleanup_bucket', p_bucket, 'audit_id', v_audit,
            'deleted_datasets', CASE WHEN p_item_id IS NULL THEN 1 ELSE 0 END,
            'cancelled_creation_jobs', v_jobs
        )
    );
    INSERT INTO public.dataset_source_deletion_operations(operation_id, actor_id, dataset_id, item_id, request, result)
    VALUES (p_operation_id, p_actor_id, p_dataset_id, p_item_id, v_request, v_result);
    RETURN v_result;
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._source_delete_failure('source_busy');
    WHEN foreign_key_violation THEN RETURN aap_assets_v1._source_delete_failure('source_in_use');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._delete_dataset_source(uuid, uuid, uuid, uuid, text, jsonb) FROM PUBLIC;
-- aap-statement-break
