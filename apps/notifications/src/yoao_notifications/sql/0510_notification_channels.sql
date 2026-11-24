CREATE TABLE yoao_notifications_v1.notification_channels (
    id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    channel_id text NOT NULL UNIQUE
        CHECK (
            channel_id ~ '^[a-z][a-z0-9_-]{1,31}$'
            AND channel_id NOT IN ('in_app', 'toast', 'email', 'external')
        ),
    display_name text NOT NULL
        CHECK (pg_catalog.char_length(pg_catalog.btrim(display_name)) BETWEEN 1 AND 64),
    description text
        CHECK (
            description IS NULL
            OR pg_catalog.char_length(pg_catalog.btrim(description)) BETWEEN 1 AND 280
        ),
    homepage text
        CHECK (homepage IS NULL OR pg_catalog.char_length(homepage) <= 255),
    source_tier text NOT NULL DEFAULT 'third_party'
        CHECK (source_tier IN ('official', 'third_party')),
    mode text NOT NULL CHECK (mode IN ('direct', 'managed')),
    status text NOT NULL CHECK (status IN ('prepared', 'active', 'disabled', 'retired')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    endpoint_url text NOT NULL,
    auth_token_ciphertext text,
    auth_token_sha256 text NOT NULL,
    protocol_version text NOT NULL,
    capabilities jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(capabilities) = 'array'),
    address_schema jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(address_schema) = 'object'),
    manifest jsonb CHECK (manifest IS NULL OR pg_catalog.jsonb_typeof(manifest) = 'object'),
    rate_limit_per_minute integer NOT NULL DEFAULT 60
        CHECK (rate_limit_per_minute BETWEEN 1 AND 600),
    next_dispatch_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    consecutive_auth_errors integer NOT NULL DEFAULT 0 CHECK (consecutive_auth_errors >= 0),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    disabled_at timestamptz,
    retired_at timestamptz,
    CHECK (
        (status = 'retired' AND auth_token_ciphertext IS NULL)
        OR (status <> 'retired' AND auth_token_ciphertext IS NOT NULL)
    )
);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_notifications_v1.notification_channels FROM PUBLIC;
-- aap-statement-break
CREATE TABLE yoao_notifications_v1.user_notification_channel_bindings (
    id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    user_id uuid NOT NULL,
    channel_id text NOT NULL
        REFERENCES yoao_notifications_v1.notification_channels(channel_id) ON DELETE RESTRICT,
    address jsonb NOT NULL
        CHECK (
            pg_catalog.jsonb_typeof(address) = 'object'
            AND pg_catalog.octet_length(address::text) <= 2048
        ),
    locale text NOT NULL DEFAULT 'zh-CN'
        CHECK (locale ~ '^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$'),
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    UNIQUE (user_id, channel_id)
);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_notifications_v1.user_notification_channel_bindings FROM PUBLIC;
-- aap-statement-break
CREATE TABLE yoao_notifications_v1.notification_channel_deliveries (
    delivery_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    operation_id uuid NOT NULL,
    notification_id uuid,
    channel_id text NOT NULL
        REFERENCES yoao_notifications_v1.notification_channels(channel_id) ON DELETE RESTRICT,
    recipient_id uuid NOT NULL,
    notification_type text NOT NULL,
    target_type text NOT NULL,
    target_id uuid NOT NULL,
    project_id uuid,
    task_id uuid,
    envelope_data jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(envelope_data) = 'object'),
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'failed_permanent', 'failed_exhausted', 'skipped')),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL DEFAULT 6 CHECK (max_attempts BETWEEN 1 AND 20),
    available_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    worker_id uuid,
    lease_id uuid,
    lease_expires_at timestamptz,
    last_reason text,
    provider_message_id text,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    finished_at timestamptz,
    UNIQUE (operation_id, recipient_id, channel_id, notification_type)
);
-- aap-statement-break
CREATE INDEX idx_notification_channel_deliveries_pending
    ON yoao_notifications_v1.notification_channel_deliveries (available_at, delivery_id)
    WHERE state = 'pending';
-- aap-statement-break
CREATE INDEX idx_notification_channel_deliveries_recipient_target
    ON yoao_notifications_v1.notification_channel_deliveries (
        recipient_id, notification_type, target_id
    );
