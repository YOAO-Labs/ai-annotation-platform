-- A reused generation is bound to the real original; only its actual lease
-- holder records an attempt.  Neither an expired lease nor a second caller
-- changes the generation's physical owner.
CREATE TABLE aap_assets_v1.fixture_independent_pyramid_bindings (
    delivery_id uuid PRIMARY KEY REFERENCES aap_assets_v1.fixture_independent_media_births(delivery_id),
    asset_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation>0)
);
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_independent_pyramid_attempts (
    asset_id uuid NOT NULL,
    generation integer NOT NULL,
    lease_token uuid NOT NULL,
    output_id uuid NOT NULL,
    delivery_id uuid NOT NULL REFERENCES aap_assets_v1.fixture_independent_pyramid_bindings(delivery_id),
    PRIMARY KEY(asset_id,generation,lease_token)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_independent_pyramid_bindings,
    aap_assets_v1.fixture_independent_pyramid_attempts FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_independent_pyramid_binding_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_independent_pyramid_bindings FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE TRIGGER fixture_independent_pyramid_attempt_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_independent_pyramid_attempts FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.bind_fixture_independent_pyramid(
    p_delivery uuid,p_token uuid,p_asset uuid,p_generation integer
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_independent_media_births%ROWTYPE;
BEGIN
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=p_delivery;
    IF NOT FOUND OR v_birth.producer_kind<>'pyramid' OR aap_assets_v1.fixture_independent_session_authorized(
        p_delivery,p_token,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,
        CASE WHEN v_birth.dataset_id IS NULL THEN v_birth.owner_id ELSE NULL END
    ) IS DISTINCT FROM true OR NOT EXISTS (
        SELECT 1 FROM public.image_pyramid_generations AS generation
        JOIN aap_assets_v1.fixture_media_births AS birth
          ON birth.owner_kind='pyramid_generation' AND birth.owner_id=generation.asset_id
         AND birth.generation=generation.generation
        WHERE generation.asset_id=p_asset AND generation.generation=p_generation
          AND generation.auth_ref -> 'original'=v_birth.source AND birth.run_id=v_birth.run_id
          AND birth.dataset_id IS NOT DISTINCT FROM v_birth.dataset_id
          AND birth.item_id IS NOT DISTINCT FROM v_birth.item_id
          AND birth.task_id IS NOT DISTINCT FROM CASE WHEN v_birth.dataset_id IS NULL THEN v_birth.owner_id ELSE NULL END
    ) THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_independent_pyramid_bindings(delivery_id,asset_id,generation)
    VALUES(p_delivery,p_asset,p_generation) ON CONFLICT DO NOTHING;
    RETURN EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_pyramid_bindings
                   WHERE delivery_id=p_delivery AND asset_id=p_asset AND generation=p_generation);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.bind_fixture_independent_pyramid(uuid,uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.record_fixture_independent_pyramid_attempt(
    p_delivery uuid,p_token uuid,p_asset uuid,p_generation integer,p_lease uuid,p_output uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_independent_media_births%ROWTYPE;
BEGIN
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=p_delivery;
    IF NOT FOUND OR aap_assets_v1.fixture_independent_session_authorized(
        p_delivery,p_token,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,
        CASE WHEN v_birth.dataset_id IS NULL THEN v_birth.owner_id ELSE NULL END
    ) IS DISTINCT FROM true OR NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_pyramid_bindings AS binding
        JOIN public.image_pyramid_generations AS generation
          ON generation.asset_id=binding.asset_id AND generation.generation=binding.generation
        JOIN aap_assets_v1.fixture_media_births AS output
          ON output.owner_kind='pyramid_output' AND output.owner_id=p_output AND output.generation=0
        WHERE binding.delivery_id=p_delivery AND binding.asset_id=p_asset AND binding.generation=p_generation
          AND generation.status='building' AND generation.lease_token=p_lease AND generation.output_id=p_output
          AND output.run_id=v_birth.run_id AND output.dataset_id IS NOT DISTINCT FROM v_birth.dataset_id
          AND output.item_id IS NOT DISTINCT FROM v_birth.item_id
          AND output.task_id IS NOT DISTINCT FROM CASE WHEN v_birth.dataset_id IS NULL THEN v_birth.owner_id ELSE NULL END
    ) THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_independent_pyramid_attempts(asset_id,generation,lease_token,output_id,delivery_id)
    VALUES(p_asset,p_generation,p_lease,p_output,p_delivery) ON CONFLICT DO NOTHING;
    RETURN EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_pyramid_attempts
                   WHERE asset_id=p_asset AND generation=p_generation AND lease_token=p_lease
                     AND output_id=p_output AND delivery_id=p_delivery);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_fixture_independent_pyramid_attempt(uuid,uuid,uuid,integer,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_independent_pyramid_bindings_retired(
    p_run uuid,p_dataset uuid,p_asset uuid,p_generation integer
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_pyramid_bindings AS binding
        JOIN aap_assets_v1.fixture_independent_media_births AS birth USING(delivery_id)
        LEFT JOIN aap_assets_v1.fixture_independent_media_executions AS execution USING(delivery_id)
        WHERE binding.asset_id=p_asset AND binding.generation=p_generation
          AND (birth.run_id IS DISTINCT FROM p_run OR birth.dataset_id IS DISTINCT FROM p_dataset
               OR execution.state IS DISTINCT FROM 'settled')
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_independent_pyramid_bindings_retired(uuid,uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_independent_pyramid_generation_retired(
    p_run uuid,p_dataset uuid,p_asset uuid,p_generation integer,p_expected integer
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT p_expected>=0
       AND EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_pyramid_bindings AS binding
                   JOIN aap_assets_v1.fixture_independent_media_births AS birth USING(delivery_id)
                   WHERE binding.asset_id=p_asset AND binding.generation=p_generation
                     AND birth.run_id=p_run AND birth.dataset_id IS NOT DISTINCT FROM p_dataset)
       AND aap_assets_v1.fixture_independent_pyramid_bindings_retired(p_run,p_dataset,p_asset,p_generation)
       AND p_expected = aap_jobs_v1.fixture_pyramid_settled_attempt_count(p_run,p_dataset,p_asset,p_generation)
           + (SELECT pg_catalog.count(*) FROM aap_assets_v1.fixture_independent_pyramid_attempts AS attempt
              WHERE attempt.asset_id=p_asset AND attempt.generation=p_generation)
       AND NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_independent_pyramid_attempts AS attempt
           JOIN aap_assets_v1.fixture_independent_media_births AS birth USING(delivery_id)
           LEFT JOIN aap_assets_v1.fixture_independent_media_executions AS execution USING(delivery_id)
           LEFT JOIN aap_assets_v1.fixture_independent_pyramid_bindings AS binding USING(delivery_id)
           WHERE attempt.asset_id=p_asset AND attempt.generation=p_generation
             AND (birth.run_id IS DISTINCT FROM p_run OR birth.dataset_id IS DISTINCT FROM p_dataset
                  OR execution.state IS DISTINCT FROM 'settled'
                  OR binding.asset_id IS DISTINCT FROM p_asset OR binding.generation IS DISTINCT FROM p_generation)
       );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_independent_pyramid_generation_retired(uuid,uuid,uuid,integer,integer) FROM PUBLIC;
