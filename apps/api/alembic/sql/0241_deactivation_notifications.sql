-- Durable, Notifications-owned delivery for Identity self-deactivation events.
-- Install before the adjacent Identity deactivation migration.
CREATE TABLE public.notification_delivery_outbox (
    notification_id uuid PRIMARY KEY
        REFERENCES public.notifications(id) ON DELETE CASCADE,
    operation_id uuid NOT NULL,
    recipient_id uuid NOT NULL,
    notification_type text NOT NULL CHECK (
        notification_type IN (
            'user.deactivation_requested',
            'user.deactivation_completed'
        )
    ),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done')),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    available_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    worker_id uuid,
    lease_id uuid,
    lease_expires_at timestamptz,
    last_reason text CHECK (last_reason IS NULL OR last_reason IN ('published', 'redis_error')),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    delivered_at timestamptz,
    CONSTRAINT uq_notification_delivery_operation_recipient_type
        UNIQUE (operation_id, recipient_id, notification_type),
    CONSTRAINT ck_notification_delivery_lease_identity CHECK (
        (worker_id IS NULL AND lease_id IS NULL AND lease_expires_at IS NULL)
        OR (worker_id IS NOT NULL AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT ck_notification_delivery_state CHECK (
        (state = 'pending' AND delivered_at IS NULL)
        OR (state = 'done' AND delivered_at IS NOT NULL
            AND worker_id IS NULL AND lease_id IS NULL AND lease_expires_at IS NULL)
    )
);
-- aap-statement-break
REVOKE ALL ON public.notification_delivery_outbox FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.notification_delivery_outbox IS
    'Notifications-owned at-least-once Redis handoffs for committed self-deactivation notices.';
-- aap-statement-break
CREATE INDEX ix_notification_delivery_outbox_pending
    ON public.notification_delivery_outbox (available_at, notification_id)
    WHERE state = 'pending';
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.create_identity_deactivation_notifications(
    p_operation_id uuid,
    p_type text,
    p_subject jsonb,
    p_receiver_ids uuid[]
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_field_count integer;
    v_subject_id uuid;
    v_subject_email text;
    v_subject_name text;
    v_subject_role text;
    v_scheduled_at timestamptz;
    v_payload jsonb;
    v_recipient_id uuid;
    v_recipient record;
    v_channels jsonb;
    v_in_app jsonb;
    v_is_muted boolean;
    v_existing uuid;
    v_notification_id uuid;
    v_created bigint := 0;
BEGIN
    IF p_operation_id IS NULL OR p_type IS NULL OR p_subject IS NULL
       OR pg_catalog.jsonb_typeof(p_subject) IS DISTINCT FROM 'object'
       OR p_receiver_ids IS NULL
       OR pg_catalog.array_position(p_receiver_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid identity deactivation notification';
    END IF;

    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_subject);
    IF p_type = 'user.deactivation_requested' THEN
        IF v_field_count <> 5
           OR NOT (p_subject ?& ARRAY['user_id', 'email', 'name', 'role', 'scheduled_at'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(p_subject) AS field(name)
               WHERE field.name NOT IN ('user_id', 'email', 'name', 'role', 'scheduled_at')
           )
           OR pg_catalog.jsonb_typeof(p_subject->'user_id') IS DISTINCT FROM 'string'
           OR (p_subject->>'user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_subject->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_subject->>'email') > 255
           OR pg_catalog.jsonb_typeof(p_subject->'name') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_subject->>'name') > 100
           OR pg_catalog.jsonb_typeof(p_subject->'role') IS DISTINCT FROM 'string'
           OR p_subject->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer')
           OR pg_catalog.jsonb_typeof(p_subject->'scheduled_at') IS DISTINCT FROM 'string'
           OR (p_subject->>'scheduled_at') !~* '(z|[+-][0-9]{2}:[0-9]{2})$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid identity deactivation request subject';
        END IF;
        BEGIN
            v_subject_id := (p_subject->>'user_id')::uuid;
            v_scheduled_at := (p_subject->>'scheduled_at')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid identity deactivation request subject';
        END;
        IF NOT pg_catalog.isfinite(v_scheduled_at) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid identity deactivation request subject';
        END IF;
        v_subject_email := p_subject->>'email';
        v_subject_name := p_subject->>'name';
        v_subject_role := p_subject->>'role';
        v_payload := pg_catalog.jsonb_build_object(
            'email', v_subject_email,
            'name', v_subject_name,
            'role', v_subject_role,
            'scheduled_at', v_scheduled_at
        );
        IF v_subject_role = 'super_admin'
           AND NOT (v_subject_id = ANY(p_receiver_ids)) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'missing self notification recipient';
        END IF;
    ELSIF p_type = 'user.deactivation_completed' THEN
        IF v_field_count <> 3
           OR NOT (p_subject ?& ARRAY['user_id', 'email', 'name'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(p_subject) AS field(name)
               WHERE field.name NOT IN ('user_id', 'email', 'name')
           )
           OR pg_catalog.jsonb_typeof(p_subject->'user_id') IS DISTINCT FROM 'string'
           OR (p_subject->>'user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_subject->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_subject->>'email') > 255
           OR pg_catalog.jsonb_typeof(p_subject->'name') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_subject->>'name') > 100 THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid identity deactivation completion subject';
        END IF;
        BEGIN
            v_subject_id := (p_subject->>'user_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid identity deactivation completion subject';
        END;
        v_subject_email := p_subject->>'email';
        v_subject_name := p_subject->>'name';
        v_payload := pg_catalog.jsonb_build_object(
            'email', v_subject_email,
            'name', v_subject_name,
            'auto', true
        );
    ELSE
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid identity deactivation notification type';
    END IF;

    FOR v_recipient_id IN
        SELECT DISTINCT recipient.id
        FROM pg_catalog.unnest(p_receiver_ids) AS recipient(id)
        ORDER BY recipient.id
    LOOP
        IF p_type = 'user.deactivation_completed'
           AND v_recipient_id = v_subject_id THEN
            CONTINUE;
        END IF;

        SELECT facts.actor_id, facts.platform_role, facts.is_active
        INTO v_recipient
        FROM aap_identity_v1.read_actor_facts(v_recipient_id) AS facts;
        IF NOT FOUND OR v_recipient.actor_id IS DISTINCT FROM v_recipient_id
           OR v_recipient.platform_role IS DISTINCT FROM 'super_admin'
           OR v_recipient.is_active IS DISTINCT FROM true THEN
            CONTINUE;
        END IF;

        SELECT preference.channels INTO v_channels
        FROM public.notification_preferences AS preference
        WHERE preference.user_id = v_recipient_id
          AND preference.type = p_type;
        v_is_muted := false;
        IF FOUND AND pg_catalog.jsonb_typeof(v_channels) = 'object'
           AND v_channels ? 'in_app' THEN
            v_in_app := v_channels->'in_app';
            v_is_muted := CASE pg_catalog.jsonb_typeof(v_in_app)
                WHEN 'null' THEN true
                WHEN 'boolean' THEN v_in_app = 'false'::jsonb
                WHEN 'number' THEN (v_in_app #>> '{}')::numeric = 0
                WHEN 'string' THEN (v_in_app #>> '{}') = ''
                WHEN 'array' THEN pg_catalog.jsonb_array_length(v_in_app) = 0
                WHEN 'object' THEN v_in_app = '{}'::jsonb
                ELSE false
            END;
        END IF;
        IF v_is_muted THEN
            CONTINUE;
        END IF;

        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap-identity-deactivation-notification:' || p_operation_id::text
                    || ':' || v_recipient_id::text || ':' || p_type,
                0
            )
        );
        SELECT outbox.notification_id INTO v_existing
        FROM public.notification_delivery_outbox AS outbox
        WHERE outbox.operation_id = p_operation_id
          AND outbox.recipient_id = v_recipient_id
          AND outbox.notification_type = p_type;
        IF FOUND THEN
            CONTINUE;
        END IF;

        v_notification_id := pg_catalog.gen_random_uuid();
        INSERT INTO public.notifications (
            id, user_id, type, target_type, target_id, payload
        ) VALUES (
            v_notification_id, v_recipient_id, p_type, 'user', v_subject_id, v_payload
        );
        INSERT INTO public.notification_delivery_outbox (
            notification_id, operation_id, recipient_id, notification_type
        ) VALUES (
            v_notification_id, p_operation_id, v_recipient_id, p_type
        );
        v_created := v_created + 1;
    END LOOP;
    RETURN v_created;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.create_identity_deactivation_notifications(
    uuid, text, jsonb, uuid[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.identity_delivery_policy()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-notifications-delivery-policy/v1',
        'policy', 'aap-notifications-delivery/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.identity_delivery_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.claim_identity_deliveries(p_request jsonb)
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
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notifications-delivery/v1', 'ok', false,
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
       OR p_request->>'schema' IS DISTINCT FROM 'aap-notifications-delivery/v1'
       OR pg_catalog.jsonb_typeof(p_request->'worker_id') IS DISTINCT FROM 'string'
       OR (p_request->>'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'limit') IS DISTINCT FROM 'number'
       OR (p_request->>'limit') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_request->'lease_seconds') IS DISTINCT FROM 'number'
       OR (p_request->>'lease_seconds') !~ '^[1-9][0-9]*$' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notifications-delivery/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        v_worker_id := (p_request->>'worker_id')::uuid;
        v_limit := (p_request->>'limit')::integer;
        v_lease_seconds := (p_request->>'lease_seconds')::integer;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notifications-delivery/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    IF v_limit NOT BETWEEN 1 AND 100 OR v_lease_seconds NOT BETWEEN 1 AND 300 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notifications-delivery/v1', 'ok', false,
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
                  outbox.lease_expires_at
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
    WHERE notification.type IN (
        'user.deactivation_requested', 'user.deactivation_completed'
    )
      AND notification.target_type = 'user';

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notifications-delivery/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object('items', v_items)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.claim_identity_deliveries(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.finish_identity_delivery(p_request jsonb)
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
    v_notification_id uuid;
    v_worker_id uuid;
    v_lease_id uuid;
    v_outcome text;
    v_reason text;
    v_accepted boolean := false;
    v_now timestamptz;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notifications-delivery/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 6
       OR NOT (p_request ?& ARRAY[
           'schema', 'notification_id', 'worker_id', 'lease_id', 'outcome', 'reason'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN (
               'schema', 'notification_id', 'worker_id', 'lease_id', 'outcome', 'reason'
           )
       )
       OR p_request->>'schema' IS DISTINCT FROM 'aap-notifications-delivery/v1'
       OR pg_catalog.jsonb_typeof(p_request->'notification_id') IS DISTINCT FROM 'string'
       OR (p_request->>'notification_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'worker_id') IS DISTINCT FROM 'string'
       OR (p_request->>'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'lease_id') IS DISTINCT FROM 'string'
       OR (p_request->>'lease_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'outcome') IS DISTINCT FROM 'string'
       OR p_request->>'outcome' NOT IN ('delivered', 'retry')
       OR pg_catalog.jsonb_typeof(p_request->'reason') IS DISTINCT FROM 'string'
       OR (p_request->>'outcome' = 'delivered' AND p_request->>'reason' <> 'published')
       OR (p_request->>'outcome' = 'retry' AND p_request->>'reason' <> 'redis_error') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notifications-delivery/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        v_notification_id := (p_request->>'notification_id')::uuid;
        v_worker_id := (p_request->>'worker_id')::uuid;
        v_lease_id := (p_request->>'lease_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notifications-delivery/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    v_outcome := p_request->>'outcome';
    v_reason := p_request->>'reason';

    BEGIN
        PERFORM outbox.notification_id
        FROM public.notification_delivery_outbox AS outbox
        WHERE outbox.notification_id = v_notification_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notifications-delivery/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END;
    IF FOUND THEN
        v_now := pg_catalog.clock_timestamp();
        IF v_outcome = 'delivered' THEN
            UPDATE public.notification_delivery_outbox AS outbox
            SET state = 'done',
                worker_id = NULL,
                lease_id = NULL,
                lease_expires_at = NULL,
                last_reason = v_reason,
                delivered_at = v_now
            WHERE outbox.notification_id = v_notification_id
              AND outbox.state = 'pending'
              AND outbox.worker_id = v_worker_id
              AND outbox.lease_id = v_lease_id
              AND outbox.lease_expires_at > v_now;
        ELSE
            UPDATE public.notification_delivery_outbox AS outbox
            SET worker_id = NULL,
                lease_id = NULL,
                lease_expires_at = NULL,
                last_reason = v_reason,
                available_at = v_now + INTERVAL '10 seconds'
            WHERE outbox.notification_id = v_notification_id
              AND outbox.state = 'pending'
              AND outbox.worker_id = v_worker_id
              AND outbox.lease_id = v_lease_id
              AND outbox.lease_expires_at > v_now;
        END IF;
        v_accepted := FOUND;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notifications-delivery/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object('accepted', v_accepted)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.finish_identity_delivery(jsonb)
    FROM PUBLIC;
