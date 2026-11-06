-- Native Images can create the generation independently of a Jobs backfill.
-- Preserve its actual producer, not a later child that merely reuses it.
CREATE TABLE aap_assets_v1.fixture_native_pyramid_generations (
    asset_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation>0),
    job_id uuid NOT NULL,
    job_generation bigint NOT NULL CHECK (job_generation>0),
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    PRIMARY KEY (asset_id,generation)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_pyramid_generations FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_pyramid_evidence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_pyramid_immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_pyramid_evidence() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_pyramid_evidence_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_pyramid_generations FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_pyramid_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.capture_fixture_native_pyramid()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_native_media_jobs%ROWTYPE;
BEGIN
    IF TG_OP='UPDATE' THEN
        IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_pyramid_generations AS binding
                   WHERE binding.asset_id=OLD.asset_id AND binding.generation=OLD.generation)
           AND (NEW.asset_id,NEW.generation,NEW.job_id,NEW.job_generation)
               IS DISTINCT FROM (OLD.asset_id,OLD.generation,OLD.job_id,OLD.job_generation) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_pyramid_immutable';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.job_id IS NULL THEN RETURN NEW; END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family='image' AND job_id=NEW.job_id;
    IF NOT FOUND THEN
        IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_births
                   WHERE owner_kind='pyramid_generation' AND owner_id=NEW.asset_id AND generation=NEW.generation) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_pyramid_untracked';
        END IF;
        RETURN NEW;
    END IF;
    IF v_birth.item_id IS NULL OR v_birth.job_kind<>'pyramid' OR NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_births AS generation
        JOIN aap_assets_v1.fixture_media_births AS asset ON asset.owner_kind='pyramid_asset'
          AND asset.owner_id=generation.owner_id AND asset.generation=0
        JOIN public.asset_image_jobs AS job ON job.job_id=NEW.job_id
        WHERE generation.owner_kind='pyramid_generation' AND generation.owner_id=NEW.asset_id
          AND generation.generation=NEW.generation AND generation.run_id=v_birth.run_id
          AND generation.dataset_id=v_birth.dataset_id AND generation.item_id=v_birth.item_id
          AND asset.run_id=v_birth.run_id AND asset.dataset_id=v_birth.dataset_id AND asset.item_id=v_birth.item_id
          AND job.kind='pyramid' AND job.generation=NEW.job_generation
          AND job.pyramid_asset_id=NEW.asset_id AND job.pyramid_generation=NEW.generation
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_pyramid_scope_changed'; END IF;
    INSERT INTO aap_assets_v1.fixture_native_pyramid_generations
        (asset_id,generation,job_id,job_generation,run_id,dataset_id,item_id)
    VALUES (NEW.asset_id,NEW.generation,NEW.job_id,NEW.job_generation,
        v_birth.run_id,v_birth.dataset_id,v_birth.item_id);
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.capture_fixture_native_pyramid() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_z_native_pyramid_birth AFTER INSERT ON public.image_pyramid_generations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.capture_fixture_native_pyramid();
-- aap-statement-break
CREATE TRIGGER fixture_native_pyramid_guard BEFORE UPDATE OF asset_id,generation,job_id,job_generation
ON public.image_pyramid_generations FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.capture_fixture_native_pyramid();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_pyramid_generation_retired(
    p_run uuid,p_dataset uuid,p_asset uuid,p_generation integer
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_pyramid_generations AS binding
        JOIN aap_assets_v1.fixture_media_births AS birth ON birth.owner_kind='pyramid_generation'
          AND birth.owner_id=binding.asset_id AND birth.generation=binding.generation
        JOIN public.image_pyramid_generations AS generation ON generation.asset_id=binding.asset_id
          AND generation.generation=binding.generation
        WHERE binding.run_id=p_run AND binding.dataset_id=p_dataset
          AND binding.asset_id=p_asset AND binding.generation=p_generation
          AND (birth.run_id,birth.dataset_id,birth.item_id)=(binding.run_id,binding.dataset_id,binding.item_id)
          AND generation.job_id=binding.job_id AND generation.job_generation=binding.job_generation
          AND generation.attempts=0
          AND (generation.lease_token IS NULL OR EXISTS (
              SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
              JOIN aap_assets_v1.fixture_native_media_executions AS execution ON execution.id=claim.execution_id
              WHERE claim.family='image' AND claim.job_id=binding.job_id
                AND claim.job_generation=binding.job_generation
                AND claim.lease_token=generation.lease_token AND claim.state='claimed'
                AND execution.run_id=p_run AND execution.dataset_id=p_dataset AND execution.state='settled'
          ))
          AND NOT EXISTS (
              SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
              LEFT JOIN aap_assets_v1.fixture_native_media_executions AS execution ON execution.id=claim.execution_id
              WHERE claim.family='image' AND claim.job_id=binding.job_id
                AND claim.job_generation=binding.job_generation
                AND (claim.state<>'claimed' OR execution.state IS DISTINCT FROM 'settled'
                  OR execution.run_id IS DISTINCT FROM p_run OR execution.dataset_id IS DISTINCT FROM p_dataset)
          )
          AND aap_jobs_v1.fixture_pyramid_children_retired(p_run,p_dataset,p_asset,p_generation)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_pyramid_generation_retired(uuid,uuid,uuid,integer) FROM PUBLIC;
