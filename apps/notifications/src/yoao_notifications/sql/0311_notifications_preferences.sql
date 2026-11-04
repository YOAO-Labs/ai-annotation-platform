CREATE FUNCTION aap_notifications_v1._preference_object_keys_valid(
    p_value jsonb, p_allowed text[], p_minimum integer, p_maximum integer
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count integer;
BEGIN
    IF p_value IS NULL
       OR pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'object'
       OR p_allowed IS NULL
       OR p_minimum IS NULL OR p_maximum IS NULL
       OR p_minimum < 0 OR p_maximum < p_minimum THEN
        RETURN false;
    END IF;
    SELECT pg_catalog.count(*)::integer
    INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_value);
    IF v_count NOT BETWEEN p_minimum AND p_maximum THEN
        RETURN false;
    END IF;
    RETURN NOT EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_object_keys(p_value) AS key(name)
        WHERE NOT key.name = ANY(p_allowed)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1._preference_object_keys_valid(
    jsonb, text[], integer, integer
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1._preference_policy()
RETURNS TABLE(ordinal smallint, notification_type text, default_toast boolean)
LANGUAGE sql IMMUTABLE PARALLEL SAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    VALUES
        (1::smallint, 'bug_report.commented'::text, false),
        (2, 'bug_report.reopened', false),
        (3, 'bug_report.status_changed', false),
        (4, 'batch.rejected', true),
        (5, 'batch.review_reopened', true),
        (6, 'batch.admin_locked', true),
        (7, 'batch.admin_unlocked', false),
        (8, 'batch.unarchived', false),
        (9, 'task.approved', false),
        (10, 'task.rejected', true),
        (11, 'task.reopened', false),
        (12, 'failed_prediction.retry.started', false),
        (13, 'failed_prediction.retry.succeeded', false),
        (14, 'failed_prediction.retry.failed', false),
        (15, 'export.ready', false),
        (16, 'export.failed', true),
        (17, 'job.completed', false),
        (18, 'job.failed', true),
        (19, 'job.cancelled', false),
        (20, 'user.deactivation_requested', false),
        (21, 'user.deactivation_completed', false),
        (22, 'feedback.reply_created', true),
        (23, 'feedback.status_changed', false),
        (24, 'feedback.comment_mentioned', true),
        (25, 'annotation.comment_mentioned', true)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1._preference_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1._preference_jsonb_truthy(p_value jsonb)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE pg_catalog.jsonb_typeof(p_value)
        WHEN 'boolean' THEN p_value = 'true'::jsonb
        WHEN 'number' THEN (p_value #>> '{}')::numeric <> 0
        WHEN 'string' THEN (p_value #>> '{}') <> ''
        WHEN 'array' THEN pg_catalog.jsonb_array_length(p_value) > 0
        WHEN 'object' THEN p_value <> '{}'::jsonb
        ELSE false
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1._preference_jsonb_truthy(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.preference_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_user_id uuid;
    v_type text;
    v_patch jsonb;
    v_default_toast boolean;
    v_channels jsonb;
    v_muted boolean;
    v_items jsonb;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-notification-preferences/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'user_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-preferences/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_operation := p_request ->> 'operation';
    v_user_id := (p_request ->> 'user_id')::uuid;
    IF v_operation = 'get' THEN
        IF NOT aap_notifications_v1._preference_object_keys_valid(
            p_request, ARRAY['schema', 'operation', 'user_id'], 3, 3
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-notification-preferences/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'type', policy.notification_type,
                    'in_app', aap_notifications_v1._preference_jsonb_truthy(
                        COALESCE(preference.channels -> 'in_app', 'true'::jsonb)
                    ),
                    'email', aap_notifications_v1._preference_jsonb_truthy(
                        COALESCE(preference.channels -> 'email', 'false'::jsonb)
                    ),
                    'toast', CASE
                        WHEN pg_catalog.jsonb_typeof(preference.channels -> 'toast')
                                = 'boolean'
                            THEN preference.channels -> 'toast' = 'true'::jsonb
                        ELSE policy.default_toast
                    END
                ) ORDER BY policy.ordinal
            ),
            '[]'::jsonb
        )
        INTO v_items
        FROM aap_notifications_v1._preference_policy() AS policy
        LEFT JOIN public.notification_preferences AS preference
          ON preference.user_id = v_user_id
         AND preference.type = policy.notification_type;

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-preferences/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'get',
                'items', v_items
            )
        );
    ELSIF v_operation = 'patch' THEN
        IF NOT aap_notifications_v1._preference_object_keys_valid(
                p_request,
                ARRAY['schema', 'operation', 'user_id', 'type', 'patch'],
                5,
                5
           )
           OR pg_catalog.jsonb_typeof(p_request -> 'type') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_request ->> 'type') NOT BETWEEN 1 AND 60
           OR (p_request ->> 'type') ~ '[[:cntrl:]]'
           OR NOT aap_notifications_v1._preference_object_keys_valid(
                p_request -> 'patch', ARRAY['in_app', 'toast'], 1, 2
           ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-notification-preferences/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
        IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_object_keys(p_request -> 'patch') AS key(name)
                WHERE key.name NOT IN ('in_app', 'toast')
                   OR pg_catalog.jsonb_typeof(p_request -> 'patch' -> key.name)
                        IS DISTINCT FROM 'boolean'
           ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-notification-preferences/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        v_type := p_request ->> 'type';
        SELECT policy.default_toast
        INTO v_default_toast
        FROM aap_notifications_v1._preference_policy() AS policy
        WHERE policy.notification_type = v_type;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-notification-preferences/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'unknown_type')
            );
        END IF;

        v_patch := p_request -> 'patch';
        INSERT INTO public.notification_preferences (user_id, type, channels)
        VALUES (
            v_user_id,
            v_type,
            pg_catalog.jsonb_build_object(
                'in_app', true,
                'email', false,
                'toast', v_default_toast
            ) || v_patch
        )
        ON CONFLICT (user_id, type) DO UPDATE
        SET channels = public.notification_preferences.channels || v_patch,
            updated_at = pg_catalog.now();

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-preferences/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'patch',
                'updated', true
            )
        );
    ELSIF v_operation = 'is_in_app_muted' THEN
        IF NOT aap_notifications_v1._preference_object_keys_valid(
                p_request, ARRAY['schema', 'operation', 'user_id', 'type'], 4, 4
           )
           OR pg_catalog.jsonb_typeof(p_request -> 'type') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_request ->> 'type') NOT BETWEEN 1 AND 60
           OR (p_request ->> 'type') ~ '[[:cntrl:]]' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-notification-preferences/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        SELECT preference.channels
        INTO v_channels
        FROM public.notification_preferences AS preference
        WHERE preference.user_id = v_user_id
          AND preference.type = p_request ->> 'type';
        v_muted := FOUND AND NOT aap_notifications_v1._preference_jsonb_truthy(
            COALESCE(v_channels -> 'in_app', 'true'::jsonb)
        );

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-preferences/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'is_in_app_muted',
                'muted', v_muted
            )
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notification-preferences/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.preference_command(jsonb) FROM PUBLIC;
