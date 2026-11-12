-- Independent deliveries retain their real execution state even after a worker
-- exits; a missing worker or a terminal business Job is never settlement.
CREATE TABLE aap_assets_v1.fixture_independent_media_executions (
    delivery_id uuid PRIMARY KEY REFERENCES aap_assets_v1.fixture_independent_media_births(delivery_id),
    token uuid NOT NULL UNIQUE,
    backend_pid integer NOT NULL,
    state text NOT NULL CHECK (state IN ('running','settled'))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_independent_media_executions FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_independent_execution()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.delivery_id,NEW.token,NEW.backend_pid)
        IS DISTINCT FROM (OLD.delivery_id,OLD.token,OLD.backend_pid)
        OR OLD.state<>'running' OR NEW.state<>'settled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_independent_execution_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_independent_execution() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_independent_execution_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_independent_media_executions FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_independent_execution();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_independent_session_owns(p_delivery uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks AS held
        CROSS JOIN LATERAL (
            SELECT pg_catalog.hashtextextended('aap-fixture-independent-execution:'||p_delivery::text,0) AS key
        ) AS lock_key
        WHERE held.locktype='advisory' AND held.pid=pg_catalog.pg_backend_pid()
          AND held.mode='ExclusiveLock' AND held.granted AND held.objsubid=1
          AND held.classid::bigint=((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint=(lock_key.key & 4294967295::bigint)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_independent_session_owns(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_independent_delivery_envelope(p_delivery uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT birth.envelope FROM aap_assets_v1.fixture_independent_media_births AS birth
    WHERE birth.delivery_id=p_delivery AND aap_jobs_v1.fixture_media_principal_matches(birth.run_id);
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_independent_delivery_envelope(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_independent_execution(p_delivery uuid,p_token uuid,p_envelope jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_independent_media_births%ROWTYPE;
    v_root uuid; v_root_state text; v_state text; v_delivery jsonb; v_digest text;
BEGIN
    IF p_delivery IS NULL OR p_token IS NULL OR p_envelope IS NULL THEN RETURN NULL; END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=p_delivery;
    IF NOT FOUND OR p_envelope IS DISTINCT FROM v_birth.envelope
       OR aap_jobs_v1.fixture_media_principal_matches(v_birth.run_id) IS DISTINCT FROM true THEN RETURN NULL; END IF;
    SELECT COALESCE(run.parent_run_id,run.id) INTO v_root
    FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id=v_birth.run_id;
    SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
    WHERE root.id=v_root AND root.parent_run_id IS NULL FOR SHARE;
    SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id=v_birth.run_id AND COALESCE(run.parent_run_id,run.id)=v_root FOR SHARE;
    IF v_root_state IS DISTINCT FROM 'open' OR v_state IS DISTINCT FROM 'open' THEN RETURN NULL; END IF;
    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_envelope::text,'UTF8')),'hex');
    v_delivery := aap_jobs_v1.fixture_media_delivery(p_delivery);
    IF v_delivery IS DISTINCT FROM pg_catalog.jsonb_build_object(
        'schema','aap-jobs-fixture-media-delivery/v1','delivery_id',p_delivery,
        'run_id',v_birth.run_id,'dataset_id',v_birth.dataset_id,'item_id',v_birth.item_id,
        'owner_kind',v_birth.owner_kind,'owner_id',v_birth.owner_id,'producer_kind',v_birth.producer_kind,
        'queue',v_birth.queue,'source',v_birth.source,'envelope',v_birth.envelope,'payload_digest',v_digest
    ) OR aap_jobs_v1.fixture_media_publication_accepted(v_birth.run_id,p_delivery,v_digest)
         IS DISTINCT FROM true THEN RETURN NULL; END IF;
    IF NOT pg_catalog.pg_try_advisory_lock(
        pg_catalog.hashtextextended('aap-fixture-independent-execution:'||p_delivery::text,0)
    ) THEN RETURN NULL; END IF;
    INSERT INTO aap_assets_v1.fixture_independent_media_executions(delivery_id,token,backend_pid,state)
    VALUES (p_delivery,p_token,pg_catalog.pg_backend_pid(),'running') ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
        PERFORM pg_catalog.pg_advisory_unlock(
            pg_catalog.hashtextextended('aap-fixture-independent-execution:'||p_delivery::text,0)
        );
        RETURN NULL;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-fixture-independent-execution/v1','delivery_id',p_delivery,
        'run_id',v_birth.run_id,'dataset_id',v_birth.dataset_id,'item_id',v_birth.item_id,
        'owner_kind',v_birth.owner_kind,'owner_id',v_birth.owner_id,'producer_kind',v_birth.producer_kind,
        'source',v_birth.source,'envelope',v_birth.envelope
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_independent_execution(uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_independent_session_authorized(
    p_delivery uuid,p_token uuid,p_run uuid,p_dataset uuid,p_item uuid,p_task uuid
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT aap_assets_v1.fixture_independent_session_owns(p_delivery)
       AND aap_jobs_v1.fixture_media_principal_matches(p_run)
       AND EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_independent_media_executions AS execution
           JOIN aap_assets_v1.fixture_independent_media_births AS birth USING(delivery_id)
           WHERE execution.delivery_id=p_delivery AND execution.token=p_token
             AND execution.backend_pid=pg_catalog.pg_backend_pid() AND execution.state='running'
             AND birth.run_id=p_run AND birth.dataset_id IS NOT DISTINCT FROM p_dataset
             AND birth.item_id IS NOT DISTINCT FROM p_item
             AND (p_task IS NULL OR (birth.owner_kind='task' AND birth.owner_id=p_task))
       );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_independent_session_authorized(uuid,uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_fixture_independent_execution(p_delivery uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_independent_media_births%ROWTYPE;
BEGIN
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=p_delivery;
    IF NOT FOUND OR aap_assets_v1.fixture_independent_session_authorized(
        p_delivery,p_token,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,
        CASE WHEN v_birth.dataset_id IS NULL THEN v_birth.owner_id ELSE NULL END
    ) IS DISTINCT FROM true OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_versions AS version
        WHERE version.execution_kind='independent' AND version.execution_id=p_delivery
          AND version.state<>'verified'
    ) THEN RETURN false; END IF;
    UPDATE aap_assets_v1.fixture_independent_media_executions SET state='settled'
    WHERE delivery_id=p_delivery AND token=p_token AND backend_pid=pg_catalog.pg_backend_pid()
      AND state='running';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_fixture_independent_execution(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_legacy_execution_target(p_kind text,p_id uuid,p_token uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid;
BEGIN
    IF p_kind='independent' THEN
        SELECT birth.run_id INTO v_run
        FROM aap_assets_v1.fixture_independent_media_births AS birth
        JOIN aap_assets_v1.fixture_independent_media_executions AS execution USING(delivery_id)
        WHERE birth.delivery_id=p_id AND execution.token=p_token AND execution.state='running'
          AND execution.backend_pid=pg_catalog.pg_backend_pid()
          AND aap_assets_v1.fixture_independent_session_owns(p_id)
          AND aap_jobs_v1.fixture_media_principal_matches(birth.run_id);
    ELSE
        v_run := aap_jobs_v1.fixture_media_execution_run(p_kind,p_id,p_token);
    END IF;
    IF v_run IS NULL THEN RETURN NULL; END IF;
    RETURN (SELECT pg_catalog.jsonb_build_object(
        'run_id',target.run_id,'endpoint',target.endpoint,'bucket',target.bucket,
        'storage_identity',target.storage_identity,'receipt_sha256',target.receipt_sha256,
        'location_id',target.location_id,'binding_generation',target.binding_generation
    ) FROM aap_assets_v1.fixture_native_cache_targets AS target WHERE target.run_id=v_run);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_legacy_execution_target(text,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_independent_media_children (
    parent_delivery uuid NOT NULL REFERENCES aap_assets_v1.fixture_independent_media_births(delivery_id),
    child_delivery uuid PRIMARY KEY REFERENCES aap_assets_v1.fixture_independent_media_births(delivery_id),
    CHECK (parent_delivery<>child_delivery)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_independent_media_children FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_independent_media_child_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_independent_media_children FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.bind_fixture_independent_child(p_parent uuid,p_token uuid,p_child uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_parent aap_assets_v1.fixture_independent_media_births%ROWTYPE;
BEGIN
    SELECT * INTO v_parent FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=p_parent;
    IF NOT FOUND OR v_parent.producer_kind NOT IN ('thumbnail','task_thumbnail')
       OR aap_assets_v1.fixture_independent_session_authorized(
           p_parent,p_token,v_parent.run_id,v_parent.dataset_id,v_parent.item_id,
           CASE WHEN v_parent.dataset_id IS NULL THEN v_parent.owner_id ELSE NULL END
       ) IS DISTINCT FROM true OR NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_independent_media_births AS child
           WHERE child.delivery_id=p_child AND child.producer_kind='pyramid'
             AND child.run_id=v_parent.run_id AND child.dataset_id IS NOT DISTINCT FROM v_parent.dataset_id
             AND child.item_id IS NOT DISTINCT FROM v_parent.item_id
             AND child.owner_kind=v_parent.owner_kind AND child.owner_id=v_parent.owner_id
             AND child.source=v_parent.source
       ) THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_independent_media_children(parent_delivery,child_delivery)
    VALUES (p_parent,p_child) ON CONFLICT DO NOTHING;
    RETURN EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_media_children
                   WHERE parent_delivery=p_parent AND child_delivery=p_child);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.bind_fixture_independent_child(uuid,uuid,uuid) FROM PUBLIC;
