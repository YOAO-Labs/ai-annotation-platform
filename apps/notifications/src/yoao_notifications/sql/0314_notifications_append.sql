CREATE FUNCTION aap_notifications_v1.append_notification(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_id uuid;
    v_user_id uuid;
    v_type text;
    v_target_type text;
    v_target_id uuid;
    v_payload jsonb;
    v_notification public.notifications%ROWTYPE;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-notification-append/v1'
       OR p_request ->> 'operation' IS DISTINCT FROM 'append'
       OR NOT aap_notifications_v1._preference_object_keys_valid(
            p_request,
            ARRAY['schema', 'operation', 'id', 'user_id', 'type', 'target_type', 'target_id', 'payload'],
            8,
            8
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
       OR pg_catalog.char_length(p_request ->> 'type') NOT BETWEEN 1 AND 60
       OR (p_request ->> 'type') ~ '[[:cntrl:]]'
       OR pg_catalog.jsonb_typeof(p_request -> 'target_type') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_request ->> 'target_type') NOT BETWEEN 0 AND 30
       OR pg_catalog.jsonb_typeof(p_request -> 'payload') IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-append/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_id := (p_request ->> 'id')::uuid;
    v_user_id := (p_request ->> 'user_id')::uuid;
    v_type := p_request ->> 'type';
    v_target_type := p_request ->> 'target_type';
    v_target_id := (p_request ->> 'target_id')::uuid;
    v_payload := p_request -> 'payload';

    INSERT INTO public.notifications (
        id, user_id, type, target_type, target_id, payload
    ) VALUES (
        v_id, v_user_id, v_type, v_target_type, v_target_id, v_payload
    )
    RETURNING * INTO v_notification;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notification-append/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation', 'append',
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
REVOKE ALL ON FUNCTION aap_notifications_v1.append_notification(jsonb) FROM PUBLIC;
