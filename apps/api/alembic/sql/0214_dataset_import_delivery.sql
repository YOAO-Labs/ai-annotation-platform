CREATE TABLE public.dataset_import_deliveries (
    id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    job_id uuid NOT NULL,
    kind text NOT NULL CHECK (
        kind IN ('thumbnail', 'video_metadata', 'notification')
    ),
    target_id uuid NOT NULL,
    lease_token uuid,
    lease_expires_at timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT ck_dataset_import_deliveries_lease_pair CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_dataset_import_deliveries_delivered_lease CHECK (
        delivered_at IS NULL OR lease_token IS NULL
    ),
    CONSTRAINT uq_dataset_import_deliveries_kind_target UNIQUE (kind, target_id)
);
-- aap-statement-break
REVOKE ALL ON public.dataset_import_deliveries FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.dataset_import_deliveries IS
    'Assets-owned at-least-once broker handoffs for committed connector-import results.';
-- aap-statement-break
CREATE INDEX ix_dataset_import_deliveries_pending_created
    ON public.dataset_import_deliveries (created_at, id)
    WHERE delivered_at IS NULL;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_import_delivery_reply(
    p_data jsonb, p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NOT NULL THEN
        pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-import-delivery/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-import-delivery/v1',
        'ok', true,
        'data', p_data
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_import_delivery_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.queue_dataset_import_delivery(
    p_job_id uuid, p_kind text, p_target_id uuid
)
RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_job_id IS NULL OR p_kind IS NULL OR p_target_id IS NULL
       OR p_kind NOT IN ('thumbnail', 'video_metadata', 'notification') THEN
        RAISE EXCEPTION 'invalid dataset import delivery';
    END IF;

    INSERT INTO public.dataset_import_deliveries (
        job_id, kind, target_id
    ) VALUES (
        p_job_id, p_kind, p_target_id
    )
    ON CONFLICT (kind, target_id) DO UPDATE SET
        job_id = EXCLUDED.job_id,
        lease_token = NULL,
        lease_expires_at = NULL,
        attempts = 0,
        delivered_at = NULL,
        created_at = pg_catalog.now()
    WHERE public.dataset_import_deliveries.job_id IS DISTINCT FROM EXCLUDED.job_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.queue_dataset_import_delivery(uuid, text, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.dataset_import_delivery_target_is_current(
    p_kind text, p_target_id uuid
)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE p_kind
        WHEN 'thumbnail' THEN EXISTS (
            SELECT 1
            FROM public.dataset_items AS item
            WHERE item.id = p_target_id
              AND item.file_type = 'image'
              AND item.file_path IS NOT NULL
              AND item.file_path <> ''
        )
        WHEN 'video_metadata' THEN EXISTS (
            SELECT 1
            FROM public.dataset_items AS item
            WHERE item.id = p_target_id
              AND item.file_type = 'video'
              AND item.file_path IS NOT NULL
              AND item.file_path <> ''
        )
        ELSE false
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.dataset_import_delivery_target_is_current(text, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.claim_dataset_import_delivery(
    p_lease_token uuid, p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_delivery public.dataset_import_deliveries%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_lease_token IS NULL
       OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RETURN aap_assets_v1._dataset_import_delivery_reply(NULL, 'invalid_request');
    END IF;

    WITH candidate AS (
        SELECT delivery.id
        FROM public.dataset_import_deliveries AS delivery
        WHERE delivery.delivered_at IS NULL
          AND (
              delivery.lease_expires_at IS NULL
              OR delivery.lease_expires_at <= v_now
          )
        ORDER BY delivery.created_at, delivery.id
        FOR UPDATE OF delivery SKIP LOCKED
        LIMIT 1
    )
    UPDATE public.dataset_import_deliveries AS delivery
    SET lease_token = p_lease_token,
        lease_expires_at = v_now + pg_catalog.make_interval(secs => p_lease_seconds),
        attempts = delivery.attempts + 1
    FROM candidate
    WHERE delivery.id = candidate.id
    RETURNING delivery.* INTO v_delivery;

    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_delivery_reply(NULL, NULL);
    END IF;

    RETURN aap_assets_v1._dataset_import_delivery_reply(
        pg_catalog.jsonb_build_object(
            'delivery_id', v_delivery.id,
            'job_id', v_delivery.job_id,
            'kind', v_delivery.kind,
            'target_id', v_delivery.target_id,
            'lease_token', v_delivery.lease_token,
            'lease_expires_at', v_delivery.lease_expires_at
        ),
        NULL
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_dataset_import_delivery(uuid, integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_dataset_import_delivery(
    p_delivery_id uuid, p_lease_token uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_delivery_id IS NULL OR p_lease_token IS NULL THEN
        RETURN aap_assets_v1._dataset_import_delivery_reply(NULL, 'invalid_request');
    END IF;

    UPDATE public.dataset_import_deliveries AS delivery
    SET delivered_at = v_now,
        lease_token = NULL,
        lease_expires_at = NULL
    WHERE delivery.id = p_delivery_id
      AND delivery.lease_token = p_lease_token
      AND delivery.lease_expires_at > v_now
      AND delivery.delivered_at IS NULL;

    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_import_delivery_reply(NULL, 'lease_lost');
    END IF;

    RETURN aap_assets_v1._dataset_import_delivery_reply(
        pg_catalog.jsonb_build_object(
            'delivery_id', p_delivery_id,
            'lease_token', p_lease_token,
            'delivered', true
        ),
        NULL
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_dataset_import_delivery(uuid, uuid)
    FROM PUBLIC;