-- aap-statement-break
REVOKE ALL ON TABLE yoao_notifications_v1.notification_channel_deliveries FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1._preference_policy_v2()
RETURNS TABLE(
    ordinal smallint,
    notification_type text,
    default_toast boolean,
    external_deliverable boolean
)
LANGUAGE sql IMMUTABLE PARALLEL SAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    VALUES
        (1::smallint, 'bug_report.commented'::text, false, true),
        (2, 'bug_report.reopened', false, true),
        (3, 'bug_report.status_changed', false, true),
        (4, 'batch.rejected', true, true),
        (5, 'batch.review_reopened', true, true),
        (6, 'batch.admin_locked', true, true),
        (7, 'batch.admin_unlocked', false, true),
        (8, 'batch.unarchived', false, true),
        (9, 'task.approved', false, true),
        (10, 'task.rejected', true, true),
        (11, 'task.reopened', false, true),
        (12, 'failed_prediction.retry.started', false, true),
        (13, 'failed_prediction.retry.succeeded', false, true),
        (14, 'failed_prediction.retry.failed', false, true),
        (15, 'export.ready', false, true),
        (16, 'export.failed', true, true),
        (17, 'job.completed', false, true),
        (18, 'job.failed', true, true),
        (19, 'job.cancelled', false, true),
        (20, 'user.deactivation_requested', false, false),
        (21, 'user.deactivation_completed', false, false),
        (22, 'feedback.reply_created', true, true),
        (23, 'feedback.status_changed', false, true),
        (24, 'feedback.comment_mentioned', true, true),
        (25, 'annotation.comment_mentioned', true, true)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1._preference_policy_v2() FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.preference_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_user_id uuid;
    v_type text;
    v_patch jsonb;
    v_external_patch jsonb;
    v_has_patch boolean;
    v_has_external boolean;
    v_default_toast boolean;
    v_external_deliverable boolean;
    v_channels jsonb;
    v_muted boolean;
    v_items jsonb;
    v_key text;
    v_val jsonb;
    v_channel_status text;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'yoao-notification-preferences/v2'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'user_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'yoao-notification-preferences/v2',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_operation := p_request ->> 'operation';
    v_user_id := (p_request ->> 'user_id')::uuid;

    IF v_operation = 'get' THEN
        IF NOT yoao_notifications_v1._preference_object_keys_valid(
            p_request, ARRAY['schema', 'operation', 'user_id'], 3, 3
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'yoao-notification-preferences/v2',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'type', policy.notification_type,
                    'in_app', yoao_notifications_v1._preference_jsonb_truthy(
                        COALESCE(preference.channels -> 'in_app', 'true'::jsonb)
                    ),
                    'email', yoao_notifications_v1._preference_jsonb_truthy(
                        COALESCE(preference.channels -> 'email', 'false'::jsonb)
                    ),
                    'toast', CASE
                        WHEN pg_catalog.jsonb_typeof(preference.channels -> 'toast') = 'boolean'
                            THEN preference.channels -> 'toast' = 'true'::jsonb
                        ELSE policy.default_toast
                    END,
                    'external_channels', CASE
                        WHEN NOT policy.external_deliverable THEN '{}'::jsonb
                        ELSE COALESCE(
                            (
                                SELECT pg_catalog.jsonb_object_agg(
                                    ch.channel_id,
                                    CASE
                                        WHEN pg_catalog.jsonb_typeof(
                                            preference.channels -> 'external' -> ch.channel_id
                                        ) = 'boolean'
                                            THEN (preference.channels -> 'external' -> ch.channel_id) = 'true'::jsonb
                                        ELSE false
                                    END
                                    ORDER BY ch.channel_id
                                )
                                FROM yoao_notifications_v1.notification_channels AS ch
                                WHERE ch.status IN ('active', 'disabled')
                            ),
                            '{}'::jsonb
                        )
                    END
                ) ORDER BY policy.ordinal
            ),
            '[]'::jsonb
        )
        INTO v_items
        FROM yoao_notifications_v1._preference_policy_v2() AS policy
        LEFT JOIN public.notification_preferences AS preference
          ON preference.user_id = v_user_id
         AND preference.type = policy.notification_type;

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'yoao-notification-preferences/v2',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'get',
                'items', v_items
            )
        );

    ELSIF v_operation = 'patch' THEN
        IF NOT yoao_notifications_v1._preference_object_keys_valid(
                p_request,
                ARRAY['schema', 'operation', 'user_id', 'type', 'patch', 'external_channels'],
                5,
                6
           )
           OR pg_catalog.jsonb_typeof(p_request -> 'type') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_request ->> 'type') NOT BETWEEN 1 AND 60
           OR (p_request ->> 'type') ~ '[[:cntrl:]]' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'yoao-notification-preferences/v2',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        v_patch := COALESCE(p_request -> 'patch', '{}'::jsonb);
        IF pg_catalog.jsonb_typeof(v_patch) IS DISTINCT FROM 'object' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'yoao-notification-preferences/v2',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
        v_has_patch := v_patch <> '{}'::jsonb;
        IF v_has_patch AND (
            NOT yoao_notifications_v1._preference_object_keys_valid(
                v_patch, ARRAY['in_app', 'toast'], 1, 2
            )
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_object_keys(v_patch) AS key(name)
                WHERE pg_catalog.jsonb_typeof(v_patch -> key.name) IS DISTINCT FROM 'boolean'
            )
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'yoao-notification-preferences/v2',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        IF p_request ? 'external_channels' AND p_request -> 'external_channels' IS NOT NULL
           AND pg_catalog.jsonb_typeof(p_request -> 'external_channels') IS DISTINCT FROM 'null' THEN
            v_external_patch := p_request -> 'external_channels';
            IF pg_catalog.jsonb_typeof(v_external_patch) IS DISTINCT FROM 'object' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'yoao-notification-preferences/v2',
                    'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                );
            END IF;
            v_has_external := v_external_patch <> '{}'::jsonb;
        ELSE
            v_external_patch := '{}'::jsonb;
            v_has_external := false;
        END IF;

        IF NOT v_has_patch AND NOT v_has_external THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'yoao-notification-preferences/v2',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        v_type := p_request ->> 'type';
        SELECT policy.default_toast, policy.external_deliverable
        INTO v_default_toast, v_external_deliverable
        FROM yoao_notifications_v1._preference_policy_v2() AS policy
        WHERE policy.notification_type = v_type;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'yoao-notification-preferences/v2',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'unknown_type')
            );
        END IF;

        IF v_has_external THEN
            FOR v_key, v_val IN SELECT key, value FROM pg_catalog.jsonb_each(v_external_patch)
            LOOP
                IF v_key !~ '^[a-z][a-z0-9_-]{1,31}$'
                   OR v_key IN ('in_app', 'toast', 'email', 'external')
                   OR pg_catalog.jsonb_typeof(v_val) IS DISTINCT FROM 'boolean' THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'yoao-notification-preferences/v2',
                        'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
                IF NOT v_external_deliverable AND v_val = 'true'::jsonb THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'yoao-notification-preferences/v2',
                        'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'channel_not_deliverable')
                    );
                END IF;
                SELECT ch.status INTO v_channel_status
                FROM yoao_notifications_v1.notification_channels AS ch
                WHERE ch.channel_id = v_key;
                IF NOT FOUND OR v_channel_status NOT IN ('active', 'disabled') THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'yoao-notification-preferences/v2',
                        'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'unknown_channel')
                    );
                END IF;
            END LOOP;
        END IF;

        INSERT INTO public.notification_preferences (user_id, type, channels)
        VALUES (
            v_user_id,
            v_type,
            CASE
                WHEN v_has_external THEN pg_catalog.jsonb_set(
                    pg_catalog.jsonb_build_object(
                        'in_app', true,
                        'email', false,
                        'toast', v_default_toast
                    ) || v_patch,
                    '{external}',
                    v_external_patch,
                    true
                )
                ELSE pg_catalog.jsonb_build_object(
                    'in_app', true,
                    'email', false,
                    'toast', v_default_toast
                ) || v_patch
            END
        )
        ON CONFLICT (user_id, type) DO UPDATE
        SET channels = CASE
                WHEN v_has_external THEN pg_catalog.jsonb_set(
                    public.notification_preferences.channels || v_patch,
                    '{external}',
                    CASE
                        WHEN pg_catalog.jsonb_typeof(
                            public.notification_preferences.channels -> 'external'
                        ) = 'object'
                            THEN (public.notification_preferences.channels -> 'external') || v_external_patch
                        ELSE v_external_patch
                    END,
                    true
                )
                ELSE public.notification_preferences.channels || v_patch
            END,
            updated_at = pg_catalog.now();

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'yoao-notification-preferences/v2',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'patch',
                'updated', true
            )
        );

    ELSIF v_operation = 'is_in_app_muted' THEN
        IF NOT yoao_notifications_v1._preference_object_keys_valid(
                p_request, ARRAY['schema', 'operation', 'user_id', 'type'], 4, 4
           )
           OR pg_catalog.jsonb_typeof(p_request -> 'type') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_request ->> 'type') NOT BETWEEN 1 AND 60
           OR (p_request ->> 'type') ~ '[[:cntrl:]]' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'yoao-notification-preferences/v2',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        SELECT preference.channels
        INTO v_channels
        FROM public.notification_preferences AS preference
        WHERE preference.user_id = v_user_id
          AND preference.type = p_request ->> 'type';
        v_muted := FOUND AND NOT yoao_notifications_v1._preference_jsonb_truthy(
            COALESCE(v_channels -> 'in_app', 'true'::jsonb)
        );

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'yoao-notification-preferences/v2',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'is_in_app_muted',
                'muted', v_muted
            )
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'yoao-notification-preferences/v2',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.preference_command(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1._channel_public_projection(
    p_row yoao_notifications_v1.notification_channels
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_row.id,
        'channel_id', p_row.channel_id,
        'display_name', p_row.display_name,
        'description', p_row.description,
        'homepage', p_row.homepage,
        'source_tier', p_row.source_tier,
        'mode', p_row.mode,
        'status', p_row.status,
        'revision', p_row.revision,
        'endpoint_url', p_row.endpoint_url,
        'auth_token_sha256', p_row.auth_token_sha256,
        'protocol_version', p_row.protocol_version,
        'capabilities', p_row.capabilities,
        'address_schema', p_row.address_schema,
        'manifest', p_row.manifest,
        'rate_limit_per_minute', p_row.rate_limit_per_minute,
        'consecutive_auth_errors', p_row.consecutive_auth_errors,
        'created_at', pg_catalog.to_char(
            p_row.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        ),
        'updated_at', pg_catalog.to_char(
            p_row.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        ),
        'disabled_at', CASE
            WHEN p_row.disabled_at IS NULL THEN NULL
            ELSE pg_catalog.to_char(
                p_row.disabled_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
            )
        END,
        'retired_at', CASE
            WHEN p_row.retired_at IS NULL THEN NULL
            ELSE pg_catalog.to_char(
                p_row.retired_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
            )
        END
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1._channel_public_projection(
    yoao_notifications_v1.notification_channels
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.list_notification_channels(
    p_include_retired boolean
)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            yoao_notifications_v1._channel_public_projection(ch)
            ORDER BY ch.created_at ASC, ch.channel_id ASC
        ),
        '[]'::jsonb
    )
    FROM yoao_notifications_v1.notification_channels AS ch
    WHERE COALESCE(p_include_retired, false) OR ch.status <> 'retired'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.list_notification_channels(boolean) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.get_notification_channel(p_channel_id text)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT yoao_notifications_v1._channel_public_projection(ch)
    FROM yoao_notifications_v1.notification_channels AS ch
    WHERE ch.channel_id = p_channel_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.get_notification_channel(text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.register_notification_channel(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row yoao_notifications_v1.notification_channels%ROWTYPE;
    v_channel_id text;
    v_source_tier text;
    v_mode text;
    v_status text;
    v_rate_limit integer;
BEGIN
    IF p_request IS NULL OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
    END IF;
    v_channel_id := p_request ->> 'channel_id';
    v_source_tier := COALESCE(NULLIF(p_request ->> 'source_tier', ''), 'third_party');
    v_mode := COALESCE(NULLIF(p_request ->> 'mode', ''), 'direct');
    v_status := COALESCE(NULLIF(p_request ->> 'status', ''), 'active');
    v_rate_limit := COALESCE((p_request ->> 'rate_limit_per_minute')::integer, 60);

    IF v_channel_id IS NULL
       OR v_channel_id !~ '^[a-z][a-z0-9_-]{1,31}$'
       OR v_channel_id IN ('in_app', 'toast', 'email', 'external')
       OR v_source_tier NOT IN ('official', 'third_party')
       OR v_mode NOT IN ('direct', 'managed')
       OR v_status NOT IN ('prepared', 'active')
       OR v_rate_limit NOT BETWEEN 1 AND 600
       OR NULLIF(p_request ->> 'auth_token_ciphertext', '') IS NULL
       OR NULLIF(p_request ->> 'auth_token_sha256', '') IS NULL
       OR NULLIF(p_request ->> 'endpoint_url', '') IS NULL
       OR NULLIF(p_request ->> 'protocol_version', '') IS NULL
       OR NULLIF(pg_catalog.btrim(p_request ->> 'display_name'), '') IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
    END IF;

    BEGIN
        INSERT INTO yoao_notifications_v1.notification_channels (
            channel_id,
            display_name,
            description,
            homepage,
            source_tier,
            mode,
            status,
            endpoint_url,
            auth_token_ciphertext,
            auth_token_sha256,
            protocol_version,
            capabilities,
            address_schema,
            manifest,
            rate_limit_per_minute
        ) VALUES (
            v_channel_id,
            pg_catalog.btrim(p_request ->> 'display_name'),
            NULLIF(pg_catalog.btrim(COALESCE(p_request ->> 'description', '')), ''),
            NULLIF(p_request ->> 'homepage', ''),
            v_source_tier,
            v_mode,
            v_status,
            p_request ->> 'endpoint_url',
            p_request ->> 'auth_token_ciphertext',
            p_request ->> 'auth_token_sha256',
            p_request ->> 'protocol_version',
            p_request -> 'capabilities',
            p_request -> 'address_schema',
            CASE
                WHEN p_request ? 'manifest' AND pg_catalog.jsonb_typeof(p_request -> 'manifest') = 'object'
                    THEN p_request -> 'manifest'
                ELSE NULL
            END,
            v_rate_limit
        )
        RETURNING * INTO v_row;
    EXCEPTION WHEN unique_violation THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'conflict');
    WHEN check_violation THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
    END;

    RETURN pg_catalog.jsonb_build_object(
        'ok', true,
        'channel', yoao_notifications_v1._channel_public_projection(v_row)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.register_notification_channel(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.transition_notification_channel(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_channel_id text;
    v_action text;
    v_expected_rev integer;
    v_row yoao_notifications_v1.notification_channels%ROWTYPE;
    v_next_status text;
    v_now timestamptz := pg_catalog.now();
BEGIN
    IF p_request IS NULL OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
    END IF;
    v_channel_id := p_request ->> 'channel_id';
    v_action := p_request ->> 'action';
    v_expected_rev := (p_request ->> 'expected_revision')::integer;
    IF v_channel_id IS NULL OR v_expected_rev IS NULL OR v_action NOT IN (
        'activate', 'disable', 'retire', 'update'
    ) THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
    END IF;

    SELECT ch.* INTO v_row
    FROM yoao_notifications_v1.notification_channels AS ch
    WHERE ch.channel_id = v_channel_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'not_found');
    END IF;
    IF v_row.revision <> v_expected_rev THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok', false,
            'error', 'revision_conflict',
            'current_revision', v_row.revision
        );
    END IF;

    IF v_action = 'activate' THEN
        IF v_row.status NOT IN ('prepared', 'disabled') THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_transition');
        END IF;
        UPDATE yoao_notifications_v1.notification_channels
        SET status = 'active',
            disabled_at = NULL,
            revision = revision + 1,
            updated_at = v_now
        WHERE id = v_row.id
        RETURNING * INTO v_row;

    ELSIF v_action = 'disable' THEN
        IF v_row.status <> 'active' THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_transition');
        END IF;
        UPDATE yoao_notifications_v1.notification_channels
        SET status = 'disabled',
            disabled_at = v_now,
            revision = revision + 1,
            updated_at = v_now
        WHERE id = v_row.id
        RETURNING * INTO v_row;

    ELSIF v_action = 'retire' THEN
        IF v_row.status NOT IN ('prepared', 'disabled') THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_transition');
        END IF;
        UPDATE yoao_notifications_v1.notification_channels
        SET status = 'retired',
            auth_token_ciphertext = NULL,
            retired_at = v_now,
            revision = revision + 1,
            updated_at = v_now
        WHERE id = v_row.id
        RETURNING * INTO v_row;

    ELSIF v_action = 'update' THEN
        IF v_row.status = 'retired' THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_transition');
        END IF;
        BEGIN
            UPDATE yoao_notifications_v1.notification_channels
            SET display_name = CASE
                    WHEN p_request ? 'display_name' THEN pg_catalog.btrim(p_request ->> 'display_name')
                    ELSE display_name
                END,
                description = CASE
                    WHEN p_request ? 'description' THEN NULLIF(pg_catalog.btrim(COALESCE(p_request ->> 'description', '')), '')
                    ELSE description
                END,
                homepage = CASE
                    WHEN p_request ? 'homepage' THEN NULLIF(p_request ->> 'homepage', '')
                    ELSE homepage
                END,
                source_tier = CASE
                    WHEN p_request ? 'source_tier' THEN p_request ->> 'source_tier'
                    ELSE source_tier
                END,
                endpoint_url = CASE
                    WHEN p_request ? 'endpoint_url' THEN p_request ->> 'endpoint_url'
                    ELSE endpoint_url
                END,
                auth_token_ciphertext = CASE
                    WHEN p_request ? 'auth_token_ciphertext' THEN p_request ->> 'auth_token_ciphertext'
                    ELSE auth_token_ciphertext
                END,
                auth_token_sha256 = CASE
                    WHEN p_request ? 'auth_token_sha256' THEN p_request ->> 'auth_token_sha256'
                    ELSE auth_token_sha256
                END,
                capabilities = CASE
                    WHEN p_request ? 'capabilities' THEN p_request -> 'capabilities'
                    ELSE capabilities
                END,
                address_schema = CASE
                    WHEN p_request ? 'address_schema' THEN p_request -> 'address_schema'
                    ELSE address_schema
                END,
                rate_limit_per_minute = CASE
                    WHEN p_request ? 'rate_limit_per_minute' THEN (p_request ->> 'rate_limit_per_minute')::integer
                    ELSE rate_limit_per_minute
                END,
                consecutive_auth_errors = CASE
                    WHEN p_request ? 'auth_token_ciphertext' THEN 0
                    ELSE consecutive_auth_errors
                END,
                revision = revision + 1,
                updated_at = v_now
            WHERE id = v_row.id
            RETURNING * INTO v_row;
        EXCEPTION WHEN check_violation OR not_null_violation THEN
            RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
        END;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'ok', true,
        'channel', yoao_notifications_v1._channel_public_projection(v_row)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.transition_notification_channel(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.open_notification_channel_secret(p_channel_id text)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'channel_id', ch.channel_id,
        'status', ch.status,
        'endpoint_url', ch.endpoint_url,
        'auth_token_ciphertext', ch.auth_token_ciphertext,
        'capabilities', ch.capabilities,
        'address_schema', ch.address_schema,
        'consecutive_auth_errors', ch.consecutive_auth_errors
    )
    FROM yoao_notifications_v1.notification_channels AS ch
    WHERE ch.channel_id = p_channel_id
      AND ch.status <> 'retired'
      AND ch.auth_token_ciphertext IS NOT NULL
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.open_notification_channel_secret(text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.list_user_channel_bindings(p_user_id uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'channel_id', ch.channel_id,
                'display_name', ch.display_name,
                'description', ch.description,
                'homepage', ch.homepage,
                'source_tier', ch.source_tier,
                'status', ch.status,
                'capabilities', ch.capabilities,
                'address_schema', ch.address_schema,
                'bound', (b.id IS NOT NULL),
                'address', b.address,
                'locale', COALESCE(b.locale, 'zh-CN'),
                'verified_at', CASE
                    WHEN b.verified_at IS NULL THEN NULL
                    ELSE pg_catalog.to_char(
                        b.verified_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    )
                END,
                'updated_at', CASE
                    WHEN b.updated_at IS NULL THEN NULL
                    ELSE pg_catalog.to_char(
                        b.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    )
                END
            ) ORDER BY ch.created_at ASC, ch.channel_id ASC
        ),
        '[]'::jsonb
    )
    FROM yoao_notifications_v1.notification_channels AS ch
    LEFT JOIN yoao_notifications_v1.user_notification_channel_bindings AS b
      ON b.channel_id = ch.channel_id
     AND b.user_id = p_user_id
    WHERE ch.status IN ('active', 'disabled')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.list_user_channel_bindings(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.upsert_user_channel_binding(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_user_id uuid;
    v_channel_id text;
    v_address jsonb;
    v_locale text;
    v_status text;
    v_row yoao_notifications_v1.user_notification_channel_bindings%ROWTYPE;
BEGIN
    IF p_request IS NULL OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
    END IF;
    v_user_id := (p_request ->> 'user_id')::uuid;
    v_channel_id := p_request ->> 'channel_id';
    v_address := p_request -> 'address';
    v_locale := COALESCE(NULLIF(p_request ->> 'locale', ''), 'zh-CN');

    SELECT ch.status INTO v_status
    FROM yoao_notifications_v1.notification_channels AS ch
    WHERE ch.channel_id = v_channel_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'not_found');
    END IF;
    IF v_status <> 'active' THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'channel_not_active');
    END IF;

    BEGIN
        INSERT INTO yoao_notifications_v1.user_notification_channel_bindings (
            user_id, channel_id, address, locale, verified_at
        ) VALUES (
            v_user_id, v_channel_id, v_address, v_locale, NULL
        )
        ON CONFLICT (user_id, channel_id) DO UPDATE
        SET address = EXCLUDED.address,
            locale = EXCLUDED.locale,
            verified_at = NULL,
            updated_at = pg_catalog.now()
        RETURNING * INTO v_row;
    EXCEPTION WHEN check_violation THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
    END;

    RETURN pg_catalog.jsonb_build_object(
        'ok', true,
        'binding', pg_catalog.jsonb_build_object(
            'channel_id', v_row.channel_id,
            'address', v_row.address,
            'locale', v_row.locale,
            'verified_at', NULL,
            'updated_at', pg_catalog.to_char(
                v_row.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
            )
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.upsert_user_channel_binding(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.mark_user_channel_binding_verified(
    p_user_id uuid,
    p_channel_id text,
    p_verified_at timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_verified timestamptz;
BEGIN
    UPDATE yoao_notifications_v1.user_notification_channel_bindings
    SET verified_at = COALESCE(p_verified_at, pg_catalog.now()),
        updated_at = pg_catalog.now()
    WHERE user_id = p_user_id
      AND channel_id = p_channel_id
    RETURNING verified_at INTO v_verified;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'not_found');
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'ok', true,
        'verified_at', pg_catalog.to_char(
            v_verified AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.mark_user_channel_binding_verified(
    uuid, text, timestamptz
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.delete_user_channel_binding(
    p_user_id uuid,
    p_channel_id text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted integer;
BEGIN
    DELETE FROM yoao_notifications_v1.user_notification_channel_bindings
    WHERE user_id = p_user_id
      AND channel_id = p_channel_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN pg_catalog.jsonb_build_object('ok', true, 'deleted', v_deleted > 0);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.delete_user_channel_binding(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.enqueue_channel_deliveries(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation_id uuid;
    v_notification_id uuid;
    v_recipient_id uuid;
    v_type text;
    v_target_type text;
    v_target_id uuid;
    v_project_id uuid;
    v_task_id uuid;
    v_envelope_data jsonb;
    v_deliverable boolean;
    v_enqueued jsonb;
BEGIN
    IF p_request IS NULL OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
    END IF;
    v_operation_id := (p_request ->> 'operation_id')::uuid;
    v_notification_id := CASE
        WHEN NULLIF(p_request ->> 'notification_id', '') IS NOT NULL
            THEN (p_request ->> 'notification_id')::uuid
        ELSE NULL
    END;
    v_recipient_id := (p_request ->> 'recipient_id')::uuid;
    v_type := p_request ->> 'notification_type';
    v_target_type := p_request ->> 'target_type';
    v_target_id := (p_request ->> 'target_id')::uuid;
    v_project_id := CASE
        WHEN NULLIF(p_request ->> 'project_id', '') IS NOT NULL
            THEN (p_request ->> 'project_id')::uuid
        ELSE NULL
    END;
    v_task_id := CASE
        WHEN NULLIF(p_request ->> 'task_id', '') IS NOT NULL
            THEN (p_request ->> 'task_id')::uuid
        ELSE NULL
    END;
    v_envelope_data := p_request -> 'envelope_data';

    IF v_operation_id IS NULL OR v_recipient_id IS NULL OR v_type IS NULL
       OR v_target_type IS NULL OR v_target_id IS NULL
       OR v_envelope_data IS NULL OR pg_catalog.jsonb_typeof(v_envelope_data) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object('ok', false, 'error', 'invalid_request');
    END IF;

    SELECT policy.external_deliverable INTO v_deliverable
    FROM yoao_notifications_v1._preference_policy_v2() AS policy
    WHERE policy.notification_type = v_type;
    IF NOT FOUND OR NOT v_deliverable THEN
        RETURN pg_catalog.jsonb_build_object('ok', true, 'enqueued', '[]'::jsonb);
    END IF;

    WITH eligible AS (
        SELECT ch.channel_id
        FROM yoao_notifications_v1.notification_channels AS ch
        JOIN yoao_notifications_v1.user_notification_channel_bindings AS b
          ON b.channel_id = ch.channel_id
         AND b.user_id = v_recipient_id
        JOIN public.notification_preferences AS pref
          ON pref.user_id = v_recipient_id
         AND pref.type = v_type
        WHERE ch.status = 'active'
          AND pg_catalog.jsonb_typeof(pref.channels -> 'external' -> ch.channel_id) = 'boolean'
          AND (pref.channels -> 'external' -> ch.channel_id) = 'true'::jsonb
    ), inserted AS (
        INSERT INTO yoao_notifications_v1.notification_channel_deliveries (
            operation_id,
            notification_id,
            channel_id,
            recipient_id,
            notification_type,
            target_type,
            target_id,
            project_id,
            task_id,
            envelope_data
        )
        SELECT
            v_operation_id,
            v_notification_id,
            e.channel_id,
            v_recipient_id,
            v_type,
            v_target_type,
            v_target_id,
            v_project_id,
            v_task_id,
            v_envelope_data
        FROM eligible AS e
        ON CONFLICT (operation_id, recipient_id, channel_id, notification_type) DO NOTHING
        RETURNING delivery_id, channel_id
    )
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object('delivery_id', delivery_id, 'channel_id', channel_id)
        ),
        '[]'::jsonb
    )
    INTO v_enqueued
    FROM inserted;

    RETURN pg_catalog.jsonb_build_object('ok', true, 'enqueued', v_enqueued);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.enqueue_channel_deliveries(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.has_terminal_channel_delivery(
    p_user_id uuid,
    p_type text,
    p_target_id uuid
)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM yoao_notifications_v1.notification_channel_deliveries AS d
        WHERE d.recipient_id = p_user_id
          AND d.notification_type = p_type
          AND d.target_id = p_target_id
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.has_terminal_channel_delivery(
    uuid, text, uuid
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.channel_delivery_policy()
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'yoao-notification-channel-delivery/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'policy', 'yoao-notification-channel-delivery/v1'
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.channel_delivery_policy() FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.claim_channel_deliveries(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_worker_id uuid;
    v_limit integer;
    v_lease_seconds integer;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_items jsonb := '[]'::jsonb;
    v_channel record;
    v_claimed_ids uuid[];
    v_claimed_count integer;
    v_remaining integer;
    v_per_channel_limit integer;
    v_chunk jsonb;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'yoao-notification-channel-delivery/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'worker_id') IS DISTINCT FROM 'string'
       OR (p_request ->> 'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request -> 'limit') IS DISTINCT FROM 'number'
       OR (p_request ->> 'limit') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_request -> 'lease_seconds') IS DISTINCT FROM 'number'
       OR (p_request ->> 'lease_seconds') !~ '^[1-9][0-9]*$' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'yoao-notification-channel-delivery/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_worker_id := (p_request ->> 'worker_id')::uuid;
    v_limit := (p_request ->> 'limit')::integer;
    v_lease_seconds := (p_request ->> 'lease_seconds')::integer;
    IF v_limit NOT BETWEEN 1 AND 100 OR v_lease_seconds NOT BETWEEN 15 AND 300 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'yoao-notification-channel-delivery/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    UPDATE yoao_notifications_v1.notification_channel_deliveries AS d
    SET state = 'failed_exhausted',
        finished_at = v_now,
        last_reason = COALESCE(d.last_reason, 'lease_exhausted'),
        worker_id = NULL,
        lease_id = NULL,
        lease_expires_at = NULL
    WHERE d.state = 'pending'
      AND d.lease_expires_at IS NOT NULL
      AND d.lease_expires_at <= v_now
      AND d.attempt >= d.max_attempts;

    v_remaining := v_limit;
    FOR v_channel IN
        SELECT ch.id, ch.channel_id, ch.status, ch.rate_limit_per_minute, ch.next_dispatch_at
        FROM yoao_notifications_v1.notification_channels AS ch
        WHERE (ch.status <> 'active' OR ch.next_dispatch_at <= v_now)
          AND EXISTS (
              SELECT 1
              FROM yoao_notifications_v1.notification_channel_deliveries AS d
              WHERE d.channel_id = ch.channel_id
                AND d.state = 'pending'
                AND d.available_at <= v_now
                AND (d.lease_expires_at IS NULL OR d.lease_expires_at <= v_now)
                AND d.attempt < d.max_attempts
          )
        ORDER BY ch.next_dispatch_at ASC, ch.channel_id ASC
        FOR UPDATE OF ch SKIP LOCKED
    LOOP
        EXIT WHEN v_remaining <= 0;

        v_per_channel_limit := CASE
            WHEN v_channel.status <> 'active' THEN v_remaining
            ELSE LEAST(v_remaining, GREATEST(1, v_channel.rate_limit_per_minute / 6))
        END;

        WITH candidate AS (
            SELECT d.delivery_id
            FROM yoao_notifications_v1.notification_channel_deliveries AS d
            WHERE d.channel_id = v_channel.channel_id
              AND d.state = 'pending'
              AND d.available_at <= v_now
              AND (d.lease_expires_at IS NULL OR d.lease_expires_at <= v_now)
              AND d.attempt < d.max_attempts
            ORDER BY d.available_at ASC, d.delivery_id ASC
            FOR UPDATE OF d SKIP LOCKED
            LIMIT v_per_channel_limit
        )
        SELECT ARRAY_AGG(delivery_id) INTO v_claimed_ids FROM candidate;

        v_claimed_count := COALESCE(pg_catalog.array_length(v_claimed_ids, 1), 0);
        IF v_claimed_count = 0 THEN
            CONTINUE;
        END IF;

        IF v_channel.status = 'active' THEN
            UPDATE yoao_notifications_v1.notification_channels
            SET next_dispatch_at = GREATEST(next_dispatch_at, v_now)
                + pg_catalog.make_interval(
                    secs => ((60.0 / v_channel.rate_limit_per_minute) * v_claimed_count)::double precision
                )
            WHERE id = v_channel.id;
        END IF;

        WITH claimed AS (
            UPDATE yoao_notifications_v1.notification_channel_deliveries AS d
            SET attempt = d.attempt + 1,
                worker_id = v_worker_id,
                lease_id = pg_catalog.gen_random_uuid(),
                lease_expires_at = v_now + pg_catalog.make_interval(secs => v_lease_seconds)
            WHERE d.delivery_id = ANY(v_claimed_ids)
            RETURNING d.*
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'delivery_id', cl.delivery_id,
                    'operation_id', cl.operation_id,
                    'notification_id', cl.notification_id,
                    'channel_id', cl.channel_id,
                    'recipient_id', cl.recipient_id,
                    'notification_type', cl.notification_type,
                    'target_type', cl.target_type,
                    'target_id', cl.target_id,
                    'project_id', cl.project_id,
                    'task_id', cl.task_id,
                    'envelope_data', cl.envelope_data,
                    'attempt', cl.attempt,
                    'max_attempts', cl.max_attempts,
                    'worker_id', cl.worker_id,
                    'lease_id', cl.lease_id,
                    'occurred_at', pg_catalog.to_char(
                        cl.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    ),
                    'channel_status', ch.status,
                    'endpoint_url', ch.endpoint_url,
                    'auth_token_ciphertext', ch.auth_token_ciphertext,
                    'address_schema', ch.address_schema,
                    'account_active', COALESCE(acct.is_active, false),
                    'preference_enabled', (
                        pg_catalog.jsonb_typeof(pref.channels -> 'external' -> cl.channel_id) = 'boolean'
                        AND (pref.channels -> 'external' -> cl.channel_id) = 'true'::jsonb
                    ),
                    'recipient_address', b.address,
                    'locale', COALESCE(b.locale, 'zh-CN')
                ) ORDER BY cl.created_at ASC, cl.delivery_id ASC
            ),
            '[]'::jsonb
        )
        INTO v_chunk
        FROM claimed AS cl
        JOIN yoao_notifications_v1.notification_channels AS ch
          ON ch.channel_id = cl.channel_id
        LEFT JOIN yoao_identity_v1.account_profiles AS acct
          ON acct.id = cl.recipient_id
        LEFT JOIN public.notification_preferences AS pref
          ON pref.user_id = cl.recipient_id
         AND pref.type = cl.notification_type
        LEFT JOIN yoao_notifications_v1.user_notification_channel_bindings AS b
          ON b.user_id = cl.recipient_id
         AND b.channel_id = cl.channel_id;

        v_items := v_items || v_chunk;
        v_remaining := v_remaining - v_claimed_count;
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'yoao-notification-channel-delivery/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object('items', v_items)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.claim_channel_deliveries(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_notifications_v1.finish_channel_delivery(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_delivery_id uuid;
    v_worker_id uuid;
    v_lease_id uuid;
    v_outcome text;
    v_reason text;
    v_provider_msg_id text;
    v_retry_after integer;
    v_row yoao_notifications_v1.notification_channel_deliveries%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_delay integer;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'yoao-notification-channel-delivery/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'delivery_id') IS DISTINCT FROM 'string'
       OR (p_request ->> 'delivery_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request -> 'worker_id') IS DISTINCT FROM 'string'
       OR (p_request ->> 'worker_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request -> 'lease_id') IS DISTINCT FROM 'string'
       OR (p_request ->> 'lease_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR p_request ->> 'outcome' NOT IN (
            'delivered', 'permanent', 'skipped', 'retryable', 'auth_error'
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'yoao-notification-channel-delivery/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_delivery_id := (p_request ->> 'delivery_id')::uuid;
    v_worker_id := (p_request ->> 'worker_id')::uuid;
    v_lease_id := (p_request ->> 'lease_id')::uuid;
    v_outcome := p_request ->> 'outcome';
    v_reason := NULLIF(pg_catalog.left(COALESCE(p_request ->> 'reason', ''), 128), '');
    v_provider_msg_id := NULLIF(
        pg_catalog.left(COALESCE(p_request ->> 'provider_message_id', ''), 256), ''
    );
    v_retry_after := CASE
        WHEN p_request ? 'retry_after_seconds'
             AND pg_catalog.jsonb_typeof(p_request -> 'retry_after_seconds') = 'number'
            THEN (p_request ->> 'retry_after_seconds')::integer
        ELSE NULL
    END;

    SELECT d.* INTO v_row
    FROM yoao_notifications_v1.notification_channel_deliveries AS d
    WHERE d.delivery_id = v_delivery_id
      AND d.worker_id = v_worker_id
      AND d.lease_id = v_lease_id
      AND d.state = 'pending'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'yoao-notification-channel-delivery/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;

    IF v_outcome = 'delivered' THEN
        UPDATE yoao_notifications_v1.notification_channel_deliveries
        SET state = 'delivered',
            provider_message_id = v_provider_msg_id,
            last_reason = NULL,
            finished_at = v_now,
            worker_id = NULL,
            lease_id = NULL,
            lease_expires_at = NULL
        WHERE delivery_id = v_row.delivery_id;

        UPDATE yoao_notifications_v1.notification_channels
        SET consecutive_auth_errors = 0
        WHERE channel_id = v_row.channel_id
          AND consecutive_auth_errors > 0;

    ELSIF v_outcome = 'permanent' THEN
        UPDATE yoao_notifications_v1.notification_channel_deliveries
        SET state = 'failed_permanent',
            last_reason = COALESCE(v_reason, 'permanent_error'),
            finished_at = v_now,
            worker_id = NULL,
            lease_id = NULL,
            lease_expires_at = NULL
        WHERE delivery_id = v_row.delivery_id;

    ELSIF v_outcome = 'skipped' THEN
        UPDATE yoao_notifications_v1.notification_channel_deliveries
        SET state = 'skipped',
            last_reason = COALESCE(v_reason, 'skipped'),
            finished_at = v_now,
            worker_id = NULL,
            lease_id = NULL,
            lease_expires_at = NULL
        WHERE delivery_id = v_row.delivery_id;

    ELSIF v_outcome IN ('retryable', 'auth_error') THEN
        IF v_outcome = 'auth_error' THEN
            UPDATE yoao_notifications_v1.notification_channels
            SET consecutive_auth_errors = consecutive_auth_errors + 1,
                next_dispatch_at = GREATEST(
                    next_dispatch_at,
                    v_now + pg_catalog.make_interval(secs => 60)
                )
            WHERE channel_id = v_row.channel_id;
        END IF;

        IF v_row.attempt >= v_row.max_attempts THEN
            UPDATE yoao_notifications_v1.notification_channel_deliveries
            SET state = 'failed_exhausted',
                last_reason = COALESCE(v_reason, v_outcome),
                finished_at = v_now,
                worker_id = NULL,
                lease_id = NULL,
                lease_expires_at = NULL
            WHERE delivery_id = v_row.delivery_id;
        ELSE
            v_delay := COALESCE(
                v_retry_after,
                CASE
                    WHEN v_outcome = 'auth_error' THEN 60
                    ELSE LEAST(
                        3600,
                        (15 * pg_catalog.power(
                            2::double precision,
                            LEAST(10, GREATEST(0, v_row.attempt - 1))::double precision
                        ))::integer
                    )
                END
            );
            v_delay := GREATEST(5, LEAST(3600, v_delay));
            UPDATE yoao_notifications_v1.notification_channel_deliveries
            SET state = 'pending',
                last_reason = COALESCE(v_reason, v_outcome),
                available_at = v_now + pg_catalog.make_interval(secs => v_delay),
                worker_id = NULL,
                lease_id = NULL,
                lease_expires_at = NULL
            WHERE delivery_id = v_row.delivery_id;
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'yoao-notification-channel-delivery/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object('accepted', true)
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_notifications_v1.finish_channel_delivery(jsonb) FROM PUBLIC;
