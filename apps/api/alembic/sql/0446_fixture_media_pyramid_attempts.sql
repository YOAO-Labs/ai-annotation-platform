-- Every claimed generation attempt names its real child and exact lease.
-- Reusers bind the generation without pretending to be its writer.
CREATE TABLE aap_jobs_v1.fixture_media_pyramid_attempts (
    asset_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation > 0),
    lease_token uuid NOT NULL,
    output_id uuid NOT NULL,
    child_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('running','settled')),
    PRIMARY KEY (asset_id,generation,lease_token)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_media_pyramid_attempts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_pyramid_attempt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.asset_id,NEW.generation,NEW.lease_token,NEW.output_id,
          NEW.child_id,NEW.run_id,NEW.dataset_id,NEW.item_id) IS DISTINCT FROM
         (OLD.asset_id,OLD.generation,OLD.lease_token,OLD.output_id,
          OLD.child_id,OLD.run_id,OLD.dataset_id,OLD.item_id)
       OR OLD.state <> 'running' OR NEW.state <> 'settled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_attempt_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_pyramid_attempt() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_pyramid_attempt_immutable BEFORE UPDATE OR DELETE
ON aap_jobs_v1.fixture_media_pyramid_attempts FOR EACH ROW
EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_pyramid_attempt();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_pyramid_attempt_matches(
    p_run uuid,p_dataset uuid,p_item uuid,p_asset uuid,p_generation integer,p_lease uuid,p_output uuid
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT aap_assets_v1.fixture_pyramid_generation_owned(p_run,p_dataset,p_item,p_asset,p_generation)
       AND EXISTS (
           SELECT 1 FROM public.image_pyramid_generations AS generation
           JOIN aap_assets_v1.fixture_media_births AS output ON output.owner_kind='pyramid_output'
             AND output.owner_id=generation.output_id AND output.generation=0
           WHERE generation.asset_id=p_asset AND generation.generation=p_generation
             AND generation.status='building' AND generation.lease_token=p_lease
             AND generation.output_id=p_output AND output.run_id=p_run
             AND output.dataset_id=p_dataset AND output.item_id=p_item
       );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_pyramid_attempt_matches(uuid,uuid,uuid,uuid,integer,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.record_fixture_pyramid_attempt(
    p_child uuid,p_token uuid,p_asset uuid,p_generation integer,p_lease uuid,p_output uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_child aap_jobs_v1.fixture_media_children%ROWTYPE;
BEGIN
    IF NOT aap_jobs_v1.fixture_media_session_owns('pyramid',p_child) THEN RETURN false; END IF;
    SELECT * INTO v_child FROM aap_jobs_v1.fixture_media_children
    WHERE child_id=p_child AND state='running' AND token=p_token
      AND backend_pid=pg_catalog.pg_backend_pid() AND asset_id=p_asset AND generation=p_generation
    FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
        JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
        WHERE principal.run_id=v_child.run_id AND role.rolname=SESSION_USER
          AND principal.principal_name=role.rolname AND principal.state='active'
    ) OR aap_assets_v1.fixture_pyramid_attempt_matches(
        v_child.run_id,v_child.dataset_id,v_child.item_id,p_asset,p_generation,p_lease,p_output
    ) IS DISTINCT FROM true THEN RETURN false; END IF;
    INSERT INTO aap_jobs_v1.fixture_media_pyramid_attempts
        (asset_id,generation,lease_token,output_id,child_id,run_id,dataset_id,item_id,state)
    VALUES (p_asset,p_generation,p_lease,p_output,p_child,v_child.run_id,v_child.dataset_id,v_child.item_id,'running');
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.record_fixture_pyramid_attempt(uuid,uuid,uuid,integer,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_pyramid_attempts_retired(
    p_run uuid,p_dataset uuid,p_asset uuid,p_generation integer,p_expected integer
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT p_expected >= 0
       AND EXISTS (
           SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
           WHERE child.run_id=p_run AND child.dataset_id=p_dataset AND child.asset_id=p_asset
             AND child.generation=p_generation AND child.state='retired'
       )
       AND NOT EXISTS (
           SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
           WHERE child.asset_id=p_asset AND child.generation=p_generation
             AND (child.run_id<>p_run OR child.dataset_id<>p_dataset OR child.state<>'retired')
       )
       AND p_expected=(SELECT pg_catalog.count(*) FROM aap_jobs_v1.fixture_media_pyramid_attempts AS attempt
           WHERE attempt.run_id=p_run AND attempt.dataset_id=p_dataset
             AND attempt.asset_id=p_asset AND attempt.generation=p_generation)
       AND NOT EXISTS (
           SELECT 1 FROM aap_jobs_v1.fixture_media_pyramid_attempts AS attempt
           LEFT JOIN aap_jobs_v1.fixture_media_children AS child ON child.child_id=attempt.child_id
           WHERE attempt.asset_id=p_asset AND attempt.generation=p_generation
             AND (attempt.run_id<>p_run OR attempt.dataset_id<>p_dataset
                  OR attempt.state<>'settled' OR child.state IS DISTINCT FROM 'retired'
                  OR child.run_id IS DISTINCT FROM p_run OR child.dataset_id IS DISTINCT FROM p_dataset
                  OR child.asset_id IS DISTINCT FROM p_asset OR child.generation IS DISTINCT FROM p_generation)
       );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_pyramid_attempts_retired(uuid,uuid,uuid,integer,integer) FROM PUBLIC;
-- A child may have reused a generation with a different actual producer.
-- Its own execution still has to retire without being credited as the writer.
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_pyramid_children_retired(
    p_run uuid,p_dataset uuid,p_asset uuid,p_generation integer
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
        WHERE child.asset_id=p_asset AND child.generation=p_generation
          AND (child.run_id<>p_run OR child.dataset_id<>p_dataset OR child.state<>'retired')
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_pyramid_attempts AS attempt
        WHERE attempt.asset_id=p_asset AND attempt.generation=p_generation
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_pyramid_children_retired(uuid,uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_pyramid_settled_attempt_count(
    p_run uuid,p_dataset uuid,p_asset uuid,p_generation integer
) RETURNS bigint LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
        WHERE child.asset_id=p_asset AND child.generation=p_generation
          AND (child.run_id IS DISTINCT FROM p_run OR child.dataset_id IS DISTINCT FROM p_dataset
               OR child.state<>'retired')
    ) OR EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_pyramid_attempts AS attempt
        LEFT JOIN aap_jobs_v1.fixture_media_children AS child ON child.child_id=attempt.child_id
        WHERE attempt.asset_id=p_asset AND attempt.generation=p_generation
          AND (attempt.run_id IS DISTINCT FROM p_run OR attempt.dataset_id IS DISTINCT FROM p_dataset
               OR attempt.state<>'settled' OR child.state IS DISTINCT FROM 'retired'
               OR child.run_id IS DISTINCT FROM p_run OR child.dataset_id IS DISTINCT FROM p_dataset
               OR child.asset_id IS DISTINCT FROM p_asset OR child.generation IS DISTINCT FROM p_generation)
    ) THEN NULL ELSE (
        SELECT pg_catalog.count(*) FROM aap_jobs_v1.fixture_media_pyramid_attempts AS attempt
        WHERE attempt.run_id=p_run AND attempt.dataset_id=p_dataset
          AND attempt.asset_id=p_asset AND attempt.generation=p_generation
    ) END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_pyramid_settled_attempt_count(uuid,uuid,uuid,integer) FROM PUBLIC;
