CREATE OR REPLACE FUNCTION aap_inference_gpu_v1.gpu_authority_step(p_action text, p_arguments text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
SET DateStyle = 'ISO, YMD'
SET extra_float_digits = 3
AS $function$
DECLARE
    v_args jsonb;
    v_result jsonb;
    v_id uuid;
    v_id2 uuid;
    v_resource text;
    v_now timestamp with time zone;
    v_time timestamp with time zone;
    v_token_expiry timestamp with time zone;
    v_membership public.gpu_backend_memberships%ROWTYPE;
    v_fence public.gpu_backend_fences%ROWTYPE;
    v_registry public.ml_backend_registry%ROWTYPE;
    v_health jsonb;
    v_residency jsonb;
    v_probe jsonb;
    v_subject jsonb;
    v_expected jsonb;
    v_proof jsonb;
    v_memberships jsonb;
    v_expected_item jsonb;
    v_previous_id uuid;
    v_text text;
    v_state text;
    v_reason text;
    v_counter text;
    v_next bigint;
    v_generation bigint;
    v_control bigint;
    v_runtime bigint;
    v_budget bigint;
    v_priority bigint;
    v_concurrency integer;
    v_boot text;
    v_observed_generation text;
    v_pools jsonb;
    v_require_idle boolean;
    v_challenge text;
    v_transition uuid;
    v_target text;
    v_operation text;
    v_blocker text;
    v_wait boolean;
    v_status text;
    v_trusted_empty boolean;
    v_reset_ack boolean;
    v_intent_matches boolean;
    v_enforce_intent boolean;
    v_rollout public.gpu_arbiter_rollouts%ROWTYPE;
    v_cancel public.gpu_backend_cancel_intents%ROWTYPE;
    v_terminal_intent aap_inference_gpu_v1.installation_terminal_intents%ROWTYPE;
    v_install_control aap_inference_gpu_v1.installation_control_intents%ROWTYPE;
    v_key bigint;
    v_probe_started timestamp with time zone;
    v_observed timestamp with time zone;
    v_deadline bigint;
    v_fingerprint text;
    v_domain jsonb;
    v_registry_exists boolean;
    v_retirement uuid;
BEGIN
    IF p_action IS NULL OR p_action !~ '^[a-z][a-z0-9_]{0,63}$'
       OR p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 1048576 THEN
        PERFORM aap_inference_gpu_v1._fail();
    END IF;
    BEGIN v_args := p_arguments::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN
        PERFORM aap_inference_gpu_v1._fail();
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        PERFORM aap_inference_gpu_v1._fail();
    END IF;
    v_now := pg_catalog.transaction_timestamp();

    IF p_action = 'fence_advance' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY['backend_registry_id','counter','gpu_resource_id','membership_epoch','token_expires_at']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_id := aap_inference_gpu_v1._uuid(v_args->'backend_registry_id');
        v_resource := aap_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        v_next := aap_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807);
        v_counter := aap_inference_gpu_v1._string(v_args->'counter',32);
        IF v_counter NOT IN ('generation','control_epoch') OR (v_args->'token_expires_at' <> 'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'token_expires_at') <> 'string') THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_token_expiry := CASE WHEN v_args->'token_expires_at' = 'null'::jsonb THEN NULL ELSE aap_inference_gpu_v1._timestamp(v_args->'token_expires_at') END;
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_membership.state <> 'active' OR v_membership.membership_epoch <> v_next THEN
            v_result := pg_catalog.jsonb_build_object('status','membership_changed');
        ELSE
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_result := pg_catalog.jsonb_build_object('status','fence_missing');
            ELSIF v_counter='generation' AND v_fence.generation_high_water=9223372036854775807 OR v_counter='control_epoch' AND v_fence.control_epoch_high_water=9223372036854775807 THEN
                v_result := pg_catalog.jsonb_build_object('status','high_water_exhausted');
            ELSIF v_counter='generation' THEN
                UPDATE public.gpu_backend_fences f SET generation_high_water=f.generation_high_water+1,
                    token_expiry_high_water=CASE WHEN v_token_expiry IS NULL THEN f.token_expiry_high_water ELSE GREATEST(COALESCE(f.token_expiry_high_water,v_token_expiry),v_token_expiry) END,
                    updated_at=v_now WHERE f.backend_registry_id=v_id RETURNING f.generation_high_water INTO v_next;
                v_result := pg_catalog.jsonb_build_object('status','advanced','value',v_next);
            ELSE
                UPDATE public.gpu_backend_fences f SET control_epoch_high_water=f.control_epoch_high_water+1,
                    token_expiry_high_water=CASE WHEN v_token_expiry IS NULL THEN f.token_expiry_high_water ELSE GREATEST(COALESCE(f.token_expiry_high_water,v_token_expiry),v_token_expiry) END,
                    updated_at=v_now WHERE f.backend_registry_id=v_id RETURNING f.control_epoch_high_water INTO v_next;
                v_result := pg_catalog.jsonb_build_object('status','advanced','value',v_next);
            END IF;
        END IF;

    ELSIF p_action = 'fence_token_expiry' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','token_expires_at']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=aap_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        v_next:=aap_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807); v_time:=aap_inference_gpu_v1._timestamp(v_args->'token_expires_at');
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_membership.state <> 'active' OR v_membership.membership_epoch<>v_next THEN v_result:=pg_catalog.jsonb_build_object('status','membership_changed');
        ELSE
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_result:=pg_catalog.jsonb_build_object('status','fence_missing');
            ELSE UPDATE public.gpu_backend_fences f SET token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE f.backend_registry_id=v_id;
                v_result:=pg_catalog.jsonb_build_object('status','recorded','token_expires_at',aap_inference_gpu_v1._iso(v_time)); END IF;
        END IF;

    ELSIF p_action = 'fence_activate_membership' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY['backend_registry_id','gpu_resource_id','membership_epoch']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=aap_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        v_next:=aap_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807);
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_membership.membership_epoch<>v_next OR v_membership.state NOT IN ('pending','active') THEN v_result:=pg_catalog.jsonb_build_object('status','membership_changed');
        ELSE SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_result:=pg_catalog.jsonb_build_object('status','fence_missing');
            ELSIF v_fence.runtime_epoch_high_water=9223372036854775807 THEN v_result:=pg_catalog.jsonb_build_object('status','high_water_exhausted');
            ELSE UPDATE public.gpu_backend_fences f SET runtime_epoch_high_water=f.runtime_epoch_high_water+1,updated_at=v_now WHERE f.backend_registry_id=v_id RETURNING f.runtime_epoch_high_water INTO v_next;
                IF v_membership.state='pending' THEN UPDATE public.gpu_backend_memberships m SET state='active',updated_at=v_now WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource; END IF;
                v_result:=pg_catalog.jsonb_build_object('status','activated','runtime_epoch_high_water',v_next); END IF;
        END IF;

    ELSIF p_action = 'fence_read' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY['backend_registry_id']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_registry_id');
        v_result:=pg_catalog.jsonb_build_object('fence',aap_inference_gpu_v1._fence_read_doc(v_id));

    ELSIF p_action = 'rollout_read' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY['resource_id']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id');
        v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));

    ELSIF p_action = 'rollout_read_all' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY[]::text[]) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(aap_inference_gpu_v1._rollout_snapshot(r.gpu_resource_id) ORDER BY r.gpu_resource_id),'[]'::jsonb)
          INTO v_memberships FROM public.gpu_arbiter_rollouts r;
        v_result:=pg_catalog.jsonb_build_object('snapshots',v_memberships);

    ELSIF p_action = 'rollout_boundary' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY[]::text[]) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        SELECT EXISTS(SELECT 1 FROM public.gpu_arbiter_rollouts r WHERE r.state <> 'off') INTO v_wait;
        v_result:=pg_catalog.jsonb_build_object('active',v_wait);

    ELSIF p_action = 'rollout_begin' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY['resource_id','target_mode','candidate_transition_id']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id'); v_target:=aap_inference_gpu_v1._string(v_args->'target_mode',16);
        v_transition:=aap_inference_gpu_v1._uuid(v_args->'candidate_transition_id');
        IF v_target NOT IN ('off','observe','enforce') THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap:gpu-resource:'||v_resource,0));
        SELECT * INTO v_rollout FROM public.gpu_arbiter_rollouts r WHERE r.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND THEN
            IF v_target='enforce' THEN
                INSERT INTO public.gpu_arbiter_rollouts(gpu_resource_id,state,effective_mode,target_mode,revision,created_at,updated_at)
                VALUES(v_resource,'off','off','off',1,v_now,v_now);
                UPDATE public.gpu_arbiter_rollouts SET state='promoting',target_mode='enforce',transition_id=v_transition,transition_started_at=v_now,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            ELSE
                INSERT INTO public.gpu_arbiter_rollouts(gpu_resource_id,state,effective_mode,target_mode,revision,created_at,updated_at)
                VALUES(v_resource,'off','off',v_target,1,v_now,v_now);
            END IF;
            v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_rollout.state IN ('promoting','demoting') THEN
            IF v_rollout.target_mode=v_target THEN v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
            ELSE v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_in_progress'); END IF;
        ELSIF v_target='enforce' AND v_rollout.state='enforcing' THEN
            v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_target IN ('off','observe') AND v_rollout.state='off' THEN
            IF v_rollout.target_mode<>v_target THEN UPDATE public.gpu_arbiter_rollouts SET target_mode=v_target,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource; END IF;
            v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_transition=v_rollout.last_transition_id THEN
            v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_id_reused');
        ELSIF v_target='enforce' AND v_rollout.effective_mode='off' THEN
            UPDATE public.gpu_arbiter_rollouts SET state='promoting',target_mode='enforce',transition_id=v_transition,transition_started_at=v_now,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_target IN ('off','observe') AND v_rollout.effective_mode='enforce' THEN
            UPDATE public.gpu_arbiter_rollouts SET state='demoting',target_mode=v_target,transition_id=v_transition,transition_started_at=v_now,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSE v_result:=pg_catalog.jsonb_build_object('outcome','unavailable'); END IF;

    ELSIF p_action = 'rollout_complete' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY['resource_id','transition_id']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id'); v_transition:=aap_inference_gpu_v1._uuid(v_args->'transition_id');
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap:gpu-resource:'||v_resource,0));
        SELECT * INTO v_rollout FROM public.gpu_arbiter_rollouts r WHERE r.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND THEN v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_missing');
        ELSIF v_rollout.transition_id IS NULL AND v_rollout.last_transition_id=v_transition THEN v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_rollout.transition_id<>v_transition THEN v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_changed');
        ELSIF v_rollout.state='promoting' THEN
            UPDATE public.gpu_arbiter_rollouts SET state='enforcing',effective_mode='enforce',transition_id=NULL,transition_started_at=NULL,last_transition_id=v_transition,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_rollout.state='demoting' THEN
            UPDATE public.gpu_arbiter_rollouts SET state='off',effective_mode='off',transition_id=NULL,transition_started_at=NULL,last_transition_id=v_transition,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSE v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_not_completable'); END IF;

    ELSIF p_action = 'rollout_block' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args, ARRAY['resource_id','transition_id','reason']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id'); v_transition:=aap_inference_gpu_v1._uuid(v_args->'transition_id'); v_reason:=aap_inference_gpu_v1._string(v_args->'reason',256);
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap:gpu-resource:'||v_resource,0));
        SELECT * INTO v_rollout FROM public.gpu_arbiter_rollouts r WHERE r.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_rollout.transition_id<>v_transition THEN v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_changed');
        ELSIF v_rollout.state='blocked' AND v_rollout.blocker_reason=v_reason THEN v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSE UPDATE public.gpu_arbiter_rollouts SET state='blocked',blocker_reason=v_reason,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',aap_inference_gpu_v1._rollout_snapshot(v_resource)); END IF;

    ELSIF p_action IN ('runtime_subject','cold_runtime_subject','idle_eviction_subject','busy_eviction_subject') THEN
        IF p_action='runtime_subject' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='cold_runtime_subject' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id','expected_challenge']) OR (v_args->'expected_challenge'<>'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'expected_challenge')<>'string') THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id','expected_generation','challenge']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=aap_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        IF p_action IN ('idle_eviction_subject','busy_eviction_subject') THEN
            v_expected:=v_args->'expected_generation';
            IF pg_catalog.jsonb_typeof(v_expected)<>'string' OR v_expected#>>'{}' !~ '^[1-9][0-9]{0,18}$' OR (v_expected#>>'{}')::numeric>9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            v_challenge:=aap_inference_gpu_v1._string(v_args->'challenge',64);
            IF v_challenge !~ '^[0-9a-f]{64}$' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSE v_expected:=v_args->'expected_challenge'; IF v_expected<>'null'::jsonb THEN v_challenge:=aap_inference_gpu_v1._string(v_expected,64); IF v_challenge !~ '^[0-9a-f]{64}$' THEN PERFORM aap_inference_gpu_v1._fail(); END IF; END IF; END IF;
        IF NOT aap_inference_gpu_v1._lock_resource(v_resource,true) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_membership.state<>'active' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','membership_not_active');
        ELSE
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            IF p_action IN ('runtime_subject','cold_runtime_subject') THEN
                BEGIN
                    SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
                EXCEPTION WHEN lock_not_available THEN
                    RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
                END;
                IF v_registry.id IS NOT NULL AND v_registry.extra_params ? 'plugin'
                   AND (v_registry.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'active'
                        OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_registry.gpu_resource_id) THEN
                    RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','plugin_not_active'));
                END IF;
            ELSE
                SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
            END IF;
            IF NOT FOUND OR v_registry.id IS NULL OR v_fence.backend_registry_id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_missing');
            ELSE
                v_health:=v_registry.health_meta; v_probe:=v_health->'gpu_arbiter_probe'; v_residency:=v_health->'residency'; v_now:=pg_catalog.transaction_timestamp();
                v_reason:=NULL;
                IF v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority OR aap_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency THEN v_reason:='registry_claim_mismatch';
                ELSIF v_fence.runtime_epoch_high_water<=v_membership.runtime_epoch_baseline THEN v_reason:='active_runtime_epoch_invalid';
                ELSIF v_fence.token_expiry_high_water IS NULL THEN v_reason:='token_horizon_missing';
                ELSIF v_registry.last_checked_at IS NULL OR v_registry.last_checked_at>v_now OR v_registry.last_checked_at<v_now-interval '5 minutes' THEN v_reason:='probe_expired';
                ELSIF pg_catalog.jsonb_typeof(v_probe)<>'object' OR pg_catalog.jsonb_typeof(v_residency)<>'object' THEN v_reason:='health_unavailable';
                ELSIF v_probe->>'backend_registry_id'<>v_id::text OR v_probe->>'gpu_resource_id'<>v_resource OR v_probe->>'membership_epoch'<>v_membership.membership_epoch::text OR v_probe->>'membership_state'<>'active' OR v_probe->>'observed_at' IS NULL THEN v_reason:='probe_membership_mismatch';
                ELSIF (v_registry.last_checked_at IS DISTINCT FROM (v_probe->>'observed_at')::timestamptz) THEN v_reason:='probe_registry_clock_mismatch';
                ELSIF v_residency->>'evictable'='true' AND (v_residency->>'generation' IS NULL OR v_residency->'identity' IS NULL OR v_residency->'identity'='null'::jsonb OR v_residency->>'lifecycle_gate'<>'enforce') THEN v_reason:='residency_evictable_inconsistent';
                ELSIF v_residency->>'boot_id' IS NULL OR v_residency->>'control_epoch' IS NULL OR (v_residency->>'lifecycle_gate'<>'enforce' AND p_action IN ('idle_eviction_subject','busy_eviction_subject')) THEN v_reason:='runtime_not_ready';
                ELSIF p_action='runtime_subject' AND (v_residency->>'lifecycle_gate'<>'enforce' OR v_residency->>'state'<>'resident' OR v_residency->>'gpu_loaded'<>'true' OR v_residency->>'draining'<>'false' OR v_residency->>'generation' IS NULL) THEN v_reason:='resident_runtime_not_ready';
                ELSIF p_action='runtime_subject' AND (v_residency->>'generation')::bigint>v_fence.generation_high_water THEN v_reason:='residency_generation_ahead';
                ELSIF p_action='runtime_subject' AND (v_residency->>'control_epoch')::bigint<>v_fence.control_epoch_high_water THEN v_reason:='residency_control_epoch_mismatch';
                ELSIF p_action='cold_runtime_subject' AND (v_residency->>'lifecycle_gate'<>'enforce' OR v_residency->>'gpu_loaded'<>'false' OR v_residency->>'state' NOT IN ('unloaded','resident') OR v_residency->>'draining'<>'false' OR v_residency->>'evictable'<>'false' OR COALESCE((v_residency->>'active_requests')::bigint,1)<>0 OR COALESCE((v_residency->>'builders')::bigint,1)<>0 OR COALESCE((v_residency->>'borrowers')::bigint,1)<>0) THEN v_reason:='cold_runtime_not_ready';
                ELSIF p_action='cold_runtime_subject' AND (v_residency->>'generation' IS NOT NULL AND (v_residency->>'generation')::bigint>v_fence.generation_high_water) THEN v_reason:='residency_generation_ahead';
                ELSIF p_action='cold_runtime_subject' AND (v_residency->>'control_epoch')::bigint<>v_fence.control_epoch_high_water THEN v_reason:='residency_control_epoch_mismatch';
                ELSIF p_action IN ('idle_eviction_subject','busy_eviction_subject') AND v_probe->>'challenge' IS DISTINCT FROM v_challenge THEN v_reason:=CASE WHEN p_action='idle_eviction_subject' THEN 'idle_eviction_challenge_mismatch' ELSE 'busy_eviction_challenge_mismatch' END;
                ELSIF p_action IN ('idle_eviction_subject','busy_eviction_subject') AND v_residency->>'generation' IS NOT NULL AND v_residency->>'generation' IS DISTINCT FROM v_expected#>>'{}' THEN v_reason:=CASE WHEN p_action='idle_eviction_subject' THEN 'idle_eviction_generation_mismatch' ELSE 'busy_eviction_generation_mismatch' END;
                ELSIF p_action IN ('idle_eviction_subject','busy_eviction_subject') AND (v_residency->>'state'<>'resident' OR v_residency->>'gpu_loaded'<>'true' OR v_residency->>'evictable'<>'true' OR (v_residency->>'control_epoch')::bigint<>v_fence.control_epoch_high_water OR (v_residency->>'generation')::bigint>v_fence.generation_high_water OR (p_action='idle_eviction_subject' AND (COALESCE((v_residency->>'active_requests')::bigint,1)<>0 OR COALESCE((v_residency->>'builders')::bigint,1)<>0 OR COALESCE((v_residency->>'borrowers')::bigint,1)<>0))) THEN v_reason:=CASE WHEN p_action='idle_eviction_subject' THEN 'idle_eviction_runtime_not_ready' ELSE 'busy_eviction_runtime_not_ready' END;
                ELSIF p_action='cold_runtime_subject' AND v_expected<>'null'::jsonb AND v_probe->>'challenge' IS DISTINCT FROM v_challenge THEN v_reason:='cold_runtime_challenge_mismatch';
                END IF;
                IF v_reason IS NOT NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason',v_reason);
                ELSE
                    v_budget:=v_membership.vram_budget_mb; v_priority:=v_membership.eviction_priority; v_concurrency:=v_membership.max_concurrency;
                    v_boot:=v_residency->>'boot_id'; v_control:=v_fence.control_epoch_high_water; v_runtime:=v_fence.runtime_epoch_high_water;
                    SELECT COALESCE(pg_catalog.jsonb_agg(k ORDER BY k),'[]'::jsonb) INTO v_pools FROM pg_catalog.jsonb_object_keys(v_residency->'pools') AS p(k);
                    IF p_action='runtime_subject' THEN
                        v_subject:=pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_budget,'eviction_priority',v_priority,'max_concurrency',v_concurrency,'boot_id',v_boot,'generation',v_residency->>'generation','control_epoch',v_residency->>'control_epoch','runtime_epoch',v_runtime::text,'db_now',aap_inference_gpu_v1._iso(v_now));
                    ELSIF p_action='cold_runtime_subject' THEN
                        v_subject:=pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_budget,'eviction_priority',v_priority,'max_concurrency',v_concurrency,'boot_id',v_boot,'observed_generation',v_residency->>'generation','generation_high_water',v_fence.generation_high_water,'control_epoch',v_residency->>'control_epoch','runtime_epoch',v_runtime::text,'db_now',aap_inference_gpu_v1._iso(v_now));
                    ELSE
                        v_subject:=pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_budget,'eviction_priority',v_priority,'max_concurrency',v_concurrency,'boot_id',v_boot,'generation',v_residency->>'generation','generation_high_water',v_fence.generation_high_water,'pool_ids',v_pools,'control_epoch',v_residency->>'control_epoch','runtime_epoch',v_runtime::text,'challenge',v_challenge,'require_idle',p_action='idle_eviction_subject','db_now',aap_inference_gpu_v1._iso(v_now));
                    END IF;
                    v_proof:=pg_catalog.jsonb_build_object('membership',aap_inference_gpu_v1._membership_doc(v_id,v_resource),'fence',aap_inference_gpu_v1._fence_doc(v_id),'registry',aap_inference_gpu_v1._registry_doc(v_id),'db_now',aap_inference_gpu_v1._iso(v_now));
                    v_result:=pg_catalog.jsonb_build_object('status','ready','subject',v_subject,'proof',v_proof);
                END IF;
            END IF;
        END IF;

    ELSIF p_action IN ('proof_domain','reconcile_resource','repair_resource','proof_reset_from_health') THEN
        IF p_action='proof_domain' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','wait_for_lock']) OR pg_catalog.jsonb_typeof(v_args->'wait_for_lock')<>'boolean' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='reconcile_resource' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','allocatable_mb']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            PERFORM aap_inference_gpu_v1._int(v_args->'allocatable_mb',1,2147483647);
        ELSIF p_action='repair_resource' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','allocatable_mb','force_proof_reset','readiness_blocker']) OR pg_catalog.jsonb_typeof(v_args->'force_proof_reset')<>'boolean' OR (v_args->'readiness_blocker'<>'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'readiness_blocker')<>'string') THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            PERFORM aap_inference_gpu_v1._int(v_args->'allocatable_mb',1,2147483647);
            IF v_args->'readiness_blocker'<>'null'::jsonb THEN PERFORM aap_inference_gpu_v1._string(v_args->'readiness_blocker',256); END IF;
        ELSE
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','expected_backend_memberships','wait_for_lock']) OR pg_catalog.jsonb_typeof(v_args->'expected_backend_memberships')<>'array' OR pg_catalog.jsonb_typeof(v_args->'wait_for_lock')<>'boolean' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            v_previous_id:=NULL;
            FOR v_expected_item IN SELECT value FROM pg_catalog.jsonb_array_elements(v_args->'expected_backend_memberships') LOOP
                IF NOT aap_inference_gpu_v1._exact_keys(v_expected_item,ARRAY['backend_id','membership_epoch','state']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
                v_id:=aap_inference_gpu_v1._uuid(v_expected_item->'backend_id');
                IF v_previous_id IS NOT NULL AND v_id<=v_previous_id THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
                v_previous_id:=v_id;
                v_text:=aap_inference_gpu_v1._string(v_expected_item->'membership_epoch',19);
                IF v_text !~ '^[1-9][0-9]{0,18}$' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
                IF v_text::numeric>9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
                v_state:=aap_inference_gpu_v1._string(v_expected_item->'state',16);
                IF v_state NOT IN ('pending','active','retiring') THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            END LOOP;
        END IF;
        v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id');
        v_wait:=CASE WHEN p_action='proof_domain' OR p_action='proof_reset_from_health' THEN (v_args->>'wait_for_lock')::boolean ELSE true END;
        v_result:=aap_inference_gpu_v1._domain(v_resource,true,v_wait);
        IF p_action='proof_reset_from_health' AND v_result->>'status'='ok' THEN
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('backend_id',m.backend_registry_id,'membership_epoch',m.membership_epoch::text,'state',m.state) ORDER BY m.backend_registry_id),'[]'::jsonb)
              INTO v_memberships FROM public.gpu_backend_memberships m WHERE m.gpu_resource_id=v_resource;
            IF v_memberships IS DISTINCT FROM v_args->'expected_backend_memberships' THEN v_result:=pg_catalog.jsonb_set(v_result,'{status}','"domain_changed"'::jsonb); ELSE v_result:=pg_catalog.jsonb_set(v_result,'{status}','"matched"'::jsonb); END IF;
        END IF;

    ELSIF p_action = 'resource_runtime_observation' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id');
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('backend_id',m.backend_registry_id,'membership_epoch',m.membership_epoch,'state',m.state) ORDER BY m.backend_registry_id),'[]'::jsonb)
          INTO v_memberships FROM public.gpu_backend_memberships m WHERE m.gpu_resource_id=v_resource;
        v_result:=pg_catalog.jsonb_build_object('status','ok','memberships',v_memberships);

    ELSIF p_action = 'membership_candidates' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['gpu_resource_id','states']) OR pg_catalog.jsonb_typeof(v_args->'states')<>'array' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_resource:=aap_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        IF v_args->'states' NOT IN ('["pending"]'::jsonb,'["pending","active"]'::jsonb,'["retiring"]'::jsonb) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('backend_id',m.backend_registry_id,'membership_epoch',m.membership_epoch,'state',m.state) ORDER BY m.backend_registry_id),'[]'::jsonb)
          INTO v_memberships FROM public.gpu_backend_memberships m WHERE m.gpu_resource_id=v_resource AND m.state=ANY(ARRAY(SELECT jsonb_array_elements_text(v_args->'states')));
        v_result:=pg_catalog.jsonb_build_object('candidates',v_memberships);

    ELSIF p_action = 'membership_max_concurrency' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_registry_id');
        SELECT m.max_concurrency INTO v_concurrency FROM public.gpu_backend_memberships m
         WHERE m.backend_registry_id=v_id AND m.state IN ('pending','active');
        v_result:=pg_catalog.jsonb_build_object('max_concurrency',v_concurrency);

    ELSIF p_action = 'shadow_snapshot' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','backend_id'])
           OR (v_args->'resource_id'<>'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'resource_id')<>'string')
           OR (v_args->'backend_id'<>'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'backend_id')<>'string')
           OR (v_args->'resource_id'<>'null'::jsonb AND v_args->'backend_id'<>'null'::jsonb) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        IF v_args->'resource_id'='null'::jsonb THEN v_resource:=NULL; ELSE v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id'); END IF;
        IF v_args->'backend_id'='null'::jsonb THEN v_id:=NULL; ELSE v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_id'); END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('backend_id',r.id,'gpu_resource_id',r.gpu_resource_id,'vram_budget_mb',r.vram_budget_mb,'eviction_priority',r.eviction_priority,'extra_params',r.extra_params,'state',r.state,'last_checked_at',aap_inference_gpu_v1._iso(r.last_checked_at),'health_meta',r.health_meta) ORDER BY r.id),'[]'::jsonb)
          INTO v_memberships FROM public.ml_backend_registry r WHERE (v_resource IS NULL OR r.gpu_resource_id=v_resource) AND (v_id IS NULL OR r.id=v_id);
        v_result:=pg_catalog.jsonb_build_object('backends',v_memberships);

    ELSIF p_action = 'prepare_cold_generation' OR p_action='prepare_idle_eviction_generation' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','token_expires_at']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_expected:=v_args->'expected_subject'; v_time:=aap_inference_gpu_v1._timestamp(v_args->'token_expires_at');
        IF p_action='prepare_cold_generation' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','observed_generation','generation_high_water','control_epoch','runtime_epoch','db_now']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT aap_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','generation','generation_high_water','pool_ids','control_epoch','runtime_epoch','challenge','require_idle','db_now']) OR pg_catalog.jsonb_typeof(v_expected->'pool_ids')<>'array' OR pg_catalog.jsonb_typeof(v_expected->'require_idle')<>'boolean' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_expected->'backend_registry_id');
        v_resource:=aap_inference_gpu_v1._resource(v_expected->'gpu_resource_id');
        v_next:=aap_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807);
        PERFORM aap_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647);
        PERFORM aap_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647);
        PERFORM aap_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000);
        v_boot:=aap_inference_gpu_v1._string(v_expected->'boot_id',128);
        PERFORM aap_inference_gpu_v1._timestamp(v_expected->'db_now');
        IF p_action='prepare_cold_generation' THEN
            PERFORM aap_inference_gpu_v1._int(v_expected->'generation_high_water',0,9223372036854775807);
            v_control:=aap_inference_gpu_v1._string(v_expected->'control_epoch',19)::bigint;
            v_runtime:=aap_inference_gpu_v1._string(v_expected->'runtime_epoch',19)::bigint;
            IF v_expected->>'control_epoch' !~ '^[1-9][0-9]{0,18}$' OR v_expected->>'runtime_epoch' !~ '^[1-9][0-9]{0,18}$' OR v_control>9223372036854775807 OR v_runtime>9223372036854775807 OR (v_expected->'observed_generation'<>'null'::jsonb AND (pg_catalog.jsonb_typeof(v_expected->'observed_generation')<>'string' OR v_expected->>'observed_generation' !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'observed_generation')::numeric>9223372036854775807)) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSE
            PERFORM aap_inference_gpu_v1._int(v_expected->'generation_high_water',0,9223372036854775807);
            IF aap_inference_gpu_v1._string(v_expected->'generation',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'generation')::numeric>9223372036854775807 OR aap_inference_gpu_v1._string(v_expected->'control_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'control_epoch')::numeric>9223372036854775807 OR aap_inference_gpu_v1._string(v_expected->'runtime_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'runtime_epoch')::numeric>9223372036854775807 OR aap_inference_gpu_v1._string(v_expected->'challenge',64) !~ '^[0-9a-f]{64}$' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            IF EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value) WHERE pg_catalog.jsonb_typeof(x.value)<>'string' OR x.value#>>'{}'='' OR x.value#>>'{}'<>pg_catalog.btrim(x.value#>>'{}')) OR v_expected->'pool_ids' IS DISTINCT FROM (SELECT COALESCE(pg_catalog.jsonb_agg(x.value#>>'{}' ORDER BY x.value#>>'{}'),'[]'::jsonb) FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value)) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_expected->'backend_registry_id'); v_resource:=aap_inference_gpu_v1._resource(v_expected->'gpu_resource_id');
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        IF p_action='prepare_cold_generation' THEN
            BEGIN
                SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
            EXCEPTION WHEN lock_not_available THEN
                RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
            END;
            IF v_registry.id IS NOT NULL AND v_registry.extra_params ? 'plugin'
               AND (v_registry.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'active'
                    OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_registry.gpu_resource_id) THEN
                RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','plugin_not_active'));
            END IF;
        ELSE
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
        END IF;
        v_now:=pg_catalog.transaction_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_missing');
        ELSE
            v_health:=v_registry.health_meta; v_residency:=v_health->'residency';
            IF v_time<=v_now OR v_fence.generation_high_water=9223372036854775807 OR v_fence.generation_high_water<>aap_inference_gpu_v1._int(v_expected->'generation_high_water',0,9223372036854775807) OR v_fence.control_epoch_high_water::text<>v_expected->>'control_epoch' OR v_fence.runtime_epoch_high_water::text<>v_expected->>'runtime_epoch' OR v_membership.membership_epoch<>aap_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807) OR v_membership.vram_budget_mb<>aap_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647) OR v_membership.eviction_priority<>aap_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647) OR v_membership.max_concurrency<>aap_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000) OR v_residency->>'boot_id' IS DISTINCT FROM v_expected->>'boot_id' OR v_registry.state<>'connected' OR v_registry.gpu_resource_id<>v_resource OR v_registry.vram_budget_mb<>v_membership.vram_budget_mb OR v_registry.eviction_priority<>v_membership.eviction_priority OR aap_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency OR pg_catalog.jsonb_typeof(v_residency)<>'object' OR v_registry.last_checked_at IS NULL OR v_registry.last_checked_at>v_now OR v_registry.last_checked_at<v_now-interval '3 minutes' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
            ELSIF p_action='prepare_cold_generation' AND v_expected->'observed_generation' IS DISTINCT FROM COALESCE(v_residency->'generation','null'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
            ELSIF p_action='prepare_idle_eviction_generation' AND (v_expected->>'generation' IS DISTINCT FROM v_residency->>'generation' OR v_expected->'pool_ids' IS DISTINCT FROM (SELECT COALESCE(pg_catalog.jsonb_agg(k ORDER BY k),'[]'::jsonb) FROM pg_catalog.jsonb_object_keys(v_residency->'pools') p(k)) OR v_expected->>'control_epoch' IS DISTINCT FROM v_residency->>'control_epoch') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
            ELSE
                v_generation:=v_fence.generation_high_water+1;
                UPDATE public.gpu_backend_fences f SET generation_high_water=v_generation,token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE f.backend_registry_id=v_id;
                IF p_action='prepare_cold_generation' THEN
                    v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_membership.vram_budget_mb,'eviction_priority',v_membership.eviction_priority,'max_concurrency',v_membership.max_concurrency,'boot_id',v_residency->>'boot_id','observed_generation',v_residency->>'generation','generation',v_generation::text,'control_epoch',v_residency->>'control_epoch','runtime_epoch',v_fence.runtime_epoch_high_water::text,'token_expires_at',aap_inference_gpu_v1._iso(v_time),'db_now',aap_inference_gpu_v1._iso(v_now)));
                ELSE
                    v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_membership.vram_budget_mb,'eviction_priority',v_membership.eviction_priority,'max_concurrency',v_membership.max_concurrency,'boot_id',v_residency->>'boot_id','source_generation',v_residency->>'generation','generation',v_generation::text,'pool_ids',v_expected->'pool_ids','control_epoch',v_residency->>'control_epoch','runtime_epoch',v_fence.runtime_epoch_high_water::text,'token_expires_at',aap_inference_gpu_v1._iso(v_time),'require_idle',v_expected->'require_idle','db_now',aap_inference_gpu_v1._iso(v_now)));
                END IF;
            END IF;
        END IF;

    ELSIF p_action='record_runtime_token_expiry' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','token_expires_at']) OR NOT aap_inference_gpu_v1._exact_keys(v_args->'expected_subject',ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','generation','control_epoch','runtime_epoch','db_now']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_expected:=v_args->'expected_subject'; v_id:=aap_inference_gpu_v1._uuid(v_expected->'backend_registry_id'); v_resource:=aap_inference_gpu_v1._resource(v_expected->'gpu_resource_id');
        v_next:=aap_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807); v_budget:=aap_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647); v_priority:=aap_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647); v_concurrency:=aap_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000); v_boot:=aap_inference_gpu_v1._string(v_expected->'boot_id',128); v_time:=aap_inference_gpu_v1._timestamp(v_args->'token_expires_at'); PERFORM aap_inference_gpu_v1._timestamp(v_expected->'db_now');
        v_text:=aap_inference_gpu_v1._string(v_expected->'generation',19); IF v_text !~ '^[1-9][0-9]{0,18}$' THEN PERFORM aap_inference_gpu_v1._fail(); END IF; IF v_text::numeric>9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF; v_generation:=v_text::bigint;
        v_text:=aap_inference_gpu_v1._string(v_expected->'control_epoch',19); IF v_text !~ '^[1-9][0-9]{0,18}$' THEN PERFORM aap_inference_gpu_v1._fail(); END IF; IF v_text::numeric>9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF; v_control:=v_text::bigint;
        v_text:=aap_inference_gpu_v1._string(v_expected->'runtime_epoch',19); IF v_text !~ '^[1-9][0-9]{0,18}$' THEN PERFORM aap_inference_gpu_v1._fail(); END IF; IF v_text::numeric>9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF; v_runtime:=v_text::bigint;
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        BEGIN
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available THEN
            RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
        END;
        IF v_registry.id IS NOT NULL AND v_registry.extra_params ? 'plugin'
           AND (v_registry.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'active'
                OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_registry.gpu_resource_id) THEN
            RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','plugin_not_active'));
        END IF;
        v_residency:=v_registry.health_meta->'residency'; v_now:=pg_catalog.transaction_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL OR v_time<=v_now OR v_membership.membership_epoch<>v_next OR v_membership.vram_budget_mb<>v_budget OR v_membership.eviction_priority<>v_priority OR v_membership.max_concurrency<>v_concurrency OR v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_budget OR v_registry.eviction_priority IS DISTINCT FROM v_priority OR aap_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_concurrency OR v_fence.generation_high_water<v_generation OR v_fence.control_epoch_high_water<>v_control OR v_fence.runtime_epoch_high_water<>v_runtime OR NOT aap_inference_gpu_v1._residency_shape(v_residency) OR v_residency->>'state'<>'resident' OR v_residency->>'gpu_loaded'<>'true' OR v_residency->>'draining'<>'false' OR v_residency->>'lifecycle_gate'<>'enforce' OR v_residency->>'generation' IS DISTINCT FROM v_expected->>'generation' OR v_residency->>'control_epoch' IS DISTINCT FROM v_expected->>'control_epoch' OR v_residency->>'boot_id' IS DISTINCT FROM v_boot OR v_residency #>> '{identity,backend_registry_id}' IS DISTINCT FROM v_id::text OR v_residency #>> '{identity,gpu_resource_id}' IS DISTINCT FROM v_resource OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident'='null'::jsonb) OR NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident'='true'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
        ELSE UPDATE public.gpu_backend_fences f SET token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE f.backend_registry_id=v_id;
            v_result:=pg_catalog.jsonb_build_object('status','recorded','token_expires_at',aap_inference_gpu_v1._iso(v_time)); END IF;

    ELSIF p_action='prepare_eviction_cancel_generation' THEN
        IF v_args->>'phase'='recover' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['phase','backend_registry_id','gpu_resource_id','owner_id']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=aap_inference_gpu_v1._resource(v_args->'gpu_resource_id'); v_reason:=aap_inference_gpu_v1._string(v_args->'owner_id',256);
            PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
            SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
            SELECT * INTO v_cancel FROM public.gpu_backend_cancel_intents c WHERE c.backend_registry_id=v_id FOR UPDATE;
            IF v_cancel.backend_registry_id IS NULL OR v_cancel.gpu_resource_id IS DISTINCT FROM v_resource OR v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_membership.membership_epoch<>v_cancel.membership_epoch OR v_fence.backend_registry_id IS NULL OR v_fence.generation_high_water<>v_cancel.generation OR v_fence.control_epoch_high_water<>v_cancel.control_epoch OR v_fence.runtime_epoch_high_water<>v_cancel.runtime_epoch OR v_fence.token_expiry_high_water<v_cancel.token_expires_at OR v_registry.id IS NULL OR v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority OR aap_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency OR v_cancel.token_expires_at<=v_now THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_missing');
            ELSIF v_cancel.owner_id IS DISTINCT FROM v_reason THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_identity_changed');
            ELSIF v_cancel.subject_fingerprint IS DISTINCT FROM pg_catalog.encode(public.digest(pg_catalog.convert_to(pg_catalog.jsonb_build_object('backend_registry_id',v_cancel.backend_registry_id,'gpu_resource_id',v_cancel.gpu_resource_id,'membership_epoch',v_cancel.membership_epoch,'boot_id',v_cancel.boot_id,'control_epoch',v_cancel.control_epoch,'runtime_epoch',v_cancel.runtime_epoch,'source_generation',v_cancel.source_generation,'drain_generation',v_cancel.drain_generation,'generation',v_cancel.generation,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',aap_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',aap_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'pool_ids',v_cancel.pool_ids,'backend_url',v_registry.url,'backend_auth_method',v_registry.auth_method,'backend_auth_token',v_registry.auth_token)::text,'UTF8'),'sha256'),'hex') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_source_changed');
            ELSE v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_cancel.membership_epoch,'budget_mb',v_membership.vram_budget_mb,'eviction_priority',v_membership.eviction_priority,'max_concurrency',v_membership.max_concurrency,'boot_id',v_cancel.boot_id,'source_generation',v_cancel.source_generation::text,'drain_generation',v_cancel.drain_generation::text,'generation',v_cancel.generation::text,'pool_ids',v_cancel.pool_ids,'control_epoch',v_cancel.control_epoch::text,'runtime_epoch',v_cancel.runtime_epoch::text,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',aap_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',aap_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'idempotent',true,'db_now',aap_inference_gpu_v1._iso(v_now))); END IF;
        ELSIF v_args->>'phase'='prepare' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['phase','expected_subject','owner_id','owner_hard_deadline_ms','token_expires_at']) OR NOT aap_inference_gpu_v1._exact_keys(v_args->'expected_subject',ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','source_generation','generation','pool_ids','control_epoch','runtime_epoch','token_expires_at','require_idle','db_now']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            v_expected:=v_args->'expected_subject'; v_id:=aap_inference_gpu_v1._uuid(v_expected->'backend_registry_id'); v_resource:=aap_inference_gpu_v1._resource(v_expected->'gpu_resource_id'); v_reason:=aap_inference_gpu_v1._string(v_args->'owner_id',256); v_next:=aap_inference_gpu_v1._int(v_args->'owner_hard_deadline_ms',1,253402300799999); v_time:=aap_inference_gpu_v1._timestamp(v_args->'token_expires_at'); v_token_expiry:=aap_inference_gpu_v1._timestamp(v_expected->'token_expires_at');
            IF pg_catalog.jsonb_typeof(v_expected->'require_idle')<>'boolean' OR pg_catalog.jsonb_typeof(v_expected->'pool_ids')<>'array' OR pg_catalog.jsonb_array_length(v_expected->'pool_ids')=0 THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            PERFORM aap_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807);
            PERFORM aap_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647);
            PERFORM aap_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647);
            PERFORM aap_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000);
            PERFORM aap_inference_gpu_v1._string(v_expected->'boot_id',128);
            PERFORM aap_inference_gpu_v1._timestamp(v_expected->'db_now');
            IF aap_inference_gpu_v1._string(v_expected->'source_generation',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'source_generation')::numeric>9223372036854775807 OR aap_inference_gpu_v1._string(v_expected->'generation',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'generation')::numeric>9223372036854775807 OR aap_inference_gpu_v1._string(v_expected->'control_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'control_epoch')::numeric>9223372036854775807 OR aap_inference_gpu_v1._string(v_expected->'runtime_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'runtime_epoch')::numeric>9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            IF EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value) WHERE pg_catalog.jsonb_typeof(x.value)<>'string' OR x.value#>>'{}'='' OR x.value#>>'{}'<>pg_catalog.btrim(x.value#>>'{}')) OR v_expected->'pool_ids' IS DISTINCT FROM (SELECT COALESCE(pg_catalog.jsonb_agg(x.value#>>'{}' ORDER BY x.value#>>'{}'),'[]'::jsonb) FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value)) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            IF (v_expected->>'require_idle')::boolean THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','busy_cancel_required');
            ELSE
                PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
                SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
                SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
                SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
                SELECT * INTO v_cancel FROM public.gpu_backend_cancel_intents c WHERE c.backend_registry_id=v_id FOR UPDATE;
                v_now:=pg_catalog.transaction_timestamp();
                IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_membership.membership_epoch<>aap_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807) OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL OR v_time<=v_now OR v_next<=floor(extract(epoch from v_now)*1000)::bigint OR v_time>pg_catalog.to_timestamp(v_next::double precision/1000) OR v_fence.control_epoch_high_water::text IS DISTINCT FROM v_expected->>'control_epoch' OR v_fence.runtime_epoch_high_water::text IS DISTINCT FROM v_expected->>'runtime_epoch' OR v_membership.vram_budget_mb<>aap_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647) OR v_membership.eviction_priority<>aap_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647) OR v_membership.max_concurrency<>aap_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000) OR v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority OR aap_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency OR v_registry.health_meta->'residency'->>'boot_id' IS DISTINCT FROM v_expected->>'boot_id' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_conflict');
                ELSIF v_cancel.backend_registry_id IS NOT NULL THEN
                    IF v_cancel.gpu_resource_id IS DISTINCT FROM v_resource OR v_cancel.membership_epoch<>v_membership.membership_epoch OR v_cancel.boot_id IS DISTINCT FROM v_expected->>'boot_id' OR v_cancel.control_epoch::text IS DISTINCT FROM v_expected->>'control_epoch' OR v_cancel.runtime_epoch::text IS DISTINCT FROM v_expected->>'runtime_epoch' OR v_cancel.source_generation<>(v_expected->>'source_generation')::bigint OR v_cancel.drain_generation<>(v_expected->>'generation')::bigint OR v_cancel.owner_id IS DISTINCT FROM v_reason OR v_cancel.operation<>'evict' OR v_cancel.owner_hard_deadline_ms<>v_next OR v_cancel.drain_token_expires_at IS DISTINCT FROM v_token_expiry OR v_cancel.token_expires_at IS DISTINCT FROM v_time OR v_cancel.pool_ids IS DISTINCT FROM v_expected->'pool_ids' OR v_fence.generation_high_water<>v_cancel.generation OR v_fence.token_expiry_high_water IS NULL OR v_fence.token_expiry_high_water<v_cancel.token_expires_at OR v_cancel.subject_fingerprint IS DISTINCT FROM pg_catalog.encode(public.digest(pg_catalog.convert_to(pg_catalog.jsonb_build_object('backend_registry_id',v_cancel.backend_registry_id,'gpu_resource_id',v_cancel.gpu_resource_id,'membership_epoch',v_cancel.membership_epoch,'boot_id',v_cancel.boot_id,'control_epoch',v_cancel.control_epoch,'runtime_epoch',v_cancel.runtime_epoch,'source_generation',v_cancel.source_generation,'drain_generation',v_cancel.drain_generation,'generation',v_cancel.generation,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',aap_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',aap_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'pool_ids',v_cancel.pool_ids,'backend_url',v_registry.url,'backend_auth_method',v_registry.auth_method,'backend_auth_token',v_registry.auth_token)::text,'UTF8'),'sha256'),'hex') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_conflict');
                    ELSE v_status:='true'; END IF;
                ELSE
                    IF v_fence.generation_high_water=9223372036854775807 OR v_fence.generation_high_water<(v_expected->>'generation')::bigint OR v_token_expiry<=v_now OR v_fence.token_expiry_high_water IS DISTINCT FROM v_token_expiry THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_token_horizon_invalid');
                    ELSE
                        v_generation:=v_fence.generation_high_water+1; v_control:=v_fence.control_epoch_high_water; v_runtime:=v_fence.runtime_epoch_high_water; v_id2:=public.gen_random_uuid();
                        INSERT INTO public.gpu_backend_cancel_intents(backend_registry_id,gpu_resource_id,membership_epoch,boot_id,control_epoch,runtime_epoch,source_generation,drain_generation,generation,owner_id,operation,owner_hard_deadline_ms,drain_token_expires_at,token_expires_at,jti,pool_ids,subject_fingerprint)
                        VALUES(v_id,v_resource,v_membership.membership_epoch,v_expected->>'boot_id',v_control,v_runtime,(v_expected->>'source_generation')::bigint,(v_expected->>'generation')::bigint,v_generation,v_reason,'evict',v_next,v_token_expiry,v_time,'transition:'||v_id2::text,v_expected->'pool_ids',pg_catalog.encode(public.digest(pg_catalog.convert_to(pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'boot_id',v_expected->>'boot_id','control_epoch',v_control,'runtime_epoch',v_runtime,'source_generation',(v_expected->>'source_generation')::bigint,'drain_generation',(v_expected->>'generation')::bigint,'generation',v_generation,'owner_id',v_reason,'operation','evict','owner_hard_deadline_ms',v_next,'drain_token_expires_at',aap_inference_gpu_v1._iso(v_token_expiry),'token_expires_at',aap_inference_gpu_v1._iso(v_time),'jti','transition:'||v_id2::text,'pool_ids',v_expected->'pool_ids','backend_url',v_registry.url,'backend_auth_method',v_registry.auth_method,'backend_auth_token',v_registry.auth_token)::text,'UTF8'),'sha256'),'hex'));
                        UPDATE public.gpu_backend_fences f SET generation_high_water=v_generation,token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE f.backend_registry_id=v_id;
                        SELECT * INTO v_cancel FROM public.gpu_backend_cancel_intents c WHERE c.backend_registry_id=v_id;
                        v_status:='false';
                    END IF;
                END IF;
                IF v_result IS NULL THEN
                    v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_cancel.membership_epoch,'budget_mb',v_membership.vram_budget_mb,'eviction_priority',v_membership.eviction_priority,'max_concurrency',v_membership.max_concurrency,'boot_id',v_cancel.boot_id,'source_generation',v_cancel.source_generation::text,'drain_generation',v_cancel.drain_generation::text,'generation',v_cancel.generation::text,'pool_ids',v_cancel.pool_ids,'control_epoch',v_cancel.control_epoch::text,'runtime_epoch',v_cancel.runtime_epoch::text,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',aap_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',aap_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'idempotent',v_status='true','db_now',aap_inference_gpu_v1._iso(v_now)));
                END IF;
            END IF;
        ELSE
            PERFORM aap_inference_gpu_v1._fail();
        END IF;

    ELSIF p_action IN ('eviction_drain_health','cold_terminal_from_health','eviction_cancel_from_health','eviction_phase_from_health') THEN
        IF p_action='eviction_drain_health' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','challenge']) OR pg_catalog.jsonb_typeof(v_args->'challenge')<>'string' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='cold_terminal_from_health' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','challenge']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='eviction_phase_from_health' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','phase','challenge']) OR aap_inference_gpu_v1._string(v_args->'phase',16) NOT IN ('drain','unload') THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','ack_confirmed','challenge']) OR pg_catalog.jsonb_typeof(v_args->'ack_confirmed')<>'boolean' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        END IF;
        v_expected:=v_args->'expected_subject';
        IF p_action='cold_terminal_from_health' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','observed_generation','generation','control_epoch','runtime_epoch','token_expires_at','db_now']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='eviction_cancel_from_health' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','source_generation','drain_generation','generation','pool_ids','control_epoch','runtime_epoch','owner_id','operation','owner_hard_deadline_ms','drain_token_expires_at','token_expires_at','jti','idempotent','db_now']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT aap_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','source_generation','generation','pool_ids','control_epoch','runtime_epoch','token_expires_at','require_idle','db_now']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        END IF;
        PERFORM aap_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807);
        PERFORM aap_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647);
        PERFORM aap_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647);
        PERFORM aap_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000);
        PERFORM aap_inference_gpu_v1._string(v_expected->'boot_id',128);
        PERFORM aap_inference_gpu_v1._timestamp(v_expected->'token_expires_at');
        PERFORM aap_inference_gpu_v1._timestamp(v_expected->'db_now');
        IF aap_inference_gpu_v1._string(v_expected->'generation',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'generation')::numeric>9223372036854775807 OR aap_inference_gpu_v1._string(v_expected->'control_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'control_epoch')::numeric>9223372036854775807 OR aap_inference_gpu_v1._string(v_expected->'runtime_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'runtime_epoch')::numeric>9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        IF p_action='cold_terminal_from_health' THEN
            IF v_expected->'observed_generation'<>'null'::jsonb AND (pg_catalog.jsonb_typeof(v_expected->'observed_generation')<>'string' OR v_expected->>'observed_generation' !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'observed_generation')::numeric>9223372036854775807) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSE
            PERFORM aap_inference_gpu_v1._string(v_expected->'source_generation',19);
            IF v_expected->>'source_generation' !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'source_generation')::numeric>9223372036854775807 OR pg_catalog.jsonb_typeof(v_expected->'pool_ids')<>'array' OR pg_catalog.jsonb_array_length(v_expected->'pool_ids')=0 OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value) WHERE pg_catalog.jsonb_typeof(x.value)<>'string' OR x.value#>>'{}'='' OR x.value#>>'{}'<>pg_catalog.btrim(x.value#>>'{}')) OR v_expected->'pool_ids' IS DISTINCT FROM (SELECT COALESCE(pg_catalog.jsonb_agg(x.value#>>'{}' ORDER BY x.value#>>'{}'),'[]'::jsonb) FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value)) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            IF p_action='eviction_cancel_from_health' THEN
                PERFORM aap_inference_gpu_v1._string(v_expected->'drain_generation',19);
                PERFORM aap_inference_gpu_v1._string(v_expected->'owner_id',256);
                PERFORM aap_inference_gpu_v1._string(v_expected->'jti',256);
                PERFORM aap_inference_gpu_v1._timestamp(v_expected->'drain_token_expires_at');
                PERFORM aap_inference_gpu_v1._int(v_expected->'owner_hard_deadline_ms',1,9223372036854775807);
                IF v_expected->>'operation'<>'evict' OR pg_catalog.jsonb_typeof(v_expected->'idempotent')<>'boolean' OR v_expected->>'drain_generation' !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'drain_generation')::numeric>9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            ELSE
                IF pg_catalog.jsonb_typeof(v_expected->'require_idle')<>'boolean' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            END IF;
        END IF;
        v_expected:=v_args->'expected_subject';
        IF v_args->'challenge'='null'::jsonb THEN
            IF p_action='eviction_drain_health' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
            v_challenge:=NULL;
        ELSE
            v_challenge:=aap_inference_gpu_v1._string(v_args->'challenge',64);
            IF v_challenge !~ '^[0-9a-f]{64}$' THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_expected->'backend_registry_id'); v_resource:=aap_inference_gpu_v1._resource(v_expected->'gpu_resource_id');
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
        IF p_action='eviction_cancel_from_health' THEN
            SELECT * INTO v_cancel FROM public.gpu_backend_cancel_intents c WHERE c.backend_registry_id=v_id FOR UPDATE;
        END IF;
        v_now:=pg_catalog.transaction_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL OR v_membership.membership_epoch<>aap_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_missing');
        ELSIF v_membership.vram_budget_mb<>aap_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647) OR v_membership.eviction_priority<>aap_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647) OR v_membership.max_concurrency<>aap_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','membership_changed');
        ELSIF (p_action='cold_terminal_from_health' AND v_fence.generation_high_water<>(v_expected->>'generation')::bigint
               AND NOT EXISTS (SELECT 1 FROM aap_inference_gpu_v1.installation_terminal_intents t
                   WHERE t.backend_registry_id=v_id AND NOT t.settled
                     AND t.request->>'resource_id'=v_resource AND t.request->>'boot_id'=v_expected->>'boot_id'
                     AND t.membership_epoch=v_membership.membership_epoch
                     AND t.source_generation=v_expected->>'generation'
                     AND t.generation=v_fence.generation_high_water
                     AND t.control_epoch=v_fence.control_epoch_high_water AND t.runtime_epoch=v_fence.runtime_epoch_high_water))
           OR (p_action<>'cold_terminal_from_health' AND v_fence.generation_high_water<(v_expected->>'generation')::bigint) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','generation_changed');
        ELSIF v_fence.control_epoch_high_water::text IS DISTINCT FROM v_expected->>'control_epoch' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','control_epoch_changed');
        ELSIF v_fence.runtime_epoch_high_water::text IS DISTINCT FROM v_expected->>'runtime_epoch' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_epoch_changed');
        ELSIF v_fence.token_expiry_high_water IS NULL OR v_fence.token_expiry_high_water<aap_inference_gpu_v1._timestamp(v_expected->'token_expires_at') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','token_horizon_changed');
        ELSIF v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority OR aap_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','registry_claim_changed');
        ELSIF aap_inference_gpu_v1._timestamp(v_expected->'token_expires_at')<=v_now THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
        ELSIF p_action='eviction_cancel_from_health' AND v_cancel.backend_registry_id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_missing');
        ELSIF p_action='eviction_cancel_from_health' AND (v_cancel.gpu_resource_id IS DISTINCT FROM v_resource OR v_cancel.membership_epoch<>v_membership.membership_epoch OR v_cancel.generation<>(v_expected->>'generation')::bigint OR v_cancel.drain_generation<>(v_expected->>'drain_generation')::bigint OR v_cancel.source_generation<>(v_expected->>'source_generation')::bigint OR v_cancel.owner_id IS DISTINCT FROM v_expected->>'owner_id' OR v_cancel.operation IS DISTINCT FROM v_expected->>'operation' OR v_cancel.owner_hard_deadline_ms<>aap_inference_gpu_v1._int(v_expected->'owner_hard_deadline_ms',1,9223372036854775807) OR v_cancel.jti IS DISTINCT FROM v_expected->>'jti' OR v_cancel.pool_ids IS DISTINCT FROM v_expected->'pool_ids' OR v_cancel.drain_token_expires_at IS DISTINCT FROM aap_inference_gpu_v1._timestamp(v_expected->'drain_token_expires_at') OR v_cancel.token_expires_at IS DISTINCT FROM aap_inference_gpu_v1._timestamp(v_expected->'token_expires_at')) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_changed');
        ELSIF p_action='eviction_cancel_from_health' AND v_cancel.subject_fingerprint IS DISTINCT FROM pg_catalog.encode(public.digest(pg_catalog.convert_to(pg_catalog.jsonb_build_object('backend_registry_id',v_cancel.backend_registry_id,'gpu_resource_id',v_cancel.gpu_resource_id,'membership_epoch',v_cancel.membership_epoch,'boot_id',v_cancel.boot_id,'control_epoch',v_cancel.control_epoch,'runtime_epoch',v_cancel.runtime_epoch,'source_generation',v_cancel.source_generation,'drain_generation',v_cancel.drain_generation,'generation',v_cancel.generation,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',aap_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',aap_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'pool_ids',v_cancel.pool_ids,'backend_url',v_registry.url,'backend_auth_method',v_registry.auth_method,'backend_auth_token',v_registry.auth_token)::text,'UTF8'),'sha256'),'hex') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_source_changed');
        ELSE
            v_proof:=pg_catalog.jsonb_build_object('membership',aap_inference_gpu_v1._membership_doc(v_id,v_resource),'fence',aap_inference_gpu_v1._fence_doc(v_id),'registry',aap_inference_gpu_v1._registry_doc(v_id),'db_now',aap_inference_gpu_v1._iso(v_now));
            v_result:=pg_catalog.jsonb_build_object('status','ready','subject',v_proof);
        END IF;

    ELSIF p_action='prepare_legacy_ack' OR p_action='prepare_rollout_control' THEN
        IF p_action='prepare_legacy_ack' THEN
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','token_ttl_seconds','evidence_ttl_seconds']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','transition_id','target_gate','token_ttl_seconds','evidence_ttl_seconds']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=aap_inference_gpu_v1._resource(v_args->'gpu_resource_id'); v_next:=aap_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807); v_budget:=aap_inference_gpu_v1._int(v_args->'token_ttl_seconds',1,300);
        IF pg_catalog.jsonb_typeof(v_args->'evidence_ttl_seconds')<>'number' OR (v_args->>'evidence_ttl_seconds')::numeric<=0 OR (v_args->>'evidence_ttl_seconds')::numeric>300 THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        IF p_action='prepare_rollout_control' THEN v_transition:=aap_inference_gpu_v1._uuid(v_args->'transition_id'); v_target:=aap_inference_gpu_v1._string(v_args->'target_gate',16); IF v_target NOT IN ('legacy','enforce') THEN PERFORM aap_inference_gpu_v1._fail(); END IF; END IF;
        IF NOT aap_inference_gpu_v1._lock_resource(v_resource,false) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','gpu_resource_busy');
        ELSE
            PERFORM m.backend_registry_id FROM public.gpu_backend_memberships AS m WHERE m.gpu_resource_id=v_resource ORDER BY m.backend_registry_id FOR UPDATE OF m;
            PERFORM f.backend_registry_id FROM public.gpu_backend_fences AS f WHERE f.backend_registry_id IN (SELECT m.backend_registry_id FROM public.gpu_backend_memberships AS m WHERE m.gpu_resource_id=v_resource) ORDER BY f.backend_registry_id FOR UPDATE OF f;
            SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource;
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id;
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
            v_now:=pg_catalog.transaction_timestamp();
            IF v_membership.backend_registry_id IS NULL OR v_membership.membership_epoch<>v_next OR v_membership.state NOT IN ('pending','active') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','membership_changed');
            ELSIF v_fence.backend_registry_id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','fence_missing');
            ELSIF v_registry.id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','registry_missing');
            ELSIF p_action='prepare_rollout_control' AND v_membership.state<>'active' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','membership_not_active');
            ELSIF p_action='prepare_rollout_control' AND NOT EXISTS(SELECT 1 FROM public.gpu_arbiter_rollouts r WHERE r.gpu_resource_id=v_resource AND r.transition_id=v_transition AND ((v_target='enforce' AND r.state='promoting') OR (v_target='legacy' AND r.state='demoting'))) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','rollout_transition_changed');
            ELSIF p_action='prepare_legacy_ack' AND v_membership.state='pending' AND v_fence.runtime_epoch_high_water<>v_membership.runtime_epoch_baseline THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','pending_runtime_epoch_mismatch');
            ELSIF v_membership.state='active' AND v_fence.runtime_epoch_high_water<=v_membership.runtime_epoch_baseline THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','active_runtime_epoch_invalid');
            ELSIF v_registry.gpu_resource_id<>v_resource OR v_registry.state<>'connected' OR v_registry.vram_budget_mb<>v_membership.vram_budget_mb OR v_registry.eviction_priority<>v_membership.eviction_priority OR aap_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','registry_claim_mismatch');
            ELSE
                v_health:=v_registry.health_meta; v_residency:=v_health->'residency'; v_probe:=v_health->'gpu_arbiter_probe';
                IF pg_catalog.jsonb_typeof(v_health) IS DISTINCT FROM 'object' OR pg_catalog.jsonb_typeof(v_probe) IS DISTINCT FROM 'object' OR NOT aap_inference_gpu_v1._residency_shape(v_residency) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_schema_invalid');
                ELSIF v_probe->>'managed_lifecycle_sha256' IS NULL OR v_probe->>'managed_lifecycle_sha256' !~ '^[0-9a-f]{64}$' OR pg_catalog.jsonb_typeof(v_health#>'{capabilities,managed_lifecycle}') IS DISTINCT FROM 'object' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','managed_lifecycle_capability_mismatch');
                ELSIF v_probe->>'protocol_version' IS DISTINCT FROM '1' OR v_probe->>'challenge' IS NULL OR v_probe->>'challenge' !~ '^[0-9a-f]{64}$' OR v_probe->>'backend_registry_id' IS DISTINCT FROM v_id::text OR v_probe->>'gpu_resource_id' IS DISTINCT FROM v_resource OR v_probe->>'membership_epoch' IS DISTINCT FROM v_membership.membership_epoch::text OR v_probe->>'membership_state' IS NULL OR v_probe->>'membership_state' NOT IN ('pending','active') OR v_probe->>'membership_state' IS DISTINCT FROM v_membership.state THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_membership_mismatch');
                ELSIF v_probe->>'observed_at' IS NULL OR v_probe->>'observed_at' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}(:[0-9]{2})?)$' OR v_probe->>'probe_started_at' IS NULL OR v_probe->>'probe_started_at' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}(:[0-9]{2})?)$' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_registry_clock_mismatch');
                ELSE
                    v_observed:=(v_probe->>'observed_at')::timestamp with time zone;
                    v_probe_started:=(v_probe->>'probe_started_at')::timestamp with time zone;
                    v_trusted_empty:=v_residency->>'state' IN ('unloaded','resident') AND v_residency->'gpu_loaded'='false'::jsonb AND v_residency->'generation'='null'::jsonb AND v_residency->'evictable'='false'::jsonb AND NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident' IS DISTINCT FROM 'false'::jsonb);
                    v_enforce_intent:=FALSE; v_reset_ack:=FALSE; v_intent_matches:=FALSE;
                    IF v_registry.last_checked_at IS NULL OR v_registry.last_checked_at IS DISTINCT FROM v_observed THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_registry_clock_mismatch');
                    ELSIF v_observed>v_now THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_from_future');
                    ELSIF v_now-v_observed>make_interval(secs=>(v_args->>'evidence_ttl_seconds')::double precision) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_expired');
                    ELSIF v_residency->'identity'<>'null'::jsonb AND v_residency->'identity' IS DISTINCT FROM pg_catalog.jsonb_build_object('audience','aap-gpu-lifecycle','backend_registry_id',v_id::text,'gpu_resource_id',v_resource) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_identity_mismatch');
                    ELSIF p_action='prepare_legacy_ack' AND (v_residency->>'lifecycle_gate'<>'legacy' OR v_residency->'generation'<>'null'::jsonb OR v_residency->'evictable'<>'false'::jsonb OR v_residency->>'state' NOT IN ('unloaded','resident') OR v_residency->'gpu_loaded'='null'::jsonb OR (v_residency->>'active_requests')::bigint<>0 OR (v_residency->>'builders')::bigint<>0 OR (v_residency->>'borrowers')::bigint<>0 OR v_residency->'draining'<>'false'::jsonb OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE pg_catalog.jsonb_typeof(p.pool_value->'resident') IS DISTINCT FROM 'boolean')) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','legacy_residency_not_stably_idle');
                    ELSIF p_action='prepare_legacy_ack' AND v_residency->'gpu_loaded'='true'::jsonb AND (v_residency->>'state'<>'resident' OR NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident'='true'::jsonb)) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','legacy_residency_loaded_inconsistent');
                    ELSIF p_action='prepare_legacy_ack' AND v_membership.state='pending' AND (v_residency->'identity'<>'null'::jsonb OR v_residency->'control_epoch'<>'null'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','pending_lifecycle_identity_already_bound');
                    ELSIF p_action='prepare_legacy_ack' AND v_membership.state='active' AND ((v_residency->'identity'='null'::jsonb)<>(v_residency->'control_epoch'='null'::jsonb)) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','active_lifecycle_identity_incomplete');
                    ELSIF p_action='prepare_legacy_ack' AND v_residency->'control_epoch'<>'null'::jsonb AND (v_residency->>'control_epoch')::numeric>v_fence.control_epoch_high_water THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_control_epoch_ahead');
                    ELSIF p_action='prepare_rollout_control' AND (v_residency->'identity'='null'::jsonb OR v_residency->'control_epoch'='null'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_control_identity_missing');
                    ELSIF p_action='prepare_rollout_control' AND (v_residency->>'control_epoch')::numeric>v_fence.control_epoch_high_water THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_control_epoch_ahead');
                    ELSIF p_action='prepare_rollout_control' AND (v_residency->>'state' NOT IN ('unloaded','resident') OR v_residency->'gpu_loaded'='null'::jsonb OR (v_residency->>'active_requests')::bigint<>0 OR (v_residency->>'builders')::bigint<>0 OR (v_residency->>'borrowers')::bigint<>0 OR v_residency->'draining'<>'false'::jsonb OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE pg_catalog.jsonb_typeof(p.pool_value->'resident') IS DISTINCT FROM 'boolean')) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','rollout_control_residency_not_stably_idle');
                    ELSIF p_action='prepare_rollout_control' AND v_residency->'gpu_loaded'='true'::jsonb AND (v_residency->>'state'<>'resident' OR NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident'='true'::jsonb)) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','rollout_control_residency_loaded_inconsistent');
                    ELSIF p_action='prepare_rollout_control' AND v_residency->'gpu_loaded'='false'::jsonb AND EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident' IS DISTINCT FROM 'false'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','rollout_control_residency_unloaded_inconsistent');
                    ELSE
                        IF p_action='prepare_legacy_ack' THEN
                            v_operation:='reset';
                        ELSE
                            v_enforce_intent:=v_fence.rollout_control_operation='mode_enforce' AND v_fence.rollout_control_transition_id=v_transition AND v_fence.rollout_control_epoch=v_fence.control_epoch_high_water AND v_fence.rollout_control_membership_epoch=v_membership.membership_epoch AND v_fence.rollout_control_boot_id=v_residency->>'boot_id' AND v_fence.rollout_control_token_expires_at IS NOT NULL;
                            v_reset_ack:=v_fence.rollout_control_operation='reset' AND v_fence.rollout_control_transition_id=v_transition AND v_fence.rollout_control_epoch=v_fence.control_epoch_high_water AND v_fence.rollout_control_membership_epoch=v_membership.membership_epoch AND v_fence.rollout_control_boot_id=v_residency->>'boot_id' AND v_fence.rollout_control_token_expires_at IS NOT NULL AND v_residency->>'control_epoch'=v_fence.rollout_control_epoch::text AND v_residency->>'lifecycle_gate'='legacy' AND v_probe_started>v_fence.rollout_control_token_expires_at AND v_trusted_empty;
                            IF v_target='legacy' THEN v_operation:='mode_legacy';
                            ELSIF v_enforce_intent THEN v_operation:='mode_enforce';
                            ELSIF v_residency->>'lifecycle_gate'='enforce' THEN v_operation:='mode_enforce';
                            ELSIF v_reset_ack THEN v_operation:='mode_enforce';
                            ELSE v_operation:='reset'; END IF;
                            v_intent_matches:=v_fence.rollout_control_operation=v_operation AND v_fence.rollout_control_transition_id=v_transition AND v_fence.rollout_control_epoch=v_fence.control_epoch_high_water AND v_fence.rollout_control_membership_epoch=v_membership.membership_epoch AND v_fence.rollout_control_boot_id=v_residency->>'boot_id' AND v_fence.rollout_control_token_expires_at IS NOT NULL;
                        END IF;
                        v_status:='create';
                        IF p_action='prepare_legacy_ack' AND v_membership.state='active' AND v_residency->>'control_epoch'=v_fence.control_epoch_high_water::text THEN
                            v_status:='legacy_acknowledged';
                        ELSIF v_intent_matches AND v_probe_started>v_fence.rollout_control_token_expires_at AND v_residency->>'control_epoch'=v_fence.rollout_control_epoch::text AND ((v_operation='mode_legacy' AND v_residency->>'lifecycle_gate'='legacy' AND v_residency->'evictable'='false'::jsonb) OR (v_operation<>'mode_legacy' AND v_trusted_empty AND v_residency->>'lifecycle_gate'=(CASE WHEN v_operation='reset' THEN 'legacy' ELSE 'enforce' END))) THEN
                            v_status:='acknowledged';
                        ELSIF v_intent_matches THEN
                            v_control:=v_fence.rollout_control_epoch; v_time:=v_fence.rollout_control_token_expires_at;
                            IF v_time>v_now THEN v_status:='replay';
                            ELSIF v_probe_started<=v_time THEN v_status:='awaiting_control_health';
                            END IF;
                        END IF;
                        IF v_status='legacy_acknowledged' THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','acknowledged','backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'boot_id',v_residency->>'boot_id','runtime_epoch',v_fence.runtime_epoch_high_water::text,'control_epoch',v_fence.control_epoch_high_water::text,'token_expires_at',NULL,'proof_ready',COALESCE(v_fence.token_expiry_high_water IS NOT NULL AND v_probe_started>v_fence.token_expiry_high_water AND v_trusted_empty,false)));
                        ELSIF v_status='acknowledged' THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','acknowledged','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_fence.control_epoch_high_water::text,'token_expires_at',aap_inference_gpu_v1._iso(v_fence.rollout_control_token_expires_at),'reason','fresh_health_acknowledged'));
                        ELSIF v_status='replay' THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','issue','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_control::text,'token_expires_at',aap_inference_gpu_v1._iso(v_time),'reason','control_intent_replay'));
                        ELSIF v_status='awaiting_control_health' THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','awaiting_health','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_control::text,'token_expires_at',aap_inference_gpu_v1._iso(v_time),'reason','control_health_not_after_token_horizon'));
                        ELSIF p_action='prepare_rollout_control' AND v_operation='reset' AND (v_residency->>'lifecycle_gate'<>'legacy' OR v_residency->'evictable'<>'false'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','reset_requires_legacy_gate');
                        ELSIF p_action='prepare_rollout_control' AND v_operation='mode_enforce' AND NOT v_trusted_empty THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','enforce_mode_requires_reset_empty');
                        ELSIF p_action='prepare_rollout_control' AND v_operation='mode_enforce' AND v_residency->>'lifecycle_gate'='legacy' AND NOT v_reset_ack THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','enforce_mode_requires_reset_ack');
                        ELSIF p_action='prepare_rollout_control' AND v_operation='mode_legacy' AND v_residency->>'lifecycle_gate' NOT IN ('legacy','enforce') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','legacy_mode_source_gate_invalid');
                        ELSIF p_action='prepare_rollout_control' AND v_fence.token_expiry_high_water IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','token_horizon_missing');
                        ELSIF p_action='prepare_legacy_ack' AND v_fence.token_expiry_high_water IS NOT NULL AND v_probe_started<=v_fence.token_expiry_high_water THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_not_after_token_horizon');
                        ELSIF p_action='prepare_rollout_control' AND v_probe_started<=v_fence.token_expiry_high_water THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','awaiting_health','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_fence.control_epoch_high_water::text,'token_expires_at',aap_inference_gpu_v1._iso(v_fence.token_expiry_high_water),'reason','control_health_not_after_token_horizon'));
                        ELSIF v_fence.control_epoch_high_water=9223372036854775807 OR (v_membership.state='pending' AND v_fence.runtime_epoch_high_water=9223372036854775807) THEN PERFORM aap_inference_gpu_v1._fail();
                        ELSE
                            IF v_membership.state='pending' THEN
                                UPDATE public.gpu_backend_fences f SET runtime_epoch_high_water=f.runtime_epoch_high_water+1 WHERE f.backend_registry_id=v_id RETURNING f.runtime_epoch_high_water INTO v_runtime;
                                UPDATE public.gpu_backend_memberships m SET state='active',updated_at=v_now WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource;
                            ELSE v_runtime:=v_fence.runtime_epoch_high_water; END IF;
                            v_control:=v_fence.control_epoch_high_water+1; v_time:=v_now+make_interval(secs=>v_budget);
                            UPDATE public.gpu_backend_fences f SET control_epoch_high_water=v_control,token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),rollout_control_operation=CASE WHEN p_action='prepare_rollout_control' THEN v_operation ELSE f.rollout_control_operation END,rollout_control_transition_id=CASE WHEN p_action='prepare_rollout_control' THEN v_transition ELSE f.rollout_control_transition_id END,rollout_control_epoch=CASE WHEN p_action='prepare_rollout_control' THEN v_control ELSE f.rollout_control_epoch END,rollout_control_membership_epoch=CASE WHEN p_action='prepare_rollout_control' THEN v_membership.membership_epoch ELSE f.rollout_control_membership_epoch END,rollout_control_boot_id=CASE WHEN p_action='prepare_rollout_control' THEN v_residency->>'boot_id' ELSE f.rollout_control_boot_id END,rollout_control_token_expires_at=CASE WHEN p_action='prepare_rollout_control' THEN v_time ELSE f.rollout_control_token_expires_at END,updated_at=v_now WHERE f.backend_registry_id=v_id;
                            IF p_action='prepare_legacy_ack' THEN
                                v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','issue','backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'boot_id',v_residency->>'boot_id','runtime_epoch',v_runtime::text,'control_epoch',v_control::text,'token_expires_at',aap_inference_gpu_v1._iso(v_time),'proof_ready',false));
                            ELSE
                                v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','issue','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_control::text,'token_expires_at',aap_inference_gpu_v1._iso(v_time),'reason','control_intent_created'));
                            END IF;
                        END IF;
                    END IF;
                END IF;
            END IF;
        END IF;

    ELSIF p_action='retired_probe_subject' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_id','resource_id','membership_epoch']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_id'); v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id'); v_next:=aap_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807);
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'retiring' OR v_membership.membership_epoch<>v_next OR v_fence.backend_registry_id IS NULL OR v_fence.generation_high_water IS DISTINCT FROM v_membership.retired_generation_high_water OR v_fence.control_epoch_high_water IS DISTINCT FROM v_membership.retired_control_epoch_high_water THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','retired_membership_changed');
        ELSE
            v_now:=pg_catalog.transaction_timestamp();
            IF v_membership.retired_token_expiry_high_water IS NOT NULL AND v_now<=v_membership.retired_token_expiry_high_water THEN v_reason:='waiting_token_horizon';
            ELSIF v_registry.id IS NULL THEN v_reason:='registry_missing';
            ELSE v_reason:='ready'; END IF;
            IF v_reason='ready' THEN v_challenge:=encode(public.gen_random_bytes(32),'hex'); v_probe_started:=pg_catalog.clock_timestamp();
                v_result:=pg_catalog.jsonb_build_object('status','ready','reason','','subject',pg_catalog.jsonb_build_object('challenge',v_challenge,'probe_started_at',aap_inference_gpu_v1._iso(v_probe_started)));
            ELSE v_result:=pg_catalog.jsonb_build_object('status','blocked','reason',v_reason,'subject',NULL); END IF;
        END IF;

    ELSIF p_action='retired_probe_commit' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_id','resource_id','membership_epoch','challenge','probe_started_at','endpoint_sha256','residency']) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_id'); v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id'); v_next:=aap_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807); v_challenge:=aap_inference_gpu_v1._string(v_args->'challenge',64); v_probe_started:=aap_inference_gpu_v1._timestamp(v_args->'probe_started_at'); v_fingerprint:=aap_inference_gpu_v1._string(v_args->'endpoint_sha256',64); v_residency:=v_args->'residency';
        IF v_challenge !~ '^[0-9a-f]{64}$' OR v_fingerprint !~ '^[0-9a-f]{64}$' OR NOT aap_inference_gpu_v1._residency_shape(v_residency) THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
        v_now:=pg_catalog.clock_timestamp(); v_observed:=v_now;
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'retiring' OR v_membership.membership_epoch<>v_next OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL OR v_fence.generation_high_water IS DISTINCT FROM v_membership.retired_generation_high_water OR v_fence.control_epoch_high_water IS DISTINCT FROM v_membership.retired_control_epoch_high_water OR v_membership.retired_token_expiry_high_water IS NOT NULL AND v_now<=v_membership.retired_token_expiry_high_water OR v_probe_started>=v_observed OR v_observed-v_probe_started>interval '5 minutes' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','retired_membership_changed');
        ELSIF v_residency->>'state' NOT IN ('unloaded','resident') OR v_residency->>'gpu_loaded'<>'false' OR v_residency->>'draining'<>'false' OR v_residency->>'evictable'<>'false' OR (v_residency->>'active_requests')::bigint<>0 OR (v_residency->>'builders')::bigint<>0 OR (v_residency->>'borrowers')::bigint<>0 OR v_residency->'identity'='null'::jsonb OR v_residency #>> '{identity,audience}' IS DISTINCT FROM 'aap-gpu-lifecycle' OR v_residency #>> '{identity,backend_registry_id}' IS DISTINCT FROM v_id::text OR v_residency #>> '{identity,gpu_resource_id}' IS DISTINCT FROM v_resource OR (v_residency->>'generation' IS NOT NULL AND (v_membership.retired_generation_high_water IS NULL OR (v_residency->>'generation')::bigint>v_membership.retired_generation_high_water)) OR (v_residency->>'control_epoch' IS NOT NULL AND (v_membership.retired_control_epoch_high_water IS NULL OR (v_residency->>'control_epoch')::bigint>v_membership.retired_control_epoch_high_water)) OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->>'resident' IS DISTINCT FROM 'false') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','retired_residency_not_unloaded');
        ELSE
            v_deadline:=floor(extract(epoch FROM (v_observed+interval '3 minutes'))*1000)::bigint;
            v_fingerprint:=encode(public.digest(pg_catalog.jsonb_build_object('backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_next,'retirement_id',v_membership.retirement_id,'challenge',v_challenge,'probe_started_at',aap_inference_gpu_v1._iso(v_probe_started),'observed_at',aap_inference_gpu_v1._iso(v_observed),'evidence_deadline_ms',v_deadline,'endpoint_sha256',v_args->>'endpoint_sha256','residency',v_residency)::text,'sha256'),'hex');
            UPDATE public.gpu_backend_memberships m SET retired_health_state='connected',retired_health_meta=v_residency,retired_health_checked_at=v_observed,updated_at=v_observed WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('status','live_unloaded','reason','','proof',pg_catalog.jsonb_build_object('observed_at',aap_inference_gpu_v1._iso(v_observed),'evidence_deadline_ms',v_deadline,'evidence_fingerprint',v_fingerprint,'residency',v_residency));
        END IF;

    ELSIF p_action='installation_control' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['request','phase','residency','probe_started_at','token_expires_at'])
           OR v_args->>'phase' NOT IN ('reset','mode_enforce')
           OR NOT aap_inference_gpu_v1._exact_keys(v_args->'request',ARRAY['registry_id','resource_id','endpoint','boot_id','operation_id','container_id','pool_ids'])
           OR pg_catalog.jsonb_typeof(v_args->'request'->'pool_ids') IS DISTINCT FROM 'array'
           OR NOT aap_inference_gpu_v1._residency_shape(v_args->'residency') THEN
            PERFORM aap_inference_gpu_v1._fail();
        END IF;
        v_expected:=v_args->'request'; v_residency:=v_args->'residency';
        v_id:=aap_inference_gpu_v1._uuid(v_expected->'registry_id');
        v_resource:=aap_inference_gpu_v1._resource(v_expected->'resource_id');
        v_id2:=aap_inference_gpu_v1._uuid(v_expected->'operation_id');
        v_boot:=aap_inference_gpu_v1._string(v_expected->'boot_id',128);
        v_text:=aap_inference_gpu_v1._string(v_expected->'endpoint',2048);
        v_reason:=aap_inference_gpu_v1._string(v_expected->'container_id',64);
        v_operation:=aap_inference_gpu_v1._string(v_args->'phase',16);
        v_probe_started:=aap_inference_gpu_v1._timestamp(v_args->'probe_started_at');
        v_time:=aap_inference_gpu_v1._timestamp(v_args->'token_expires_at');
        IF v_reason !~ '^[0-9a-f]{64}$'
           OR v_expected->'pool_ids' IS DISTINCT FROM
              (SELECT pg_catalog.jsonb_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(v_residency->'pools') p(k)) THEN
            PERFORM aap_inference_gpu_v1._fail();
        END IF;
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        BEGIN
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available THEN
            RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
        END;
        SELECT * INTO v_install_control FROM aap_inference_gpu_v1.installation_control_intents i WHERE i.backend_registry_id=v_id FOR UPDATE;
        v_now:=pg_catalog.clock_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state NOT IN ('pending','active')
           OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL
           OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.url IS DISTINCT FROM v_text
           OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb
           OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority
           OR aap_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency
           OR v_registry.extra_params #>> '{plugin,installation_id}' IS DISTINCT FROM v_id::text
           OR v_registry.extra_params #>> '{plugin,status}' IS NULL
           OR v_registry.extra_params #>> '{plugin,status}' NOT IN ('prepared','disabled')
           OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_resource
           OR v_registry.extra_params #>> '{plugin_lifecycle,launch,container_id}' IS DISTINCT FROM v_reason
           OR v_registry.extra_params #>> '{plugin_lifecycle,launch,registry_id}' IS DISTINCT FROM v_id::text
           OR v_registry.extra_params #> '{plugin_lifecycle,launch,resource}' IS DISTINCT FROM v_registry.extra_params #> '{plugin,resource}'
           OR pg_catalog.jsonb_typeof(v_registry.extra_params #> '{plugin_lifecycle,stopped}') IS DISTINCT FROM 'array'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(v_registry.extra_params #> '{plugin_lifecycle,stopped}')='array' THEN v_registry.extra_params #> '{plugin_lifecycle,stopped}' ELSE '[]'::jsonb END) s(value)
                     WHERE s.value->>'container_id'=v_reason OR s.value->>'boot_id'=v_boot)
           OR (v_registry.extra_params #>> '{plugin_lifecycle,activation,boot_id}' IS NOT NULL AND v_registry.extra_params #>> '{plugin_lifecycle,activation,boot_id}' IS DISTINCT FROM v_boot)
           OR v_residency->>'boot_id' IS DISTINCT FROM v_boot
           OR v_residency->>'state' NOT IN ('unloaded','resident')
           OR v_residency->'draining' IS DISTINCT FROM 'false'::jsonb
           OR (v_residency->'identity' IS DISTINCT FROM 'null'::jsonb AND v_residency->'identity' IS DISTINCT FROM pg_catalog.jsonb_build_object('audience','aap-gpu-lifecycle','backend_registry_id',v_id::text,'gpu_resource_id',v_resource))
           OR ((v_residency->'identity'='null'::jsonb) <> (v_residency->'control_epoch'='null'::jsonb))
           OR (v_residency->>'control_epoch' IS NOT NULL AND (v_residency->>'control_epoch')::bigint>v_fence.control_epoch_high_water)
           OR (v_membership.state='pending' AND (v_fence.runtime_epoch_high_water<>v_membership.runtime_epoch_baseline OR v_residency->'identity' IS DISTINCT FROM 'null'::jsonb))
           OR (v_membership.state='active' AND v_fence.runtime_epoch_high_water<=v_membership.runtime_epoch_baseline)
           OR EXISTS(SELECT 1 FROM aap_inference_gpu_v1.installation_terminal_intents t
                     WHERE t.backend_registry_id=v_id AND t.request->>'boot_id'=v_boot)
           OR v_probe_started>v_now OR v_probe_started<v_now-interval '3 minutes'
           OR v_time<=v_now OR v_time>v_now+interval '5 minutes' THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_control_changed');
        ELSIF v_membership.state='active' AND v_residency->>'lifecycle_gate'='enforce'
           AND v_residency->'identity'=pg_catalog.jsonb_build_object('audience','aap-gpu-lifecycle','backend_registry_id',v_id::text,'gpu_resource_id',v_resource)
           AND v_residency->>'control_epoch'=v_fence.control_epoch_high_water::text
           AND v_fence.control_epoch_high_water>0
           AND v_residency->'gpu_loaded' IN ('true'::jsonb,'false'::jsonb)
           AND NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE pg_catalog.jsonb_typeof(p.v->'resident') IS DISTINCT FROM 'boolean')
           AND (v_residency->>'generation' IS NULL OR ((v_residency->>'generation')::bigint>0 AND (v_residency->>'generation')::bigint<=v_fence.generation_high_water))
           AND ((v_residency->'gpu_loaded'='true'::jsonb AND v_residency->>'state'='resident'
                 AND v_residency->>'generation' IS NOT NULL
                 AND EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE p.v->'resident'='true'::jsonb))
                OR (v_residency->'gpu_loaded'='false'::jsonb
                    AND NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE p.v->'resident' IS DISTINCT FROM 'false'::jsonb))) THEN
            v_result:=pg_catalog.jsonb_build_object('status','complete','preparation',
                pg_catalog.jsonb_build_object('membership_epoch',v_membership.membership_epoch,
                    'control_epoch',v_fence.control_epoch_high_water::text,
                    'token_expires_at',aap_inference_gpu_v1._iso(COALESCE(v_fence.token_expiry_high_water,v_now))));
        ELSIF v_residency->'gpu_loaded' IS DISTINCT FROM 'false'::jsonb
           OR v_residency->'generation' IS DISTINCT FROM 'null'::jsonb
           OR v_residency->'evictable' IS DISTINCT FROM 'false'::jsonb
           OR v_residency->>'active_requests' IS DISTINCT FROM '0'
           OR v_residency->>'builders' IS DISTINCT FROM '0' OR v_residency->>'borrowers' IS DISTINCT FROM '0'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE p.v->'resident' IS DISTINCT FROM 'false'::jsonb) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_control_not_empty');
        ELSIF v_install_control.backend_registry_id IS NOT NULL
           AND v_install_control.request IS DISTINCT FROM v_expected
           AND v_install_control.revision IS NOT DISTINCT FROM v_registry.extra_params #> '{plugin,revision}'
           AND NOT (v_operation='reset' AND (v_install_control.request-'operation_id')=(v_expected-'operation_id')) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_control_conflict');
        ELSE
            v_intent_matches:=v_install_control.backend_registry_id IS NOT NULL AND v_install_control.request=v_expected
                AND v_install_control.revision IS NOT DISTINCT FROM v_registry.extra_params #> '{plugin,revision}'
                AND v_install_control.membership_epoch=v_membership.membership_epoch
                AND v_install_control.control_epoch=v_fence.control_epoch_high_water;
            IF v_intent_matches AND v_install_control.operation='mode_enforce'
               AND v_residency->>'lifecycle_gate'='enforce'
               AND v_residency->>'control_epoch'=v_install_control.control_epoch::text THEN
                v_status:='complete';
            ELSIF v_intent_matches AND v_install_control.operation='reset' AND v_operation='reset'
               AND v_residency->>'lifecycle_gate'='legacy'
               AND v_residency->>'control_epoch'=v_install_control.control_epoch::text THEN
                v_status:='acknowledged';
            ELSIF v_intent_matches AND v_install_control.operation=v_operation AND v_install_control.token_expires_at>v_now THEN
                v_status:='prepared';
            ELSE
                v_status:='create';
            END IF;
            IF v_status IN ('complete','acknowledged','prepared') THEN
                v_result:=pg_catalog.jsonb_build_object('status',v_status,'preparation',
                    pg_catalog.jsonb_build_object('membership_epoch',v_membership.membership_epoch,
                        'control_epoch',v_install_control.control_epoch::text,
                        'token_expires_at',aap_inference_gpu_v1._iso(v_install_control.token_expires_at)));
            ELSIF v_fence.token_expiry_high_water IS NOT NULL AND v_probe_started<=v_fence.token_expiry_high_water THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','control_token_horizon','token_horizon',aap_inference_gpu_v1._iso(v_fence.token_expiry_high_water));
            ELSIF v_operation='mode_enforce' AND NOT (
                v_intent_matches AND v_residency->>'lifecycle_gate'='legacy'
                AND v_probe_started>v_install_control.token_expires_at
                AND ((v_install_control.operation='reset' AND v_residency->>'control_epoch'=v_install_control.control_epoch::text)
                     OR (v_install_control.operation='mode_enforce' AND v_install_control.control_epoch>1
                         AND v_residency->>'control_epoch'=(v_install_control.control_epoch-1)::text))) THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_reset_unconfirmed');
            ELSIF v_operation='reset' AND v_residency->>'lifecycle_gate'<>'legacy' THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_reset_gate_invalid');
            ELSIF v_fence.control_epoch_high_water=9223372036854775807
               OR (v_membership.state='pending' AND v_fence.runtime_epoch_high_water=9223372036854775807) THEN
                PERFORM aap_inference_gpu_v1._fail();
            ELSE
                IF v_membership.state='pending' THEN
                    UPDATE public.gpu_backend_fences SET runtime_epoch_high_water=runtime_epoch_high_water+1 WHERE backend_registry_id=v_id;
                    UPDATE public.gpu_backend_memberships SET state='active',updated_at=v_now WHERE backend_registry_id=v_id AND gpu_resource_id=v_resource;
                END IF;
                v_control:=v_fence.control_epoch_high_water+1;
                UPDATE public.gpu_backend_fences SET control_epoch_high_water=v_control,
                    token_expiry_high_water=GREATEST(COALESCE(token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE backend_registry_id=v_id;
                INSERT INTO aap_inference_gpu_v1.installation_control_intents
                    (backend_registry_id,request,revision,membership_epoch,operation,control_epoch,token_expires_at)
                VALUES(v_id,v_expected,v_registry.extra_params #> '{plugin,revision}',v_membership.membership_epoch,v_operation,v_control,v_time)
                ON CONFLICT(backend_registry_id) DO UPDATE SET request=EXCLUDED.request,revision=EXCLUDED.revision,
                    membership_epoch=EXCLUDED.membership_epoch,operation=EXCLUDED.operation,
                    control_epoch=EXCLUDED.control_epoch,token_expires_at=EXCLUDED.token_expires_at;
                v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',
                    pg_catalog.jsonb_build_object('membership_epoch',v_membership.membership_epoch,
                        'control_epoch',v_control::text,'token_expires_at',aap_inference_gpu_v1._iso(v_time)));
            END IF;
        END IF;

    ELSIF p_action='installation_terminal' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['phase','request','residency','preparation','token_expires_at','ledger_source_generation','probe_started_at'])
           OR v_args->>'phase' NOT IN ('reserve','authorize','prove_idle','verify','commit')
           OR NOT aap_inference_gpu_v1._exact_keys(v_args->'request',ARRAY['registry_id','resource_id','endpoint','boot_id','operation_id','container_id','pool_ids'])
           OR pg_catalog.jsonb_typeof(v_args->'request'->'pool_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(v_args->'request'->'pool_ids')=0
           OR NOT aap_inference_gpu_v1._residency_shape(v_args->'residency') THEN
            PERFORM aap_inference_gpu_v1._fail();
        END IF;
        v_expected:=v_args->'request';
        v_residency:=v_args->'residency';
        v_id:=aap_inference_gpu_v1._uuid(v_expected->'registry_id');
        v_resource:=aap_inference_gpu_v1._resource(v_expected->'resource_id');
        v_id2:=aap_inference_gpu_v1._uuid(v_expected->'operation_id');
        v_boot:=aap_inference_gpu_v1._string(v_expected->'boot_id',128);
        v_text:=aap_inference_gpu_v1._string(v_expected->'endpoint',2048);
        v_reason:=aap_inference_gpu_v1._string(v_expected->'container_id',64);
        v_time:=aap_inference_gpu_v1._timestamp(v_args->'token_expires_at');
        IF v_args->'ledger_source_generation' IS DISTINCT FROM 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(v_args->'ledger_source_generation') IS DISTINCT FROM 'string'
                OR v_args->>'ledger_source_generation' !~ '^[1-9][0-9]{0,18}$'
                OR (v_args->>'ledger_source_generation')::numeric>9223372036854775807) THEN
            PERFORM aap_inference_gpu_v1._fail();
        END IF;
        IF v_reason !~ '^[0-9a-f]{64}$'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value)
                     WHERE pg_catalog.jsonb_typeof(x.value) IS DISTINCT FROM 'string'
                     OR x.value#>>'{}'='' OR x.value#>>'{}'<>pg_catalog.btrim(x.value#>>'{}'))
           OR v_expected->'pool_ids' IS DISTINCT FROM
              (SELECT pg_catalog.jsonb_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(v_residency->'pools') p(k)) THEN
            PERFORM aap_inference_gpu_v1._fail();
        END IF;
        IF v_args->>'phase'='reserve' THEN
            IF v_args->'preparation' IS DISTINCT FROM 'null'::jsonb THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
        ELSIF NOT aap_inference_gpu_v1._exact_keys(v_args->'preparation',ARRAY['membership_epoch','revision','generation','source_generation','control_epoch','runtime_epoch','token_expires_at','settled']) OR pg_catalog.jsonb_typeof(v_args->'preparation'->'settled') IS DISTINCT FROM 'boolean' THEN
            PERFORM aap_inference_gpu_v1._fail();
        END IF;
        IF v_args->>'phase'='prove_idle' THEN
            v_probe_started:=aap_inference_gpu_v1._timestamp(v_args->'probe_started_at');
        ELSIF v_args->'probe_started_at' IS DISTINCT FROM 'null'::jsonb THEN
            PERFORM aap_inference_gpu_v1._fail();
        END IF;
        PERFORM aap_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        BEGIN
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available THEN
            RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
        END;
        SELECT * INTO v_terminal_intent FROM aap_inference_gpu_v1.installation_terminal_intents i WHERE i.backend_registry_id=v_id FOR UPDATE;
        v_now:=pg_catalog.clock_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state NOT IN ('pending','active')
           OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL
           OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource
           OR v_registry.url IS DISTINCT FROM v_text
           OR v_registry.extra_params #>> '{plugin,installation_id}' IS DISTINCT FROM v_id::text
           OR v_registry.extra_params #>> '{plugin,status}' NOT IN ('prepared','disabled')
           OR v_registry.extra_params #>> '{plugin,status}' IS NULL
           OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_resource
           OR v_registry.extra_params #>> '{plugin_lifecycle,launch,container_id}' IS DISTINCT FROM v_reason
           OR v_registry.extra_params #>> '{plugin_lifecycle,launch,registry_id}' IS DISTINCT FROM v_id::text
           OR v_registry.extra_params #> '{plugin_lifecycle,launch,resource}' IS DISTINCT FROM v_registry.extra_params #> '{plugin,resource}'
           OR pg_catalog.jsonb_typeof(v_registry.extra_params #> '{plugin_lifecycle,stopped}') IS DISTINCT FROM 'array'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(v_registry.extra_params #> '{plugin_lifecycle,stopped}')='array' THEN v_registry.extra_params #> '{plugin_lifecycle,stopped}' ELSE '[]'::jsonb END) s(value)
                     WHERE s.value->>'container_id'=v_reason OR s.value->>'boot_id'=v_boot)
           OR (v_registry.extra_params #>> '{plugin_lifecycle,activation,boot_id}' IS NOT NULL AND v_registry.extra_params #>> '{plugin_lifecycle,activation,boot_id}' IS DISTINCT FROM v_boot)
           OR v_residency->>'boot_id' IS DISTINCT FROM v_boot
           OR NOT COALESCE((
               (v_args->>'phase' IN ('reserve','authorize') AND v_residency->'identity'='null'::jsonb
                AND v_residency->'control_epoch'='null'::jsonb AND v_residency->>'lifecycle_gate'='legacy'
                AND ((v_membership.state='pending' AND v_fence.runtime_epoch_high_water=v_membership.runtime_epoch_baseline)
                     OR (v_membership.state='active' AND v_fence.runtime_epoch_high_water>v_membership.runtime_epoch_baseline)))
               OR (v_residency #>> '{identity,audience}'='aap-gpu-lifecycle'
                   AND v_residency #>> '{identity,backend_registry_id}'=v_id::text
                   AND v_residency #>> '{identity,gpu_resource_id}'=v_resource
                   AND v_residency->>'control_epoch'=v_fence.control_epoch_high_water::text
                   AND v_fence.control_epoch_high_water>0
                   AND v_membership.state='active' AND v_fence.runtime_epoch_high_water>v_membership.runtime_epoch_baseline)), false)
           OR (v_args->>'phase' IN ('reserve','authorize','verify','commit')
               AND (v_time<=v_now OR v_time>v_now+interval '5 minutes')) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_changed');
        ELSIF v_args->>'phase'='reserve' THEN
            IF v_terminal_intent.backend_registry_id IS NOT NULL
               AND v_terminal_intent.request IS DISTINCT FROM v_expected
               AND (NOT v_terminal_intent.settled OR v_terminal_intent.revision IS NOT DISTINCT FROM v_registry.extra_params #> '{plugin,revision}') THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_intent_conflict');
            ELSIF v_terminal_intent.backend_registry_id IS NOT NULL AND v_terminal_intent.request=v_expected THEN
                IF v_terminal_intent.revision IS DISTINCT FROM v_registry.extra_params #> '{plugin,revision}'
                   OR v_terminal_intent.membership_epoch<>v_membership.membership_epoch
                   OR v_terminal_intent.generation<>v_fence.generation_high_water
                   OR v_terminal_intent.control_epoch<>v_fence.control_epoch_high_water
                   OR v_terminal_intent.runtime_epoch<>v_fence.runtime_epoch_high_water THEN
                    v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_intent_changed');
                END IF;
            ELSIF v_fence.generation_high_water=9223372036854775807
               OR (v_residency->>'generation' IS NOT NULL AND (v_residency->>'generation')::bigint>v_fence.generation_high_water)
               OR (v_args->>'ledger_source_generation' IS NOT NULL AND (v_args->>'ledger_source_generation')::bigint>v_fence.generation_high_water) THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','generation_changed');
            ELSE
                IF v_terminal_intent.backend_registry_id IS NULL OR v_terminal_intent.request IS DISTINCT FROM v_expected THEN
                    IF v_residency->'identity'='null'::jsonb THEN
                        IF v_fence.control_epoch_high_water=9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
                        v_fence.control_epoch_high_water:=v_fence.control_epoch_high_water+1;
                        UPDATE public.gpu_backend_fences SET control_epoch_high_water=v_fence.control_epoch_high_water WHERE backend_registry_id=v_id;
                    END IF;
                    IF v_membership.state='pending' THEN
                        IF v_fence.runtime_epoch_high_water=9223372036854775807 THEN PERFORM aap_inference_gpu_v1._fail(); END IF;
                        v_fence.runtime_epoch_high_water:=v_fence.runtime_epoch_high_water+1;
                        UPDATE public.gpu_backend_fences SET runtime_epoch_high_water=v_fence.runtime_epoch_high_water WHERE backend_registry_id=v_id;
                        UPDATE public.gpu_backend_memberships SET state='active',updated_at=v_now WHERE backend_registry_id=v_id AND gpu_resource_id=v_resource;
                    END IF;
                END IF;
                v_generation:=v_fence.generation_high_water+1;
                INSERT INTO aap_inference_gpu_v1.installation_terminal_intents AS i
                    (backend_registry_id,request,revision,membership_epoch,generation,source_generation,control_epoch,runtime_epoch,settled)
                VALUES (v_id,v_expected,v_registry.extra_params #> '{plugin,revision}',v_membership.membership_epoch,
                    v_generation,v_args->>'ledger_source_generation',v_fence.control_epoch_high_water,v_fence.runtime_epoch_high_water,false)
                ON CONFLICT (backend_registry_id) DO UPDATE SET request=EXCLUDED.request,revision=EXCLUDED.revision,
                    membership_epoch=EXCLUDED.membership_epoch,generation=EXCLUDED.generation,source_generation=EXCLUDED.source_generation,
                    control_epoch=EXCLUDED.control_epoch,runtime_epoch=EXCLUDED.runtime_epoch,settled=false
                RETURNING * INTO v_terminal_intent;
            END IF;
            IF v_result IS NULL THEN
                UPDATE public.gpu_backend_fences SET generation_high_water=v_terminal_intent.generation,
                    updated_at=v_now WHERE backend_registry_id=v_id;
                v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',
                    pg_catalog.jsonb_build_object('membership_epoch',v_terminal_intent.membership_epoch,
                        'revision',v_terminal_intent.revision,'generation',v_terminal_intent.generation::text,
                        'source_generation',v_terminal_intent.source_generation,'control_epoch',v_terminal_intent.control_epoch::text,
                        'runtime_epoch',v_terminal_intent.runtime_epoch::text,'token_expires_at',aap_inference_gpu_v1._iso(v_time),'settled',v_terminal_intent.settled));
            END IF;
        ELSIF v_args->>'phase' IN ('authorize','prove_idle') THEN
            IF v_terminal_intent.backend_registry_id IS NULL OR v_terminal_intent.request IS DISTINCT FROM v_expected
               OR v_terminal_intent.revision IS DISTINCT FROM v_registry.extra_params #> '{plugin,revision}'
               OR v_terminal_intent.revision IS DISTINCT FROM v_args->'preparation'->'revision'
               OR v_terminal_intent.membership_epoch IS DISTINCT FROM v_membership.membership_epoch
               OR v_terminal_intent.membership_epoch IS DISTINCT FROM aap_inference_gpu_v1._int(v_args->'preparation'->'membership_epoch',1,9223372036854775807)
               OR v_terminal_intent.generation::text IS DISTINCT FROM v_args->'preparation'->>'generation'
               OR v_terminal_intent.source_generation IS DISTINCT FROM v_args->'preparation'->>'source_generation'
               OR v_terminal_intent.source_generation IS DISTINCT FROM v_args->>'ledger_source_generation'
               OR v_terminal_intent.control_epoch::text IS DISTINCT FROM v_args->'preparation'->>'control_epoch'
               OR v_terminal_intent.runtime_epoch::text IS DISTINCT FROM v_args->'preparation'->>'runtime_epoch'
               OR v_terminal_intent.settled IS DISTINCT FROM (v_args->'preparation'->>'settled')::boolean
               OR v_fence.generation_high_water IS DISTINCT FROM v_terminal_intent.generation
               OR v_fence.control_epoch_high_water IS DISTINCT FROM v_terminal_intent.control_epoch
               OR v_fence.runtime_epoch_high_water IS DISTINCT FROM v_terminal_intent.runtime_epoch THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_intent_changed');
            ELSIF v_args->>'phase'='prove_idle' THEN
                IF v_terminal_intent.settled OR v_probe_started>v_now
                   OR v_probe_started<v_now-interval '3 minutes'
                   OR (v_fence.token_expiry_high_water IS NOT NULL AND v_probe_started<=v_fence.token_expiry_high_water)
                   OR v_residency->>'generation' IS DISTINCT FROM v_terminal_intent.source_generation
                   OR v_residency->>'active_requests' IS DISTINCT FROM '0'
                   OR v_residency->>'lifecycle_gate' IS DISTINCT FROM 'enforce'
                   OR v_residency->>'state' NOT IN ('resident','unloaded')
                   OR v_residency->'draining' IS DISTINCT FROM 'false'::jsonb
                   OR v_residency->>'builders' IS DISTINCT FROM '0'
                   OR v_residency->>'borrowers' IS DISTINCT FROM '0' THEN
                    v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_idle_unproven');
                ELSE
                    v_result:=pg_catalog.jsonb_build_object('status','verified');
                END IF;
            ELSE
                UPDATE public.gpu_backend_fences SET
                    token_expiry_high_water=GREATEST(COALESCE(token_expiry_high_water,v_time),v_time),
                    updated_at=v_now WHERE backend_registry_id=v_id;
                v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',
                    pg_catalog.jsonb_set(v_args->'preparation','{token_expires_at}',pg_catalog.to_jsonb(aap_inference_gpu_v1._iso(v_time))));
            END IF;
        ELSIF v_terminal_intent.backend_registry_id IS NULL OR v_terminal_intent.request IS DISTINCT FROM v_expected
           OR v_terminal_intent.generation::text IS DISTINCT FROM v_args->'preparation'->>'generation'
           OR v_terminal_intent.source_generation IS DISTINCT FROM v_args->'preparation'->>'source_generation'
           OR v_terminal_intent.source_generation IS DISTINCT FROM v_args->>'ledger_source_generation'
           OR v_terminal_intent.settled IS DISTINCT FROM (v_args->'preparation'->>'settled')::boolean
           OR v_membership.membership_epoch IS DISTINCT FROM aap_inference_gpu_v1._int(v_args->'preparation'->'membership_epoch',1,9223372036854775807)
           OR v_registry.extra_params #> '{plugin,revision}' IS DISTINCT FROM v_args->'preparation'->'revision'
           OR v_fence.generation_high_water::text IS DISTINCT FROM v_args->'preparation'->>'generation'
           OR v_fence.control_epoch_high_water::text IS DISTINCT FROM v_args->'preparation'->>'control_epoch'
           OR v_fence.runtime_epoch_high_water::text IS DISTINCT FROM v_args->'preparation'->>'runtime_epoch'
           OR v_time IS DISTINCT FROM aap_inference_gpu_v1._timestamp(v_args->'preparation'->'token_expires_at')
           OR v_fence.token_expiry_high_water<v_time
           OR v_residency->>'generation' IS DISTINCT FROM v_args->'preparation'->>'generation'
           OR v_residency->>'lifecycle_gate' IS DISTINCT FROM 'enforce'
           OR v_residency->>'state' IS DISTINCT FROM 'draining'
           OR v_residency->'draining' IS DISTINCT FROM 'true'::jsonb
           OR v_residency->'gpu_loaded' IS DISTINCT FROM 'false'::jsonb
           OR v_residency->'evictable' IS DISTINCT FROM 'false'::jsonb
           OR v_residency->>'active_requests' IS DISTINCT FROM '0'
           OR v_residency->>'builders' IS DISTINCT FROM '0'
           OR v_residency->>'borrowers' IS DISTINCT FROM '0'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE p.v->'resident' IS DISTINCT FROM 'false'::jsonb) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_proof_changed');
        ELSE
            IF v_args->>'phase'='commit' THEN
                UPDATE aap_inference_gpu_v1.installation_terminal_intents SET settled=true WHERE backend_registry_id=v_id;
            END IF;
            v_result:=pg_catalog.jsonb_build_object('status','verified');
        END IF;

    ELSE
        PERFORM aap_inference_gpu_v1._fail();
    END IF;

    v_result:=pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',v_result);
    IF pg_catalog.octet_length(v_result::text)>4194304 THEN
        RAISE EXCEPTION 'GPU Native response exceeds bounded size' USING ERRCODE='22023';
    END IF;
    RETURN v_result;
END
$function$;
