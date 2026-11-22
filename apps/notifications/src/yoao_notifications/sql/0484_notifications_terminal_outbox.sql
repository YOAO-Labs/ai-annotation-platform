ALTER TABLE public.notification_delivery_outbox
    DROP CONSTRAINT notification_delivery_outbox_notification_type_check,
    ADD CONSTRAINT notification_delivery_outbox_notification_type_check CHECK (
        notification_type IN (
            'user.deactivation_requested', 'user.deactivation_completed',
            'job.completed', 'job.failed', 'job.cancelled'
        )
    );
-- aap-statement-break
COMMENT ON TABLE public.notification_delivery_outbox IS
    'Notifications-owned at-least-once Redis handoffs for committed inbox rows.';
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.append_job_terminal_notification(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_id uuid;
    v_user_id uuid;
    v_type text;
    v_target_id uuid;
    v_payload jsonb;
    v_created boolean := false;
    v_notification public.notifications%ROWTYPE;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-notification-terminal-append/v1'
       OR p_request ->> 'operation' IS DISTINCT FROM 'append_job_terminal'
       OR NOT yoao_notifications_v1._preference_object_keys_valid(
            p_request,
            ARRAY['schema', 'operation', 'id', 'user_id', 'type', 'target_id', 'payload'],
            7,
            7
       )
       OR pg_catalog.jsonb_typeof(p_request -> 'id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE
       OR pg_catalog.jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'user_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE
       OR pg_catalog.jsonb_typeof(p_request -> 'target_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'target_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE
       OR pg_catalog.jsonb_typeof(p_request -> 'type') IS DISTINCT FROM 'string'
       OR p_request ->> 'type' NOT IN ('job.completed', 'job.failed', 'job.cancelled')
       OR pg_catalog.jsonb_typeof(p_request -> 'payload') IS DISTINCT FROM 'object'
       OR p_request -> 'payload' ->> 'status' IS DISTINCT FROM
            pg_catalog.substr(p_request ->> 'type', 5) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-terminal-append/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_id := (p_request ->> 'id')::uuid;
    v_user_id := (p_request ->> 'user_id')::uuid;
    v_type := p_request ->> 'type';
    v_target_id := (p_request ->> 'target_id')::uuid;
    v_payload := p_request -> 'payload';

    IF pg_catalog.current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-terminal-append/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'unsupported_isolation')
        );
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-dataset-import-notification:' || v_user_id::text || ':'
                || v_type || ':' || v_target_id::text,
            0
        )
    );

    SELECT notification.* INTO v_notification
    FROM public.notifications AS notification
    WHERE notification.user_id = v_user_id
      AND notification.type = v_type
      AND notification.target_type = 'async_job'
      AND notification.target_id = v_target_id
    LIMIT 1;

    IF NOT FOUND THEN
        INSERT INTO public.notifications (
            id, user_id, type, target_type, target_id, payload
        ) VALUES (
            v_id, v_user_id, v_type, 'async_job', v_target_id, v_payload
        )
        RETURNING * INTO v_notification;
        INSERT INTO public.notification_delivery_outbox (
            notification_id, operation_id, recipient_id, notification_type
        ) VALUES (v_notification.id, v_target_id, v_user_id, v_type);
        v_created := true;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notification-terminal-append/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation', 'append_job_terminal',
            'created', v_created,
            'notification', pg_catalog.jsonb_build_object(
                'id', v_notification.id,
                'user_id', v_notification.user_id,
                'type', v_notification.type,
                'target_type', v_notification.target_type,
                'target_id', v_notification.target_id,
                'payload', v_notification.payload,
                'read_at', v_notification.read_at,
                'created_at', pg_catalog.to_char(
                    v_notification.created_at AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                )
            )
        )
    );
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.claim_identity_deliveries(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_field_count integer;
    v_worker_id uuid;
    v_limit integer;
    v_lease_seconds integer;
    v_items jsonb;
    v_mixed boolean := (p_request->>'schema' IS NOT DISTINCT FROM 'aap-notifications-delivery/v2');
    v_schema text := CASE WHEN v_mixed THEN 'aap-notifications-delivery/v2'
                          ELSE 'aap-notifications-delivery/v1' END;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 4
       OR NOT (p_request ?& ARRAY['schema', 'worker_id', 'limit', 'lease_seconds'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN ('schema', 'worker_id', 'limit', 'lease_seconds')
       )
       OR p_request->>'schema' IS DISTINCT FROM v_schema
       OR pg_catalog.jsonb_typeof(p_request->'worker_id') IS DISTINCT FROM 'string'
       OR (p_request->>'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'limit') IS DISTINCT FROM 'number'
       OR (p_request->>'limit') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_request->'lease_seconds') IS DISTINCT FROM 'number'
       OR (p_request->>'lease_seconds') !~ '^[1-9][0-9]*$' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        v_worker_id := (p_request->>'worker_id')::uuid;
        v_limit := (p_request->>'limit')::integer;
        v_lease_seconds := (p_request->>'lease_seconds')::integer;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    IF v_limit NOT BETWEEN 1 AND 100 OR v_lease_seconds NOT BETWEEN 1 AND 300 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    WITH candidate AS (
        SELECT outbox.notification_id
        FROM public.notification_delivery_outbox AS outbox
        WHERE outbox.state = 'pending'
          AND outbox.available_at <= pg_catalog.clock_timestamp()
          AND (outbox.lease_expires_at IS NULL
               OR outbox.lease_expires_at <= pg_catalog.clock_timestamp())
          AND (v_mixed OR outbox.notification_type IN (
              'user.deactivation_requested', 'user.deactivation_completed'
          ))
        ORDER BY outbox.available_at, outbox.notification_id
        FOR UPDATE OF outbox SKIP LOCKED
        LIMIT v_limit
    ), claimed AS (
        UPDATE public.notification_delivery_outbox AS outbox
        SET attempt = outbox.attempt + 1,
            worker_id = v_worker_id,
            lease_id = pg_catalog.gen_random_uuid(),
            lease_expires_at = pg_catalog.clock_timestamp()
                + pg_catalog.make_interval(secs => v_lease_seconds)
        FROM candidate
        WHERE outbox.notification_id = candidate.notification_id
        RETURNING outbox.notification_id, outbox.worker_id, outbox.lease_id,
                  outbox.lease_expires_at, outbox.notification_type
    )
    SELECT COALESCE(pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
            'id', notification.id,
            'user_id', notification.user_id,
            'type', notification.type,
            'target_type', notification.target_type,
            'target_id', notification.target_id,
            'payload', notification.payload,
            'created_at', notification.created_at,
            'worker_id', claimed.worker_id,
            'lease_id', claimed.lease_id,
            'lease_expires_at', claimed.lease_expires_at
        ) ORDER BY notification.created_at, notification.id
    ), '[]'::jsonb)
    INTO v_items
    FROM claimed
    JOIN public.notifications AS notification
      ON notification.id = claimed.notification_id
     AND notification.type = claimed.notification_type
    WHERE (notification.target_type = 'user' AND notification.type IN (
               'user.deactivation_requested', 'user.deactivation_completed'
           ))
       OR (v_mixed AND notification.target_type = 'async_job' AND notification.type IN (
               'job.completed', 'job.failed', 'job.cancelled'
           ));

    RETURN pg_catalog.jsonb_build_object(
        'schema', v_schema, 'ok', true,
        'data', pg_catalog.jsonb_build_object('items', v_items)
    );
END
$function$;
