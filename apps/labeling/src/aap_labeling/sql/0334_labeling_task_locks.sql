CREATE FUNCTION aap_labeling_v1.task_edit_lock_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_action text;
    v_keys text[];
    v_task_id uuid;
    v_batch_id uuid;
    v_user_id uuid;
    v_actor_id uuid;
    v_ttl double precision;
    v_force_takeover boolean;
    v_allow_own boolean;
    v_allowed_statuses text[];
    v_account_reply jsonb;
    v_accounts jsonb;
    v_account jsonb;
    v_is_active boolean;
    v_now timestamptz;
    v_new_expire timestamptz;
    v_stale_threshold timestamptz;
    v_has_mine boolean;
    v_has_others boolean;
    v_others_stale boolean;
    v_considered_ids uuid[] := ARRAY[]::uuid[];
    v_deleted_ids uuid[] := ARRAY[]::uuid[];
    v_lock jsonb;
    v_locks jsonb;
    v_row record;
    v_owner_id uuid;
    v_available boolean;
    v_deleted_count integer;
    v_task_ids uuid[];
    v_statuses text[];
    v_index integer;
    v_task_id_item uuid;
    v_status text;
    v_lock_acquired boolean;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-task-locks/v1'
       OR jsonb_typeof(p_request -> 'action') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling task-lock request' USING ERRCODE = '22023';
    END IF;
    v_action := p_request ->> 'action';
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS request_keys(key);

    IF v_action = 'acquire' THEN
        IF v_keys IS DISTINCT FROM ARRAY['action','force_takeover','schema','task_id','ttl','user_id']::text[]
           OR jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'user_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'force_takeover') IS DISTINCT FROM 'boolean'
           OR (jsonb_typeof(p_request -> 'ttl') NOT IN ('null', 'number'))
           OR (jsonb_typeof(p_request -> 'ttl') = 'number'
               AND p_request ->> 'ttl' !~ '^-?(0|[1-9][0-9]*)$') THEN
            RAISE EXCEPTION 'Invalid Labeling task-lock acquire request' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_user_id := (p_request ->> 'user_id')::uuid;
        v_force_takeover := (p_request ->> 'force_takeover')::boolean;
        v_ttl := COALESCE(NULLIF((p_request ->> 'ttl')::double precision, 0), 300);

        v_account_reply := aap_identity_v1.lock_accounts_share(ARRAY[v_user_id], false);
        v_accounts := v_account_reply -> 'accounts';
        IF jsonb_typeof(v_accounts) IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Identity account-lock reply' USING ERRCODE = '55000';
        END IF;
        IF jsonb_array_length(v_accounts) = 0 THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object(
                    'lock', NULL, 'considered_ids', to_jsonb(v_considered_ids),
                    'deleted_ids', to_jsonb(v_deleted_ids)
                )
            );
        END IF;
        v_account := v_accounts -> 0;
        IF jsonb_typeof(v_account) IS DISTINCT FROM 'object'
           OR v_account ->> 'id' IS DISTINCT FROM v_user_id::text
           OR jsonb_typeof(v_account -> 'is_active') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'Invalid Identity account-lock reply' USING ERRCODE = '55000';
        END IF;
        v_is_active := (v_account ->> 'is_active')::boolean;
        IF NOT v_is_active THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object(
                    'lock', NULL, 'considered_ids', to_jsonb(v_considered_ids),
                    'deleted_ids', to_jsonb(v_deleted_ids)
                )
            );
        END IF;

        PERFORM pg_advisory_xact_lock(hashtextextended(
            'aap:task-edit-lock:' || v_task_id::text, 0
        ));
        v_now := clock_timestamp();
        WITH deleted AS (
            DELETE FROM public.task_locks AS task_lock
            WHERE task_lock.task_id = v_task_id AND task_lock.expire_at < v_now
            RETURNING task_lock.id
        )
        SELECT COALESCE(array_agg(deleted.id ORDER BY deleted.id), ARRAY[]::uuid[])
        INTO v_deleted_ids FROM deleted;
        v_stale_threshold := clock_timestamp() + make_interval(secs => 300 / 2);
        SELECT COALESCE(array_agg(task_lock.id ORDER BY task_lock.id), ARRAY[]::uuid[]),
               COALESCE(bool_or(task_lock.user_id = v_user_id), false),
               COALESCE(bool_or(task_lock.user_id <> v_user_id), false),
               COALESCE(bool_and(task_lock.expire_at < v_stale_threshold)
                   FILTER (WHERE task_lock.user_id <> v_user_id), false)
        INTO v_considered_ids, v_has_mine, v_has_others, v_others_stale
        FROM public.task_locks AS task_lock
        WHERE task_lock.task_id = v_task_id;

        IF v_has_others AND NOT v_has_mine
           AND NOT v_force_takeover AND NOT v_others_stale THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object(
                    'lock', NULL, 'considered_ids', to_jsonb(v_considered_ids),
                    'deleted_ids', to_jsonb(v_deleted_ids)
                )
            );
        END IF;
        IF v_has_others THEN
            DELETE FROM public.task_locks AS task_lock
            WHERE task_lock.task_id = v_task_id AND task_lock.user_id <> v_user_id;
        END IF;
        v_new_expire := clock_timestamp() + make_interval(secs => v_ttl);

        INSERT INTO public.task_locks AS task_lock (id, task_id, user_id, expire_at, unique_id)
        VALUES (
            gen_random_uuid(), v_task_id, v_user_id,
            v_new_expire, gen_random_uuid()
        )
        ON CONFLICT (task_id, user_id) DO UPDATE
            SET expire_at = EXCLUDED.expire_at
        RETURNING jsonb_build_object(
            'id', task_lock.id, 'task_id', task_lock.task_id,
            'user_id', task_lock.user_id, 'expire_at', task_lock.expire_at,
            'unique_id', task_lock.unique_id, 'created_at', task_lock.created_at
        ) INTO v_lock;
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
            'ok', true, 'data', jsonb_build_object(
                'lock', v_lock, 'considered_ids', to_jsonb(v_considered_ids),
                'deleted_ids', to_jsonb(v_deleted_ids)
            )
        );

    ELSIF v_action IN ('lock_scope', 'try_final_scope', 'release', 'heartbeat', 'assert_write_allowed', 'is_locked', 'active_lock', 'active_locks') THEN
        IF v_action IN ('release', 'heartbeat', 'assert_write_allowed') THEN
            IF v_keys IS DISTINCT FROM CASE v_action
                WHEN 'release' THEN ARRAY['action','schema','task_id','user_id']::text[]
                WHEN 'heartbeat' THEN ARRAY['action','schema','task_id','ttl','user_id']::text[]
                ELSE ARRAY['action','schema','task_id','user_id']::text[] END
               OR jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'user_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_action = 'heartbeat' AND jsonb_typeof(p_request -> 'ttl') NOT IN ('null', 'number'))
               OR (v_action = 'heartbeat' AND jsonb_typeof(p_request -> 'ttl') = 'number'
                   AND p_request ->> 'ttl' !~ '^-?(0|[1-9][0-9]*)$') THEN
                RAISE EXCEPTION 'Invalid Labeling task-lock request' USING ERRCODE = '22023';
            END IF;
            v_user_id := (p_request ->> 'user_id')::uuid;
            IF v_action = 'heartbeat' THEN
                v_ttl := COALESCE(NULLIF((p_request ->> 'ttl')::double precision, 0), 300);
            END IF;
        ELSIF v_keys IS DISTINCT FROM ARRAY['action','schema','task_id']::text[] THEN
            RAISE EXCEPTION 'Invalid Labeling task-lock request' USING ERRCODE = '22023';
        END IF;
        IF jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'Invalid Labeling task-lock request' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;

        IF v_action = 'lock_scope' THEN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'aap:task-edit-lock:' || v_task_id::text, 0
            ));
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object('locked', true)
            );
        ELSIF v_action = 'try_final_scope' THEN
            v_available := pg_try_advisory_xact_lock(hashtextextended(
                'aap:task-edit-lock:' || v_task_id::text, 0
            ));
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object('available', v_available)
            );
        ELSIF v_action = 'release' THEN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'aap:task-edit-lock:' || v_task_id::text, 0
            ));
            DELETE FROM public.task_locks AS task_lock
            WHERE task_lock.task_id = v_task_id AND task_lock.user_id = v_user_id
            RETURNING jsonb_build_object(
                'id', task_lock.id, 'task_id', task_lock.task_id,
                'user_id', task_lock.user_id, 'expire_at', task_lock.expire_at,
                'unique_id', task_lock.unique_id, 'created_at', task_lock.created_at
            ) INTO v_lock;
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object(
                    'released', v_lock IS NOT NULL, 'lock', v_lock
                )
            );
        ELSIF v_action = 'heartbeat' THEN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'aap:task-edit-lock:' || v_task_id::text, 0
            ));
            UPDATE public.task_locks AS task_lock
            SET expire_at = clock_timestamp() + make_interval(secs => v_ttl)
            WHERE task_lock.task_id = v_task_id AND task_lock.user_id = v_user_id
            RETURNING jsonb_build_object(
                'id', task_lock.id, 'task_id', task_lock.task_id,
                'user_id', task_lock.user_id, 'expire_at', task_lock.expire_at,
                'unique_id', task_lock.unique_id, 'created_at', task_lock.created_at
            ) INTO v_lock;
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object(
                    'renewed', v_lock IS NOT NULL, 'lock', v_lock
                )
            );
        ELSIF v_action = 'assert_write_allowed' THEN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'aap:task-edit-lock:' || v_task_id::text, 0
            ));
            v_now := clock_timestamp();
            WITH deleted AS (
                DELETE FROM public.task_locks AS task_lock
                WHERE task_lock.task_id = v_task_id AND task_lock.expire_at < v_now
                RETURNING task_lock.id
            )
            SELECT COALESCE(array_agg(deleted.id ORDER BY deleted.id), ARRAY[]::uuid[])
            INTO v_deleted_ids FROM deleted;
            v_owner_id := NULL;
            v_locks := '[]'::jsonb;
            FOR v_row IN
                SELECT task_lock.id, task_lock.task_id, task_lock.user_id,
                       task_lock.expire_at, task_lock.unique_id, task_lock.created_at
                FROM public.task_locks AS task_lock
                WHERE task_lock.task_id = v_task_id
                ORDER BY task_lock.expire_at DESC
                FOR UPDATE
            LOOP
                v_locks := v_locks || jsonb_build_array(jsonb_build_object(
                    'id', v_row.id, 'task_id', v_row.task_id,
                    'user_id', v_row.user_id, 'expire_at', v_row.expire_at,
                    'unique_id', v_row.unique_id, 'created_at', v_row.created_at
                ));
                IF v_owner_id IS NULL AND v_row.user_id <> v_user_id THEN
                    v_owner_id := v_row.user_id;
                END IF;
            END LOOP;
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object(
                    'owner_id', v_owner_id, 'deleted_ids', to_jsonb(v_deleted_ids),
                    'locks', v_locks
                )
            );
        ELSE
            v_now := clock_timestamp();
            WITH deleted AS (
                DELETE FROM public.task_locks AS task_lock
                WHERE task_lock.task_id = v_task_id AND task_lock.expire_at < v_now
                RETURNING task_lock.id
            )
            SELECT COALESCE(array_agg(deleted.id ORDER BY deleted.id), ARRAY[]::uuid[])
            INTO v_deleted_ids FROM deleted;
            IF v_action = 'is_locked' THEN
                SELECT jsonb_build_object(
                    'id', task_lock.id, 'task_id', task_lock.task_id,
                    'user_id', task_lock.user_id, 'expire_at', task_lock.expire_at,
                    'unique_id', task_lock.unique_id, 'created_at', task_lock.created_at
                ) INTO v_lock
                FROM public.task_locks AS task_lock
                WHERE task_lock.task_id = v_task_id
                LIMIT 1;
                v_user_id := NULLIF(v_lock ->> 'user_id', '')::uuid;
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                    'ok', true, 'data', jsonb_build_object(
                        'locked', v_user_id IS NOT NULL, 'user_id', v_user_id,
                        'lock', v_lock, 'deleted_ids', to_jsonb(v_deleted_ids)
                    )
                );
            ELSIF v_action = 'active_lock' THEN
                SELECT jsonb_build_object(
                    'id', task_lock.id, 'task_id', task_lock.task_id,
                    'user_id', task_lock.user_id, 'expire_at', task_lock.expire_at,
                    'unique_id', task_lock.unique_id, 'created_at', task_lock.created_at
                ) INTO v_lock
                FROM public.task_locks AS task_lock
                WHERE task_lock.task_id = v_task_id
                ORDER BY task_lock.expire_at DESC
                LIMIT 1;
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                    'ok', true, 'data', jsonb_build_object(
                        'lock', v_lock, 'deleted_ids', to_jsonb(v_deleted_ids)
                    )
                );
            ELSE
                SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'id', task_lock.id, 'task_id', task_lock.task_id,
                    'user_id', task_lock.user_id, 'expire_at', task_lock.expire_at,
                    'unique_id', task_lock.unique_id, 'created_at', task_lock.created_at
                )), '[]'::jsonb) INTO v_locks
                FROM public.task_locks AS task_lock
                WHERE task_lock.task_id = v_task_id;
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                    'ok', true, 'data', jsonb_build_object(
                        'locks', v_locks, 'deleted_ids', to_jsonb(v_deleted_ids)
                    )
                );
            END IF;
        END IF;

    ELSIF v_action = 'cleanup_expired' THEN
        IF v_keys IS DISTINCT FROM ARRAY['action','schema','task_id']::text[]
           OR (jsonb_typeof(p_request -> 'task_id') NOT IN ('null','string'))
           OR (jsonb_typeof(p_request -> 'task_id') = 'string'
               AND p_request ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RAISE EXCEPTION 'Invalid Labeling task-lock cleanup request' USING ERRCODE = '22023';
        END IF;
        v_now := clock_timestamp();
        IF jsonb_typeof(p_request -> 'task_id') = 'string' THEN
            v_task_id := (p_request ->> 'task_id')::uuid;
            WITH deleted AS (
                DELETE FROM public.task_locks AS task_lock
                WHERE task_lock.task_id = v_task_id AND task_lock.expire_at < v_now
                RETURNING task_lock.id
            )
            SELECT COALESCE(array_agg(deleted.id ORDER BY deleted.id), ARRAY[]::uuid[])
            INTO v_deleted_ids FROM deleted;
        ELSE
            WITH deleted AS (
                DELETE FROM public.task_locks AS task_lock
                WHERE task_lock.expire_at < v_now
                RETURNING task_lock.id
            )
            SELECT COALESCE(array_agg(deleted.id ORDER BY deleted.id), ARRAY[]::uuid[])
            INTO v_deleted_ids FROM deleted;
        END IF;
        v_deleted_count := cardinality(v_deleted_ids);
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
            'ok', true, 'data', jsonb_build_object(
                'deleted_count', v_deleted_count, 'deleted_ids', to_jsonb(v_deleted_ids)
            )
        );

    ELSIF v_action = 'batch_final_write_available' THEN
        IF v_keys IS DISTINCT FROM ARRAY['action','actor_id','allow_own','allowed_statuses','batch_id','schema']::text[]
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'batch_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'actor_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'allow_own') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'allowed_statuses') IS DISTINCT FROM 'array'
           OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(p_request -> 'allowed_statuses') AS item(value)
               WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string'
           ) THEN
            RAISE EXCEPTION 'Invalid Labeling batch task-lock request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_allow_own := (p_request ->> 'allow_own')::boolean;
        SELECT COALESCE(array_agg(value ORDER BY ordinal), ARRAY[]::text[])
        INTO v_allowed_statuses
        FROM jsonb_array_elements_text(p_request -> 'allowed_statuses') WITH ORDINALITY AS statuses(value, ordinal);

        WITH locked AS MATERIALIZED (
            SELECT task.id, task.status
            FROM public.tasks AS task
            WHERE task.batch_id = v_batch_id
            ORDER BY task.id
            FOR UPDATE OF task NOWAIT
        )
        SELECT COALESCE(array_agg(locked.id ORDER BY locked.id), ARRAY[]::uuid[]),
               COALESCE(array_agg(locked.status ORDER BY locked.id), ARRAY[]::text[])
        INTO v_task_ids, v_statuses
        FROM locked;

        v_available := true;
        IF cardinality(v_task_ids) > 0 THEN
            FOR v_index IN 1..cardinality(v_task_ids) LOOP
                v_task_id_item := v_task_ids[v_index];
                v_status := v_statuses[v_index];
                v_lock_acquired := pg_try_advisory_xact_lock(hashtextextended(
                    'aap:task-edit-lock:' || v_task_id_item::text, 0
                ));
                IF NOT v_lock_acquired OR NOT (v_status = ANY(v_allowed_statuses)) THEN
                    v_available := false;
                END IF;
            END LOOP;
        END IF;
        IF v_available THEN
            SELECT NOT EXISTS (
                SELECT 1
                FROM public.task_locks AS task_lock
                JOIN public.tasks AS task ON task.id = task_lock.task_id
                WHERE task.batch_id = v_batch_id
                  AND task_lock.expire_at > clock_timestamp()
                  AND (NOT v_allow_own OR task_lock.user_id <> v_actor_id)
            ) INTO v_available;
        END IF;
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
            'ok', true, 'data', jsonb_build_object('available', v_available)
        );

    END IF;

    RAISE EXCEPTION 'Invalid Labeling task-lock action' USING ERRCODE = '22023';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.task_edit_lock_command(jsonb) FROM PUBLIC;
