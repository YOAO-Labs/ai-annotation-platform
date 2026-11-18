-- Move the task edit-lock advisory domain from the historical aap: prefix to
-- the current task-edit-lock: prefix in one forward cutover.
--
-- Stop admission for every writer below and drain their connections first.
-- The guard rejects any remaining client that can execute one of the four
-- entry points, and any legacy advisory lock for an existing Task.  The new
-- function bodies acquire only the current key: they never take the legacy key
-- and never fall back to it.  The legacy prefix appears only in the fail-closed
-- detection branch so a stale writer cannot silently share the new domain.

DO $cutover_guard$
DECLARE
    v_signatures text[] := ARRAY[
        'aap_labeling_v1.task_edit_lock_command(jsonb)',
        'aap_labeling_v1.lock_video_input_manifest_member(uuid,uuid,uuid,jsonb,jsonb)',
        'aap_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])',
        'aap_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)'
    ]::text[];
    v_expected_legacy integer[] := ARRAY[7, 1, 1, 1];
    v_legacy_literal text := '''aap:task-edit-lock:''';
    v_current_literal text := '''task-edit-lock:''';
    v_signature text;
    v_function oid;
    v_body text;
    v_owner oid;
    v_owner_seen oid;
    v_security_definer boolean;
    v_config text[];
    v_legacy_count integer;
    v_current_count integer;
    v_index integer;
