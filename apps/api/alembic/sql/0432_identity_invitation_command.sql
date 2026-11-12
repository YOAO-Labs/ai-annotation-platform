CREATE OR REPLACE FUNCTION aap_identity_invitations_v1.apply_invitation_command(p_command jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_count integer;
    v_operation text;
    v_operation_id uuid;
    v_fingerprint text;
    v_derivation_id text;
    v_actor_input jsonb;
    v_expected_key_hash text;
    v_context jsonb;
    v_config jsonb;
    v_args jsonb;
    v_derived jsonb;
    v_policy jsonb;
    v_actor_parse jsonb;
    v_actor_lock jsonb;
    v_principal jsonb;
    v_actor_native jsonb;
    v_actor_id uuid;
    v_actor_role text;
    v_allow_null boolean;
    v_user_ids uuid[] := ARRAY[]::uuid[];
    v_initial record;
    v_invitation record;
    v_owner record;
    v_inviter record;
    v_target record;
    v_project jsonb;
    v_workspace jsonb;
    v_project_id uuid;
    v_owner_id uuid;
    v_target_id uuid;
    v_snapshot_target_id uuid;
    v_project_role text;
    v_group jsonb;
    v_password_hash text;
    v_claims jsonb;
    v_group_id uuid;
    v_group_name text;
    v_user_id uuid;
    v_invitation_id uuid;
    v_mail_hash text;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_token text;
    v_status text;
    v_receipt record;
    v_outbox record;
    v_result jsonb;
    v_data jsonb;
    v_acceptance jsonb;
    v_batch jsonb;
    v_batch_item jsonb;
    v_scope_project record;
    v_batch_result jsonb;
    v_batch_snapshot jsonb;
    v_mail_context jsonb;
    v_audit_id bigint;
    v_detail jsonb;
    v_member_reply jsonb;
    v_active_count bigint;
    v_replayed boolean := false;
    v_operation_lock boolean;
BEGIN
    SELECT account.id,account.name,account.role,account.is_active INTO v_owner
    FROM public.users AS account WHERE false;
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_command);
    IF v_count <> 11 OR NOT (p_command ?& ARRAY[
        'schema','operation','operation_id','actor','expected_key_hash',
        'request_fingerprint','derivation_id','audit_context','config','args','derived'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_command) AS field(name)
        WHERE field.name NOT IN (
            'schema','operation','operation_id','actor','expected_key_hash',
            'request_fingerprint','derivation_id','audit_context','config','args','derived'
        )
    ) OR p_command->>'schema' IS DISTINCT FROM 'aap-identity-invitations-native/v1'
      OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
      OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
      OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'actor') NOT IN ('object','null')
      OR pg_catalog.jsonb_typeof(p_command->'expected_key_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(p_command->'audit_context') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'config') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'args') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'derived') IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_operation := p_command->>'operation';
    IF v_operation NOT IN (
        'create_invitation','apply_invitation_batch','revoke_invitation',
        'resend_invitation','send_invitation_email','register_invitation','accept_invitation'
    ) OR NOT aap_identity_invitations_v1._args_valid(v_operation,p_command->'args') THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    BEGIN v_operation_id := (p_command->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END;
    v_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_actor_input := p_command->'actor';
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_context := p_command->'audit_context';
    v_config := p_command->'config';
    v_args := p_command->'args';
    v_derived := p_command->'derived';
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(v_derived);
    IF v_count <> 9 OR NOT (v_derived ?& ARRAY[
        'password_hash','claims','token','batch_tokens','mail_token_hash',
        'mail_token_ciphertext','mail_encryption_id','frontend_base_url','mail_frontend_url'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(v_derived) AS field(name)
        WHERE field.name NOT IN (
            'password_hash','claims','token','batch_tokens','mail_token_hash',
            'mail_token_ciphertext','mail_encryption_id','frontend_base_url','mail_frontend_url'
        )
    ) OR pg_catalog.jsonb_typeof(v_derived->'password_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'claims') NOT IN ('object','null')
      OR pg_catalog.jsonb_typeof(v_derived->'token') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'batch_tokens') NOT IN ('array','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_token_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_token_ciphertext') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_encryption_id') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'frontend_base_url') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_frontend_url') NOT IN ('string','null') THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    IF (pg_catalog.jsonb_typeof(v_derived->'password_hash') = 'string'
        AND ((v_derived->>'password_hash') !~ '^\$2[aby]\$12\$[./A-Za-z0-9]{53}$'
             OR pg_catalog.char_length(v_derived->>'password_hash') <> 60))
       OR (pg_catalog.jsonb_typeof(v_derived->'token') = 'string'
           AND pg_catalog.char_length(v_derived->>'token') NOT BETWEEN 1 AND 64)
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_token_hash') = 'string'
           AND (v_derived->>'mail_token_hash') COLLATE "C" !~ '^[0-9a-f]{64}$')
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_encryption_id') = 'string'
           AND (v_derived->>'mail_encryption_id') COLLATE "C" !~ '^[0-9a-f]{64}$')
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_token_ciphertext') = 'string'
           AND pg_catalog.char_length(v_derived->>'mail_token_ciphertext') NOT BETWEEN 1 AND 4096)
       OR (pg_catalog.jsonb_typeof(v_derived->'frontend_base_url') = 'string'
           AND pg_catalog.char_length(v_derived->>'frontend_base_url') < 1)
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_frontend_url') = 'string'
           AND pg_catalog.char_length(v_derived->>'mail_frontend_url') < 1) THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    IF v_operation IN ('create_invitation','resend_invitation') THEN
        IF v_derived->'token' IS NULL OR v_derived->'token' = 'null'::jsonb
           OR v_derived->'frontend_base_url' IS NULL OR v_derived->'frontend_base_url' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN ('token','frontend_base_url') AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF v_operation = 'register_invitation' THEN
        IF v_derived->'password_hash' = 'null'::jsonb OR v_derived->'claims' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN ('password_hash','claims') AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF v_operation = 'send_invitation_email' THEN
        IF v_derived->'mail_token_hash' = 'null'::jsonb
           OR v_derived->'mail_token_ciphertext' = 'null'::jsonb
           OR v_derived->'mail_encryption_id' = 'null'::jsonb
           OR v_derived->'mail_frontend_url' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN (
                          'mail_token_hash','mail_token_ciphertext','mail_encryption_id','mail_frontend_url'
                      ) AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF v_operation = 'apply_invitation_batch' THEN
        IF v_derived->'batch_tokens' = 'null'::jsonb
           OR v_derived->'frontend_base_url' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN ('batch_tokens','frontend_base_url') AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                  WHERE pair.value <> 'null'::jsonb) THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_allow_null := v_operation = 'register_invitation';
    v_actor_parse := aap_identity_invitations_v1._actor(
        v_actor_input,v_expected_key_hash,v_allow_null,false
    );
    IF v_actor_parse->'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor_parse; END IF;
    IF v_actor_input IS NOT NULL AND pg_catalog.jsonb_typeof(v_actor_input) <> 'null' THEN
        BEGIN v_actor_id := (v_actor_input->>'actor_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END;
    ELSIF NOT v_allow_null THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;

    IF v_operation = 'create_invitation' THEN
        IF pg_catalog.jsonb_typeof(v_args->'project_id') = 'string' THEN
            v_project_id := (v_args->>'project_id')::uuid;
            v_project := aap_identity_invitations_v1._project_record(v_project_id);
            IF v_project IS NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目不存在'
                );
            END IF;
            v_owner_id := (v_project->>'owner_id')::uuid;
        END IF;
        SELECT account.id INTO v_target_id FROM public.users AS account
        WHERE account.email = v_args->>'email' AND account.is_active
        ORDER BY account.id LIMIT 1;
        v_snapshot_target_id := v_target_id;
        v_user_ids := ARRAY[v_actor_id,v_owner_id,v_target_id];
    ELSIF v_operation = 'apply_invitation_batch' THEN
        v_batch_snapshot := aap_identity_invitations_v1._batch_snapshot(
            v_actor_id,v_args->'items'
        );
        SELECT COALESCE(
            pg_catalog.array_agg((item.value #>> '{}')::uuid), ARRAY[]::uuid[]
        ) INTO v_user_ids
        FROM pg_catalog.jsonb_array_elements(v_batch_snapshot->'user_ids') AS item(value);
    ELSIF v_operation IN ('revoke_invitation','resend_invitation','send_invitation_email') THEN
        SELECT invitation.* INTO v_initial FROM public.user_invitations AS invitation
        WHERE invitation.id = (v_args->>'invitation_id')::uuid;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        IF v_operation = 'send_invitation_email' AND v_initial.project_id IS NOT NULL THEN
            v_project_id := v_initial.project_id;
            v_project := aap_identity_invitations_v1._project_record(v_project_id);
            IF v_project IS NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_owner_id := (v_project->>'owner_id')::uuid;
        END IF;
        v_user_ids := ARRAY[v_actor_id,v_owner_id,
            CASE WHEN v_operation = 'send_invitation_email' THEN v_initial.invited_by END];
    ELSIF v_operation IN ('register_invitation','accept_invitation') THEN
        v_token := v_args->>'invitation_token';
        SELECT invitation.* INTO v_initial FROM public.user_invitations AS invitation
        WHERE invitation.token = v_token;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请链接无效'
            );
        END IF;
        v_project_id := v_initial.project_id;
        v_project := aap_identity_invitations_v1._project_record(v_project_id);
        IF v_project_id IS NOT NULL AND v_project IS NULL THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_owner_unavailable','目标项目已删除，无法接受邀请'
            );
        END IF;
        v_owner_id := (v_project->>'owner_id')::uuid;
        IF v_operation = 'register_invitation' THEN
            SELECT account.id INTO v_target_id FROM public.users AS account
            WHERE account.id = v_operation_id;
        ELSE
            v_target_id := v_actor_id;
        END IF;
        v_user_ids := ARRAY[v_initial.invited_by,v_owner_id,v_target_id,v_actor_id];
    ELSE
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_user_ids := ARRAY(SELECT DISTINCT id FROM unnest(v_user_ids) AS users(id)
                        WHERE id IS NOT NULL ORDER BY id);
    IF NOT aap_identity_invitations_v1._lock_users(v_user_ids) THEN
        RETURN aap_identity_invitations_v1._failure('current_scope_busy');
    END IF;
    v_actor_lock := aap_identity_invitations_v1._actor(
        v_actor_input,v_expected_key_hash,v_allow_null,true
    );
    IF v_actor_lock->'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor_lock; END IF;
    v_principal := v_actor_lock->'principal';
    v_actor_native := v_actor_lock->'actor';
    v_actor_role := v_actor_native->>'role';
    IF v_actor_id IS NOT NULL AND v_operation <> 'accept_invitation'
       AND v_actor_role NOT IN ('super_admin','project_admin') THEN
        RETURN aap_identity_invitations_v1._failure(
            'policy_rejected','manager_required','当前账号不再具备邀请权限'
        );
    END IF;
    v_policy := aap_identity_invitations_v1._policy(v_config);

    IF v_operation = 'apply_invitation_batch' THEN
        IF v_actor_role NOT IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','manager_required','当前账号不再具备邀请权限'
            );
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        BEGIN
            PERFORM account.id FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_result := v_receipt.result;
            -- A batch receipt is not a durable permission grant. Recheck every
            -- successful Project scope and referenced invitation before
            -- returning the frozen response to a project administrator.
            FOR v_scope_project IN
                SELECT DISTINCT
                    (argument.value->>'project_id')::uuid AS project_id,
                    (v_batch_snapshot #>> ARRAY[
                        'owners', argument.value->>'index'
                    ])::uuid AS owner_id
                FROM pg_catalog.jsonb_array_elements(v_args->'items') AS argument(value)
                JOIN pg_catalog.jsonb_array_elements(v_result->'items') AS prior(value)
                  ON prior.value->>'index' = argument.value->>'index'
                WHERE prior.value->'ok' = 'true'::jsonb
                  AND pg_catalog.jsonb_typeof(argument.value->'project_id') = 'string'
                ORDER BY project_id
            LOOP
                IF v_scope_project.owner_id IS NULL THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                BEGIN
                    v_workspace := aap_workspace_v1.lock_invitation_project(
                        v_scope_project.project_id, v_scope_project.owner_id
                    );
                EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END;
                IF v_workspace->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-project/v1'
                   OR v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                IF v_actor_role = 'project_admin'
                   AND v_scope_project.owner_id IS DISTINCT FROM v_actor_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
            END LOOP;
            FOR v_batch_item IN
                SELECT prior.value
                FROM pg_catalog.jsonb_array_elements(v_result->'items') AS prior(value)
                WHERE prior.value->'ok' = 'true'::jsonb
                ORDER BY (prior.value->>'invitation_id')::uuid
            LOOP
                BEGIN
                    SELECT invitation.* INTO v_invitation
                    FROM public.user_invitations AS invitation
                    WHERE invitation.id = (v_batch_item->>'invitation_id')::uuid
                      AND invitation.invited_by = v_actor_id
                      AND invitation.email = v_batch_item->>'email'
                      AND invitation.project_id IS NOT DISTINCT FROM
                          CASE WHEN pg_catalog.jsonb_typeof(v_batch_item->'project_id') = 'null'
                              THEN NULL ELSE (v_batch_item->>'project_id')::uuid END
                    FOR KEY SHARE NOWAIT;
                EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END;
                IF NOT FOUND THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
            END LOOP;
            v_replayed := true;
        ELSE
            v_result := aap_identity_invitations_v1._batch_effects(
                false,v_actor_id,v_actor_role,v_principal->>'email',v_context,
                v_policy,v_args->'items',v_derived->'batch_tokens',v_batch_snapshot
            );
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    v_operation_lock := pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
    );
    IF NOT v_operation_lock THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;

    IF v_operation = 'create_invitation' THEN
        IF v_args->>'role' NOT IN ('super_admin','project_admin','employee','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invalid_platform_role','非法角色: ' || (v_args->>'role')
            );
        END IF;
        IF v_project_id IS NULL AND v_args->'project_member_role' <> 'null'::jsonb THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','account_project_role','账号级邀请不得包含项目职责'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'project_member_role' NOT IN ('annotator','reviewer','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invalid_project_role',
                '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'role' = 'viewer'
           AND v_args->>'project_member_role' <> 'viewer' THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','platform_project_role_incompatible',
                '账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'role' IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','platform_project_role_incompatible','账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目'
            );
        END IF;
        IF v_actor_role = 'project_admin' AND v_args->>'role' NOT IN ('employee','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_admin_role_forbidden','项目管理员仅能邀请员工或观察者'
            );
        END IF;
        BEGIN
            PERFORM account.id FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(pg_catalog.hashtextextended(v_args->>'email',0)) THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace #>> '{error,code}' = 'project_missing' THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目不存在'
                );
            ELSIF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
            IF v_actor_role = 'project_admin' AND v_owner_id <> v_actor_id THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
                );
            END IF;
        END IF;
        SELECT * INTO v_receipt FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation <> v_operation OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_invitation_id := (v_receipt.result #>> '{invitation,id}')::uuid;
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_invitation_id AND invitation.invited_by = v_actor_id
              AND invitation.email = v_args->>'email'
            FOR KEY SHARE NOWAIT;
            IF NOT FOUND OR v_invitation.project_id IS DISTINCT FROM v_project_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_result := v_receipt.result;
            v_replayed := true;
        ELSE
            IF aap_identity_invitations_v1.invitation_quota_reached(
                v_actor_id,v_now,(v_policy->>'max_invitations_per_day')::integer
            ) THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','daily_quota',
                    '每位管理员滚动 24 小时最多邀请 ' ||
                    (v_policy->>'max_invitations_per_day') || ' 人，请稍后再试'
                );
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_target
            FROM public.users AS account
            WHERE account.email = v_args->>'email' AND account.is_active
            ORDER BY account.id LIMIT 1;
            IF FOUND THEN
                IF v_target.id IS DISTINCT FROM v_snapshot_target_id THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END IF;
                v_target_id := v_target.id;
                IF v_project_id IS NULL THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','active_email_exists',
                        '邮箱 ' || (v_args->>'email') || ' 已注册，请选择目标项目后邀请该用户加入'
                    );
                END IF;
                IF v_target.role NOT IN ('employee','viewer')
                   OR (v_target.role = 'viewer' AND v_args->>'project_member_role' <> 'viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','incompatible_account_role',
                        aap_identity_invitations_v1._incompatible_role_detail(
                            v_target.role,v_args->>'project_member_role'
                        )
                    );
                END IF;
                IF aap_workspace_v1.read_invitation_membership(v_project_id,v_target_id) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','already_member','该账号已是目标项目成员'
                    );
                END IF;
            ELSIF v_snapshot_target_id IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END IF;
            BEGIN
                FOR v_invitation IN
                    SELECT invitation.id FROM public.user_invitations AS invitation
                    WHERE invitation.email = v_args->>'email'
                      AND invitation.accepted_at IS NULL
                      AND (v_actor_role = 'super_admin' OR invitation.invited_by = v_actor_id)
                    ORDER BY invitation.id
                    FOR UPDATE NOWAIT
                LOOP
                    UPDATE public.user_invitations AS invitation
                    SET expires_at = v_now
                    WHERE invitation.id = v_invitation.id;
                END LOOP;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END;
            v_token := v_derived->>'token';
            INSERT INTO public.user_invitations (
                id,email,role,project_role,group_name,project_id,token,expires_at,invited_by
            ) VALUES (
                pg_catalog.gen_random_uuid(),v_args->>'email',v_args->>'role',
                v_args->>'project_member_role',v_args->>'group_name',v_project_id,
                v_token,v_now + pg_catalog.make_interval(days => (v_policy->>'invitation_ttl_days')::integer),
                v_actor_id
            ) RETURNING id INTO v_invitation_id;
            v_audit_id := aap_audit_v1.record_identity_invitation_event(
                'user.invite',
                pg_catalog.jsonb_build_object('actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role),
                pg_catalog.jsonb_build_object('type','user','id',v_args->>'email'),
                v_context,
                pg_catalog.jsonb_build_object(
                    'role',v_args->>'role','group_name',v_args->'group_name','project_id',v_args->'project_id'
                )
            );
            v_result := aap_identity_invitations_v1._empty_result()
                || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation_id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    ),'frontend_base_url',v_policy->'frontend_base_url'
                );
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result,audit_id
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result,v_audit_id
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    IF v_operation IN ('revoke_invitation','resend_invitation') THEN
        IF v_actor_role = 'project_admin'
           AND v_initial.invited_by IS DISTINCT FROM v_actor_id THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_result := v_receipt.result;
            v_replayed := true;
        ELSE
            IF v_invitation.accepted_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_accepted',CASE WHEN v_operation = 'revoke_invitation' THEN '该邀请已被接受，无法撤销' ELSE '该邀请已被接受，无法重发' END
                );
            END IF;
            IF v_operation = 'resend_invitation'
               AND v_actor_role = 'project_admin'
               AND v_invitation.role NOT IN ('employee','viewer') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','privileged_resend','项目管理员不能重发高权限邀请'
                );
            END IF;
            IF v_operation = 'revoke_invitation' THEN
                IF v_invitation.revoked_at IS NULL THEN
                    UPDATE public.user_invitations AS invitation
                    SET revoked_at = v_now, expires_at = v_now
                    WHERE invitation.id = v_invitation.id;
                END IF;
                v_detail := pg_catalog.jsonb_build_object(
                    'email',v_invitation.email,'role',v_invitation.role
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.invite_revoke',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',v_invitation.id::text),
                    v_context,v_detail
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'invitation_id',v_invitation.id
                    );
            ELSE
                IF v_invitation.role IN ('annotator','reviewer') THEN
                    IF v_invitation.project_id IS NOT NULL AND v_invitation.project_role IS NULL THEN
                        v_invitation.project_role := v_invitation.role;
                    END IF;
                    v_invitation.role := 'employee';
                END IF;
                v_token := v_derived->>'token';
                UPDATE public.user_invitations AS invitation
                SET role = v_invitation.role,
                    project_role = v_invitation.project_role,
                    token = v_token,
                    expires_at = v_now + pg_catalog.make_interval(
                        days => (v_policy->>'invitation_ttl_days')::integer
                    ),
                    revoked_at = NULL
                WHERE invitation.id = v_invitation.id;
                v_invitation.role := v_invitation.role;
                v_invitation.token := v_token;
                v_invitation.expires_at := v_now + pg_catalog.make_interval(
                    days => (v_policy->>'invitation_ttl_days')::integer
                );
                v_invitation.revoked_at := NULL;
                v_detail := pg_catalog.jsonb_build_object(
                    'email',v_invitation.email,'role',v_invitation.role
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.invite_resend',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',v_invitation.id::text),
                    v_context,v_detail
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'invitation',aap_identity_invitations_v1._invitation_record(v_invitation.id,NULL),
                        'frontend_base_url',v_derived->'frontend_base_url'
                    );
            END IF;
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result,audit_id
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result,v_audit_id
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    IF v_operation = 'send_invitation_email' THEN
        IF v_actor_role = 'project_admin'
           AND v_initial.invited_by IS DISTINCT FROM v_actor_id THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(v_initial.email,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_project := pg_catalog.jsonb_build_object(
                'id',v_project_id,'name',v_workspace #>> '{data,name}',
                'owner_id',v_owner_id
            );
            SELECT account.id,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR KEY SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND OR v_invitation.invited_by IS DISTINCT FROM v_initial.invited_by THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            SELECT outbox.* INTO v_outbox
            FROM public.identity_credential_mail_outbox AS outbox
            WHERE outbox.id = v_operation_id AND outbox.purpose = 'invitation'
              AND outbox.invitation_id = v_invitation.id;
            IF NOT FOUND OR v_outbox.token_hash IS DISTINCT FROM pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_invitation.token,'UTF8')),'hex'
            ) OR v_outbox.email IS DISTINCT FROM v_invitation.email
              OR v_outbox.actor_id IS DISTINCT FROM v_invitation.invited_by
              OR v_outbox.expires_at IS DISTINCT FROM v_invitation.expires_at
            THEN RETURN aap_identity_invitations_v1._failure('operation_conflict'); END IF;
            v_result := v_receipt.result || pg_catalog.jsonb_build_object(
                'mail_status',CASE WHEN v_outbox.status = 'delivered' THEN 'sent'
                    WHEN v_outbox.status IN ('rejected','cancelled','expired') THEN 'failed'
                    ELSE 'pending' END,
                'mail_failure_code',CASE WHEN v_outbox.status IN ('rejected','cancelled','expired')
                    THEN v_outbox.last_reason ELSE NULL END
            );
            v_replayed := true;
        ELSE
            IF v_invitation.accepted_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_used','该邀请已被使用'
                );
            ELSIF v_invitation.revoked_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_revoked','该邀请已撤销'
                );
            ELSIF v_invitation.expires_at <= v_now THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_expired','该邀请已过期'
                );
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_inviter
            FROM public.users AS account WHERE account.id = v_invitation.invited_by;
            IF NOT FOUND OR NOT v_inviter.is_active
               OR v_inviter.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','issuer_inactive','邀请人已停用或不再具备管理权限'
                );
            END IF;
            IF v_invitation.role IN ('super_admin','project_admin') AND v_inviter.role <> 'super_admin' THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','issuer_role_invalid','该邀请权限已失效'
                );
            END IF;
            IF v_project_id IS NOT NULL AND v_inviter.role = 'project_admin'
               AND v_owner_id <> v_invitation.invited_by THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
                );
            END IF;
            v_mail_hash := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_invitation.token,'UTF8')),'hex'
            );
            IF v_derived->>'mail_token_hash' IS DISTINCT FROM v_mail_hash THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_project_role := CASE WHEN v_project_id IS NULL THEN NULL
                ELSE COALESCE(v_invitation.project_role,
                    CASE WHEN v_invitation.role IN ('annotator','reviewer','viewer')
                         THEN v_invitation.role END) END;
            IF v_project_id IS NOT NULL AND v_project_role NOT IN ('annotator','reviewer','viewer') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invalid_project_role',
                    '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
                );
            END IF;
            v_mail_context := pg_catalog.jsonb_build_object(
                'project_name',CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END,
                'project_member_role',v_project_role,
                'sender_actor',pg_catalog.jsonb_build_object(
                    'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                ),
                'audit_context',v_context
            );
            INSERT INTO public.identity_credential_mail_outbox (
                id,actor_id,challenge_id,purpose,email,frontend_base_url,
                token_hash,token_ciphertext,encryption_id,expires_at,
                invitation_id,invitation_context
            ) VALUES (
                v_operation_id,v_invitation.invited_by,NULL,'invitation',v_invitation.email,
                v_derived->>'mail_frontend_url',v_mail_hash,
                v_derived->>'mail_token_ciphertext',v_derived->>'mail_encryption_id',
                v_invitation.expires_at,v_invitation.id,v_mail_context
            );
            v_result := aap_identity_invitations_v1._empty_result()
                || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    ),
                    'invitation_id',v_invitation.id,'email',v_invitation.email,
                    'mail_status','pending','mail_failure_code',NULL
                );
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    IF v_operation IN ('register_invitation','accept_invitation') THEN
        v_token := v_args->>'invitation_token';
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(v_initial.email,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_project := pg_catalog.jsonb_build_object(
                'id',v_project_id,'name',v_workspace #>> '{data,name}',
                'owner_id',v_owner_id
            );
            SELECT account.id,account.name,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND OR v_invitation.token IS DISTINCT FROM v_token THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请链接无效'
            );
        END IF;
        SELECT account.id,account.name,account.email,account.role,account.is_active
        INTO v_inviter FROM public.users AS account WHERE account.id = v_invitation.invited_by;
        IF NOT FOUND OR NOT v_inviter.is_active
           OR v_inviter.role NOT IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','issuer_inactive','邀请人已停用或不再具备管理权限'
            );
        END IF;
        IF v_invitation.role IN ('super_admin','project_admin') AND v_inviter.role <> 'super_admin' THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','issuer_role_invalid','该邀请权限已失效'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_inviter.role = 'project_admin'
           AND v_owner_id <> v_invitation.invited_by THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
            );
        END IF;
        IF v_invitation.accepted_at IS NOT NULL THEN
            IF v_operation = 'register_invitation'
               AND v_invitation.accepted_user_id = v_operation_id THEN
                SELECT * INTO v_receipt
                FROM public.identity_invitation_operation_receipts AS receipt
                WHERE receipt.operation_id = v_operation_id;
                IF NOT FOUND OR v_receipt.operation <> v_operation
                   OR v_receipt.actor_id IS NOT NULL
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                SELECT account.password_hash,account.credential_generation,account.role
                INTO v_target FROM public.users AS account
                WHERE account.id = v_operation_id AND account.is_active
                  AND account.email = v_invitation.email;
                IF NOT FOUND THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'operation_conflict'
                    );
                END IF;
                IF v_target.password_hash IS DISTINCT FROM v_derived->>'password_hash'
                   OR v_target.credential_generation IS DISTINCT FROM
                      (v_receipt.result #>> '{claims,gen}')::bigint
                   OR v_target.role IS DISTINCT FROM v_receipt.result #>> '{claims,role}'
                   OR v_receipt.result->'claims' IS DISTINCT FROM v_derived->'claims' THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                v_result := v_receipt.result;
                v_replayed := true;
            ELSIF v_operation = 'accept_invitation' THEN
                SELECT * INTO v_receipt
                FROM public.identity_invitation_operation_receipts AS receipt
                WHERE receipt.operation_id = v_operation_id;
                IF NOT FOUND OR v_receipt.operation <> v_operation
                   OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id
                   OR v_invitation.accepted_user_id IS DISTINCT FROM v_actor_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                v_result := v_receipt.result;
                v_replayed := true;
            ELSE
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_used','该邀请已被使用'
                );
            END IF;
        ELSE
            SELECT * INTO v_receipt
            FROM public.identity_invitation_operation_receipts AS receipt
            WHERE receipt.operation_id = v_operation_id;
            IF FOUND THEN RETURN aap_identity_invitations_v1._failure('operation_conflict'); END IF;
            IF v_invitation.revoked_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_revoked','该邀请已撤销'
                );
            END IF;
            IF v_invitation.expires_at <= v_now THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_expired','该邀请已过期'
                );
            END IF;
            v_project_role := CASE WHEN v_project_id IS NULL THEN NULL
                ELSE COALESCE(v_invitation.project_role,
                    CASE WHEN v_invitation.role IN ('annotator','reviewer','viewer')
                         THEN v_invitation.role END) END;
            IF v_operation = 'register_invitation' THEN
                IF v_invitation.role NOT IN ('super_admin','project_admin','employee','viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','legacy_platform_role','该邀请包含已失效的账号角色，请联系管理员重发'
                    );
                END IF;
                IF v_project_id IS NOT NULL AND v_invitation.role IN ('super_admin','project_admin') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','platform_project_role_incompatible','账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目'
                    );
                END IF;
                IF pg_catalog.char_length(COALESCE(v_invitation.group_name,'')) > 100 THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','group_name_invalid','邀请中的数据组名称无效，请联系管理员重发邀请'
                    );
                END IF;
                IF EXISTS (SELECT 1 FROM public.users AS account
                           WHERE account.email = v_invitation.email) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','email_exists','该邮箱已注册，请让该用户登录后确认项目邀请'
                    );
                END IF;
                v_password_hash := v_derived->>'password_hash';
                v_claims := v_derived->'claims';
                SELECT pg_catalog.count(*)::integer INTO v_count
                FROM pg_catalog.jsonb_object_keys(v_claims);
                IF v_count <> 6 OR NOT (v_claims ?& ARRAY['sub','role','exp','jti','gen','iss'])
                   OR EXISTS (
                       SELECT 1 FROM pg_catalog.jsonb_object_keys(v_claims) AS field(name)
                       WHERE field.name NOT IN ('sub','role','exp','jti','gen','iss')
                   ) OR pg_catalog.jsonb_typeof(v_claims->'sub') IS DISTINCT FROM 'string'
                   OR v_claims->>'sub' IS DISTINCT FROM v_operation_id::text
                   OR pg_catalog.jsonb_typeof(v_claims->'jti') IS DISTINCT FROM 'string'
                   OR v_claims->>'jti' IS DISTINCT FROM v_operation_id::text
                   OR pg_catalog.jsonb_typeof(v_claims->'role') IS DISTINCT FROM 'string'
                   OR v_claims->>'role' IS DISTINCT FROM v_invitation.role
                   OR pg_catalog.jsonb_typeof(v_claims->'exp') IS DISTINCT FROM 'number'
                   OR (v_claims->>'exp') !~ '^[1-9][0-9]*$'
                   OR (v_claims->>'exp')::numeric > 9223372036854775807
                   OR pg_catalog.jsonb_typeof(v_claims->'gen') IS DISTINCT FROM 'number'
                   OR v_claims->>'gen' IS DISTINCT FROM '0'
                   OR pg_catalog.jsonb_typeof(v_claims->'iss') IS DISTINCT FROM 'string'
                   OR v_claims->>'iss' IS DISTINCT FROM 'aap-identity/v1' THEN
                    RETURN aap_identity_invitations_v1._failure('invalid_request');
                END IF;
                BEGIN
                    IF pg_catalog.to_timestamp((v_claims->>'exp')::double precision) <= v_now THEN
                        RETURN aap_identity_invitations_v1._failure('token_expired');
                    END IF;
                EXCEPTION WHEN datetime_field_overflow OR numeric_value_out_of_range THEN
                    RETURN aap_identity_invitations_v1._failure('invalid_request');
                END;
                v_group_name := aap_identity_invitations_v1._strip_unicode_space(v_invitation.group_name);
                IF v_group_name = '' THEN v_group_name := NULL; END IF;
                IF v_group_name IS NOT NULL THEN
                    v_group := aap_identity_groups_v1.resolve_invitation_group(
                        v_invitation.id,v_invitation.token
                    );
                    IF pg_catalog.jsonb_typeof(v_group->'error') = 'object' THEN
                        IF v_group #>> '{error,code}' = 'invalid_group_name' THEN
                            RETURN aap_identity_invitations_v1._failure(
                                'policy_rejected','group_name_invalid','邀请中的数据组名称无效，请联系管理员重发邀请'
                            );
                        END IF;
                        RETURN aap_identity_invitations_v1._failure(
                            'policy_rejected','invitation_not_found','邀请链接无效'
                        );
                    END IF;
                    v_group_id := (v_group->>'id')::uuid;
                    v_group_name := v_group->>'name';
                    UPDATE public.user_invitations AS invitation
                    SET group_name = v_group_name WHERE invitation.id = v_invitation.id;
                END IF;
                INSERT INTO public.users (
                    id,email,name,password_hash,credential_generation,role,group_name,group_id,
                    is_active,email_verified_at
                ) VALUES (
                    v_operation_id,v_invitation.email,v_args->>'name',v_password_hash,0,
                    v_invitation.role,v_group_name,v_group_id,true,v_now
                );
                PERFORM aap_identity_invitations_v1.bind_fixture_invitation_birth(
                    v_invitation.id, v_operation_id
                );
                v_user_id := v_operation_id;
                UPDATE public.identity_account_presence SET status = 'online'
                WHERE user_id = v_user_id;
                IF v_project_id IS NOT NULL THEN
                    v_member_reply := aap_workspace_v1.grant_invitation_membership(
                        v_invitation.invited_by,v_project_id,v_project_role,
                        v_user_id,v_owner_id
                    );
                    IF v_member_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'invitation membership scope changed';
                    END IF;
                END IF;
                UPDATE public.user_invitations AS invitation
                SET accepted_at = v_now, accepted_user_id = v_user_id
                WHERE invitation.id = v_invitation.id;
                v_acceptance := pg_catalog.jsonb_build_object(
                    'project_id',v_project_id,
                    'project_name',CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END,
                    'project_member_role',v_project_role,
                    'next_action','wait_for_allocation',
                    'next_action_label',CASE WHEN v_project_id IS NULL THEN '等待管理员分配项目'
                                             ELSE '等待分派' END,
                    'responsible_person_name',CASE WHEN v_project_id IS NULL THEN NULL ELSE v_owner.name END,
                    'active_batch_count',0
                );
                IF v_project_id IS NOT NULL AND v_project_role IN ('annotator','reviewer') THEN
                    v_active_count := aap_labeling_v1.invitation_work_summary(
                        v_project_id,v_user_id,v_project_role
                    );
                    v_acceptance := v_acceptance || pg_catalog.jsonb_build_object(
                        'active_batch_count',v_active_count,
                        'next_action',CASE WHEN v_active_count > 0 THEN 'start_work' ELSE 'wait_for_allocation' END,
                        'next_action_label',CASE WHEN v_active_count > 0 THEN '开始工作' ELSE '等待分派' END
                    );
                END IF;
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.register',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_user_id,'email',v_invitation.email,'role',v_invitation.role
                    ),
                    pg_catalog.jsonb_build_object('type','user','id',v_user_id::text),
                    v_context,
                    pg_catalog.jsonb_build_object(
                        'email',v_invitation.email,'role',v_invitation.role,
                        'invitation_id',v_invitation.id,'project_id',v_project_id
                    )
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'user',aap_identity_invitations_v1._account_record(v_user_id),
                        'claims',v_claims,'acceptance',v_acceptance,
                        'email_verification_required',false
                    );
            ELSE
                IF v_project_id IS NULL THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','account_only_invitation',
                        '该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请'
                    );
                END IF;
                IF v_project_role NOT IN ('annotator','reviewer','viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','invalid_project_role',
                        '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
                    );
                END IF;
                IF v_actor_native->>'email' IS DISTINCT FROM v_invitation.email THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','email_mismatch','当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录'
                    );
                END IF;
                IF v_actor_role NOT IN ('employee','viewer')
                   OR (v_actor_role = 'viewer' AND v_project_role <> 'viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','incompatible_account_role',
                        aap_identity_invitations_v1._incompatible_role_detail(
                            v_actor_role,v_project_role
                        )
                    );
                END IF;
                IF aap_workspace_v1.read_invitation_membership(v_project_id,v_actor_id) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','already_member','该账号已是目标项目成员'
                    );
                END IF;
                v_member_reply := aap_workspace_v1.grant_invitation_membership(
                    v_invitation.invited_by,v_project_id,v_project_role,v_actor_id,v_owner_id
                );
                IF v_member_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    IF v_member_reply #>> '{error,code}' = 'membership_exists' THEN
                        RETURN aap_identity_invitations_v1._failure(
                            'policy_rejected','already_member','该账号已是目标项目成员'
                        );
                    END IF;
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END IF;
                UPDATE public.user_invitations AS invitation
                SET accepted_at = v_now, accepted_user_id = v_actor_id
                WHERE invitation.id = v_invitation.id;
                v_active_count := 0;
                IF v_project_role IN ('annotator','reviewer') THEN
                    v_active_count := aap_labeling_v1.invitation_work_summary(
                        v_project_id,v_actor_id,v_project_role
                    );
                END IF;
                v_acceptance := pg_catalog.jsonb_build_object(
                    'project_id',v_project_id,'project_name',v_project->>'name',
                    'project_member_role',v_project_role,
                    'next_action',CASE WHEN v_active_count > 0 THEN 'start_work' ELSE 'wait_for_allocation' END,
                    'next_action_label',CASE WHEN v_active_count > 0 THEN '开始工作' ELSE '等待分派' END,
                    'responsible_person_name',v_owner.name,
                    'active_batch_count',v_active_count
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.invite_accept_existing',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',v_invitation.id::text),
                    v_context,
                    pg_catalog.jsonb_build_object('email',v_actor_native->>'email','project_id',v_project_id)
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'user',aap_identity_invitations_v1._account_record(v_actor_id),
                        'acceptance',v_acceptance
                    );
            END IF;
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result,audit_id
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result,v_audit_id
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    -- Other transactional operations are handled below.
    RETURN aap_identity_invitations_v1._failure('unavailable');
END;
$function$;
