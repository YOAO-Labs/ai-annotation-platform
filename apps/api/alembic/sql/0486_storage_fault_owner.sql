CREATE TABLE yoao_assets_v1.storage_fault_observation_key (
    singleton boolean PRIMARY KEY CHECK (singleton),
    secret bytea NOT NULL CHECK (pg_catalog.octet_length(secret) = 32)
);
-- aap-statement-break
INSERT INTO yoao_assets_v1.storage_fault_observation_key VALUES (true, public.gen_random_bytes(32));
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._storage_fault_seal(
    p_sequence bigint, p_location uuid, p_source uuid, p_operation text,
    p_node text, p_scopes jsonb, p_evidence jsonb
)
RETURNS bytea
LANGUAGE sql STABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT public.hmac(pg_catalog.convert_to(pg_catalog.jsonb_build_array(
        p_sequence, p_location, p_source, p_operation, p_node, p_scopes, p_evidence
    )::text, 'UTF8'), key.secret, 'sha256')
    FROM yoao_assets_v1.storage_fault_observation_key AS key
    WHERE key.singleton
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reserve_storage_fault_observation(
    p_location uuid, p_source uuid, p_operation text, p_node text, p_scopes jsonb, p_evidence jsonb
)
RETURNS TABLE (observation_sequence bigint, seal bytea)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_location IS NULL OR p_source IS NULL
       OR p_operation IS NULL OR p_operation NOT IN ('scan', 'scan_content', 'reference_read', 'preview_generate')
       OR p_node IS NULL OR pg_catalog.length(p_node) NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(p_scopes) IS DISTINCT FROM 'object' OR p_scopes = '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_evidence) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'Invalid storage fault observation' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.jsonb_each(p_scopes) AS scope(category, value)
        WHERE scope.category NOT IN ('root_unavailable', 'access_denied', 'source_changed', 'preview_failed')
           OR scope.value ->> 'key' IS NULL OR scope.value ->> 'key' !~ '^[a-f0-9]{64}$'
           OR pg_catalog.jsonb_typeof(scope.value -> 'facts' -> 'common') IS DISTINCT FROM 'object'
    ) OR EXISTS (
        SELECT FROM pg_catalog.jsonb_each_text(p_evidence) AS fact(name, value)
        WHERE CASE WHEN fact.name = 'request_id' THEN
            fact.value IS NULL OR fact.value !~ '^[A-Za-z0-9_.:-]{1,36}$'
        ELSE
            fact.name NOT IN ('dataset_id', 'item_id', 'asset_id', 'task_id', 'run_id', 'job_id', 'registration_id')
            OR (fact.value IS NOT NULL AND fact.value !~ '^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
        END
    ) THEN
        RAISE EXCEPTION 'Invalid storage fault facts' USING ERRCODE = '22023';
    END IF;
    observation_sequence := pg_catalog.nextval('public.storage_fault_observation_seq'::regclass);
    seal := yoao_assets_v1._storage_fault_seal(
        observation_sequence, p_location, p_source, p_operation, p_node, p_scopes, p_evidence
    );
    IF seal IS NULL THEN
        RAISE EXCEPTION 'Storage fault observation key unavailable';
    END IF;
    RETURN NEXT;
END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_storage_fault_observation(
    p_sequence bigint, p_seal bytea, p_location uuid, p_source uuid,
    p_operation text, p_node text, p_scopes jsonb, p_evidence jsonb, p_failure_category text
)
RETURNS TABLE (fault_id uuid, category text, transition text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_seal bytea;
    v_scope jsonb;
    v_key text;
    v_row record;
BEGIN
    v_seal := yoao_assets_v1._storage_fault_seal(
        p_sequence, p_location, p_source, p_operation, p_node, p_scopes, p_evidence
    );
    IF v_seal IS NULL OR p_seal IS DISTINCT FROM v_seal
       OR (p_failure_category IS NOT NULL AND NOT (p_scopes ? p_failure_category)) THEN
        RAISE EXCEPTION 'Invalid storage fault observation proof' USING ERRCODE = '22023';
    END IF;
    FOREACH category IN ARRAY ARRAY['root_unavailable', 'access_denied', 'source_changed', 'preview_failed'] LOOP
        v_scope := p_scopes -> category;
        IF v_scope IS NULL OR (p_failure_category IS NOT NULL AND category <> p_failure_category) THEN
            CONTINUE;
        END IF;
        v_key := v_scope ->> 'key';
        IF p_failure_category IS NOT NULL THEN
            INSERT INTO public.storage_fault_states (
                id, scope_key, location_id, source_id, category, operation, node_id, state,
                first_failed_at, last_failed_at, failure_count, last_sequence, scope_facts, evidence
            ) VALUES (
                pg_catalog.gen_random_uuid(), v_key, p_location, p_source, category, p_operation, p_node, 'active',
                pg_catalog.clock_timestamp(), pg_catalog.clock_timestamp(), 1, p_sequence, v_scope -> 'facts', p_evidence
            ) ON CONFLICT (scope_key) DO NOTHING RETURNING id INTO fault_id;
            IF FOUND THEN
                transition := 'opened';
                RETURN NEXT;
                CONTINUE;
            END IF;
        END IF;
        SELECT f.id, f.location_id, f.source_id, f.node_id, f.category, f.state, f.last_sequence, f.scope_facts
        INTO v_row
        FROM public.storage_fault_states AS f WHERE f.scope_key = v_key FOR UPDATE;
        IF NOT FOUND OR v_row.last_sequence >= p_sequence THEN
            CONTINUE;
        END IF;
        IF (v_row.location_id, v_row.source_id, v_row.node_id, v_row.category::text, v_row.scope_facts)
           IS DISTINCT FROM (p_location, p_source, p_node, category, v_scope -> 'facts') THEN
            RAISE EXCEPTION 'Storage fault scope identity changed' USING ERRCODE = '22023';
        END IF;
        fault_id := v_row.id;
        IF p_failure_category IS NOT NULL THEN
            UPDATE public.storage_fault_states SET state = 'active', last_failed_at = pg_catalog.clock_timestamp(),
                failure_count = failure_count + 1, last_sequence = p_sequence, evidence = p_evidence, operation = p_operation
            WHERE id = fault_id;
            IF v_row.state = 'recovered' THEN
                transition := 'reopened';
                RETURN NEXT;
            END IF;
        ELSIF v_row.state = 'active' THEN
            UPDATE public.storage_fault_states SET state = 'recovered', recovered_at = pg_catalog.clock_timestamp(),
                recovered_sequence = p_sequence, last_sequence = p_sequence, evidence = p_evidence, operation = p_operation
            WHERE id = fault_id;
            transition := 'recovered';
            RETURN NEXT;
        ELSE
            UPDATE public.storage_fault_states SET last_sequence = p_sequence, evidence = p_evidence, operation = p_operation
            WHERE id = fault_id;
        END IF;
    END LOOP;
END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_storage_fault_facts(
    p_location uuid, p_source uuid, p_node text, p_preview boolean, p_read_subject boolean
)
RETURNS jsonb
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'location', CASE WHEN p_read_subject AND s.id IS NOT NULL THEN pg_catalog.jsonb_build_object(
            'kind', l.kind, 'config', l.config, 'revision', l.config_revision, 'connection_id', l.connection_id
        ) END,
        'source', CASE WHEN p_read_subject AND s.id IS NOT NULL THEN pg_catalog.jsonb_build_object(
            'config', s.config, 'revision', s.config_revision, 'dataset_id', s.dataset_id,
            'policy_revision', s.policy_revision, 'cache_policy', s.cache_policy
        ) END,
        'connection_id', l.connection_id, 'connection_revision', c.revision,
        'maintenance_revision', COALESCE(g.revision, 1),
        'probe_anchor', probe.result -> 'anchor',
        'binding', CASE WHEN b.location_id IS NOT NULL THEN pg_catalog.jsonb_build_array(
            b.location_id, b.generation,
            pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(pg_catalog.to_json(b.bucket)::text, 'UTF8')), 'hex')
        ) END
    )
    FROM (SELECT 1) AS request
    LEFT JOIN public.storage_locations AS l ON l.id = p_location
    LEFT JOIN public.dataset_sources AS s ON s.id = p_source AND s.location_id = l.id
    LEFT JOIN public.storage_connections AS c ON c.id = l.connection_id
    LEFT JOIN public.storage_maintenance_gates AS g ON g.location_id = l.id
    LEFT JOIN public.storage_location_probes AS probe ON probe.location_id = l.id
        AND probe.node_id = CASE WHEN l.kind = 'agent' THEN 'agent:' || (l.config ->> 'agent_id') ELSE p_node END
    LEFT JOIN public.storage_bindings AS b ON p_preview AND b.purpose = 'media-cache'
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.read_storage_fault_job(p_job uuid)
RETURNS TABLE (id uuid, kind text, run_id text, registration_id text)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT j.id, j.kind::text, j.payload ->> 'run_id', j.payload ->> 'registration_id'
    FROM public.async_jobs AS j WHERE j.id = p_job
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._storage_fault_location_visible(
    p_actor uuid, p_role text, p_location uuid, p_strict boolean
)
RETURNS boolean
LANGUAGE sql STABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.storage_locations AS l
        LEFT JOIN public.storage_connections AS c ON c.id = l.connection_id
        WHERE l.id = p_location
          AND (p_role = 'super_admin' OR c.scope = 'global' OR c.created_by = p_actor
               OR (l.kind IN ('filesystem', 'agent') AND
                   (l.config ->> 'scope' = 'global' OR l.config -> 'use_user_ids' @> pg_catalog.jsonb_build_array(p_actor))))
          AND (NOT p_strict OR l.connection_id IS NULL
               OR (c.id IS NOT NULL AND (p_role = 'super_admin' OR c.scope = 'global' OR c.created_by = p_actor)))
    )
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.storage_fault_location_visible(p_actor uuid, p_location uuid)
RETURNS boolean
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM yoao_identity_v1.read_actor_facts(p_actor) AS actor
        WHERE actor.is_active AND actor.platform_role IN ('super_admin', 'project_admin')
          AND yoao_assets_v1._storage_fault_location_visible(p_actor, actor.platform_role, p_location, true)
    )
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._storage_fault_job_visible(p_actor uuid, p_role text, p_job uuid)
RETURNS boolean
LANGUAGE sql STABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM yoao_jobs_v1.read_storage_fault_job(p_job) AS j
        WHERE p_role = 'super_admin' OR EXISTS (
            SELECT FROM (
                SELECT run.source_id FROM public.dataset_scan_runs AS run
                WHERE j.kind = 'dataset_scan' AND (run.async_job_id = j.id OR run.id::text = j.run_id)
                UNION ALL
                SELECT run.source_id FROM public.dataset_registration_runs AS run
                WHERE j.kind = 'dataset_registration' AND (run.async_job_id = j.id OR run.id::text = j.registration_id)
            ) AS linked
            JOIN public.dataset_sources AS source ON source.id = linked.source_id
            JOIN public.datasets AS dataset ON dataset.id = source.dataset_id AND dataset.created_by = p_actor
            WHERE yoao_assets_v1._storage_fault_location_visible(p_actor, p_role, source.location_id, false)
        )
    )
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.storage_fault_page(
    p_actor uuid, p_location uuid, p_state text, p_limit integer, p_offset integer
)
RETURNS TABLE (active_count bigint, recovered_count bigint, items jsonb)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH actor AS MATERIALIZED (
        SELECT a.platform_role FROM yoao_identity_v1.read_actor_facts(p_actor) AS a
        WHERE a.is_active AND a.platform_role IN ('super_admin', 'project_admin')
          AND yoao_assets_v1._storage_fault_location_visible(p_actor, a.platform_role, p_location, true)
          AND p_state IN ('active', 'recovered', 'all') AND p_limit BETWEEN 1 AND 100 AND p_offset >= 0
    ), eligible AS MATERIALIZED (
        SELECT f.id, f.state, f.last_failed_at
        FROM actor AS a CROSS JOIN public.storage_fault_states AS f
        WHERE f.location_id = p_location AND (a.platform_role = 'super_admin' OR EXISTS (
            SELECT FROM public.dataset_sources AS s
            JOIN public.datasets AS d ON d.id = s.dataset_id AND d.created_by = p_actor
            WHERE s.id = f.source_id AND s.location_id = f.location_id
        ))
    ), counts AS (
        SELECT pg_catalog.count(*) FILTER (WHERE e.state = 'active') AS active,
               pg_catalog.count(*) FILTER (WHERE e.state = 'recovered') AS recovered
        FROM eligible AS e
    ), page AS MATERIALIZED (
        SELECT e.id FROM eligible AS e WHERE p_state = 'all' OR e.state = p_state
        ORDER BY e.last_failed_at DESC, e.id LIMIT p_limit OFFSET p_offset
    )
    SELECT counts.active, counts.recovered, COALESCE((
        SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
            'id', f.id, 'location_id', f.location_id, 'source_id', f.source_id,
            'source_name', s.name, 'dataset_id', s.dataset_id,
            'node_id', f.node_id, 'category', f.category, 'operation', f.operation, 'state', f.state,
            'first_failed_at', f.first_failed_at, 'last_failed_at', f.last_failed_at,
            'recovered_at', f.recovered_at, 'failure_count', f.failure_count,
            'item_id', f.evidence -> 'item_id', 'asset_id', f.evidence -> 'asset_id',
            'task_id', f.evidence -> 'task_id', 'run_id', f.evidence -> 'run_id',
            'registration_id', f.evidence -> 'registration_id',
            'job_id', CASE WHEN yoao_assets_v1._storage_fault_job_visible(p_actor, a.platform_role,
                CASE WHEN f.evidence ->> 'job_id' ~ '^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$'
                     THEN (f.evidence ->> 'job_id')::uuid END) THEN f.evidence -> 'job_id' END,
            'current_facts', CASE WHEN l.enabled AND s.enabled AND s.unregistered_at IS NULL
                                       AND s.dataset_id IS NOT NULL AND s.location_id = l.id
                THEN yoao_assets_v1.read_storage_fault_facts(l.id, s.id, f.node_id, f.category = 'preview_failed', true) END,
            'saved_common', f.scope_facts -> 'common', 'saved_dependencies', f.scope_facts -> 'dependencies',
            'content_status', CASE
                WHEN f.category NOT IN ('source_changed', 'preview_failed') THEN 'current'
                WHEN mapping.source_id IS NULL OR COALESCE(f.scope_facts ->> 'expected_sha256', '') = '' THEN 'unknown'
                WHEN mapping.content_sha256 IS DISTINCT FROM (f.scope_facts ->> 'expected_sha256')
                  OR (COALESCE(f.scope_facts ->> 'asset_id', '') <> ''
                       AND mapping.asset_id::text IS DISTINCT FROM (f.scope_facts ->> 'asset_id')) THEN 'historical'
                ELSE 'current' END
        ) ORDER BY f.last_failed_at DESC, f.id)
        FROM page
        JOIN public.storage_fault_states AS f ON f.id = page.id
        JOIN public.storage_locations AS l ON l.id = f.location_id
        LEFT JOIN public.dataset_sources AS s ON s.id = f.source_id
        LEFT JOIN public.dataset_source_items AS mapping ON f.category IN ('source_changed', 'preview_failed')
            AND mapping.source_id = s.id AND mapping.path_digest = f.scope_facts ->> 'path_digest'
    ), '[]'::jsonb)
    FROM actor AS a CROSS JOIN counts
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_storage_observation_rate(
    p_location uuid, p_source uuid, p_metric text, p_success boolean
)
RETURNS void
LANGUAGE sql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH clock AS (SELECT pg_catalog.statement_timestamp() AS at)
    INSERT INTO public.storage_observation_buckets AS bucket (
        day, location_id, source_id, metric, success_count, failure_count, first_observed_at, last_observed_at
    )
    SELECT (clock.at AT TIME ZONE 'UTC')::date, p_location, p_source, p_metric,
           p_success::integer, (NOT p_success)::integer, clock.at, clock.at
    FROM clock
    ON CONFLICT (day, location_id, source_id, metric) DO UPDATE SET
        success_count = bucket.success_count + EXCLUDED.success_count,
        failure_count = bucket.failure_count + EXCLUDED.failure_count,
        first_observed_at = LEAST(bucket.first_observed_at, EXCLUDED.first_observed_at),
        last_observed_at = GREATEST(bucket.last_observed_at, EXCLUDED.last_observed_at)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_storage_observation_rates(p_actor uuid, p_location uuid)
