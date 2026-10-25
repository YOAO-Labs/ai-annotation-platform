CREATE FUNCTION aap_notifications_v1.read_state_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_user_id uuid;
    v_notification_id uuid;
    v_read_at timestamptz;
    v_updated_ids jsonb := '[]'::jsonb;
    v_updated integer := 0;
BEGIN
    v_operation := p_request ->> 'operation';
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-notification-read-state/v1'
       OR v_operation IS NULL
       OR v_operation NOT IN ('mark_read', 'mark_all_read')
       OR NOT aap_notifications_v1._preference_object_keys_valid(
            p_request,
            CASE v_operation
                WHEN 'mark_read' THEN ARRAY[
                    'schema', 'operation', 'user_id', 'notification_id', 'read_at'
                ]
                ELSE ARRAY['schema', 'operation', 'user_id', 'read_at']
            END,
            CASE v_operation WHEN 'mark_read' THEN 5 ELSE 4 END,
            CASE v_operation WHEN 'mark_read' THEN 5 ELSE 4 END
       )
       OR pg_catalog.jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'user_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE
       OR pg_catalog.jsonb_typeof(p_request -> 'read_at') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'read_at'
                ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?Z$',
            false
       ) IS NOT TRUE
       OR (
            v_operation = 'mark_read'
            AND (
                pg_catalog.jsonb_typeof(p_request -> 'notification_id')
                    IS DISTINCT FROM 'string'
                OR COALESCE(
                    p_request ->> 'notification_id'
                        ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                    false
                ) IS NOT TRUE
            )
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-read-state/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_user_id := (p_request ->> 'user_id')::uuid;
    IF v_operation = 'mark_read' THEN
        v_notification_id := (p_request ->> 'notification_id')::uuid;
    END IF;
    BEGIN
        v_read_at := (p_request ->> 'read_at')::timestamptz;
    EXCEPTION WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-read-state/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;

    WITH updated AS (
        UPDATE public.notifications AS notification
        SET read_at = v_read_at
        WHERE notification.user_id = v_user_id
          AND notification.read_at IS NULL
          AND (v_operation = 'mark_all_read' OR notification.id = v_notification_id)
        RETURNING notification.id
    )
    SELECT COALESCE(
               pg_catalog.jsonb_agg(updated.id ORDER BY updated.id),
               '[]'::jsonb
           )
    INTO v_updated_ids
    FROM updated;

    v_updated := pg_catalog.jsonb_array_length(v_updated_ids);
    IF v_operation = 'mark_read' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-read-state/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'mark_read',
                'updated', v_updated = 1,
                'updated_ids', v_updated_ids
            )
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notification-read-state/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation', 'mark_all_read',
            'updated', v_updated,
            'updated_ids', v_updated_ids
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.read_state_command(jsonb) FROM PUBLIC;
