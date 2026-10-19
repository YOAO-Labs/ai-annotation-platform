-- Native Labeling authority for fenced, resumable Raster Mask collection.

CREATE TABLE public.mask_gc_intents (
    operation_id uuid PRIMARY KEY,
    object_key text NOT NULL CHECK (
        object_key ~ '^raster-masks/sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}[.]json([.]gz)?$'
    ),
    etag text NOT NULL CHECK (
        pg_catalog.octet_length(etag) BETWEEN 1 AND 255
        AND etag !~ '[[:cntrl:]]'
    ),
    last_modified timestamptz NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    epoch bigint NOT NULL CHECK (epoch > 0),
    sweep_id uuid NOT NULL,
    grace_cutoff timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'indeterminate', 'completed')),
    outcome text CHECK (outcome IN ('deleted', 'missing', 'content_changed')),
    next_check_at timestamptz NOT NULL,
    claim_generation bigint NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
    claim_worker_id uuid,
    claim_lease_token uuid,
    claim_lease_expires_at timestamptz,
    last_error_code text CHECK (
        last_error_code IS NULL
        OR last_error_code IN ('peer_unavailable', 'invalid_response')
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at timestamptz,
    CHECK (
        (state = 'completed' AND outcome IS NOT NULL AND completed_at IS NOT NULL)
        OR (state <> 'completed' AND outcome IS NULL AND completed_at IS NULL)
    ),
    CHECK (
        (claim_worker_id IS NULL AND claim_lease_token IS NULL
            AND claim_lease_expires_at IS NULL)
        OR (claim_worker_id IS NOT NULL AND claim_lease_token IS NOT NULL
            AND claim_lease_expires_at IS NOT NULL)
    )
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_mask_gc_intents_active_key
    ON public.mask_gc_intents(object_key)
    WHERE state <> 'completed';
-- aap-statement-break
CREATE INDEX ix_mask_gc_intents_ready
    ON public.mask_gc_intents(next_check_at, object_key, operation_id)
    WHERE state <> 'completed';
-- aap-statement-break
CREATE TABLE public.mask_gc_key_epochs (
    object_key text PRIMARY KEY CHECK (
        object_key ~ '^raster-masks/sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}[.]json([.]gz)?$'
    ),
    epoch bigint NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    active_operation_id uuid,
    last_write_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    FOREIGN KEY (active_operation_id)
        REFERENCES public.mask_gc_intents(operation_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);
-- aap-statement-break
CREATE TABLE public.mask_gc_sweeps (
    sweep_id uuid PRIMARY KEY,
    generation bigint NOT NULL CHECK (generation > 0),
    worker_id uuid NOT NULL,
    lease_token uuid NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    cutoff timestamptz NOT NULL,
    after_key text,
    complete boolean NOT NULL DEFAULT false,
    last_page_input_after_key text,
    last_page_digest text CHECK (
        last_page_digest IS NULL OR last_page_digest ~ '^[0-9a-f]{64}$'
    ),
    last_page_result jsonb CHECK (
        last_page_result IS NULL OR pg_catalog.jsonb_typeof(last_page_result) = 'object'
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (after_key IS NULL OR (
        pg_catalog.octet_length(after_key) <= 1024
        AND after_key LIKE 'raster-masks/sha256/%'
    )),
    CHECK (
        (last_page_input_after_key IS NULL AND last_page_digest IS NULL
            AND last_page_result IS NULL)
        OR (last_page_digest IS NOT NULL AND last_page_result IS NOT NULL)
    )
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_mask_gc_sweeps_open
    ON public.mask_gc_sweeps(complete)
    WHERE complete IS FALSE;
-- aap-statement-break
CREATE INDEX ix_mask_gc_sweeps_lease
    ON public.mask_gc_sweeps(lease_expires_at, sweep_id)
    WHERE complete IS FALSE;
-- aap-statement-break
CREATE TABLE public.mask_gc_roots (
    root_kind text NOT NULL,
    root_id text NOT NULL,
    object_key text NOT NULL CHECK (
        object_key ~ '^raster-masks/sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}[.]json([.]gz)?$'
    ),
    valid_until timestamptz,
    PRIMARY KEY (root_kind, root_id, object_key)
);
-- aap-statement-break
CREATE INDEX ix_mask_gc_roots_key_live
    ON public.mask_gc_roots(object_key, valid_until);
-- aap-statement-break
CREATE TABLE public.mask_gc_write_permit_secrets (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    secret bytea NOT NULL CHECK (pg_catalog.octet_length(secret) = 32)
);
-- aap-statement-break
INSERT INTO public.mask_gc_write_permit_secrets(singleton, secret)
VALUES (true, public.gen_random_bytes(32));
-- aap-statement-break
REVOKE ALL ON public.mask_gc_intents, public.mask_gc_key_epochs,
    public.mask_gc_sweeps, public.mask_gc_roots,
    public.mask_gc_write_permit_secrets FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-mask-gc/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-mask-gc/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_key_valid(p_key text)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_parts text[];
BEGIN
    IF p_key IS NULL OR pg_catalog.octet_length(p_key) > 255 THEN
        RETURN false;
    END IF;
    v_parts := pg_catalog.regexp_match(
        p_key,
        '^raster-masks/sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})[.]json([.]gz)?$'
    );
    RETURN v_parts IS NOT NULL
        AND v_parts[1] = pg_catalog.substr(v_parts[3], 1, 2)
        AND v_parts[2] = pg_catalog.substr(v_parts[3], 3, 2);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_key_valid(text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_json_keys(p_value jsonb)
RETURNS TABLE(object_key text)
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT DISTINCT path.value #>> '{}'
    FROM pg_catalog.jsonb_path_query(p_value, '$.**.object_key') AS path(value)
    WHERE pg_catalog.jsonb_typeof(path.value) = 'string'
      AND path.value #>> '{}' LIKE 'raster-masks/sha256/%'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_json_keys(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_row_refs(p_table text, p_row jsonb)
RETURNS TABLE(object_key text, valid_until timestamptz)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_payload jsonb;
    v_status text;
    v_retention jsonb;
    v_retained_until timestamptz;
    v_key text;
BEGIN
    IF p_row IS NULL THEN RETURN; END IF;
    v_payload := NULL;
    v_retained_until := NULL;
    CASE p_table
        WHEN 'annotations' THEN
            IF p_row -> 'is_active' = 'true'::jsonb THEN
                v_payload := p_row -> 'geometry';
            END IF;
        WHEN 'predictions' THEN
            v_payload := p_row -> 'result';
        WHEN 'raster_mask_uploads' THEN
            v_payload := pg_catalog.jsonb_build_object(
                'object_key', p_row -> 'object_key'
            );
            BEGIN
                v_retained_until := (p_row ->> 'created_at')::timestamptz
                    + interval '24 hours';
            EXCEPTION WHEN OTHERS THEN
                v_retained_until := NULL;
            END;
        WHEN 'video_tracker_jobs' THEN
            v_status := p_row ->> 'status';
            IF p_row -> 'staged_result' <> 'null'::jsonb
               AND v_status IN ('pending_review', 'partially_reviewed', 'cancelled') THEN
                v_payload := p_row -> 'staged_result';
            END IF;
        WHEN 'ai_mask_accept_decisions' THEN
            BEGIN
                v_retained_until := (p_row ->> 'expires_at')::timestamptz;
                IF v_retained_until > pg_catalog.clock_timestamp() THEN
                    v_payload := p_row -> 'response_json';
                END IF;
            EXCEPTION WHEN OTHERS THEN
                v_payload := p_row -> 'response_json';
                v_retained_until := NULL;
            END;
        WHEN 'mask_annotation_revisions' THEN
            BEGIN
                v_retained_until := (p_row ->> 'expires_at')::timestamptz;
                IF v_retained_until > pg_catalog.clock_timestamp() THEN
                    v_payload := p_row -> 'geometry';
                END IF;
            EXCEPTION WHEN OTHERS THEN
                v_payload := p_row -> 'geometry';
                v_retained_until := NULL;
            END;
        WHEN 'mask_qc_runs' THEN
            IF p_row ->> 'status' IN ('pending', 'running') THEN
                v_payload := p_row -> 'source_snapshot';
            END IF;
        WHEN 'mask_qc_issues' THEN
            v_payload := p_row -> 'region_mask_ref';
        WHEN 'mask_review_scopes' THEN
            v_payload := p_row -> 'region_mask_ref';
        WHEN 'video_tracker_input_scopes' THEN
            BEGIN
                v_retention := aap_labeling_v1.video_tracker_input_referenced(
                    (p_row ->> 'input_id')::uuid
                );
                IF v_retention ->> 'schema'
                       IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
                   OR v_retention -> 'ok' IS DISTINCT FROM 'true'::jsonb
                   OR v_retention #>> '{data,input_id}'
                       IS DISTINCT FROM p_row ->> 'input_id'
                   OR pg_catalog.jsonb_typeof(v_retention #> '{data,referenced}')
                       IS DISTINCT FROM 'boolean' THEN
                    v_payload := p_row -> 'scope';
                    v_retained_until := NULL;
                ELSIF v_retention #> '{data,referenced}' = 'true'::jsonb THEN
                    v_payload := p_row -> 'scope';
                    IF v_retention #> '{data,retained_until}' <> 'null'::jsonb THEN
                        BEGIN
                            v_retained_until :=
                                (v_retention #>> '{data,retained_until}')::timestamptz;
                        EXCEPTION WHEN OTHERS THEN
                            v_retained_until := NULL;
                        END;
                    END IF;
                END IF;
            EXCEPTION WHEN OTHERS THEN
                -- The tracker retention owner is authoritative. Unknown
                -- outcomes retain the complete captured source snapshot.
                v_payload := p_row -> 'scope';
                v_retained_until := NULL;
            END;
        WHEN 'mask_repair_batches' THEN
            v_status := p_row ->> 'status';
            IF (v_status = 'planned'
                    AND (p_row ->> 'receipt_expires_at')::timestamptz
                        > pg_catalog.clock_timestamp()) THEN
                v_payload := p_row -> 'plan_json';
                v_retained_until := (p_row ->> 'receipt_expires_at')::timestamptz;
            ELSIF v_status IN ('pending', 'running', 'rolling_back') THEN
                v_payload := p_row -> 'plan_json';
            ELSIF v_status IN ('completed', 'partial', 'rollback_failed')
                  AND p_row -> 'rollback_expires_at' <> 'null'::jsonb
                  AND (p_row ->> 'rollback_expires_at')::timestamptz
                      > pg_catalog.clock_timestamp() THEN
                v_payload := p_row -> 'plan_json';
                v_retained_until := (p_row ->> 'rollback_expires_at')::timestamptz;
            END IF;
        ELSE
            RETURN;
    END CASE;

    IF v_payload IS NULL OR v_payload = 'null'::jsonb THEN RETURN; END IF;
    FOR v_key IN
        SELECT keys.object_key
        FROM aap_labeling_v1._mask_gc_json_keys(v_payload) AS keys
        WHERE aap_labeling_v1._mask_gc_key_valid(keys.object_key)
        ORDER BY keys.object_key COLLATE "C"
    LOOP
        object_key := v_key;
        valid_until := v_retained_until;
        RETURN NEXT;
    END LOOP;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_row_refs(text, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_root_entry_live(
    p_root_kind text,
    p_root_id text,
    p_valid_until timestamptz,
    p_fail_closed boolean
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_input_id uuid;
BEGIN
    IF p_root_kind = 'video_tracker_input_scopes' THEN
        BEGIN
            SELECT scope.input_id INTO v_input_id
            FROM public.video_tracker_input_scopes AS scope
            WHERE scope.tracker_job_id = p_root_id::uuid;
            IF NOT FOUND THEN RETURN p_fail_closed; END IF;
            v_reply := aap_labeling_v1.video_tracker_input_referenced(
                v_input_id
            );
            IF v_reply ->> 'schema'
                   IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
               OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
               OR v_reply #>> '{data,input_id}' IS DISTINCT FROM v_input_id::text
               OR pg_catalog.jsonb_typeof(v_reply #> '{data,referenced}')
                   IS DISTINCT FROM 'boolean' THEN
                RETURN p_fail_closed;
            END IF;
            RETURN v_reply #> '{data,referenced}' = 'true'::jsonb;
        EXCEPTION WHEN OTHERS THEN
            RETURN p_fail_closed;
        END;
    END IF;
    RETURN p_valid_until IS NULL OR p_valid_until > pg_catalog.clock_timestamp();
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_root_entry_live(
    text, text, timestamptz, boolean
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_has_live_root(
    p_key text, p_fail_closed boolean
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_root record;
BEGIN
    FOR v_root IN
        SELECT root.root_kind, root.root_id, root.valid_until
        FROM public.mask_gc_roots AS root
        WHERE root.object_key = p_key
    LOOP
        IF aap_labeling_v1._mask_gc_root_entry_live(
            v_root.root_kind, v_root.root_id, v_root.valid_until, p_fail_closed
        ) THEN
            RETURN true;
        END IF;
    END LOOP;
    RETURN false;
EXCEPTION WHEN OTHERS THEN
    RETURN p_fail_closed;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_has_live_root(text, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_write_permit_items()
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_setting text;
    v_map jsonb;
    v_body jsonb;
    v_secret bytea;
    v_expected text;
BEGIN
    v_setting := pg_catalog.current_setting('aap.mask_gc_write_permit', true);
    IF v_setting IS NULL OR pg_catalog.octet_length(v_setting) > 1048576 THEN
        RETURN '[]'::jsonb;
    END IF;
    v_map := v_setting::jsonb;
    IF pg_catalog.jsonb_typeof(v_map) IS DISTINCT FROM 'object'
       OR (v_map - ARRAY['txid', 'backend_pid', 'items', 'mac'])
            IS DISTINCT FROM '{}'::jsonb
       OR pg_catalog.jsonb_typeof(v_map -> 'items') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_map -> 'mac') IS DISTINCT FROM 'string'
       OR v_map ->> 'txid' IS DISTINCT FROM pg_catalog.txid_current()::text
       OR v_map ->> 'backend_pid' IS DISTINCT FROM pg_catalog.pg_backend_pid()::text THEN
        RETURN '[]'::jsonb;
    END IF;
    SELECT secret INTO v_secret
    FROM public.mask_gc_write_permit_secrets
    WHERE singleton IS TRUE;
    IF v_secret IS NULL THEN RETURN '[]'::jsonb; END IF;
    v_body := v_map - 'mac';
    v_expected := pg_catalog.encode(
        public.hmac(pg_catalog.convert_to(v_body::text, 'UTF8'), v_secret, 'sha256'),
        'hex'
    );
    IF v_map ->> 'mac' IS DISTINCT FROM v_expected THEN
        RETURN '[]'::jsonb;
    END IF;
    RETURN v_map -> 'items';
EXCEPTION WHEN OTHERS THEN
    RETURN '[]'::jsonb;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_write_permit_items() FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_permit_is_current(
    p_key text, p_epoch bigint
)
RETURNS boolean
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(
            aap_labeling_v1._mask_gc_write_permit_items()
        ) AS item(value)
        WHERE item.value ->> 'object_key' = p_key
          AND item.value ->> 'epoch' = p_epoch::text
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_permit_is_current(text, bigint) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_register_write_permits(p_items jsonb)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_items jsonb;
    v_map jsonb;
    v_body jsonb;
    v_secret bytea;
    v_signature text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_items) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_items) > 1000 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid permit items';
    END IF;
    v_map := '{}'::jsonb;
    FOR v_items IN
        SELECT permit.value
        FROM pg_catalog.jsonb_array_elements(
            aap_labeling_v1._mask_gc_write_permit_items()
        ) AS permit(value)
    LOOP
        IF aap_labeling_v1._mask_gc_key_valid(v_items ->> 'object_key')
           AND (v_items ->> 'epoch') ~ '^(0|[1-9][0-9]*)$' THEN
            v_map := v_map || pg_catalog.jsonb_build_object(
                v_items ->> 'object_key', (v_items ->> 'epoch')::bigint
            );
        END IF;
    END LOOP;
    FOR v_items IN
        SELECT permit.value
        FROM pg_catalog.jsonb_array_elements(p_items) AS permit(value)
    LOOP
        v_map := v_map || pg_catalog.jsonb_build_object(
            v_items ->> 'object_key', (v_items ->> 'epoch')::bigint
        );
    END LOOP;
    SELECT pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
            'object_key', entry.key, 'epoch', entry.value::text::bigint
        ) ORDER BY entry.key COLLATE "C"
    ) INTO v_items
    FROM pg_catalog.jsonb_each_text(v_map) AS entry;
    v_body := pg_catalog.jsonb_build_object(
        'txid', pg_catalog.txid_current()::text,
        'backend_pid', pg_catalog.pg_backend_pid(),
        'items', COALESCE(v_items, '[]'::jsonb)
    );
    SELECT secret INTO v_secret
    FROM public.mask_gc_write_permit_secrets
    WHERE singleton IS TRUE;
    IF v_secret IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'write permit secret missing';
    END IF;
    v_signature := pg_catalog.encode(
        public.hmac(pg_catalog.convert_to(v_body::text, 'UTF8'), v_secret, 'sha256'),
        'hex'
    );
    PERFORM pg_catalog.set_config(
        'aap.mask_gc_write_permit',
        (v_body || pg_catalog.jsonb_build_object('mac', v_signature))::text,
        true
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_register_write_permits(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_lock_keys(p_keys text[])
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key text;
BEGIN
    FOR v_key IN
        SELECT DISTINCT requested.key COLLATE "C"
        FROM pg_catalog.unnest(p_keys) AS requested(key)
        ORDER BY requested.key COLLATE "C"
    LOOP
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('aap:raster-mask:' || v_key, 0)
        );
    END LOOP;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_lock_keys(text[]) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_row_identity(
    p_fields text[], p_row jsonb
)
RETURNS text
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.string_agg(
        CASE
            WHEN field.name = 'created_at'
                 AND p_row ->> field.name IS NOT NULL THEN
                pg_catalog.to_char(
                    ((p_row ->> field.name)::timestamptz AT TIME ZONE 'UTC'),
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                )
            ELSE p_row ->> field.name
        END,
        ':' ORDER BY field.ordinality
    )
    FROM pg_catalog.unnest(p_fields) WITH ORDINALITY AS field(name, ordinality)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_row_identity(text[], jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_sync_root_row(
    p_root_kind text,
    p_old_id text,
    p_old_row jsonb,
    p_new_id text,
    p_new_row jsonb
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_ref record;
    v_epoch bigint;
    v_active_operation uuid;
    v_old_live_keys text[] := ARRAY[]::text[];
    v_keys text[] := ARRAY[]::text[];
BEGIN
    IF p_old_row IS NOT NULL AND p_old_id IS NOT NULL THEN
        SELECT COALESCE(
            pg_catalog.array_agg(refs.object_key ORDER BY refs.object_key COLLATE "C"),
            ARRAY[]::text[]
        ) INTO v_old_live_keys
        FROM public.mask_gc_roots AS refs
        WHERE refs.root_kind = p_root_kind
          AND refs.root_id = p_old_id
          AND aap_labeling_v1._mask_gc_root_entry_live(
                refs.root_kind, refs.root_id, refs.valid_until, false
          );
        SELECT COALESCE(
            pg_catalog.array_agg(ref.object_key ORDER BY ref.object_key COLLATE "C"),
            ARRAY[]::text[]
        ) INTO v_keys
        FROM aap_labeling_v1._mask_gc_row_refs(p_root_kind, p_old_row) AS ref;
    END IF;
    IF p_new_row IS NOT NULL AND p_new_id IS NOT NULL THEN
        SELECT COALESCE(
            pg_catalog.array_agg(refs.object_key ORDER BY refs.object_key COLLATE "C"),
            ARRAY[]::text[]
        ) INTO v_keys
        FROM (
            SELECT DISTINCT all_refs.object_key
            FROM (
                SELECT unnest(v_keys) AS object_key
                UNION ALL
                SELECT ref.object_key
                FROM aap_labeling_v1._mask_gc_row_refs(p_root_kind, p_new_row) AS ref
            ) AS all_refs
        ) AS refs;
    END IF;

    PERFORM aap_labeling_v1._mask_gc_lock_keys(v_keys);

    IF p_old_row IS NOT NULL AND p_old_id IS NOT NULL THEN
        DELETE FROM public.mask_gc_roots AS root
        WHERE root.root_kind = p_root_kind AND root.root_id = p_old_id;
    END IF;

    IF p_new_row IS NULL OR p_new_id IS NULL THEN RETURN; END IF;
    FOR v_ref IN
        SELECT DISTINCT ref.object_key COLLATE "C" AS object_key, ref.valid_until
        FROM aap_labeling_v1._mask_gc_row_refs(p_root_kind, p_new_row) AS ref
        ORDER BY ref.object_key COLLATE "C"
    LOOP
        v_epoch := NULL;
        v_active_operation := NULL;
        SELECT epoch.epoch, epoch.active_operation_id
        INTO v_epoch, v_active_operation
        FROM public.mask_gc_key_epochs AS epoch
        WHERE epoch.object_key = v_ref.object_key;
        IF FOUND AND v_active_operation IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'AM409', MESSAGE = 'content_deleting';
        END IF;
        IF COALESCE(v_epoch, 0) > 0
           AND NOT aap_labeling_v1._mask_gc_permit_is_current(
                v_ref.object_key, v_epoch
           )
           AND NOT (v_ref.object_key = ANY(v_old_live_keys))
           AND NOT aap_labeling_v1._mask_gc_has_live_root(v_ref.object_key, false) THEN
            RAISE EXCEPTION USING ERRCODE = 'AM409', MESSAGE = 'content_epoch_changed';
        END IF;
        INSERT INTO public.mask_gc_roots(root_kind, root_id, object_key, valid_until)
        VALUES (p_root_kind, p_new_id, v_ref.object_key, v_ref.valid_until)
        ON CONFLICT (root_kind, root_id, object_key)
        DO UPDATE SET valid_until = EXCLUDED.valid_until;
    END LOOP;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_sync_root_row(text, text, jsonb, text, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_old jsonb;
    v_new jsonb;
    v_old_id text;
    v_new_id text;
    v_root_kind text := TG_ARGV[0];
    v_identity_fields text[] := TG_ARGV[1:];
BEGIN
    IF TG_OP <> 'INSERT' THEN
        v_old := pg_catalog.to_jsonb(OLD);
        v_old_id := aap_labeling_v1._mask_gc_row_identity(v_identity_fields, v_old);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        v_new := pg_catalog.to_jsonb(NEW);
        v_new_id := aap_labeling_v1._mask_gc_row_identity(v_identity_fields, v_new);
    END IF;
    PERFORM aap_labeling_v1._mask_gc_sync_root_row(
        v_root_kind, v_old_id, v_old, v_new_id, v_new
    );
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger() FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_refresh_tracker_scope(p_input_id uuid)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scope public.video_tracker_input_scopes%ROWTYPE;
BEGIN
    IF p_input_id IS NULL THEN RETURN; END IF;
    SELECT scope.* INTO v_scope
    FROM public.video_tracker_input_scopes AS scope
    WHERE scope.input_id = p_input_id;
    IF FOUND THEN
        PERFORM aap_labeling_v1._mask_gc_sync_root_row(
            'video_tracker_input_scopes',
            v_scope.tracker_job_id::text,
            pg_catalog.to_jsonb(v_scope),
            v_scope.tracker_job_id::text,
            pg_catalog.to_jsonb(v_scope)
        );
    END IF;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_refresh_tracker_scope(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_tracker_job_changed()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input_id uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT scope.input_id INTO v_input_id
        FROM public.video_tracker_input_scopes AS scope
        WHERE scope.tracker_job_id = OLD.id;
    ELSE
        SELECT scope.input_id INTO v_input_id
        FROM public.video_tracker_input_scopes AS scope
        WHERE scope.tracker_job_id = NEW.id;
    END IF;
    IF v_input_id IS NOT NULL THEN
        PERFORM aap_labeling_v1._mask_gc_refresh_tracker_scope(v_input_id);
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_tracker_job_changed() FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_tracker_pin_changed()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        PERFORM aap_labeling_v1._mask_gc_refresh_tracker_scope(OLD.input_id);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        PERFORM aap_labeling_v1._mask_gc_refresh_tracker_scope(NEW.input_id);
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_tracker_pin_changed() FROM PUBLIC;
-- aap-statement-break

CREATE TRIGGER trg_mask_gc_roots_annotations
AFTER INSERT OR UPDATE OR DELETE ON public.annotations
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('annotations', 'id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_predictions
AFTER INSERT OR UPDATE OR DELETE ON public.predictions
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('predictions', 'id', 'created_at');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_uploads
AFTER INSERT OR UPDATE OR DELETE ON public.raster_mask_uploads
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('raster_mask_uploads', 'id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_tracker_jobs
AFTER INSERT OR UPDATE OR DELETE ON public.video_tracker_jobs
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('video_tracker_jobs', 'id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_accept_decisions
AFTER INSERT OR UPDATE OR DELETE ON public.ai_mask_accept_decisions
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('ai_mask_accept_decisions', 'id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_revisions
AFTER INSERT OR UPDATE OR DELETE ON public.mask_annotation_revisions
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('mask_annotation_revisions', 'id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_qc_runs
AFTER INSERT OR UPDATE OR DELETE ON public.mask_qc_runs
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('mask_qc_runs', 'id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_qc_issues
AFTER INSERT OR UPDATE OR DELETE ON public.mask_qc_issues
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('mask_qc_issues', 'id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_review_scopes
AFTER INSERT OR UPDATE OR DELETE ON public.mask_review_scopes
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('mask_review_scopes', 'id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_tracker_scopes
AFTER INSERT OR UPDATE OR DELETE ON public.video_tracker_input_scopes
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('video_tracker_input_scopes', 'tracker_job_id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_roots_repair_batches
AFTER INSERT OR UPDATE OR DELETE ON public.mask_repair_batches
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_sync_root_trigger('mask_repair_batches', 'id');
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_tracker_jobs_retention
AFTER INSERT OR UPDATE OR DELETE ON public.video_tracker_jobs
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_tracker_job_changed();
-- aap-statement-break
CREATE TRIGGER trg_mask_gc_tracker_pins_retention
AFTER INSERT OR UPDATE OR DELETE ON public.annotation_video_tracker_input_sources
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._mask_gc_tracker_pin_changed();
-- aap-statement-break

LOCK TABLE public.annotations, public.predictions, public.raster_mask_uploads,
    public.video_tracker_jobs, public.ai_mask_accept_decisions,
    public.mask_annotation_revisions, public.mask_qc_runs, public.mask_qc_issues,
    public.mask_review_scopes, public.video_tracker_input_scopes,
    public.annotation_video_tracker_input_sources, public.mask_repair_batches
    IN SHARE ROW EXCLUSIVE MODE;
-- aap-statement-break
INSERT INTO public.mask_gc_roots(root_kind, root_id, object_key, valid_until)
SELECT roots.root_kind, roots.root_id, roots.object_key, roots.valid_until
FROM (
    SELECT 'annotations'::text AS root_kind, source.id::text AS root_id,
           refs.object_key, refs.valid_until
    FROM public.annotations AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'annotations', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'predictions',
           aap_labeling_v1._mask_gc_row_identity(
               ARRAY['id', 'created_at'], pg_catalog.to_jsonb(source.*)
           ),
           refs.object_key, refs.valid_until
    FROM public.predictions AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'predictions', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'raster_mask_uploads', source.id::text,
           refs.object_key, refs.valid_until
    FROM public.raster_mask_uploads AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'raster_mask_uploads', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'video_tracker_jobs', source.id::text,
           refs.object_key, refs.valid_until
    FROM public.video_tracker_jobs AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'video_tracker_jobs', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'ai_mask_accept_decisions', source.id::text,
           refs.object_key, refs.valid_until
    FROM public.ai_mask_accept_decisions AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'ai_mask_accept_decisions', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'mask_annotation_revisions', source.id::text,
           refs.object_key, refs.valid_until
    FROM public.mask_annotation_revisions AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'mask_annotation_revisions', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'mask_qc_runs', source.id::text,
           refs.object_key, refs.valid_until
    FROM public.mask_qc_runs AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'mask_qc_runs', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'mask_qc_issues', source.id::text,
           refs.object_key, refs.valid_until
    FROM public.mask_qc_issues AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'mask_qc_issues', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'mask_review_scopes', source.id::text,
           refs.object_key, refs.valid_until
    FROM public.mask_review_scopes AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'mask_review_scopes', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'video_tracker_input_scopes', source.tracker_job_id::text,
           refs.object_key, refs.valid_until
    FROM public.video_tracker_input_scopes AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'video_tracker_input_scopes', pg_catalog.to_jsonb(source.*)
    ) AS refs
    UNION ALL
    SELECT 'mask_repair_batches', source.id::text,
           refs.object_key, refs.valid_until
    FROM public.mask_repair_batches AS source
    CROSS JOIN LATERAL aap_labeling_v1._mask_gc_row_refs(
        'mask_repair_batches', pg_catalog.to_jsonb(source.*)
    ) AS refs
) AS roots
WHERE aap_labeling_v1._mask_gc_key_valid(roots.object_key)
ON CONFLICT (root_kind, root_id, object_key) DO UPDATE
SET valid_until = EXCLUDED.valid_until;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_epoch_request(
    p_payload jsonb, p_expect_epochs boolean
)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_values jsonb;
    v_field text;
    v_item jsonb;
    v_key text;
    v_epoch bigint;
    v_normalized jsonb := '[]'::jsonb;
    v_count integer;
BEGIN
    IF p_payload IS NULL
       OR pg_catalog.octet_length(p_payload::text) > 1048576
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid payload';
    END IF;
    v_field := CASE WHEN p_expect_epochs THEN 'items' ELSE 'object_keys' END;
    IF (p_payload - ARRAY[v_field]) IS DISTINCT FROM '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_payload -> v_field) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid payload fields';
    END IF;
    v_values := p_payload -> v_field;
    v_count := pg_catalog.jsonb_array_length(v_values);
    IF v_count > 1000 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'too many object keys';
    END IF;
    FOR v_item IN SELECT entry.value FROM pg_catalog.jsonb_array_elements(v_values) AS entry(value)
    LOOP
        IF p_expect_epochs THEN
            IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object'
               OR (v_item - ARRAY['object_key', 'epoch']) IS DISTINCT FROM '{}'::jsonb
               OR pg_catalog.jsonb_typeof(v_item -> 'object_key') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_item -> 'epoch') IS DISTINCT FROM 'number'
               OR v_item ->> 'epoch' !~ '^(0|[1-9][0-9]*)$'
               OR pg_catalog.char_length(v_item ->> 'epoch') > 19
               OR (v_item ->> 'epoch')::numeric > 9223372036854775807 THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid epoch item';
            END IF;
            v_key := v_item ->> 'object_key';
            v_epoch := (v_item ->> 'epoch')::bigint;
        ELSE
            IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid object key';
            END IF;
            v_key := v_item #>> '{}';
            v_epoch := 0;
        END IF;
        IF NOT aap_labeling_v1._mask_gc_key_valid(v_key)
           OR EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(v_normalized) AS prior(value)
                WHERE prior.value ->> 'object_key' = v_key
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid or duplicate key';
        END IF;
        v_normalized := v_normalized || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object('object_key', v_key, 'epoch', v_epoch)
        );
    END LOOP;
    SELECT COALESCE(
        pg_catalog.jsonb_agg(item.value ORDER BY item.value ->> 'object_key' COLLATE "C"),
        '[]'::jsonb
    ) INTO v_normalized
    FROM pg_catalog.jsonb_array_elements(v_normalized) AS item(value);
    RETURN v_normalized;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_epoch_request(jsonb, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.read_mask_write_epochs(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_requested jsonb;
    v_items jsonb := '[]'::jsonb;
    v_item jsonb;
    v_key text;
    v_epoch bigint;
    v_active_operation uuid;
BEGIN
    v_requested := aap_labeling_v1._mask_gc_epoch_request(p_payload, false);
    FOR v_item IN SELECT entry.value FROM pg_catalog.jsonb_array_elements(v_requested) AS entry(value)
    LOOP
        v_key := v_item ->> 'object_key';
        v_epoch := 0;
        v_active_operation := NULL;
        SELECT epoch.epoch, epoch.active_operation_id
        INTO v_epoch, v_active_operation
        FROM public.mask_gc_key_epochs AS epoch
        WHERE epoch.object_key = v_key;
        IF v_active_operation IS NOT NULL THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'content_deleting');
        END IF;
        v_items := v_items || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object('object_key', v_key, 'epoch', COALESCE(v_epoch, 0))
        );
    END LOOP;
    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object('items', v_items), NULL
    );
EXCEPTION WHEN SQLSTATE '22023' THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_mask_write_epochs(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.begin_mask_content_write(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_requested jsonb;
    v_items jsonb := '[]'::jsonb;
    v_item jsonb;
    v_key text;
    v_epoch bigint;
    v_active_operation uuid;
    v_now timestamptz;
BEGIN
    v_requested := aap_labeling_v1._mask_gc_epoch_request(p_payload, false);
    PERFORM aap_labeling_v1._mask_gc_lock_keys(ARRAY(
        SELECT entry.value ->> 'object_key'
        FROM pg_catalog.jsonb_array_elements(v_requested) AS entry(value)
    ));
    v_now := pg_catalog.clock_timestamp();
    FOR v_item IN SELECT entry.value FROM pg_catalog.jsonb_array_elements(v_requested) AS entry(value)
    LOOP
        v_key := v_item ->> 'object_key';
        v_epoch := 0;
        v_active_operation := NULL;
        SELECT epoch.epoch, epoch.active_operation_id
        INTO v_epoch, v_active_operation
        FROM public.mask_gc_key_epochs AS epoch
        WHERE epoch.object_key = v_key;
        IF v_active_operation IS NOT NULL THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'content_deleting');
        END IF;
        v_items := v_items || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object('object_key', v_key, 'epoch', COALESCE(v_epoch, 0))
        );
    END LOOP;
    FOR v_item IN SELECT entry.value FROM pg_catalog.jsonb_array_elements(v_items) AS entry(value)
    LOOP
        v_key := v_item ->> 'object_key';
        INSERT INTO public.mask_gc_key_epochs AS current_epoch(
            object_key, epoch, last_write_at, updated_at
        ) VALUES (
            v_key, (v_item ->> 'epoch')::bigint, v_now, v_now
        )
        ON CONFLICT (object_key) DO UPDATE
        SET last_write_at = EXCLUDED.last_write_at,
            updated_at = EXCLUDED.updated_at
        WHERE current_epoch.active_operation_id IS NULL
        RETURNING epoch, active_operation_id INTO v_epoch, v_active_operation;
        IF NOT FOUND OR v_active_operation IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'AM409', MESSAGE = 'content_deleting';
        END IF;
    END LOOP;
    PERFORM aap_labeling_v1._mask_gc_register_write_permits(v_items);
    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object('items', v_items), NULL
    );
EXCEPTION WHEN SQLSTATE '22023' THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
WHEN SQLSTATE 'AM409' THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'content_deleting');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.begin_mask_content_write(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.lock_mask_write_epochs(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_expected jsonb;
    v_items jsonb := '[]'::jsonb;
    v_item jsonb;
    v_key text;
    v_expected_epoch bigint;
    v_actual_epoch bigint;
    v_active_operation uuid;
BEGIN
    v_expected := aap_labeling_v1._mask_gc_epoch_request(p_payload, true);
    PERFORM aap_labeling_v1._mask_gc_lock_keys(ARRAY(
        SELECT entry.value ->> 'object_key'
        FROM pg_catalog.jsonb_array_elements(v_expected) AS entry(value)
    ));
    FOR v_item IN SELECT entry.value FROM pg_catalog.jsonb_array_elements(v_expected) AS entry(value)
    LOOP
        v_key := v_item ->> 'object_key';
        v_expected_epoch := (v_item ->> 'epoch')::bigint;
        v_actual_epoch := 0;
        v_active_operation := NULL;
        SELECT epoch.epoch, epoch.active_operation_id
        INTO v_actual_epoch, v_active_operation
        FROM public.mask_gc_key_epochs AS epoch
        WHERE epoch.object_key = v_key;
        IF v_active_operation IS NOT NULL THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'content_deleting');
        END IF;
        IF COALESCE(v_actual_epoch, 0) <> v_expected_epoch THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'content_epoch_changed');
        END IF;
        v_items := v_items || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object('object_key', v_key, 'epoch', v_expected_epoch)
        );
    END LOOP;
    PERFORM aap_labeling_v1._mask_gc_register_write_permits(v_items);
    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object('items', v_items), NULL
    );
EXCEPTION WHEN SQLSTATE '22023' THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_mask_write_epochs(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_sweep_claim(p_sweep public.mask_gc_sweeps)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'sweep_id', (p_sweep).sweep_id,
        'generation', (p_sweep).generation,
        'lease_token', (p_sweep).lease_token,
        'lease_expires_at', (p_sweep).lease_expires_at,
        'cutoff', (p_sweep).cutoff,
        'after_key', (p_sweep).after_key,
        'complete', (p_sweep).complete
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_sweep_claim(public.mask_gc_sweeps) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.claim_mask_gc_sweep(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_worker_id uuid;
    v_lease_seconds integer := 30;
    v_sweep public.mask_gc_sweeps%ROWTYPE;
    v_now timestamptz;
BEGIN
    IF p_payload IS NULL
       OR pg_catalog.octet_length(p_payload::text) > 1048576
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['worker_id', 'lease_seconds']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ? 'worker_id')
       OR pg_catalog.jsonb_typeof(p_payload -> 'worker_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'worker_id' !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;
    v_worker_id := (p_payload ->> 'worker_id')::uuid;
    IF p_payload ? 'lease_seconds' THEN
        IF pg_catalog.jsonb_typeof(p_payload -> 'lease_seconds') IS DISTINCT FROM 'number'
           OR p_payload ->> 'lease_seconds' !~ '^[0-9]+$'
           OR pg_catalog.char_length(p_payload ->> 'lease_seconds') > 3
           OR (p_payload ->> 'lease_seconds')::integer NOT BETWEEN 5 AND 300 THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        v_lease_seconds := (p_payload ->> 'lease_seconds')::integer;
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap:mask-gc:sweep', 0)
    );
    SELECT sweep.* INTO v_sweep
    FROM public.mask_gc_sweeps AS sweep
    WHERE sweep.complete IS FALSE
    FOR UPDATE;
    v_now := pg_catalog.clock_timestamp();
    IF NOT FOUND THEN
        INSERT INTO public.mask_gc_sweeps(
            sweep_id, generation, worker_id, lease_token, lease_expires_at, cutoff
        ) VALUES (
            pg_catalog.gen_random_uuid(), 1, v_worker_id, pg_catalog.gen_random_uuid(),
            v_now + pg_catalog.make_interval(secs => v_lease_seconds),
            v_now - interval '24 hours'
        )
        RETURNING * INTO v_sweep;
    ELSIF v_sweep.lease_expires_at <= v_now THEN
        UPDATE public.mask_gc_sweeps
        SET generation = generation + 1,
            worker_id = v_worker_id,
            lease_token = pg_catalog.gen_random_uuid(),
            lease_expires_at = v_now + pg_catalog.make_interval(secs => v_lease_seconds),
            updated_at = v_now
        WHERE sweep_id = v_sweep.sweep_id
        RETURNING * INTO v_sweep;
    ELSIF v_sweep.worker_id = v_worker_id THEN
        UPDATE public.mask_gc_sweeps
        SET lease_expires_at = v_now + pg_catalog.make_interval(secs => v_lease_seconds),
            updated_at = v_now
        WHERE sweep_id = v_sweep.sweep_id
        RETURNING * INTO v_sweep;
    ELSE
        RETURN aap_labeling_v1._mask_gc_reply(
            pg_catalog.jsonb_build_object('sweep', NULL), NULL
        );
    END IF;
    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object(
            'sweep', aap_labeling_v1._mask_gc_sweep_claim(v_sweep)
        ),
        NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.claim_mask_gc_sweep(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.stage_mask_gc_page(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_sweep_id uuid;
    v_generation bigint;
    v_lease_token uuid;
    v_input_after_key text;
    v_page jsonb;
    v_page_after_key text;
    v_complete boolean;
    v_page_digest text;
    v_page_count integer;
    v_scanned integer := 0;
    v_staged integer := 0;
    v_skipped integer := 0;
    v_page_result jsonb;
    v_sweep public.mask_gc_sweeps%ROWTYPE;
    v_item jsonb;
    v_key text;
    v_etag text;
    v_last_modified timestamptz;
    v_byte_size bigint;
    v_previous_key text;
    v_epoch bigint;
    v_active_operation uuid;
    v_last_write_at timestamptz;
    v_operation_id uuid;
    v_key_list text[];
    v_has_upload boolean;
    v_stage_keys text[] := ARRAY[]::text[];
    v_stage_etags text[] := ARRAY[]::text[];
    v_stage_last_modified timestamptz[] := ARRAY[]::timestamptz[];
    v_stage_byte_sizes bigint[] := ARRAY[]::bigint[];
    v_stage_epochs bigint[] := ARRAY[]::bigint[];
    v_stage_index integer;
    v_now timestamptz;
BEGIN
    IF p_payload IS NULL
       OR pg_catalog.octet_length(p_payload::text) > 1048576
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'sweep_id', 'generation', 'lease_token', 'after_key', 'page'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'sweep_id', 'generation', 'lease_token', 'after_key', 'page'
       ])
       OR pg_catalog.jsonb_typeof(p_payload -> 'sweep_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'sweep_id' !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'generation') IS DISTINCT FROM 'number'
       OR p_payload ->> 'generation' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'generation') > 19
       OR (p_payload ->> 'generation')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'lease_token') IS DISTINCT FROM 'string'
       OR p_payload ->> 'lease_token' !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (p_payload -> 'after_key' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_payload -> 'after_key') IS DISTINCT FROM 'string'
                OR pg_catalog.octet_length(p_payload ->> 'after_key') > 1024
                OR (p_payload ->> 'after_key') NOT LIKE 'raster-masks/sha256/%'))
       OR pg_catalog.jsonb_typeof(p_payload -> 'page') IS DISTINCT FROM 'object'
       OR ((p_payload -> 'page') - ARRAY['items', 'after_key', 'complete'])
            IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload -> 'page' ?& ARRAY['items', 'after_key', 'complete'])
       OR pg_catalog.jsonb_typeof(p_payload #> '{page,items}') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_payload #> '{page,items}') > 1000
       OR pg_catalog.jsonb_typeof(p_payload #> '{page,complete}') IS DISTINCT FROM 'boolean'
       OR (p_payload #> '{page,after_key}' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_payload #> '{page,after_key}') IS DISTINCT FROM 'string'
                OR pg_catalog.octet_length(p_payload #>> '{page,after_key}') > 1024
                OR (p_payload #>> '{page,after_key}') NOT LIKE 'raster-masks/sha256/%')) THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;
    v_sweep_id := (p_payload ->> 'sweep_id')::uuid;
    v_generation := (p_payload ->> 'generation')::bigint;
    v_lease_token := (p_payload ->> 'lease_token')::uuid;
    v_input_after_key := CASE
        WHEN p_payload -> 'after_key' = 'null'::jsonb THEN NULL
        ELSE p_payload ->> 'after_key'
    END;
    v_page := p_payload -> 'page';
    v_page_after_key := CASE
        WHEN v_page -> 'after_key' = 'null'::jsonb THEN NULL
        ELSE v_page ->> 'after_key'
    END;
    v_complete := (v_page ->> 'complete')::boolean;
    v_page_count := pg_catalog.jsonb_array_length(v_page -> 'items');
    v_scanned := v_page_count;
    v_page_digest := pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_object(
                    'after_key', p_payload -> 'after_key',
                    'page', v_page
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );

    SELECT sweep.* INTO v_sweep
    FROM public.mask_gc_sweeps AS sweep
    WHERE sweep.sweep_id = v_sweep_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'lease_lost');
    END IF;
    v_now := pg_catalog.clock_timestamp();
    IF v_sweep.generation <> v_generation
       OR v_sweep.lease_token <> v_lease_token THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'lease_lost');
    END IF;
    IF v_sweep.last_page_input_after_key IS NOT DISTINCT FROM v_input_after_key
       AND v_sweep.last_page_digest IS NOT NULL THEN
        IF v_sweep.last_page_digest = v_page_digest THEN
            RETURN v_sweep.last_page_result;
        END IF;
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'cursor_conflict');
    END IF;
    IF v_sweep.complete OR v_sweep.lease_expires_at <= v_now THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'lease_lost');
    END IF;
    IF v_sweep.after_key IS DISTINCT FROM v_input_after_key THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'cursor_conflict');
    END IF;
    IF v_page_after_key IS NULL THEN
        IF NOT v_complete OR v_page_count > 0 THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'cursor_conflict');
        END IF;
    ELSIF NOT v_complete
          AND v_input_after_key IS NOT NULL
          AND v_page_after_key COLLATE "C" <= v_input_after_key COLLATE "C" THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'cursor_conflict');
    END IF;

    v_key_list := ARRAY[]::text[];
    v_previous_key := NULL;
    FOR v_item IN
        SELECT entry.value
        FROM pg_catalog.jsonb_array_elements(v_page -> 'items') AS entry(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object'
           OR (v_item - ARRAY['object_key', 'etag', 'last_modified', 'byte_size'])
                IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_item ?& ARRAY[
                'object_key', 'etag', 'last_modified', 'byte_size'
           ])
           OR pg_catalog.jsonb_typeof(v_item -> 'object_key') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_item -> 'etag') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_item -> 'last_modified') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_item -> 'byte_size') IS DISTINCT FROM 'number'
           OR v_item ->> 'byte_size' !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.char_length(v_item ->> 'byte_size') > 19
           OR (v_item ->> 'byte_size')::numeric > 9223372036854775807
           OR pg_catalog.octet_length(v_item ->> 'etag') NOT BETWEEN 1 AND 255
           OR (v_item ->> 'etag') ~ '[[:cntrl:]]'
           OR (v_item ->> 'last_modified') !~
                '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$'
           OR NOT aap_labeling_v1._mask_gc_key_valid(v_item ->> 'object_key') THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        v_key := v_item ->> 'object_key';
        BEGIN
            v_last_modified := (v_item ->> 'last_modified')::timestamptz;
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END;
        IF (v_input_after_key IS NOT NULL AND v_key COLLATE "C" <= v_input_after_key COLLATE "C")
           OR (v_page_after_key IS NULL)
           OR v_key COLLATE "C" > v_page_after_key COLLATE "C"
           OR (v_previous_key IS NOT NULL AND v_key COLLATE "C" <= v_previous_key COLLATE "C") THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'cursor_conflict');
        END IF;
        v_previous_key := v_key;
        v_key_list := pg_catalog.array_append(v_key_list, v_key);
    END LOOP;
    PERFORM aap_labeling_v1._mask_gc_lock_keys(v_key_list);

    FOR v_item IN
        SELECT entry.value
        FROM pg_catalog.jsonb_array_elements(v_page -> 'items') AS entry(value)
        ORDER BY entry.value ->> 'object_key' COLLATE "C"
    LOOP
        v_key := v_item ->> 'object_key';
        v_etag := v_item ->> 'etag';
        v_last_modified := (v_item ->> 'last_modified')::timestamptz;
        v_byte_size := (v_item ->> 'byte_size')::bigint;
        IF v_last_modified >= v_sweep.cutoff THEN
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;
        v_epoch := NULL;
        v_active_operation := NULL;
        v_last_write_at := NULL;
        SELECT epoch.epoch, epoch.active_operation_id, epoch.last_write_at
        INTO v_epoch, v_active_operation, v_last_write_at
        FROM public.mask_gc_key_epochs AS epoch
        WHERE epoch.object_key = v_key;
        IF v_last_write_at IS NOT NULL
           AND v_last_write_at >= v_sweep.cutoff THEN
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;
        SELECT EXISTS (
            SELECT 1 FROM public.raster_mask_uploads AS upload
            WHERE upload.object_key = v_key
              AND upload.created_at >= v_sweep.cutoff
        ) INTO v_has_upload;
        IF v_active_operation IS NOT NULL
           OR v_has_upload
           OR aap_labeling_v1._mask_gc_has_live_root(v_key, true) THEN
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;
        IF COALESCE(v_epoch, 0) = 9223372036854775807 THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
        END IF;
        v_stage_keys := pg_catalog.array_append(v_stage_keys, v_key);
        v_stage_etags := pg_catalog.array_append(v_stage_etags, v_etag);
        v_stage_last_modified := pg_catalog.array_append(
            v_stage_last_modified, v_last_modified
        );
        v_stage_byte_sizes := pg_catalog.array_append(v_stage_byte_sizes, v_byte_size);
        v_stage_epochs := pg_catalog.array_append(
            v_stage_epochs, COALESCE(v_epoch, 0) + 1
        );
        v_staged := v_staged + 1;
    END LOOP;

    FOR v_stage_index IN 1..COALESCE(pg_catalog.array_length(v_stage_keys, 1), 0)
    LOOP
        v_key := v_stage_keys[v_stage_index];
        v_epoch := v_stage_epochs[v_stage_index];
        v_operation_id := pg_catalog.gen_random_uuid();
        INSERT INTO public.mask_gc_intents(
            operation_id, object_key, etag, last_modified, byte_size, epoch,
            sweep_id, grace_cutoff, state, next_check_at
        ) VALUES (
            v_operation_id, v_key, v_stage_etags[v_stage_index],
            v_stage_last_modified[v_stage_index],
            v_stage_byte_sizes[v_stage_index], v_epoch,
            v_sweep.sweep_id, v_sweep.cutoff, 'pending', v_now
        );
        INSERT INTO public.mask_gc_key_epochs(
            object_key, epoch, active_operation_id, updated_at
        ) VALUES (v_key, v_epoch, v_operation_id, v_now)
        ON CONFLICT (object_key) DO UPDATE
        SET epoch = EXCLUDED.epoch,
            active_operation_id = EXCLUDED.active_operation_id,
            updated_at = EXCLUDED.updated_at;
    END LOOP;

    v_sweep.after_key := v_page_after_key;
    v_sweep.complete := v_complete;
    v_sweep.updated_at := v_now;
    v_page_result := aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object(
            'sweep', aap_labeling_v1._mask_gc_sweep_claim(v_sweep),
            'scanned', v_scanned,
            'staged', v_staged,
            'skipped', v_skipped
        ),
        NULL
    );
    UPDATE public.mask_gc_sweeps
    SET after_key = v_page_after_key,
        complete = v_complete,
        last_page_input_after_key = v_input_after_key,
        last_page_digest = v_page_digest,
        last_page_result = v_page_result,
        updated_at = v_now
    WHERE sweep_id = v_sweep.sweep_id;
    RETURN v_page_result;
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.stage_mask_gc_page(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.claim_mask_gc_deletes(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_worker_id uuid;
    v_limit integer := 100;
    v_lease_seconds integer := 30;
    v_items jsonb;
BEGIN
    IF p_payload IS NULL
       OR pg_catalog.octet_length(p_payload::text) > 1048576
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['worker_id', 'limit', 'lease_seconds']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ? 'worker_id')
       OR pg_catalog.jsonb_typeof(p_payload -> 'worker_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'worker_id' !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;
    v_worker_id := (p_payload ->> 'worker_id')::uuid;
    IF p_payload ? 'limit' THEN
        IF pg_catalog.jsonb_typeof(p_payload -> 'limit') IS DISTINCT FROM 'number'
           OR p_payload ->> 'limit' !~ '^[0-9]+$'
           OR pg_catalog.char_length(p_payload ->> 'limit') > 3
           OR (p_payload ->> 'limit')::integer NOT BETWEEN 1 AND 100 THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        v_limit := (p_payload ->> 'limit')::integer;
    END IF;
    IF p_payload ? 'lease_seconds' THEN
        IF pg_catalog.jsonb_typeof(p_payload -> 'lease_seconds') IS DISTINCT FROM 'number'
           OR p_payload ->> 'lease_seconds' !~ '^[0-9]+$'
           OR pg_catalog.char_length(p_payload ->> 'lease_seconds') > 3
           OR (p_payload ->> 'lease_seconds')::integer NOT BETWEEN 5 AND 300 THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        v_lease_seconds := (p_payload ->> 'lease_seconds')::integer;
    END IF;

    WITH ready AS (
        SELECT intent.operation_id
        FROM public.mask_gc_intents AS intent
        WHERE intent.state IN ('pending', 'indeterminate')
          AND intent.next_check_at <= pg_catalog.clock_timestamp()
          AND (
                intent.claim_lease_expires_at IS NULL
                OR intent.claim_lease_expires_at <= pg_catalog.clock_timestamp()
          )
        ORDER BY intent.next_check_at, intent.object_key COLLATE "C", intent.operation_id
        FOR UPDATE SKIP LOCKED
        LIMIT v_limit
    ),
    claimed AS (
        UPDATE public.mask_gc_intents AS intent
        SET claim_generation = intent.claim_generation + 1,
            claim_worker_id = v_worker_id,
            claim_lease_token = pg_catalog.gen_random_uuid(),
            claim_lease_expires_at = pg_catalog.clock_timestamp()
                + pg_catalog.make_interval(secs => v_lease_seconds),
            updated_at = pg_catalog.clock_timestamp()
        FROM ready
        WHERE intent.operation_id = ready.operation_id
        RETURNING intent.*
    )
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'operation_id', claimed.operation_id,
                'object_key', claimed.object_key,
                'etag', claimed.etag,
                'last_modified', claimed.last_modified,
                'byte_size', claimed.byte_size,
                'epoch', claimed.epoch,
                'generation', claimed.claim_generation,
                'lease_token', claimed.claim_lease_token,
                'lease_expires_at', claimed.claim_lease_expires_at
            )
            ORDER BY claimed.next_check_at,
                     claimed.object_key COLLATE "C",
                     claimed.operation_id
        ),
        '[]'::jsonb
    ) INTO v_items
    FROM claimed;
    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object('items', v_items), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.claim_mask_gc_deletes(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.finish_mask_gc_delete(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation_id uuid;
    v_generation bigint;
    v_lease_token uuid;
    v_receipt jsonb;
    v_error_code text;
    v_status text;
    v_outcome text;
    v_request jsonb;
    v_receipt_last_modified timestamptz;
    v_intent public.mask_gc_intents%ROWTYPE;
    v_active_operation uuid;
    v_state text;
    v_completed boolean := false;
    v_now timestamptz;
BEGIN
    IF p_payload IS NULL
       OR pg_catalog.octet_length(p_payload::text) > 1048576
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'operation_id', 'generation', 'lease_token', 'receipt', 'error_code'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'operation_id', 'generation', 'lease_token', 'receipt', 'error_code'
       ])
       OR pg_catalog.jsonb_typeof(p_payload -> 'operation_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'operation_id' !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'generation') IS DISTINCT FROM 'number'
       OR p_payload ->> 'generation' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'generation') > 19
       OR (p_payload ->> 'generation')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'lease_token') IS DISTINCT FROM 'string'
       OR p_payload ->> 'lease_token' !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (p_payload -> 'receipt' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_payload -> 'receipt') IS DISTINCT FROM 'object')
       OR (p_payload -> 'error_code' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_payload -> 'error_code') IS DISTINCT FROM 'string') THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;
    v_operation_id := (p_payload ->> 'operation_id')::uuid;
    v_generation := (p_payload ->> 'generation')::bigint;
    v_lease_token := (p_payload ->> 'lease_token')::uuid;
    v_receipt := p_payload -> 'receipt';
    v_error_code := CASE
        WHEN p_payload -> 'error_code' = 'null'::jsonb THEN NULL
        ELSE p_payload ->> 'error_code'
    END;

    SELECT intent.object_key INTO v_intent.object_key
    FROM public.mask_gc_intents AS intent
    WHERE intent.operation_id = v_operation_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'lease_lost');
    END IF;
    PERFORM aap_labeling_v1._mask_gc_lock_keys(ARRAY[v_intent.object_key]);
    SELECT intent.* INTO v_intent
    FROM public.mask_gc_intents AS intent
    WHERE intent.operation_id = v_operation_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'lease_lost');
    END IF;
    v_now := pg_catalog.clock_timestamp();
    IF v_intent.state = 'completed'
       OR v_intent.claim_generation <> v_generation
       OR v_intent.claim_lease_token IS DISTINCT FROM v_lease_token
       OR v_intent.claim_lease_expires_at IS NULL
       OR v_intent.claim_lease_expires_at <= v_now THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'lease_lost');
    END IF;

    IF v_receipt = 'null'::jsonb THEN
        IF v_error_code IS NULL
           OR v_error_code NOT IN ('peer_unavailable', 'invalid_response') THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        UPDATE public.mask_gc_intents
        SET state = 'indeterminate',
            outcome = NULL,
            next_check_at = v_now + interval '30 seconds',
            last_error_code = v_error_code,
            claim_worker_id = NULL,
            claim_lease_token = NULL,
            claim_lease_expires_at = NULL,
            updated_at = v_now,
            completed_at = NULL
        WHERE operation_id = v_operation_id;
        v_state := 'indeterminate';
    ELSE
        IF v_error_code IS NOT NULL
           OR (v_receipt - ARRAY['operation_id', 'request', 'state', 'outcome'])
                IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_receipt ?& ARRAY['operation_id', 'request', 'state', 'outcome'])
           OR pg_catalog.jsonb_typeof(v_receipt -> 'operation_id') IS DISTINCT FROM 'string'
           OR v_receipt ->> 'operation_id' IS DISTINCT FROM v_operation_id::text
           OR pg_catalog.jsonb_typeof(v_receipt -> 'state') IS DISTINCT FROM 'string' THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
        END IF;
        v_status := v_receipt ->> 'state';
        v_request := v_receipt -> 'request';
        v_outcome := CASE
            WHEN v_receipt -> 'outcome' = 'null'::jsonb THEN NULL
            ELSE v_receipt ->> 'outcome'
        END;
        IF v_status NOT IN ('pending', 'indeterminate', 'completed')
           OR pg_catalog.jsonb_typeof(v_request) IS DISTINCT FROM 'object'
           OR (v_request - ARRAY[
                'operation_id', 'object_key', 'etag', 'last_modified'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_request ?& ARRAY[
                'operation_id', 'object_key', 'etag', 'last_modified'
           ])
           OR pg_catalog.jsonb_typeof(v_request -> 'operation_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_request -> 'object_key') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_request -> 'etag') IS DISTINCT FROM 'string'
           OR v_request ->> 'operation_id' IS DISTINCT FROM v_operation_id::text
           OR v_request ->> 'object_key' IS DISTINCT FROM v_intent.object_key
           OR v_request ->> 'etag' IS DISTINCT FROM v_intent.etag
           OR pg_catalog.jsonb_typeof(v_request -> 'last_modified') IS DISTINCT FROM 'string'
           OR (v_request ->> 'last_modified') !~
                '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
        END IF;
        BEGIN
            v_receipt_last_modified :=
                (v_request ->> 'last_modified')::timestamptz;
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
        END;
        IF v_receipt_last_modified IS DISTINCT FROM v_intent.last_modified THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
        END IF;
        IF v_status IN ('pending', 'indeterminate') THEN
            IF v_outcome IS NOT NULL THEN
                RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
            END IF;
            v_state := v_status;
            UPDATE public.mask_gc_intents
            SET state = v_state,
                outcome = NULL,
                next_check_at = v_now + CASE
                    WHEN v_state = 'pending' THEN interval '10 seconds'
                    ELSE interval '30 seconds'
                END,
                last_error_code = NULL,
                claim_worker_id = NULL,
                claim_lease_token = NULL,
                claim_lease_expires_at = NULL,
                updated_at = v_now,
                completed_at = NULL
            WHERE operation_id = v_operation_id;
        ELSE
            IF v_outcome IS NULL
               OR v_outcome NOT IN ('deleted', 'missing', 'content_changed') THEN
                RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
            END IF;
            SELECT epoch.active_operation_id
            INTO v_active_operation
            FROM public.mask_gc_key_epochs AS epoch
            WHERE epoch.object_key = v_intent.object_key
            FOR UPDATE;
            IF NOT FOUND OR v_active_operation IS DISTINCT FROM v_operation_id THEN
                RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
            END IF;
            UPDATE public.mask_gc_intents
            SET state = 'completed',
                outcome = v_outcome,
                next_check_at = v_now,
                last_error_code = NULL,
                claim_worker_id = NULL,
                claim_lease_token = NULL,
                claim_lease_expires_at = NULL,
                updated_at = v_now,
                completed_at = v_now
            WHERE operation_id = v_operation_id;
            UPDATE public.mask_gc_key_epochs
            SET active_operation_id = NULL,
                updated_at = v_now
            WHERE object_key = v_intent.object_key
              AND active_operation_id = v_operation_id;
            DELETE FROM public.raster_mask_uploads AS upload
            WHERE upload.object_key = v_intent.object_key
              AND upload.created_at < v_intent.grace_cutoff;
            v_state := 'completed';
            v_completed := true;
        END IF;
    END IF;
    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', v_operation_id,
            'state', v_state,
            'completed', v_completed
        ),
        NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.finish_mask_gc_delete(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.read_mask_gc_progress(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_active integer;
    v_indeterminate integer;
    v_completed integer;
    v_sweep public.mask_gc_sweeps%ROWTYPE;
    v_sweep_json jsonb := NULL;
BEGIN
    IF p_payload IS NULL
       OR pg_catalog.octet_length(p_payload::text) > 1048576
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR p_payload IS DISTINCT FROM '{}'::jsonb THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;
    SELECT
        count(*) FILTER (WHERE intent.state = 'pending')::integer,
        count(*) FILTER (WHERE intent.state = 'indeterminate')::integer,
        count(*) FILTER (WHERE intent.state = 'completed')::integer
    INTO v_active, v_indeterminate, v_completed
    FROM public.mask_gc_intents AS intent;
    SELECT sweep.* INTO v_sweep
    FROM public.mask_gc_sweeps AS sweep
    WHERE sweep.complete IS FALSE
    ORDER BY sweep.created_at DESC, sweep.sweep_id
    LIMIT 1;
    IF FOUND THEN
        v_sweep_json := aap_labeling_v1._mask_gc_sweep_claim(v_sweep);
    END IF;
    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object(
            'active_intents', COALESCE(v_active, 0),
            'indeterminate_intents', COALESCE(v_indeterminate, 0),
            'completed_intents', COALESCE(v_completed, 0),
            'sweep', v_sweep_json
        ),
        NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_mask_gc_progress(jsonb) FROM PUBLIC;
-- aap-statement-break
