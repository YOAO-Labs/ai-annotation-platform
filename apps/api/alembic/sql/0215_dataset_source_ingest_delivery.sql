-- Operation-scoped media outbox for atomic synchronous source ingestion.
CREATE TABLE public.dataset_source_ingest_deliveries (
    id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    operation_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('thumbnail', 'video_metadata')),
    target_id uuid NOT NULL,
    lease_token uuid,
    lease_expires_at timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT ck_dataset_source_ingest_deliveries_lease_pair CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_dataset_source_ingest_deliveries_delivered_lease CHECK (
        delivered_at IS NULL OR lease_token IS NULL
    ),
    CONSTRAINT uq_dataset_source_ingest_deliveries_target UNIQUE (
        operation_id, kind, target_id
    )
);
-- aap-statement-break
REVOKE ALL ON public.dataset_source_ingest_deliveries FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.dataset_source_ingest_deliveries IS
    'Assets-owned operation-scoped at-least-once media handoffs for committed ZIP and bucket ingests.';
-- aap-statement-break
CREATE INDEX ix_dataset_source_ingest_deliveries_pending_created
    ON public.dataset_source_ingest_deliveries (created_at, id)
    WHERE delivered_at IS NULL;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_delivery_reply(
    p_data jsonb, p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NOT NULL THEN
        pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-source-ingest-delivery/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-source-ingest-delivery/v1',
        'ok', true,
        'data', p_data
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_delivery_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._dataset_source_ingest_delivery_target_is_current(
    p_operation_id uuid, p_kind text, p_target_id uuid
)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE p_kind
        WHEN 'thumbnail' THEN EXISTS (
            SELECT 1
            FROM public.dataset_source_ingest_operations AS operation
            JOIN public.dataset_items AS item
              ON item.dataset_id = operation.dataset_id
            WHERE operation.operation_id = p_operation_id
              AND operation.state = 'completed'
              AND item.id = p_target_id
              AND item.file_type = 'image'
              AND item.file_path IS NOT NULL
              AND item.file_path <> ''
        )
        WHEN 'video_metadata' THEN EXISTS (
            SELECT 1
            FROM public.dataset_source_ingest_operations AS operation
            JOIN public.dataset_items AS item
              ON item.dataset_id = operation.dataset_id
            WHERE operation.operation_id = p_operation_id
              AND operation.state = 'completed'
              AND item.id = p_target_id
              AND item.file_type = 'video'
              AND item.file_path IS NOT NULL
              AND item.file_path <> ''
        )
        ELSE false
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_source_ingest_delivery_target_is_current(uuid, text, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.claim_dataset_source_ingest_delivery(
    p_lease_token uuid, p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_delivery public.dataset_source_ingest_deliveries%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_expires_at timestamptz;
BEGIN
    IF p_lease_token IS NULL OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RETURN aap_assets_v1._dataset_source_ingest_delivery_reply(NULL, 'invalid_request');
    END IF;

    WITH stale AS (
        SELECT delivery.id
        FROM public.dataset_source_ingest_deliveries AS delivery
        WHERE delivery.delivered_at IS NULL
          AND (delivery.lease_expires_at IS NULL OR delivery.lease_expires_at <= v_now)
          AND NOT aap_assets_v1._dataset_source_ingest_delivery_target_is_current(
              delivery.operation_id, delivery.kind, delivery.target_id
          )
        ORDER BY delivery.created_at, delivery.id
        FOR UPDATE OF delivery SKIP LOCKED
        LIMIT 1000
    )
    UPDATE public.dataset_source_ingest_deliveries AS delivery
    SET delivered_at = v_now,
        lease_token = NULL,
        lease_expires_at = NULL
    FROM stale
    WHERE delivery.id = stale.id;

    WITH candidate AS (
        SELECT delivery.id
        FROM public.dataset_source_ingest_deliveries AS delivery
        WHERE delivery.delivered_at IS NULL
          AND (delivery.lease_expires_at IS NULL OR delivery.lease_expires_at <= v_now)
          AND aap_assets_v1._dataset_source_ingest_delivery_target_is_current(
              delivery.operation_id, delivery.kind, delivery.target_id
          )
        ORDER BY delivery.created_at, delivery.id
        FOR UPDATE OF delivery SKIP LOCKED
        LIMIT 1
    )
    UPDATE public.dataset_source_ingest_deliveries AS delivery
    SET lease_token = p_lease_token,
        lease_expires_at = v_now + pg_catalog.make_interval(secs => p_lease_seconds),
        attempts = delivery.attempts + 1
    FROM candidate
    WHERE delivery.id = candidate.id
    RETURNING delivery.* INTO v_delivery;

    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_delivery_reply(NULL, NULL);
    END IF;

    v_expires_at := v_delivery.lease_expires_at;
    RETURN aap_assets_v1._dataset_source_ingest_delivery_reply(
        pg_catalog.jsonb_build_object(
            'id', v_delivery.id,
            'operation_id', v_delivery.operation_id,
            'kind', v_delivery.kind,
            'target_id', v_delivery.target_id,
            'lease_token', v_delivery.lease_token,
            'lease_expires_at', v_expires_at
        ),
        NULL
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_dataset_source_ingest_delivery(uuid, integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_dataset_source_ingest_delivery(
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
        RETURN aap_assets_v1._dataset_source_ingest_delivery_reply(NULL, 'invalid_request');
    END IF;

    UPDATE public.dataset_source_ingest_deliveries AS delivery
    SET delivered_at = v_now,
        lease_token = NULL,
        lease_expires_at = NULL
    WHERE delivery.id = p_delivery_id
      AND delivery.lease_token = p_lease_token
      AND delivery.lease_expires_at > v_now
      AND delivery.delivered_at IS NULL
      AND aap_assets_v1._dataset_source_ingest_delivery_target_is_current(
          delivery.operation_id, delivery.kind, delivery.target_id
      );

    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_delivery_reply(NULL, 'lease_lost');
    END IF;

    RETURN aap_assets_v1._dataset_source_ingest_delivery_reply(
        pg_catalog.jsonb_build_object(
            'id', p_delivery_id,
            'lease_token', p_lease_token,
            'delivered', true
        ),
        NULL
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_dataset_source_ingest_delivery(uuid, uuid)
    FROM PUBLIC;
