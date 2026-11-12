-- The child is a distinct durable dispatch and execution, not parent completion.
CREATE TABLE aap_jobs_v1.fixture_media_children (
    child_id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    delegation jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(delegation)='object'),
    queue text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending','running','settled','retired')),
    retirement_outcome text CHECK (retirement_outcome IN ('executed','not_sent')),
    token uuid,
    backend_pid integer,
    asset_id uuid,
    generation integer,
    CHECK ((state='running') = (token IS NOT NULL AND backend_pid IS NOT NULL)),
    CHECK ((state='retired') = (retirement_outcome IS NOT NULL)),
    CHECK ((asset_id IS NULL) = (generation IS NULL)),
    CHECK (generation IS NULL OR generation > 0)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_media_children FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_child_evidence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE'
       OR (NEW.child_id,NEW.job_id,NEW.run_id,NEW.dataset_id,NEW.item_id,NEW.delegation,NEW.queue)
          IS DISTINCT FROM
          (OLD.child_id,OLD.job_id,OLD.run_id,OLD.dataset_id,OLD.item_id,OLD.delegation,OLD.queue)
       OR (OLD.asset_id IS NOT NULL AND (NEW.asset_id,NEW.generation)
           IS DISTINCT FROM (OLD.asset_id,OLD.generation))
       OR (NEW.retirement_outcome IS DISTINCT FROM OLD.retirement_outcome
           AND NOT (OLD.state IN ('pending','settled') AND NEW.state='retired'
                    AND NEW.retirement_outcome IN ('executed','not_sent')))
       OR (OLD.state='retired' AND NEW IS DISTINCT FROM OLD) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_child_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_child_evidence() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_child_evidence_immutable
