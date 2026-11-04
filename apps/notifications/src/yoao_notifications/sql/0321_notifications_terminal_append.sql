CREATE FUNCTION aap_notifications_v1.append_job_terminal_notification(p_request jsonb)
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
       OR NOT aap_notifications_v1._preference_object_keys_valid(
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
REVOKE ALL ON FUNCTION aap_notifications_v1.append_job_terminal_notification(jsonb) FROM PUBLIC;