RETURNS TABLE (
    measured_at timestamptz, window_start timestamptz,
    original_read_success numeric, original_read_failure numeric,
    preview_generate_success numeric, preview_generate_failure numeric
)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH clock AS MATERIALIZED (
        SELECT pg_catalog.statement_timestamp() AS at
    ), visible AS (
        SELECT b.metric, b.success_count, b.failure_count
        FROM clock CROSS JOIN yoao_identity_v1.read_actor_facts(p_actor) AS a
        JOIN public.storage_observation_buckets AS b ON b.location_id = p_location
        WHERE b.day = (clock.at AT TIME ZONE 'UTC')::date
          AND a.is_active AND a.platform_role IN ('super_admin', 'project_admin')
          AND yoao_assets_v1._storage_fault_location_visible(p_actor, a.platform_role, p_location, true)
          AND (a.platform_role = 'super_admin' OR EXISTS (
              SELECT FROM public.dataset_sources AS s
              JOIN public.datasets AS d ON d.id = s.dataset_id AND d.created_by = p_actor
              WHERE s.id = b.source_id AND s.location_id = b.location_id
          ))
    )
    SELECT clock.at, (clock.at AT TIME ZONE 'UTC')::date::timestamp AT TIME ZONE 'UTC',
        COALESCE(pg_catalog.sum(v.success_count) FILTER (WHERE v.metric = 'original_read'), 0),
        COALESCE(pg_catalog.sum(v.failure_count) FILTER (WHERE v.metric = 'original_read'), 0),
        COALESCE(pg_catalog.sum(v.success_count) FILTER (WHERE v.metric = 'preview_generate'), 0),
        COALESCE(pg_catalog.sum(v.failure_count) FILTER (WHERE v.metric = 'preview_generate'), 0)
    FROM clock LEFT JOIN visible AS v ON true
    GROUP BY clock.at
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1._storage_fault_seal(bigint,uuid,uuid,text,text,jsonb,jsonb),
    yoao_assets_v1._storage_fault_location_visible(uuid,text,uuid,boolean),
    yoao_assets_v1._storage_fault_job_visible(uuid,text,uuid),
    yoao_assets_v1.reserve_storage_fault_observation(uuid,uuid,text,text,jsonb,jsonb),
    yoao_assets_v1.record_storage_fault_observation(bigint,bytea,uuid,uuid,text,text,jsonb,jsonb,text),
    yoao_assets_v1.read_storage_fault_facts(uuid,uuid,text,boolean,boolean),
    yoao_assets_v1.storage_fault_location_visible(uuid,uuid),
    yoao_assets_v1.storage_fault_page(uuid,uuid,text,integer,integer),
    yoao_assets_v1.record_storage_observation_rate(uuid,uuid,text,boolean),
    yoao_assets_v1.read_storage_observation_rates(uuid,uuid),
    yoao_jobs_v1.read_storage_fault_job(uuid)