BEGIN
    FOR v_index IN 1..pg_catalog.cardinality(v_signatures) LOOP
        v_signature := v_signatures[v_index];
        v_function := pg_catalog.to_regprocedure(v_signature);
        IF v_function IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '42704',
                MESSAGE = 'task edit-lock entry point is missing: ' || v_signature;
        END IF;
        SELECT function.prosrc, function.proowner, function.prosecdef,
               function.proconfig
        INTO v_body, v_owner, v_security_definer, v_config
        FROM pg_catalog.pg_proc AS function
        WHERE function.oid = v_function;
        IF NOT v_security_definer
           OR NOT COALESCE(
               v_config @> ARRAY['search_path=pg_catalog, pg_temp']::text[],
               false
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'task edit-lock entry point is not a fixed SECURITY DEFINER capability: ' || v_signature;
        END IF;
        IF v_owner_seen IS NULL THEN
            v_owner_seen := v_owner;
        ELSIF v_owner_seen <> v_owner THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'task edit-lock entry points have different owners';
        END IF;
        v_legacy_count := (
            pg_catalog.length(v_body)
            - pg_catalog.length(pg_catalog.replace(v_body, v_legacy_literal, ''))
        ) / pg_catalog.length(v_legacy_literal);
        v_current_count := (
            pg_catalog.length(v_body)
            - pg_catalog.length(pg_catalog.replace(v_body, v_current_literal, ''))
        ) / pg_catalog.length(v_current_literal);
        IF v_legacy_count <> v_expected_legacy[v_index]
           OR v_current_count <> 0 THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'unexpected pre-cutover task edit-lock domain in ' || v_signature;
        END IF;
    END LOOP;

    -- Every remaining client that can enter the domain is an undrained writer.
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_stat_activity AS activity
        WHERE activity.datid = (
            SELECT db.oid FROM pg_catalog.pg_database AS db
            WHERE db.datname = pg_catalog.current_database()
        )
          AND activity.pid <> pg_catalog.pg_backend_pid()
          AND COALESCE(activity.backend_type, 'client backend') = 'client backend'
          AND activity.usesysid IS NOT NULL
          AND (
              pg_catalog.has_function_privilege(
                  activity.usesysid,
                  'aap_labeling_v1.task_edit_lock_command(jsonb)',
                  'EXECUTE'
              )
              OR pg_catalog.has_function_privilege(
                  activity.usesysid,
                  'aap_labeling_v1.lock_video_input_manifest_member(uuid,uuid,uuid,jsonb,jsonb)',
                  'EXECUTE'
              )
              OR pg_catalog.has_function_privilege(
                  activity.usesysid,
                  'aap_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])',
                  'EXECUTE'
              )
              OR pg_catalog.has_function_privilege(
                  activity.usesysid,
                  'aap_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)',
                  'EXECUTE'
              )
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55006',
            MESSAGE = 'Drain all task edit-lock writers before advisory-domain cutover';
    END IF;

    -- A held legacy lock proves the old domain is still active even if its
    -- session privilege changed after acquisition.
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_locks AS advisory_lock
        WHERE advisory_lock.locktype = 'advisory'
          AND advisory_lock.objsubid = 1
          AND advisory_lock.granted
          AND advisory_lock.database = (
              SELECT db.oid FROM pg_catalog.pg_database AS db
              WHERE db.datname = pg_catalog.current_database()
          )
          AND advisory_lock.pid IS DISTINCT FROM pg_catalog.pg_backend_pid()
          AND EXISTS (
              SELECT 1
              FROM public.tasks AS task
              CROSS JOIN LATERAL (
                  SELECT pg_catalog.hashtextextended(
                             'aap:task-edit-lock:' || task.id::text, 0
                         ) AS legacy_key,
                         pg_catalog.hashtextextended(
                             'task-edit-lock:' || task.id::text, 0
                         ) AS current_key
              ) AS keys
              WHERE (
                        advisory_lock.classid::bigint
                        = ((keys.legacy_key >> 32) & 4294967295::bigint)
                    AND advisory_lock.objid::bigint
                        = (keys.legacy_key & 4294967295::bigint)
                    )
                 OR (
                        advisory_lock.classid::bigint
                        = ((keys.current_key >> 32) & 4294967295::bigint)
                    AND advisory_lock.objid::bigint
                        = (keys.current_key & 4294967295::bigint)
                    )
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55006',
            MESSAGE = 'An active task edit-lock advisory key remains; drain it before cutover';
    END IF;
END;
$cutover_guard$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.task_edit_lock_command(p_request jsonb)
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

        -- Fail closed while any legacy-domain writer still holds this Task.
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_locks AS advisory_lock
            WHERE advisory_lock.locktype = 'advisory'
              AND advisory_lock.objsubid = 1
              AND advisory_lock.granted
              AND advisory_lock.database = (
                  SELECT db.oid FROM pg_catalog.pg_database AS db
                  WHERE db.datname = pg_catalog.current_database()
              )
              AND advisory_lock.classid::bigint = (
                  (
                      pg_catalog.hashtextextended(
                          'aap:task-edit-lock:' || v_task_id::text, 0
                      ) >> 32
                  ) & 4294967295::bigint
              )
              AND advisory_lock.objid::bigint = (
                  pg_catalog.hashtextextended(
                      'aap:task-edit-lock:' || v_task_id::text, 0
                  ) & 4294967295::bigint
              )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55006',
                MESSAGE = 'legacy task edit-lock domain is still active';
        END IF;

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
            'task-edit-lock:' || v_task_id::text, 0
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
            IF v_keys IS DISTINCT FROM (CASE v_action
                WHEN 'release' THEN ARRAY['action','schema','task_id','user_id']::text[]
                WHEN 'heartbeat' THEN ARRAY['action','schema','task_id','ttl','user_id']::text[]
                ELSE ARRAY['action','schema','task_id','user_id']::text[] END)
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

        -- No action may cross the cutover while the legacy domain is live.
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_locks AS advisory_lock
            WHERE advisory_lock.locktype = 'advisory'
              AND advisory_lock.objsubid = 1
              AND advisory_lock.granted
              AND advisory_lock.database = (
                  SELECT db.oid FROM pg_catalog.pg_database AS db
                  WHERE db.datname = pg_catalog.current_database()
              )
              AND advisory_lock.classid::bigint = (
                  (
                      pg_catalog.hashtextextended(
                          'aap:task-edit-lock:' || v_task_id::text, 0
                      ) >> 32
                  ) & 4294967295::bigint
              )
              AND advisory_lock.objid::bigint = (
                  pg_catalog.hashtextextended(
                      'aap:task-edit-lock:' || v_task_id::text, 0
                  ) & 4294967295::bigint
              )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55006',
                MESSAGE = 'legacy task edit-lock domain is still active';
        END IF;

        IF v_action = 'lock_scope' THEN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'task-edit-lock:' || v_task_id::text, 0
            ));
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object('locked', true)
            );
        ELSIF v_action = 'try_final_scope' THEN
            v_available := pg_try_advisory_xact_lock(hashtextextended(
                'task-edit-lock:' || v_task_id::text, 0
            ));
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-task-locks-data/v1', 'action', v_action,
                'ok', true, 'data', jsonb_build_object('available', v_available)
            );
        ELSIF v_action = 'release' THEN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'task-edit-lock:' || v_task_id::text, 0
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
                'task-edit-lock:' || v_task_id::text, 0
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
                'task-edit-lock:' || v_task_id::text, 0
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
                IF EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_locks AS advisory_lock
                    WHERE advisory_lock.locktype = 'advisory'
                      AND advisory_lock.objsubid = 1
                      AND advisory_lock.granted
                      AND advisory_lock.database = (
                          SELECT db.oid FROM pg_catalog.pg_database AS db
                          WHERE db.datname = pg_catalog.current_database()
                      )
                      AND advisory_lock.classid::bigint = (
                          (
                              pg_catalog.hashtextextended(
                                  'aap:task-edit-lock:' || v_task_id_item::text, 0
                              ) >> 32
                          ) & 4294967295::bigint
                      )
                      AND advisory_lock.objid::bigint = (
                          pg_catalog.hashtextextended(
                              'aap:task-edit-lock:' || v_task_id_item::text, 0
                          ) & 4294967295::bigint
                      )
                ) THEN
                    v_available := false;
                ELSE
                    v_lock_acquired := pg_try_advisory_xact_lock(hashtextextended(
                        'task-edit-lock:' || v_task_id_item::text, 0
                    ));
                    IF NOT v_lock_acquired OR NOT (v_status = ANY(v_allowed_statuses)) THEN
                        v_available := false;
                    END IF;
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
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.task_edit_lock_command(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.lock_video_input_manifest_member(
    p_actor_id uuid,
    p_project_id uuid,
    p_task_id uuid,
    p_selection jsonb,
    p_captured_task jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_access_data jsonb;
    v_code text;
    v_initial record;
    v_task record;
    v_batch record;
    v_task_access jsonb;
    v_captured_binding jsonb;
    v_current_binding jsonb;
    v_snapshot text;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL OR p_task_id IS NULL
       OR p_selection IS NULL OR p_captured_task IS NULL
       OR pg_catalog.octet_length(pg_catalog.convert_to(
            p_selection::text || p_captured_task::text, 'UTF8'
       )) > 65536
       OR NOT aap_labeling_v1._video_input_manifest_selection_valid(p_selection)
       OR NOT aap_labeling_v1._video_input_captured_task_valid(p_captured_task) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_request');
    END IF;
    v_captured_binding := p_captured_task - 'batch_id';
    IF NOT aap_labeling_v1._video_input_task_binding_valid(v_captured_binding)
       AND p_captured_task -> 'dataset_item_id' <> 'null'::jsonb THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF p_captured_task ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR p_captured_task ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR (p_selection -> 'batch_id' <> 'null'::jsonb
           AND p_captured_task -> 'batch_id' IS DISTINCT FROM p_selection -> 'batch_id') THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF p_selection -> 'task_ids' <> 'null'::jsonb THEN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_selection -> 'task_ids') AS entry(value)
            WHERE entry.value #>> '{}' = p_task_id::text
        ) THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
        END IF;
    END IF;

    v_access := aap_workspace_v1.lock_media_project_access(p_actor_id, p_project_id);
    IF v_access IS NULL
       OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_access #>> '{error,code}';
        IF v_code IN ('account_not_active', 'account_busy', 'invalid_account_role', 'task_busy') THEN
            RETURN aap_labeling_v1._video_input_selection_reply(
                NULL, CASE WHEN v_code = 'task_busy' THEN 'source_busy' ELSE v_code END
            );
        ELSIF v_code = 'project_missing' THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_not_visible');
        END IF;
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    v_access_data := v_access -> 'data';
    IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
       OR (v_access_data - ARRAY[
            'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
            'is_super_admin', 'video_collaboration_enabled', 'project_name'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_access_data ?& ARRAY[
            'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
            'is_super_admin', 'video_collaboration_enabled', 'project_name'
       ])
       OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_access_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR pg_catalog.jsonb_typeof(v_access_data -> 'platform_role') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'project_role') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF pg_catalog.jsonb_typeof(v_access_data -> 'is_super_admin') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'video_collaboration_enabled') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'project_name') IS DISTINCT FROM 'string' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF v_access_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'permission_denied');
    END IF;

    SELECT task.id, task.project_id, task.dataset_item_id, task.batch_id,
           task.file_type, task.file_path, task.created_at, task.version,
           task.media_source_generation, task.status, task.total_predictions
    INTO v_initial
    FROM public.tasks AS task
    WHERE task.id = p_task_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_missing');
    END IF;
    IF v_initial.project_id IS DISTINCT FROM p_project_id THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    v_task_access := aap_labeling_v1._video_input_selection_task_access(
        p_actor_id, p_project_id, p_task_id, v_initial.dataset_item_id, v_initial.file_type
    );
    IF v_task_access IS NULL
       OR v_task_access ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-selection/v1'
       OR pg_catalog.jsonb_typeof(v_task_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF v_task_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_task_access #>> '{error,code}';
        IF v_code IN (
            'account_not_active', 'account_busy', 'invalid_account_role',
            'task_busy', 'task_not_visible', 'permission_denied'
        ) THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, v_code);
        END IF;
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    v_access_data := v_task_access -> 'data';
    IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
       OR (v_access_data - ARRAY['actor_id', 'project_id', 'task_id']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_access_data ?& ARRAY['actor_id', 'project_id', 'task_id'])
       OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_access_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR v_access_data ->> 'task_id' IS DISTINCT FROM p_task_id::text THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;

    v_snapshot := aap_labeling_v1._image_source_snapshot_fields(
        v_initial.id, v_initial.created_at, v_initial.project_id,
        v_initial.dataset_item_id, v_initial.file_type, v_initial.file_path,
        v_initial.media_source_generation
    );
    IF v_initial.batch_id IS DISTINCT FROM
            (CASE WHEN p_captured_task -> 'batch_id' = 'null'::jsonb
                 THEN NULL ELSE (p_captured_task ->> 'batch_id')::uuid END)
       OR (p_captured_task - ARRAY['batch_id', 'task_version'])
            IS DISTINCT FROM (pg_catalog.jsonb_build_object(
                'task_id', v_initial.id,
                'project_id', v_initial.project_id,
                'task_source_generation', v_initial.media_source_generation,
                'dataset_item_id', v_initial.dataset_item_id,
                'task_source_snapshot', v_snapshot,
                'file_path', v_initial.file_path,
                'file_type', v_initial.file_type
            )) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF v_initial.status IS NULL OR v_initial.status = 'uploading' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_not_ready');
    END IF;
    IF p_selection ->> 'execution_scope' = 'bulk'
       AND (v_initial.status IS DISTINCT FROM 'pending'
            OR (p_selection -> 'batch_id' <> 'null'::jsonb
                AND v_initial.batch_id IS DISTINCT FROM (p_selection ->> 'batch_id')::uuid)) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    ELSIF p_selection ->> 'execution_scope' = 'workbench'
       AND v_initial.status NOT IN ('pending', 'in_progress', 'rejected') THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF p_selection ->> 'predict_mode' = 'skip_predicted'
       AND COALESCE(v_initial.total_predictions, 0) <> 0 THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;

    IF v_initial.batch_id IS NOT NULL THEN
        BEGIN
            SELECT batch.id, batch.project_id, batch.status, batch.admin_locked,
                   batch.annotator_id, batch.reviewer_id
            INTO v_batch
            FROM public.task_batches AS batch
            WHERE batch.id = v_initial.batch_id
            FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_busy');
        END;
        IF NOT FOUND OR v_batch.project_id IS DISTINCT FROM p_project_id
           OR v_batch.admin_locked THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
        END IF;
        IF p_selection ->> 'execution_scope' = 'bulk'
           AND NOT (
                v_batch.status = 'active'
                OR (v_batch.status = 'draft'
                    AND v_batch.annotator_id IS NULL
                    AND v_batch.reviewer_id IS NULL)
           ) THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
        END IF;
    END IF;

    -- Return busy instead of entering the current domain while a drained
    -- legacy writer still owns the old key.
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_locks AS advisory_lock
        WHERE advisory_lock.locktype = 'advisory'
          AND advisory_lock.objsubid = 1
          AND advisory_lock.granted
          AND advisory_lock.database = (
              SELECT db.oid FROM pg_catalog.pg_database AS db
              WHERE db.datname = pg_catalog.current_database()
          )
          AND advisory_lock.classid::bigint = (
              (
                  pg_catalog.hashtextextended(
                      'aap:task-edit-lock:' || p_task_id::text, 0
                  ) >> 32
              ) & 4294967295::bigint
          )
          AND advisory_lock.objid::bigint = (
              pg_catalog.hashtextextended(
                  'aap:task-edit-lock:' || p_task_id::text, 0
              ) & 4294967295::bigint
          )
    ) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_busy');
    END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('task-edit-lock:' || p_task_id::text, 0)
    ) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_busy');
    END IF;
    BEGIN
        SELECT task.id, task.project_id, task.dataset_item_id, task.batch_id,
               task.file_type, task.file_path, task.created_at, task.version,
               task.media_source_generation, task.status, task.total_predictions
        INTO v_task
        FROM public.tasks AS task
        WHERE task.id = p_task_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_missing');
    END IF;
    v_snapshot := aap_labeling_v1._image_source_snapshot_fields(
        v_task.id, v_task.created_at, v_task.project_id, v_task.dataset_item_id,
        v_task.file_type, v_task.file_path, v_task.media_source_generation
    );
    v_current_binding := pg_catalog.jsonb_build_object(
        'task_id', v_task.id,
        'project_id', v_task.project_id,
        'task_version', v_task.version,
        'task_source_generation', v_task.media_source_generation,
        'dataset_item_id', v_task.dataset_item_id,
        'task_source_snapshot', v_snapshot,
        'file_path', v_task.file_path,
        'file_type', v_task.file_type
    );
    IF v_task.project_id IS DISTINCT FROM p_project_id
       OR v_task.batch_id IS DISTINCT FROM v_initial.batch_id
       OR (v_current_binding - 'task_version') IS DISTINCT FROM
          (v_captured_binding - 'task_version') THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF v_task.status IS NULL OR v_task.status = 'uploading' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_not_ready');
    END IF;
    IF p_selection ->> 'execution_scope' = 'bulk'
       AND (v_task.status IS DISTINCT FROM 'pending'
            OR (p_selection -> 'batch_id' <> 'null'::jsonb
                AND v_task.batch_id IS DISTINCT FROM (p_selection ->> 'batch_id')::uuid)) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    ELSIF p_selection ->> 'execution_scope' = 'workbench'
       AND v_task.status NOT IN ('pending', 'in_progress', 'rejected') THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF p_selection ->> 'predict_mode' = 'skip_predicted'
       AND COALESCE(v_task.total_predictions, 0) <> 0 THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.task_locks AS task_lock
        WHERE task_lock.task_id = p_task_id
          AND task_lock.expire_at > pg_catalog.clock_timestamp()
          AND (p_selection ->> 'execution_scope' = 'bulk'
               OR task_lock.user_id <> p_actor_id)
    ) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF v_task.dataset_item_id IS NULL THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_missing');
    END IF;
    IF v_task.file_type IS DISTINCT FROM 'video' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    RETURN aap_labeling_v1._video_input_selection_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'task', v_current_binding || pg_catalog.jsonb_build_object(
                'batch_id', v_task.batch_id
            ),
            'upload_ready', true
        ), NULL
    );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_video_input_manifest_member(uuid, uuid, uuid, jsonb, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.lock_identity_offboarding_work(
    p_target_user_id uuid,
    p_project_ids uuid[]
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_batch_id uuid;
    v_task_id uuid;
    v_lock_id uuid;
BEGIN
    IF p_target_user_id IS NULL OR p_project_ids IS NULL
       OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Identity offboarding Labeling lock request';
    END IF;

    FOR v_batch_id IN
        SELECT batch.id
        FROM public.task_batches AS batch
        WHERE batch.project_id = ANY(p_project_ids)
          AND (batch.annotator_id = p_target_user_id
               OR batch.reviewer_id = p_target_user_id)
        ORDER BY batch.id
        FOR UPDATE OF batch NOWAIT
    LOOP
        NULL;
    END LOOP;

    FOR v_task_id IN
        SELECT task.id
        FROM public.tasks AS task
        WHERE task.assignee_id = p_target_user_id
           OR task.reviewer_id = p_target_user_id
           OR EXISTS (
               SELECT 1 FROM public.task_locks AS task_lock
               WHERE task_lock.task_id = task.id
                 AND task_lock.user_id = p_target_user_id
           )
        ORDER BY task.id
        FOR UPDATE OF task NOWAIT
    LOOP
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_locks AS advisory_lock
            WHERE advisory_lock.locktype = 'advisory'
              AND advisory_lock.objsubid = 1
              AND advisory_lock.granted
              AND advisory_lock.database = (
                  SELECT db.oid FROM pg_catalog.pg_database AS db
                  WHERE db.datname = pg_catalog.current_database()
              )
              AND advisory_lock.classid::bigint = (
                  (
                      pg_catalog.hashtextextended(
                          'aap:task-edit-lock:' || v_task_id::text, 0
                      ) >> 32
                  ) & 4294967295::bigint
              )
              AND advisory_lock.objid::bigint = (
                  pg_catalog.hashtextextended(
                      'aap:task-edit-lock:' || v_task_id::text, 0
                  ) & 4294967295::bigint
              )
        ) THEN
            RETURN false;
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'task-edit-lock:' || v_task_id::text, 0
            )
        ) THEN
            RETURN false;
        END IF;
    END LOOP;

    FOR v_lock_id IN
        SELECT task_lock.id
        FROM public.task_locks AS task_lock
        WHERE task_lock.user_id = p_target_user_id
        ORDER BY task_lock.id
        FOR UPDATE OF task_lock NOWAIT
    LOOP
        NULL;
    END LOOP;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_identity_offboarding_work(
    uuid, uuid[]
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.lock_membership_work(
    p_project_id uuid,
    p_user_id uuid,
    p_target_role text,
    p_is_removal boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_work jsonb;
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_lock_task_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_task_ids uuid[] := ARRAY[]::uuid[];
    v_advisory_task_ids uuid[] := ARRAY[]::uuid[];
    v_task_id uuid;
BEGIN
    IF p_project_id IS NULL
       OR p_user_id IS NULL
       OR p_target_role IS NULL
       OR p_target_role NOT IN ('annotator', 'reviewer', 'viewer')
       OR p_is_removal IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    v_work := aap_labeling_v1.read_membership_work(
        p_project_id, p_user_id, NULL, NULL
    );
    IF v_work ->> 'schema' IS DISTINCT FROM 'aap-labeling-membership-work/v1' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid membership work facts';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(ids.id ORDER BY ids.id), ARRAY[]::uuid[]
    )
    INTO v_batch_ids
    FROM (
        SELECT entry.value::uuid AS id
        FROM pg_catalog.jsonb_array_elements_text(
            v_work -> 'batch_annotator_ids'
        ) AS entry(value)
        UNION
        SELECT entry.value::uuid AS id
        FROM pg_catalog.jsonb_array_elements_text(
            v_work -> 'batch_reviewer_ids'
        ) AS entry(value)
    ) AS ids;

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_annotation_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'annotation_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_review_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'review_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_lock_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'lock_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_materialization_annotation_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'materialization_annotation_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_materialization_review_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'materialization_review_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT ids.id ORDER BY ids.id), ARRAY[]::uuid[]
    )
    INTO v_advisory_task_ids
    FROM pg_catalog.unnest(
        v_annotation_task_ids || v_review_task_ids || v_lock_task_ids
    ) AS ids(id);

    v_task_ids := v_advisory_task_ids;
    IF NOT p_is_removal THEN
        IF p_target_role <> 'annotator' THEN
            v_task_ids := v_task_ids || v_materialization_annotation_task_ids;
        END IF;
        IF p_target_role <> 'reviewer' THEN
            v_task_ids := v_task_ids || v_materialization_review_task_ids;
        END IF;
    END IF;

    BEGIN
        PERFORM batch.id
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
          AND batch.id = ANY(v_batch_ids)
        ORDER BY batch.id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '批次资源正在变更，请刷新后重试'
                )
            );
    END;

    BEGIN
        PERFORM task.id
        FROM public.tasks AS task
        WHERE task.project_id = p_project_id
          AND task.id = ANY(v_task_ids)
        ORDER BY task.id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务资源正在变更，请刷新后重试'
                )
            );
    END;

    FOREACH v_task_id IN ARRAY v_advisory_task_ids LOOP
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_locks AS advisory_lock
            WHERE advisory_lock.locktype = 'advisory'
              AND advisory_lock.objsubid = 1
              AND advisory_lock.granted
              AND advisory_lock.database = (
                  SELECT db.oid FROM pg_catalog.pg_database AS db
                  WHERE db.datname = pg_catalog.current_database()
              )
              AND advisory_lock.classid::bigint = (
                  (
                      pg_catalog.hashtextextended(
                          'aap:task-edit-lock:' || v_task_id::text, 0
                      ) >> 32
                  ) & 4294967295::bigint
              )
              AND advisory_lock.objid::bigint = (
                  pg_catalog.hashtextextended(
                      'aap:task-edit-lock:' || v_task_id::text, 0
                  ) & 4294967295::bigint
              )
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务正在编辑，请刷新后重试'
                )
            );
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'task-edit-lock:' || v_task_id::text,
                0
            )
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务正在编辑，请刷新后重试'
                )
            );
        END IF;
    END LOOP;

    BEGIN
        PERFORM task_lock.id
        FROM public.task_locks AS task_lock
        JOIN public.tasks AS task ON task.id = task_lock.task_id
        WHERE task.project_id = p_project_id
          AND task_lock.user_id = p_user_id
          AND task_lock.expire_at > pg_catalog.clock_timestamp()
        ORDER BY task_lock.task_id, task_lock.user_id
        FOR UPDATE OF task_lock NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务锁正在变更，请刷新后重试'
                )
            );
    END;

    RETURN pg_catalog.jsonb_build_object('ok', true);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_membership_work(uuid, uuid, text, boolean)
    FROM PUBLIC;
