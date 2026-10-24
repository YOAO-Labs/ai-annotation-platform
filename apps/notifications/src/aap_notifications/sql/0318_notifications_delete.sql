CREATE FUNCTION aap_notifications_v1.delete_command(p_request pg_catalog.jsonb)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_user_id uuid;
    v_notification_id uuid;
    v_deleted bigint := 0;
BEGIN
    v_operation := p_request ->> 'operation';
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-notification-delete/v1'
       OR v_operation IS NULL
       OR v_operation NOT IN ('delete_one', 'clear_read')
       OR pg_catalog.jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'user_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-delete/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    IF v_operation = 'delete_one' THEN
        IF NOT aap_notifications_v1._preference_object_keys_valid(
            p_request,
            ARRAY['schema', 'operation', 'user_id', 'notification_id'],
            4,
            4
        )
           OR pg_catalog.jsonb_typeof(p_request -> 'notification_id')
                IS DISTINCT FROM 'string'
           OR COALESCE(
                p_request ->> 'notification_id'
                    ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                false
           ) IS NOT TRUE THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-notification-delete/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        v_user_id := (p_request ->> 'user_id')::uuid;
        v_notification_id := (p_request ->> 'notification_id')::uuid;
        DELETE FROM public.notifications AS notification
        WHERE notification.user_id = v_user_id
          AND notification.id = v_notification_id;
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-delete/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'delete_one',
                'deleted', v_deleted = 1
            )
        );
    END IF;

    IF NOT aap_notifications_v1._preference_object_keys_valid(
        p_request,
        ARRAY['schema', 'operation', 'user_id'],
        3,
        3
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-delete/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_user_id := (p_request ->> 'user_id')::uuid;
    DELETE FROM public.notifications AS notification
    WHERE notification.user_id = v_user_id
      AND notification.read_at IS NOT NULL;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notification-delete/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation', 'clear_read',
            'deleted', v_deleted
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.delete_command(pg_catalog.jsonb)
    FROM PUBLIC;