FROM PUBLIC;
-- aap-statement-break
DO $ownership$
DECLARE
    v_assets name := (SELECT pg_catalog.pg_get_userbyid(proowner) FROM pg_catalog.pg_proc
        WHERE oid = 'yoao_assets_v1.read_dataset_source_authority(uuid,boolean)'::regprocedure);
    v_jobs name := (SELECT pg_catalog.pg_get_userbyid(proowner) FROM pg_catalog.pg_proc
        WHERE oid = 'yoao_jobs_v1.read_job(uuid)'::regprocedure);
    v_assets_create boolean := pg_catalog.has_schema_privilege(v_assets, 'yoao_assets_v1', 'CREATE');
    v_jobs_create boolean := pg_catalog.has_schema_privilege(v_jobs, 'yoao_jobs_v1', 'CREATE');
    v_function regprocedure;
    v_object record;
    v_grantee text;
    v_columns text;
BEGIN
    FOR v_object IN
        SELECT c.oid, c.relkind, c.relowner, c.relacl, n.nspname, c.relname
        FROM pg_catalog.pg_class AS c JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.oid IN ('public.storage_fault_states'::regclass, 'public.storage_fault_observation_seq'::regclass,
                        'public.storage_observation_buckets'::regclass, 'yoao_assets_v1.storage_fault_observation_key'::regclass)
    LOOP
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(a.attname), ',' ORDER BY a.attnum) INTO v_columns
        FROM pg_catalog.pg_attribute AS a WHERE a.attrelid = v_object.oid AND a.attnum > 0 AND NOT a.attisdropped;
        FOR v_grantee IN
            SELECT 'PUBLIC'
            UNION
            SELECT pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(grants.grantee))
            FROM (
                SELECT acl.grantee FROM pg_catalog.aclexplode(v_object.relacl) AS acl
                UNION
                SELECT acl.grantee FROM pg_catalog.pg_attribute AS a, LATERAL pg_catalog.aclexplode(a.attacl) AS acl
                WHERE a.attrelid = v_object.oid AND a.attnum > 0 AND NOT a.attisdropped
            ) AS grants WHERE grants.grantee NOT IN (0, v_object.relowner)
        LOOP
            IF v_object.relkind = 'S' THEN
                EXECUTE pg_catalog.format('REVOKE ALL ON SEQUENCE %I.%I FROM %s CASCADE', v_object.nspname, v_object.relname, v_grantee);
            ELSE
                EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %I.%I FROM %s CASCADE', v_object.nspname, v_object.relname, v_grantee);
                EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %s CASCADE', v_columns, v_object.nspname, v_object.relname, v_grantee);
            END IF;
        END LOOP;
    END LOOP;
    IF NOT v_assets_create THEN
        EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA yoao_assets_v1 TO %I', v_assets);
    END IF;
    IF NOT v_jobs_create THEN
        EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA yoao_jobs_v1 TO %I', v_jobs);
    END IF;
    FOREACH v_function IN ARRAY ARRAY[
        'yoao_assets_v1._storage_fault_seal(bigint,uuid,uuid,text,text,jsonb,jsonb)',
        'yoao_assets_v1._storage_fault_location_visible(uuid,text,uuid,boolean)',
        'yoao_assets_v1._storage_fault_job_visible(uuid,text,uuid)',
        'yoao_assets_v1.reserve_storage_fault_observation(uuid,uuid,text,text,jsonb,jsonb)',
        'yoao_assets_v1.record_storage_fault_observation(bigint,bytea,uuid,uuid,text,text,jsonb,jsonb,text)',
        'yoao_assets_v1.read_storage_fault_facts(uuid,uuid,text,boolean,boolean)',
        'yoao_assets_v1.storage_fault_location_visible(uuid,uuid)',
        'yoao_assets_v1.storage_fault_page(uuid,uuid,text,integer,integer)',
        'yoao_assets_v1.record_storage_observation_rate(uuid,uuid,text,boolean)',
        'yoao_assets_v1.read_storage_observation_rates(uuid,uuid)'
    ]::regprocedure[] LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I', v_function, v_assets);
    END LOOP;
    EXECUTE pg_catalog.format('ALTER FUNCTION yoao_jobs_v1.read_storage_fault_job(uuid) OWNER TO %I', v_jobs);
    EXECUTE pg_catalog.format('ALTER TABLE yoao_assets_v1.storage_fault_observation_key OWNER TO %I', v_assets);
    IF v_assets <> current_user THEN
        EXECUTE pg_catalog.format('GRANT SELECT, INSERT, UPDATE ON public.storage_fault_states, public.storage_observation_buckets TO %I', v_assets);
        EXECUTE pg_catalog.format('GRANT USAGE ON SEQUENCE public.storage_fault_observation_seq TO %I', v_assets);
        EXECUTE pg_catalog.format('GRANT SELECT (id, dataset_id, location_id, name, enabled, unregistered_at, config, config_revision, policy_revision, cache_policy) ON public.dataset_sources TO %I', v_assets);
        EXECUTE pg_catalog.format('GRANT SELECT (source_id, path_digest, content_sha256, asset_id) ON public.dataset_source_items TO %I', v_assets);
        EXECUTE pg_catalog.format('GRANT SELECT (id, source_id, async_job_id) ON public.dataset_scan_runs, public.dataset_registration_runs TO %I', v_assets);
        EXECUTE pg_catalog.format('GRANT SELECT (location_id, node_id, result) ON public.storage_location_probes TO %I', v_assets);
    END IF;
    IF v_assets <> v_jobs THEN
        EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA yoao_jobs_v1 TO %I', v_assets);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION yoao_jobs_v1.read_storage_fault_job(uuid) TO %I', v_assets);
    END IF;
    IF NOT v_assets_create THEN
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM %I', v_assets);
    END IF;
    IF NOT v_jobs_create THEN
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM %I', v_jobs);
    END IF;
END
$ownership$;