-- aap-statement-break
DO $postcheck$
DECLARE
    v_signatures text[] := ARRAY[
        'aap_labeling_v1.task_edit_lock_command(jsonb)',
        'aap_labeling_v1.lock_video_input_manifest_member(uuid,uuid,uuid,jsonb,jsonb)',
        'aap_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])',
        'aap_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)'
    ]::text[];
    v_expected_legacy integer[] := ARRAY[6, 2, 2, 2];
    v_expected_current integer[] := ARRAY[7, 1, 1, 1];
    v_legacy_literal text := '''aap:task-edit-lock:''';
    v_current_literal text := '''task-edit-lock:''';
    v_signature text;
    v_body text;
    v_legacy_count integer;
    v_current_count integer;
    v_index integer;
BEGIN
    FOR v_index IN 1..pg_catalog.cardinality(v_signatures) LOOP
        v_signature := v_signatures[v_index];
        SELECT function.prosrc INTO v_body
        FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(v_signature);
        IF v_body IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'task edit-lock cutover lost an entry point: ' || v_signature;
        END IF;
        v_legacy_count := (
            pg_catalog.length(v_body)
            - pg_catalog.length(pg_catalog.replace(v_body, v_legacy_literal, ''))
        ) / pg_catalog.length(v_legacy_literal);
        v_current_count := (
            pg_catalog.length(v_body)
            - pg_catalog.length(pg_catalog.replace(v_body, v_current_literal, ''))
        ) / pg_catalog.length(v_current_literal);
        IF v_legacy_count <> v_expected_legacy[v_index]
           OR v_current_count <> v_expected_current[v_index] THEN
            RAISE EXCEPTION USING ERRCODE = '55000',
                MESSAGE = 'task edit-lock cutover did not install the expected domain: ' || v_signature;
        END IF;
    END LOOP;
END;
$postcheck$;
