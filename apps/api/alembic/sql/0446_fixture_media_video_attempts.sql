-- Each claimed video attempt is owned by the actual backfill execution; an
-- expired lease, failed Job, or absent process is not completion evidence.
CREATE TABLE aap_jobs_v1.fixture_media_video_attempts (
    output_id uuid PRIMARY KEY,
    lease_token uuid NOT NULL,
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('running','settled'))
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_media_video_attempts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_video_attempt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.output_id,NEW.lease_token,NEW.job_id,NEW.run_id,NEW.dataset_id,NEW.item_id)
       IS DISTINCT FROM (OLD.output_id,OLD.lease_token,OLD.job_id,OLD.run_id,OLD.dataset_id,OLD.item_id)
       OR OLD.state <> 'running' OR NEW.state <> 'settled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_attempt_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_video_attempt() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_video_attempt_immutable BEFORE UPDATE OR DELETE
ON aap_jobs_v1.fixture_media_video_attempts FOR EACH ROW
EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_video_attempt();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_video_attempt_scope(p_output uuid,p_lease uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object('run_id',birth.run_id,'dataset_id',birth.dataset_id,'item_id',birth.item_id)
    FROM aap_assets_v1.fixture_media_births AS birth
    JOIN public.video_derivative_outputs AS output ON output.id=birth.owner_id
    WHERE birth.owner_kind='video_output' AND birth.owner_id=p_output AND birth.generation=0
      AND output.state='building' AND output.lease_token=p_lease AND output.dataset_item_id=birth.item_id;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_video_attempt_scope(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.record_fixture_video_attempt(p_job uuid,p_token uuid,p_output uuid,p_lease uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_parent aap_jobs_v1.fixture_media_backfills%ROWTYPE; v_scope jsonb;
BEGIN
    IF NOT aap_jobs_v1.fixture_media_session_owns('backfill',p_job) THEN RETURN false; END IF;
    SELECT * INTO v_parent FROM aap_jobs_v1.fixture_media_backfills
    WHERE job_id=p_job AND state='running' AND token=p_token
      AND backend_pid=pg_catalog.pg_backend_pid() FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
        JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
        WHERE principal.run_id=v_parent.run_id AND role.rolname=SESSION_USER
          AND principal.principal_name=role.rolname AND principal.state='active'
    ) THEN RETURN false; END IF;
    v_scope := aap_assets_v1.fixture_video_attempt_scope(p_output,p_lease);
    IF v_scope IS NULL OR v_scope ->> 'run_id' IS DISTINCT FROM v_parent.run_id::text
       OR v_scope ->> 'dataset_id' IS DISTINCT FROM v_parent.dataset_id::text
       OR aap_assets_v1.fixture_media_item_in_cutoff(
           v_parent.run_id,v_parent.dataset_id,(v_scope ->> 'item_id')::uuid,
           (v_parent.payload ->> 'accepted_before')::timestamptz
       ) IS DISTINCT FROM true THEN RETURN false; END IF;
    INSERT INTO aap_jobs_v1.fixture_media_video_attempts
        (output_id,lease_token,job_id,run_id,dataset_id,item_id,state)
    VALUES (p_output,p_lease,p_job,v_parent.run_id,v_parent.dataset_id,
        (v_scope ->> 'item_id')::uuid,'running');
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.record_fixture_video_attempt(uuid,uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_video_attempt_retired(p_run uuid,p_dataset uuid,p_output uuid,p_lease uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_video_attempts AS attempt
        JOIN aap_jobs_v1.fixture_media_backfills AS binding ON binding.job_id=attempt.job_id
        WHERE attempt.output_id=p_output AND attempt.lease_token=p_lease
          AND attempt.run_id=p_run AND attempt.dataset_id=p_dataset
          AND attempt.state='settled' AND binding.state='retired'
          AND binding.run_id=p_run AND binding.dataset_id=p_dataset
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_video_attempt_retired(uuid,uuid,uuid,uuid) FROM PUBLIC;
