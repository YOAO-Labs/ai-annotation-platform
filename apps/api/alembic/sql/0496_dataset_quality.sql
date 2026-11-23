CREATE FUNCTION yoao_assets_v1._dataset_quality_manage(p_actor uuid, p_dataset uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.datasets AS d
        CROSS JOIN yoao_identity_v1.read_actor_facts(p_actor) AS a
        WHERE d.id = p_dataset AND a.is_active
          AND (a.platform_role = 'super_admin'
               OR (a.platform_role = 'project_admin' AND d.created_by = p_actor))
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_manage(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_writable(p_dataset uuid, p_revision integer)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.datasets AS d
                   WHERE d.id = p_dataset AND d.archived_at IS NULL
                     AND (p_revision IS NULL OR d.lifecycle_revision = p_revision))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_writable(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_sources_visible(p_actor uuid, p_run uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM (
            SELECT DISTINCT r.source_id FROM public.dataset_quality_results AS r
            WHERE r.run_id = p_run AND r.source_id IS NOT NULL
        ) AS s WHERE yoao_assets_v1.dataset_source_visible(p_actor,s.source_id) IS NOT TRUE
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_sources_visible(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_run_visible(p_actor uuid, p_run uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.dataset_quality_runs AS r WHERE r.id = p_run
          AND yoao_assets_v1._dataset_quality_manage(p_actor,r.dataset_id)
          AND yoao_assets_v1._dataset_quality_sources_visible(p_actor,r.id)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_run_visible(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_job_visible(p_actor uuid, p_kind text, p_reference text)
RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_id uuid;
BEGIN
    IF p_reference IS NULL OR p_reference !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN false;
    END IF;
    v_id := p_reference::uuid;
    RETURN yoao_assets_v1._dataset_quality_run_visible(p_actor,CASE p_kind
        WHEN 'dataset_quality' THEN v_id
        WHEN 'dataset_quality_review' THEN (
            SELECT b.quality_run_id FROM public.dataset_quality_review_batches AS b WHERE b.id = v_id
        ) END);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_job_visible(uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_run_receipt(p_run uuid, p_execution boolean)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id, 'dataset_id', r.dataset_id, 'async_job_id', r.async_job_id,
        'rule_version', r.rule_version, 'config', r.config, 'status', r.status,
        'total', r.total, 'processed', r.processed, 'summary', r.summary,
        'error_code', r.error_code, 'created_at', r.created_at, 'completed_at', r.completed_at
    ) || CASE WHEN p_execution THEN pg_catalog.jsonb_build_object(
        'generation', r.generation, 'lease_until', r.lease_until,
        'authority', pg_catalog.jsonb_build_object(
            'actor_id', r.authority->>'actor_id', 'api_key_id', r.authority->>'api_key_id',
            'dataset_lifecycle_revision', COALESCE(r.authority->'dataset_lifecycle_revision','1'::jsonb),
            'storage_maintenance', COALESCE(r.authority->'storage_maintenance','{}'::jsonb)
        )
    ) ELSE '{}'::jsonb END
    FROM public.dataset_quality_runs AS r WHERE r.id = p_run
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_run_receipt(uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_run(p_dataset uuid, p_run uuid, p_actor uuid, p_lock boolean)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT yoao_assets_v1._dataset_quality_manage(p_actor,p_dataset)
       OR NOT yoao_assets_v1._dataset_quality_sources_visible(p_actor,p_run) THEN
        RETURN NULL;
    END IF;
    IF p_lock THEN
        PERFORM 1 FROM public.dataset_quality_runs AS r WHERE r.id = p_run AND r.dataset_id = p_dataset FOR UPDATE;
    ELSE
        PERFORM 1 FROM public.dataset_quality_runs AS r WHERE r.id = p_run AND r.dataset_id = p_dataset;
    END IF;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN yoao_assets_v1._dataset_quality_run_receipt(p_run,true);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_run(uuid,uuid,uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_run_page(p_dataset uuid, p_actor uuid, p_limit integer, p_offset integer)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH visible AS MATERIALIZED (
        SELECT r.id, r.created_at FROM public.dataset_quality_runs AS r
        WHERE r.dataset_id = p_dataset AND yoao_assets_v1._dataset_quality_manage(p_actor,p_dataset)
          AND yoao_assets_v1._dataset_quality_sources_visible(p_actor,r.id)
    ), page AS (
        SELECT * FROM visible ORDER BY created_at DESC, id DESC
        LIMIT LEAST(p_limit,100) OFFSET p_offset
    )
    SELECT pg_catalog.jsonb_build_object(
        'total', (SELECT pg_catalog.count(*) FROM visible),
        'items', COALESCE(pg_catalog.jsonb_agg(yoao_assets_v1._dataset_quality_run_receipt(page.id,false)
            ORDER BY page.created_at DESC,page.id DESC),'[]'::jsonb)
    ) FROM page
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_run_page(uuid,uuid,integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_begin(p_dataset uuid, p_actor uuid, p_config jsonb, p_lifecycle integer)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run uuid := pg_catalog.gen_random_uuid();
BEGIN
    IF NOT yoao_assets_v1._dataset_quality_manage(p_actor,p_dataset)
       OR NOT yoao_assets_v1._dataset_quality_writable(p_dataset,p_lifecycle) THEN
        RETURN pg_catalog.jsonb_build_object('error','dataset_not_found');
    END IF;
    IF EXISTS (SELECT 1 FROM public.dataset_quality_runs AS r WHERE r.dataset_id = p_dataset
               AND r.status IN ('pending','running')) THEN
        RETURN pg_catalog.jsonb_build_object('error','active');
    END IF;
    WITH members AS MATERIALIZED (
        SELECT i.id, i.file_name, i.file_type, a.id AS asset_id, a.verification_state,
               a.checksum_value, a.size_bytes, ref.source_id
        FROM public.dataset_items AS i
        LEFT JOIN public.dataset_assets AS a ON a.dataset_item_id = i.id
        LEFT JOIN public.dataset_references AS ref ON ref.dataset_item_id = i.id
        WHERE i.dataset_id = p_dataset
        ORDER BY i.id LIMIT 10001
    ), new_run AS (
        INSERT INTO public.dataset_quality_runs (
            id,dataset_id,authority,rule_version,config,status,total,processed,summary,generation
        )
        SELECT v_run,p_dataset,pg_catalog.jsonb_build_object('dataset_lifecycle_revision',p_lifecycle),
               'image-quality-1',p_config,'pending',pg_catalog.count(*),0,'{}'::jsonb,0
        FROM members HAVING pg_catalog.count(*) <= 10000
        RETURNING id
    )
    INSERT INTO public.dataset_quality_results (
        id,run_id,item_id,asset_id,source_id,file_name,sha256,size_bytes,state,reason_code,
        bytes_verified,findings,review_state,review_note,review_revision
    )
    SELECT pg_catalog.gen_random_uuid(),r.id,m.id,m.asset_id,m.source_id,m.file_name,
           CASE WHEN m.verification_state = 'verified' THEN m.checksum_value END,
           CASE WHEN m.verification_state = 'verified' THEN m.size_bytes END,
           'pending', CASE WHEN m.file_type <> 'image' THEN 'media_not_supported'
                           WHEN m.verification_state IS DISTINCT FROM 'verified' THEN 'content_unverified' END,
           false,'[]'::jsonb,'open','',0
    FROM members AS m CROSS JOIN new_run AS r;
    RETURN COALESCE(yoao_assets_v1._dataset_quality_run_receipt(v_run,true),
                    pg_catalog.jsonb_build_object('error','too_many_members'));
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_begin(uuid,uuid,jsonb,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_other_active(p_dataset uuid, p_run uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.dataset_quality_runs AS r
                   WHERE r.dataset_id = p_dataset AND r.id <> p_run AND r.status IN ('pending','running'))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_other_active(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_maintenance_members(p_run uuid)
RETURNS TABLE(source_id uuid, location_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT DISTINCT r.source_id,s.location_id FROM public.dataset_quality_results AS r
    LEFT JOIN public.dataset_sources AS s ON s.id = r.source_id
    WHERE r.run_id = p_run AND r.source_id IS NOT NULL ORDER BY r.source_id,s.location_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_maintenance_members(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_lock_locations(p_run uuid, p_locations uuid[])
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM 1 FROM public.storage_locations AS l
    WHERE l.id = ANY(p_locations)
      AND (EXISTS (SELECT 1 FROM yoao_assets_v1.dataset_quality_maintenance_members(p_run) AS m WHERE m.location_id = l.id)
           OR EXISTS (SELECT 1 FROM public.dataset_quality_runs AS r
                      WHERE r.id = p_run AND (r.authority->'storage_maintenance') ? l.id::text))
    ORDER BY l.id FOR SHARE NOWAIT;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_lock_locations(uuid,uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_job_matches(p_job uuid, p_kind text, p_task text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM yoao_jobs_v1.read_job(p_job) AS j
                   WHERE j.kind = p_kind AND j.celery_task_id IS NOT DISTINCT FROM p_task
                     AND j.status IN ('pending','running'))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_job_matches(uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_run_attempt(
    p_run uuid, p_job uuid, p_task text, p_generation integer, p_mode text, p_now timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_quality_runs%ROWTYPE;
BEGIN
    IF p_mode = 'recover' THEN
        SELECT r.* INTO v_run FROM public.dataset_quality_runs AS r
        WHERE r.id = p_run AND r.status IN ('pending','running') AND r.lease_until <= p_now
        FOR UPDATE SKIP LOCKED;
    ELSE
        SELECT r.* INTO v_run FROM public.dataset_quality_runs AS r
        WHERE CASE WHEN p_mode = 'signal' THEN r.async_job_id = p_job ELSE r.id = p_run END
        FOR UPDATE;
    END IF;
    IF NOT FOUND OR v_run.status NOT IN ('pending','running') THEN
        RETURN NULL;
    END IF;
    IF NOT (CASE p_mode
        WHEN 'recover' THEN true
        WHEN 'signal' THEN yoao_assets_v1._dataset_quality_job_matches(v_run.async_job_id,'dataset_quality',p_task)
        WHEN 'claim' THEN v_run.status = 'pending'
            AND yoao_assets_v1._dataset_quality_job_matches(v_run.async_job_id,'dataset_quality',p_task)
        WHEN 'worker' THEN v_run.async_job_id = p_job AND v_run.generation = p_generation AND v_run.lease_until > p_now
            AND yoao_assets_v1._dataset_quality_job_matches(p_job,'dataset_quality',p_task)
        ELSE false END) IS TRUE THEN
        RETURN NULL;
    END IF;
    RETURN yoao_assets_v1._dataset_quality_run_receipt(v_run.id,true);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_run_attempt(uuid,uuid,text,integer,text,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_expired_runs(p_now timestamptz)
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id FROM public.dataset_quality_runs AS r
    WHERE r.status IN ('pending','running') AND r.lease_until <= p_now LIMIT 50
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_expired_runs(timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_attach_job(
    p_run uuid, p_generation integer, p_previous_job uuid, p_job uuid, p_actor uuid, p_key uuid,
    p_key_proof text, p_lifecycle integer, p_maintenance jsonb, p_lease_until timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.dataset_quality_runs AS r SET
        async_job_id = p_job, status = 'pending', error_code = NULL, completed_at = NULL,
        generation = r.generation + 1, lease_until = p_lease_until,
        authority = pg_catalog.jsonb_build_object('actor_id',p_actor,'api_key_id',p_key,'key_proof',p_key_proof,
            'dataset_lifecycle_revision',p_lifecycle,'storage_maintenance',p_maintenance)
    WHERE r.id = p_run AND r.generation = p_generation AND r.async_job_id IS NOT DISTINCT FROM p_previous_job
      AND r.status <> 'completed' AND yoao_assets_v1._dataset_quality_manage(p_actor,r.dataset_id)
      AND yoao_assets_v1._dataset_quality_writable(r.dataset_id,p_lifecycle)
      AND yoao_assets_v1._dataset_quality_sources_visible(p_actor,r.id)
      AND EXISTS (SELECT 1 FROM yoao_jobs_v1.read_job(p_job) AS j
                  WHERE j.kind = 'dataset_quality' AND j.status = 'pending' AND j.user_id = p_actor
                    AND j.payload->>'run_id' = p_run::text AND j.payload->>'dataset_id' = r.dataset_id::text);
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN yoao_assets_v1._dataset_quality_run_receipt(p_run,true);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_attach_job(uuid,integer,uuid,uuid,uuid,uuid,text,integer,jsonb,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_key_matches(p_run uuid, p_generation integer, p_proof text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.dataset_quality_runs AS r
                   WHERE r.id = p_run AND r.generation = p_generation
                     AND r.authority->>'key_proof' IS NOT DISTINCT FROM p_proof)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_key_matches(uuid,integer,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_duplicate_groups(p_run uuid)
RETURNS TABLE(sha text, size_bytes bigint, n bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.sha256::text,r.size_bytes,pg_catalog.count(*)
    FROM public.dataset_quality_results AS r WHERE r.run_id = p_run AND r.bytes_verified
    GROUP BY r.sha256,r.size_bytes HAVING pg_catalog.count(*) > 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_duplicate_groups(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_summary(p_run uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH groups AS MATERIALIZED (SELECT g.n FROM yoao_assets_v1._dataset_quality_duplicate_groups(p_run) AS g)
    SELECT pg_catalog.jsonb_build_object(
        'processed_count',pg_catalog.count(*) FILTER (WHERE r.state IN ('checked','skipped')),
        'checked_count',pg_catalog.count(*) FILTER (WHERE r.state = 'checked'),
        'skipped_count',pg_catalog.count(*) FILTER (WHERE r.state = 'skipped'),
        'issue_count',pg_catalog.count(*) FILTER (WHERE r.findings <> '[]'::jsonb),
        'duplicate_group_count',(SELECT pg_catalog.count(*) FROM groups),
        'duplicate_member_count',(SELECT COALESCE(pg_catalog.sum(g.n),0) FROM groups AS g))
    FROM public.dataset_quality_results AS r WHERE r.run_id = p_run
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_summary(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_run_transition(
    p_run uuid, p_job uuid, p_generation integer, p_task text, p_action text,
    p_now timestamptz, p_lease_until timestamptz, p_error text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_action NOT IN ('claim','redispatch','completed','cancelled','failed') OR p_action IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset quality transition';
    END IF;
    UPDATE public.dataset_quality_runs AS r SET
        status = CASE p_action WHEN 'claim' THEN 'running' WHEN 'redispatch' THEN 'pending' ELSE p_action END,
        generation = r.generation + CASE WHEN p_action = 'claim' THEN 0 ELSE 1 END,
        lease_until = CASE WHEN p_action IN ('claim','redispatch') THEN COALESCE(p_lease_until,r.lease_until) END,
        error_code = CASE WHEN p_action IN ('claim','redispatch') THEN r.error_code ELSE p_error END,
        completed_at = CASE WHEN p_action IN ('claim','redispatch') THEN r.completed_at ELSE p_now END,
        summary = CASE WHEN p_action IN ('claim','redispatch') THEN r.summary ELSE yoao_assets_v1._dataset_quality_summary(p_run) END
    WHERE r.id = p_run AND r.async_job_id IS NOT DISTINCT FROM p_job AND r.generation = p_generation
      AND r.status IN ('pending','running') AND (p_action <> 'claim' OR r.status = 'pending')
      AND (p_action IN ('cancelled','failed') OR yoao_assets_v1._dataset_quality_writable(
            r.dataset_id,COALESCE((r.authority->>'dataset_lifecycle_revision')::integer,1)))
      AND (p_action <> 'completed' OR NOT EXISTS (SELECT 1 FROM public.dataset_quality_results AS x
                                                 WHERE x.run_id = r.id AND x.state = 'pending'))
      AND (CASE WHEN p_action IN ('claim','redispatch') OR p_task IS NOT NULL
             THEN yoao_assets_v1._dataset_quality_job_matches(p_job,'dataset_quality',p_task) ELSE true END);
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN yoao_assets_v1._dataset_quality_run_receipt(p_run,true);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_run_transition(uuid,uuid,integer,text,text,timestamptz,timestamptz,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_reconcile(p_job uuid, p_cancelled boolean, p_now timestamptz)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.dataset_quality_runs AS r SET
        status = CASE WHEN p_cancelled THEN 'cancelled' ELSE 'failed' END,
        error_code = CASE WHEN p_cancelled THEN 'cancelled' ELSE 'worker_failed' END,
        completed_at = p_now, lease_until = NULL, generation = r.generation + 1
    WHERE r.async_job_id = p_job AND r.status IN ('pending','running')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_reconcile(uuid,boolean,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_next_result(p_run uuid)
RETURNS TABLE(id uuid, item_id uuid, asset_id uuid, sha256 text, size_bytes bigint, reason_code text, node_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id,r.item_id,r.asset_id,r.sha256::text,r.size_bytes,r.reason_code::text,
           CASE WHEN r.reason_code IS NULL THEN s.config->>'node_id' END
    FROM public.dataset_quality_results AS r
    LEFT JOIN public.dataset_sources AS s ON s.id = r.source_id
    WHERE r.run_id = p_run AND r.state = 'pending' ORDER BY r.id LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_next_result(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_record_result(
    p_run uuid, p_job uuid, p_generation integer, p_task text, p_result uuid, p_observed_at timestamptz,
    p_state text, p_reason text, p_bytes_verified boolean, p_findings jsonb, p_width integer,
    p_height integer, p_lease_until timestamptz
)
RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_processed integer;
BEGIN
    UPDATE public.dataset_quality_runs AS r SET processed = r.processed + 1, lease_until = p_lease_until
    WHERE r.id = p_run AND r.async_job_id = p_job AND r.generation = p_generation
      AND r.status IN ('pending','running') AND r.lease_until > p_observed_at
      AND yoao_assets_v1._dataset_quality_job_matches(p_job,'dataset_quality',p_task)
      AND p_state IN ('checked','skipped')
      AND yoao_assets_v1._dataset_quality_writable(
            r.dataset_id,COALESCE((r.authority->>'dataset_lifecycle_revision')::integer,1))
      AND EXISTS (SELECT 1 FROM public.dataset_quality_results AS x
                  WHERE x.run_id = p_run AND x.id = p_result AND x.state = 'pending')
    RETURNING r.processed INTO v_processed;
    IF NOT FOUND THEN RETURN NULL; END IF;
    UPDATE public.dataset_quality_results AS x SET
        state = p_state, reason_code = p_reason, bytes_verified = p_bytes_verified, findings = p_findings,
        width = p_width, height = p_height, observed_at = p_observed_at
    WHERE x.run_id = p_run AND x.id = p_result AND x.state = 'pending';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'dataset quality cursor changed';
    END IF;
    RETURN v_processed;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_record_result(uuid,uuid,integer,text,uuid,timestamptz,text,text,boolean,jsonb,integer,integer,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_result_matches(
    p_run uuid, p_finding text, p_review text, p_sha text, p_results uuid[]
)
RETURNS TABLE(result_id uuid, file_name text, duplicate_count bigint, historical boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id,r.file_name::text,COALESCE(g.n,0),
           i.id IS NULL OR a.id IS DISTINCT FROM r.asset_id OR a.checksum_value IS DISTINCT FROM r.sha256
    FROM public.dataset_quality_results AS r
    LEFT JOIN yoao_assets_v1._dataset_quality_duplicate_groups(p_run) AS g
        ON r.bytes_verified AND r.sha256 = g.sha AND r.size_bytes = g.size_bytes
    LEFT JOIN public.dataset_items AS i ON i.id = r.item_id
    LEFT JOIN public.dataset_assets AS a ON a.dataset_item_id = i.id
    WHERE r.run_id = p_run
      AND (p_results IS NULL OR r.id = ANY(p_results))
      AND (NULLIF(p_review,'') IS NULL OR r.review_state = p_review)
      AND (NULLIF(p_sha,'') IS NULL OR (r.bytes_verified AND r.sha256 = p_sha))
      AND CASE p_finding
          WHEN 'exact_duplicate' THEN g.n > 1
          WHEN 'skipped' THEN r.state = 'skipped'
          ELSE NULLIF(p_finding,'') IS NULL OR r.findings @> pg_catalog.jsonb_build_array(p_finding)
      END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_result_matches(uuid,text,text,text,uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_evidence(p_run uuid, p_result uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id',r.id,'run_id',r.run_id,'item_id',r.item_id,'asset_id',r.asset_id,'source_id',r.source_id,
        'file_name',r.file_name,'sha256',r.sha256,'size_bytes',r.size_bytes,'state',r.state,
        'reason_code',r.reason_code,'bytes_verified',r.bytes_verified,'findings',r.findings,'width',r.width,
        'height',r.height,'observed_at',r.observed_at,'review_revision',r.review_revision,'review_state',r.review_state
    )
    FROM public.dataset_quality_results AS r WHERE r.run_id = p_run AND r.id = p_result
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_evidence(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_result_page(
    p_run uuid, p_actor uuid, p_limit integer, p_offset integer, p_finding text, p_review text, p_sha text
)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH visible AS MATERIALIZED (
        SELECT r.id FROM public.dataset_quality_runs AS r WHERE r.id = p_run
          AND yoao_assets_v1._dataset_quality_manage(p_actor,r.dataset_id)
          AND yoao_assets_v1._dataset_quality_sources_visible(p_actor,r.id)
    ), matches AS MATERIALIZED (
        SELECT m.* FROM visible AS r
        CROSS JOIN LATERAL yoao_assets_v1._dataset_quality_result_matches(r.id,p_finding,p_review,p_sha,NULL) AS m
    ), page AS (
        SELECT * FROM matches ORDER BY file_name,result_id LIMIT LEAST(p_limit,100) OFFSET p_offset
    )
    SELECT pg_catalog.jsonb_build_object(
        'total',(SELECT pg_catalog.count(*) FROM matches),
        'items',COALESCE(pg_catalog.jsonb_agg(
            (yoao_assets_v1._dataset_quality_evidence(p_run,r.id) - 'source_id')
            || pg_catalog.jsonb_build_object(
                'review_note',r.review_note,'reviewed_by',r.reviewed_by,'reviewed_at',r.reviewed_at,
                'duplicate_count',page.duplicate_count,'stale',page.historical
            ) ORDER BY page.file_name,page.result_id
        ),'[]'::jsonb)
    ) FROM page JOIN public.dataset_quality_results AS r ON r.id = page.result_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_result_page(uuid,uuid,integer,integer,text,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_lock_result(p_run uuid, p_result uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM 1 FROM public.dataset_quality_results AS r WHERE r.run_id = p_run AND r.id = p_result FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN yoao_assets_v1._dataset_quality_evidence(p_run,p_result);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_lock_result(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_apply_review(
    p_run uuid, p_result uuid, p_actor uuid, p_revision integer, p_state text, p_note text, p_now timestamptz
)
RETURNS integer
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.dataset_quality_results AS r SET
        review_state = p_state, review_note = p_note, review_revision = r.review_revision + 1,
        reviewed_by = p_actor, reviewed_at = p_now
    WHERE r.run_id = p_run AND r.id = p_result AND r.state <> 'pending' AND r.review_revision = p_revision
      AND p_state IN ('open','confirmed','dismissed') AND p_note <> '' AND pg_catalog.length(p_note) <= 2000
      AND EXISTS (SELECT 1 FROM public.dataset_quality_runs AS q WHERE q.id = p_run
                  AND yoao_assets_v1._dataset_quality_manage(p_actor,q.dataset_id)
                  AND yoao_assets_v1._dataset_quality_writable(q.dataset_id,NULL)
                  AND yoao_assets_v1._dataset_quality_sources_visible(p_actor,q.id))
    RETURNING r.review_revision
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_apply_review(uuid,uuid,uuid,integer,text,text,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_review_visible(p_actor uuid, p_creator uuid, p_status text)
RETURNS boolean
LANGUAGE sql IMMUTABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_status <> 'preview' OR p_creator = p_actor
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_review_visible(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_review_receipt(p_batch uuid, p_execution boolean)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id',b.id,'quality_run_id',b.quality_run_id,'created_by',b.created_by,'async_job_id',b.async_job_id,
        'selection',b.selection,'proof',b.proof,'scope_digest',b.scope_digest,'decision',b.decision,'note',b.note,
        'status',b.status,'total',b.total,'actionable_count',b.actionable_count,'historical_count',b.historical_count,
        'processed',b.processed,'summary',b.summary,'error_code',b.error_code,'created_at',b.created_at,
        'expires_at',b.expires_at,'completed_at',b.completed_at
    ) || CASE WHEN p_execution THEN pg_catalog.jsonb_build_object(
        'dataset_id',r.dataset_id,'generation',b.generation,'lease_until',b.lease_until,
        'authority',pg_catalog.jsonb_build_object(
            'actor_id',b.authority->>'actor_id','api_key_id',b.authority->>'api_key_id',
            'dataset_lifecycle_revision',COALESCE(b.authority->'dataset_lifecycle_revision','1'::jsonb)
        )
    ) ELSE '{}'::jsonb END
    FROM public.dataset_quality_review_batches AS b
    JOIN public.dataset_quality_runs AS r ON r.id = b.quality_run_id
    WHERE b.id = p_batch
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_review_receipt(uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_batch(p_run uuid, p_batch uuid, p_actor uuid, p_lock boolean)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT yoao_assets_v1._dataset_quality_run_visible(p_actor,p_run) THEN
        RETURN NULL;
    END IF;
    IF p_lock THEN
        PERFORM 1 FROM public.dataset_quality_review_batches AS b WHERE b.id = p_batch AND b.quality_run_id = p_run
          AND yoao_assets_v1._dataset_quality_review_visible(p_actor,b.created_by,b.status) FOR UPDATE;
    ELSE
        PERFORM 1 FROM public.dataset_quality_review_batches AS b WHERE b.id = p_batch AND b.quality_run_id = p_run
          AND yoao_assets_v1._dataset_quality_review_visible(p_actor,b.created_by,b.status);
    END IF;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN yoao_assets_v1._dataset_quality_review_receipt(p_batch,true);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_batch(uuid,uuid,uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_batch_page(p_run uuid, p_actor uuid, p_limit integer, p_offset integer)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH visible AS MATERIALIZED (
        SELECT b.id,b.created_at FROM public.dataset_quality_review_batches AS b
        WHERE b.quality_run_id = p_run AND yoao_assets_v1._dataset_quality_run_visible(p_actor,p_run)
          AND yoao_assets_v1._dataset_quality_review_visible(p_actor,b.created_by,b.status)
    ), page AS (
        SELECT * FROM visible ORDER BY created_at DESC,id DESC LIMIT LEAST(p_limit,100) OFFSET p_offset
    )
    SELECT pg_catalog.jsonb_build_object(
        'total',(SELECT pg_catalog.count(*) FROM visible),
        'items',COALESCE(pg_catalog.jsonb_agg(yoao_assets_v1._dataset_quality_review_receipt(page.id,false)
            ORDER BY page.created_at DESC,page.id DESC),'[]'::jsonb)
    ) FROM page
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_batch_page(uuid,uuid,integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_member_page(
    p_run uuid, p_batch uuid, p_actor uuid, p_limit integer, p_offset integer
)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object('total',b.total,'items',COALESCE((
        SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
            'id',m.id,'result_id',m.result_id,'snapshot',m.snapshot,'state',m.state,
            'reason_code',m.reason_code,'applied_revision',m.applied_revision,'processed_at',m.processed_at
        ) ORDER BY m.id)
        FROM (
            SELECT x.id,x.result_id,x.snapshot,x.state,x.reason_code,x.applied_revision,x.processed_at
            FROM public.dataset_quality_review_members AS x WHERE x.batch_id = b.id
            ORDER BY x.id LIMIT LEAST(p_limit,100) OFFSET p_offset
        ) AS m
    ),'[]'::jsonb))
    FROM public.dataset_quality_review_batches AS b
    WHERE b.id = p_batch AND b.quality_run_id = p_run
      AND yoao_assets_v1._dataset_quality_run_visible(p_actor,p_run)
      AND yoao_assets_v1._dataset_quality_review_visible(p_actor,b.created_by,b.status)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_member_page(uuid,uuid,uuid,integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_prepare_preview(p_run uuid, p_actor uuid, p_now timestamptz)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT yoao_assets_v1._dataset_quality_run_visible(p_actor,p_run) THEN
        RETURN false;
    END IF;
    DELETE FROM public.dataset_quality_review_batches AS b
    WHERE b.quality_run_id = p_run AND b.created_by = p_actor AND b.status = 'preview' AND b.expires_at <= p_now;
    RETURN (SELECT pg_catalog.count(*) < 20 FROM public.dataset_quality_review_batches AS b
            WHERE b.quality_run_id = p_run AND b.created_by = p_actor AND b.status = 'preview');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_prepare_preview(uuid,uuid,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_preview_rows(
    p_run uuid, p_actor uuid, p_finding text, p_review text, p_sha text, p_results uuid[]
)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT yoao_assets_v1._dataset_quality_evidence(p_run,m.result_id)
           || pg_catalog.jsonb_build_object('duplicate_count',m.duplicate_count,'historical',m.historical)
    FROM yoao_assets_v1._dataset_quality_result_matches(p_run,p_finding,p_review,p_sha,p_results) AS m
    WHERE yoao_assets_v1._dataset_quality_run_visible(p_actor,p_run)
    ORDER BY m.result_id LIMIT 10001
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_preview_rows(uuid,uuid,text,text,text,uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_create_preview(
    p_run uuid, p_actor uuid, p_revision integer, p_selection jsonb, p_digest text,
    p_decision text, p_note text, p_now timestamptz, p_expires timestamptz, p_members jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_batch uuid := pg_catalog.gen_random_uuid();
    v_total integer := pg_catalog.jsonb_array_length(p_members);
    v_count integer;
BEGIN
    IF v_total NOT BETWEEN 1 AND 10000 OR p_decision NOT IN ('open','confirmed','dismissed')
       OR NOT yoao_assets_v1._dataset_quality_run_visible(p_actor,p_run) THEN
        RETURN NULL;
    END IF;
    WITH members AS MATERIALIZED (
        SELECT r.id AS result_id, m.value->'snapshot' AS snapshot,
               m.value->'snapshot'->>'state' = 'pending' AS blocked,
               (m.value->'snapshot'->>'historical')::boolean AS historical
        FROM pg_catalog.jsonb_array_elements(p_members) AS m(value)
        JOIN public.dataset_quality_results AS r ON r.id = (m.value->>'result_id')::uuid AND r.run_id = p_run
    ), counts AS (
        SELECT pg_catalog.count(*) FILTER (WHERE m.blocked) AS blocked,
               pg_catalog.count(*) FILTER (WHERE m.historical) AS historical
        FROM members AS m
    ), created AS (
        INSERT INTO public.dataset_quality_review_batches (
            id,quality_run_id,created_by,authority,selection,proof,scope_digest,decision,note,status,total,
            actionable_count,historical_count,processed,summary,generation,expires_at
        )
        SELECT v_batch,r.id,p_actor,pg_catalog.jsonb_build_object('dataset_lifecycle_revision',p_revision),
               p_selection,pg_catalog.jsonb_build_object('rule_version',r.rule_version,'config',r.config),
               p_digest,p_decision,p_note,'preview',v_total,v_total - c.blocked,c.historical,c.blocked,
               pg_catalog.jsonb_build_object('applied_count',0,'conflict_count',0,'blocked_count',c.blocked,
                   'processed_count',c.blocked,'partial',c.blocked > 0),0,p_expires
        FROM public.dataset_quality_runs AS r CROSS JOIN counts AS c
        WHERE r.id = p_run AND r.status NOT IN ('pending','running')
          AND yoao_assets_v1._dataset_quality_writable(r.dataset_id,p_revision)
        RETURNING id
    )
    INSERT INTO public.dataset_quality_review_members (id,batch_id,result_id,snapshot,state,reason_code,processed_at)
    SELECT pg_catalog.gen_random_uuid(),b.id,m.result_id,m.snapshot,CASE WHEN m.blocked THEN 'blocked' ELSE 'pending' END,
           CASE WHEN m.blocked THEN 'result_pending' END,CASE WHEN m.blocked THEN p_now END
    FROM members AS m CROSS JOIN created AS b;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count <> v_total THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'dataset quality preview membership changed';
    END IF;
    RETURN yoao_assets_v1._dataset_quality_review_receipt(v_batch,true);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_create_preview(uuid,uuid,integer,jsonb,text,text,text,timestamptz,timestamptz,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_attach_job(
    p_batch uuid, p_generation integer, p_previous_job uuid, p_job uuid, p_actor uuid, p_key uuid,
    p_key_proof text, p_lifecycle integer, p_lease_until timestamptz, p_digest text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.dataset_quality_review_batches AS b SET
        async_job_id = p_job,status = 'pending',error_code = NULL,completed_at = NULL,
        generation = b.generation + 1,lease_until = p_lease_until,
        authority = pg_catalog.jsonb_build_object('actor_id',p_actor,'api_key_id',p_key,'key_proof',p_key_proof,
            'dataset_lifecycle_revision',p_lifecycle)
    WHERE b.id = p_batch AND b.generation = p_generation AND b.async_job_id IS NOT DISTINCT FROM p_previous_job
      AND b.scope_digest = p_digest
      AND ((b.status = 'preview' AND b.created_by = p_actor AND b.actionable_count > 0)
           OR (b.status IN ('failed','cancelled') AND b.processed < b.total))
      AND yoao_assets_v1._dataset_quality_run_visible(p_actor,b.quality_run_id)
      AND EXISTS (SELECT 1 FROM public.dataset_quality_runs AS r
                  WHERE r.id = b.quality_run_id AND yoao_assets_v1._dataset_quality_writable(r.dataset_id,p_lifecycle))
      AND EXISTS (SELECT 1 FROM yoao_jobs_v1.read_job(p_job) AS j
                  WHERE j.kind = 'dataset_quality_review' AND j.status = 'pending' AND j.user_id = p_actor
                    AND j.payload->>'batch_id' = p_batch::text AND j.payload->>'run_id' = b.quality_run_id::text);
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN yoao_assets_v1._dataset_quality_review_receipt(p_batch,true);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_attach_job(uuid,integer,uuid,uuid,uuid,uuid,text,integer,timestamptz,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_key_matches(p_batch uuid, p_generation integer, p_proof text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.dataset_quality_review_batches AS b
                   WHERE b.id = p_batch AND b.generation = p_generation
                     AND b.authority->>'key_proof' IS NOT DISTINCT FROM p_proof)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_key_matches(uuid,integer,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_attempt(
    p_batch uuid, p_job uuid, p_task text, p_generation integer, p_mode text, p_now timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_batch public.dataset_quality_review_batches%ROWTYPE;
BEGIN
    IF p_mode = 'recover' THEN
        SELECT b.* INTO v_batch FROM public.dataset_quality_review_batches AS b
        WHERE b.id = p_batch AND b.status IN ('pending','running') AND b.lease_until <= p_now
        FOR UPDATE SKIP LOCKED;
    ELSE
        SELECT b.* INTO v_batch FROM public.dataset_quality_review_batches AS b
        WHERE CASE WHEN p_mode = 'signal' THEN b.async_job_id = p_job ELSE b.id = p_batch END
        FOR UPDATE;
    END IF;
    IF NOT FOUND OR v_batch.status NOT IN ('pending','running') THEN
        RETURN NULL;
    END IF;
    IF NOT (CASE p_mode
        WHEN 'recover' THEN true
        WHEN 'signal' THEN yoao_assets_v1._dataset_quality_job_matches(v_batch.async_job_id,'dataset_quality_review',p_task)
        WHEN 'claim' THEN v_batch.status = 'pending'
            AND yoao_assets_v1._dataset_quality_job_matches(v_batch.async_job_id,'dataset_quality_review',p_task)
        WHEN 'worker' THEN v_batch.async_job_id = p_job AND v_batch.generation = p_generation AND v_batch.lease_until > p_now
            AND yoao_assets_v1._dataset_quality_job_matches(p_job,'dataset_quality_review',p_task)
        ELSE false END) IS TRUE THEN
        RETURN NULL;
    END IF;
    RETURN yoao_assets_v1._dataset_quality_review_receipt(v_batch.id,true);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_attempt(uuid,uuid,text,integer,text,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_expired_batches(p_now timestamptz)
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT b.id FROM public.dataset_quality_review_batches AS b
    WHERE b.status IN ('pending','running') AND b.lease_until <= p_now LIMIT 50
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_expired_batches(timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_quality_review_summary(p_batch uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'applied_count',pg_catalog.count(*) FILTER (WHERE m.state = 'applied'),
        'conflict_count',pg_catalog.count(*) FILTER (WHERE m.state = 'conflict'),
        'blocked_count',pg_catalog.count(*) FILTER (WHERE m.state = 'blocked'),
        'processed_count',pg_catalog.count(*) FILTER (WHERE m.state <> 'pending'),
        'partial',COALESCE(pg_catalog.bool_or(m.state IN ('conflict','blocked')),false)
    )
    FROM public.dataset_quality_review_members AS m WHERE m.batch_id = p_batch
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_quality_review_summary(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_transition(
    p_batch uuid, p_job uuid, p_generation integer, p_task text, p_action text,
    p_now timestamptz, p_lease_until timestamptz, p_error text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_summary jsonb;
BEGIN
    IF p_action NOT IN ('claim','redispatch','completed','cancelled','failed') OR p_action IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset quality review transition';
    END IF;
    IF p_action NOT IN ('claim','redispatch') THEN
        v_summary := yoao_assets_v1._dataset_quality_review_summary(p_batch);
    END IF;
    UPDATE public.dataset_quality_review_batches AS b SET
        status = CASE p_action WHEN 'claim' THEN 'running' WHEN 'redispatch' THEN 'pending'
            WHEN 'completed' THEN CASE WHEN (v_summary->>'partial')::boolean THEN 'partial' ELSE 'completed' END
            ELSE p_action END,
        generation = b.generation + CASE WHEN p_action = 'claim' THEN 0 ELSE 1 END,
        lease_until = CASE WHEN p_action IN ('claim','redispatch') THEN COALESCE(p_lease_until,b.lease_until) END,
        error_code = CASE WHEN p_action IN ('claim','redispatch') THEN b.error_code ELSE p_error END,
        completed_at = CASE WHEN p_action IN ('claim','redispatch') THEN b.completed_at ELSE p_now END,
        summary = COALESCE(v_summary,b.summary),
        processed = COALESCE((v_summary->>'processed_count')::integer,b.processed)
    WHERE b.id = p_batch AND b.async_job_id IS NOT DISTINCT FROM p_job AND b.generation = p_generation
      AND b.status IN ('pending','running') AND (p_action <> 'claim' OR b.status = 'pending')
      AND (p_action IN ('cancelled','failed') OR EXISTS (SELECT 1 FROM public.dataset_quality_runs AS r
           WHERE r.id = b.quality_run_id AND yoao_assets_v1._dataset_quality_writable(
               r.dataset_id,COALESCE((b.authority->>'dataset_lifecycle_revision')::integer,1))))
      AND (p_action <> 'completed' OR NOT EXISTS (SELECT 1 FROM public.dataset_quality_review_members AS m
                                                 WHERE m.batch_id = b.id AND m.state = 'pending'))
      AND (CASE WHEN p_action IN ('claim','redispatch') OR p_task IS NOT NULL
             THEN yoao_assets_v1._dataset_quality_job_matches(p_job,'dataset_quality_review',p_task) ELSE true END);
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN yoao_assets_v1._dataset_quality_review_receipt(p_batch,true);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_transition(uuid,uuid,integer,text,text,timestamptz,timestamptz,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_reconcile(p_job uuid, p_cancelled boolean, p_now timestamptz)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.dataset_quality_review_batches AS b SET
        status = CASE WHEN p_cancelled THEN 'cancelled' ELSE 'failed' END,
        error_code = CASE WHEN p_cancelled THEN 'cancelled' ELSE 'worker_failed' END,
        completed_at = p_now,lease_until = NULL,generation = b.generation + 1
    WHERE b.async_job_id = p_job AND b.status IN ('pending','running')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_reconcile(uuid,boolean,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_next_member(p_batch uuid)
RETURNS TABLE(id uuid, result_id uuid, snapshot jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT m.id,m.result_id,m.snapshot FROM public.dataset_quality_review_members AS m
    WHERE m.batch_id = p_batch AND m.state = 'pending' ORDER BY m.id LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_next_member(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_quality_review_checkpoint(
    p_batch uuid, p_job uuid, p_generation integer, p_task text, p_member uuid,
    p_revision integer, p_now timestamptz, p_lease_until timestamptz
)
RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_summary jsonb;
    v_processed integer;
BEGIN
    PERFORM 1 FROM public.dataset_quality_review_batches AS b
    WHERE b.id = p_batch AND b.async_job_id = p_job AND b.generation = p_generation
      AND b.status IN ('pending','running') AND b.lease_until > p_now
      AND yoao_assets_v1._dataset_quality_job_matches(p_job,'dataset_quality_review',p_task)
      AND EXISTS (SELECT 1 FROM public.dataset_quality_runs AS r
                  WHERE r.id = b.quality_run_id AND yoao_assets_v1._dataset_quality_writable(
                      r.dataset_id,COALESCE((b.authority->>'dataset_lifecycle_revision')::integer,1)))
    FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;

    UPDATE public.dataset_quality_review_members AS m SET
        state = CASE WHEN p_revision IS NULL THEN 'conflict' ELSE 'applied' END,
        reason_code = CASE WHEN p_revision IS NULL THEN 'evidence_or_review_changed' END,
        applied_revision = p_revision, processed_at = p_now
    WHERE m.batch_id = p_batch AND m.id = p_member AND m.state = 'pending'
      AND (p_revision IS NULL OR (
          p_revision = (m.snapshot->>'review_revision')::integer + 1
          AND EXISTS (
              SELECT 1 FROM public.dataset_quality_results AS r
              JOIN public.dataset_quality_review_batches AS b ON b.id = p_batch AND b.quality_run_id = r.run_id
              WHERE r.id = m.result_id AND r.review_revision = p_revision
                AND r.reviewed_by::text = b.authority->>'actor_id'
                AND r.review_state = b.decision AND r.review_note = b.note
          )
      ));
    IF NOT FOUND THEN RETURN NULL; END IF;

    v_summary := yoao_assets_v1._dataset_quality_review_summary(p_batch);
    UPDATE public.dataset_quality_review_batches AS b SET
        processed = (v_summary->>'processed_count')::integer, summary = v_summary, lease_until = p_lease_until
    WHERE b.id = p_batch RETURNING b.processed INTO v_processed;
    RETURN v_processed;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_quality_review_checkpoint(uuid,uuid,integer,text,uuid,integer,timestamptz,timestamptz) FROM PUBLIC;