BEFORE UPDATE OR DELETE ON aap_jobs_v1.fixture_media_children
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_child_evidence();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.prepare_fixture_media_child(
    p_job_id uuid,p_parent_token uuid,p_child_id uuid,p_item_id uuid,p_delegation jsonb
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_parent aap_jobs_v1.fixture_media_backfills%ROWTYPE; v_existing record;
BEGIN
    IF p_job_id IS NULL OR p_parent_token IS NULL OR p_child_id IS NULL OR p_item_id IS NULL
       OR NOT aap_jobs_v1.fixture_media_session_owns('backfill',p_job_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_lock_required';
    END IF;
    SELECT * INTO v_parent FROM aap_jobs_v1.fixture_media_backfills WHERE job_id=p_job_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_parent_missing';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE run.id=v_parent.run_id AND run.state='open' AND root.state='open'
    FOR SHARE OF run,root;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed'; END IF;
    SELECT * INTO v_parent FROM aap_jobs_v1.fixture_media_backfills
    WHERE job_id=p_job_id FOR UPDATE;
    IF NOT FOUND OR v_parent.state <> 'running' OR v_parent.token <> p_parent_token
       OR v_parent.backend_pid <> pg_catalog.pg_backend_pid()
       OR NOT EXISTS (
          SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
          JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
          WHERE principal.run_id=v_parent.run_id AND role.rolname=SESSION_USER
            AND principal.principal_name=role.rolname AND principal.state='active'
       ) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    IF pg_catalog.jsonb_typeof(p_delegation) IS DISTINCT FROM 'object'
       OR p_delegation -> 'schema' IS DISTINCT FROM '1'::jsonb
       OR p_delegation ->> 'mode' IS DISTINCT FROM 'system'
       OR p_delegation ->> 'owner_kind' IS DISTINCT FROM 'dataset_item'
       OR p_delegation ->> 'owner_id' IS DISTINCT FROM p_item_id::text
       OR p_delegation -> 'task_binding' IS DISTINCT FROM 'null'::jsonb
       OR (p_delegation - ARRAY['schema','mode','owner_kind','owner_id','original','task_binding']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_delegation -> 'original') IS DISTINCT FROM 'object'
       OR aap_assets_v1.fixture_media_child_source_matches(
           v_parent.run_id,v_parent.dataset_id,p_item_id,
           (v_parent.payload ->> 'accepted_before')::timestamptz,p_delegation -> 'original'
       ) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_child_source_mismatch';
    END IF;
    SELECT child.child_id,child.queue INTO v_existing FROM aap_jobs_v1.fixture_media_children AS child
    WHERE child.job_id=p_job_id AND child.item_id=p_item_id AND child.delegation=p_delegation;
    IF FOUND THEN
        RETURN pg_catalog.jsonb_build_object('child_id',v_existing.child_id,'queue',v_existing.queue);
    END IF;
    INSERT INTO aap_jobs_v1.fixture_media_children
        (child_id,job_id,run_id,dataset_id,item_id,delegation,queue,state)
    VALUES (p_child_id,p_job_id,v_parent.run_id,v_parent.dataset_id,p_item_id,p_delegation,
        'aap_workspace_worker_' || pg_catalog.replace(v_parent.run_id::text,'-','') || '_pyramid',
        'pending') RETURNING child_id,queue INTO v_existing;
    RETURN pg_catalog.jsonb_build_object('child_id',v_existing.child_id,'queue',v_existing.queue);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.prepare_fixture_media_child(uuid,uuid,uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.begin_fixture_media_child(
    p_child_id uuid,p_queue text,p_item_id uuid,p_delegation jsonb,p_token uuid
) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_child aap_jobs_v1.fixture_media_children%ROWTYPE; v_run record;
BEGIN
    IF p_child_id IS NULL OR p_token IS NULL
       OR NOT aap_jobs_v1.fixture_media_session_owns('pyramid',p_child_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_lock_required';
    END IF;
    SELECT run.state,root.state AS root_state INTO v_run
    FROM aap_jobs_v1.fixture_media_children AS child
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=child.run_id
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE child.child_id=p_child_id FOR SHARE OF run,root;
    IF NOT FOUND THEN RETURN 'blocked'; END IF;
    SELECT * INTO v_child FROM aap_jobs_v1.fixture_media_children
    WHERE child_id=p_child_id FOR UPDATE;
    IF NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
        JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
        WHERE principal.run_id=v_child.run_id AND role.rolname=SESSION_USER
          AND principal.principal_name=role.rolname AND principal.state='active'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    IF v_run.state <> 'open' OR v_run.root_state <> 'open'
       OR v_child.queue IS DISTINCT FROM p_queue OR v_child.item_id IS DISTINCT FROM p_item_id
       OR v_child.delegation IS DISTINCT FROM p_delegation
       OR v_child.state IN ('running','retired') THEN RETURN 'blocked'; END IF;
    IF v_child.state='settled' THEN RETURN 'settled'; END IF;
    IF (SELECT pg_catalog.count(*) FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
        JOIN aap_jobs_v1.dispatch_intents AS intent ON intent.intent_id=dispatch.intent_id
        WHERE dispatch.job_id=v_child.job_id AND dispatch.run_id=v_child.run_id
          AND dispatch.dataset_id=v_child.dataset_id AND intent.job_id=v_child.job_id
          AND intent.execution_id=p_child_id::text
          AND dispatch.envelope=intent.envelope) <> 1 THEN RETURN 'blocked'; END IF;
    UPDATE aap_jobs_v1.fixture_media_children SET state='running',token=p_token,
        backend_pid=pg_catalog.pg_backend_pid() WHERE child_id=p_child_id;
    RETURN 'started';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.begin_fixture_media_child(uuid,text,uuid,jsonb,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.bind_fixture_media_child_generation(
    p_child_id uuid,p_token uuid,p_asset_id uuid,p_generation integer
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_jobs_v1.fixture_media_session_owns('pyramid',p_child_id) THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.fixture_media_children AS child
    SET asset_id=p_asset_id,generation=p_generation
    WHERE child.child_id=p_child_id AND child.state='running' AND child.token=p_token
      AND child.backend_pid=pg_catalog.pg_backend_pid()
      AND (child.asset_id IS NULL OR (child.asset_id,child.generation)=(p_asset_id,p_generation))
      AND EXISTS (
          SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
          JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
          WHERE principal.run_id=child.run_id AND role.rolname=SESSION_USER
            AND principal.principal_name=role.rolname AND principal.state='active'
      )
      AND aap_assets_v1.fixture_pyramid_generation_owned(
          child.run_id,child.dataset_id,child.item_id,p_asset_id,p_generation
      ) IS TRUE;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.bind_fixture_media_child_generation(uuid,uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.finish_fixture_media_child(p_child_id uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_jobs_v1.fixture_media_session_owns('pyramid',p_child_id) THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.fixture_media_children AS child
    SET state='settled',token=NULL,backend_pid=NULL
    WHERE child.child_id=p_child_id AND child.state='running' AND child.token=p_token
      AND child.backend_pid=pg_catalog.pg_backend_pid()
      AND EXISTS (
          SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
          JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
          WHERE principal.run_id=child.run_id AND role.rolname=SESSION_USER
            AND principal.principal_name=role.rolname AND principal.state='active'
      );
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.fixture_media_pyramid_attempts
    SET state='settled' WHERE child_id=p_child_id AND state='running';
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_fixture_media_child(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_work_exists(p_kind text,p_id text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_kind='backfill' THEN
        RETURN EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_backfills
                       WHERE job_id::text=p_id);
    ELSIF p_kind='pyramid' THEN
        RETURN EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_children
                       WHERE child_id::text=p_id);
    ELSIF p_kind='independent' THEN
        RETURN EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_deliveries
                       WHERE delivery_id::text=p_id);
    END IF;
    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_media_kind_invalid';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_work_exists(text,text) FROM PUBLIC;
