-- The independent video producer, not a fabricated parent Job, owns each
-- claimed output attempt and its real lease.
CREATE TABLE aap_assets_v1.fixture_independent_video_attempts (
    output_id uuid NOT NULL,
    lease_token uuid NOT NULL,
    delivery_id uuid NOT NULL REFERENCES aap_assets_v1.fixture_independent_media_births(delivery_id),
    PRIMARY KEY(output_id,lease_token)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_independent_video_attempts FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_independent_video_attempt_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_independent_video_attempts FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.record_fixture_independent_video_attempt(
    p_delivery uuid,p_token uuid,p_output uuid,p_lease uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_independent_media_births%ROWTYPE;
BEGIN
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=p_delivery;
    IF NOT FOUND OR v_birth.producer_kind NOT IN ('video_metadata','video_derivatives')
       OR aap_assets_v1.fixture_independent_session_authorized(
           p_delivery,p_token,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,NULL
       ) IS DISTINCT FROM true OR NOT EXISTS (
           SELECT 1 FROM public.video_derivative_outputs AS output
           JOIN aap_assets_v1.fixture_media_births AS owner
             ON owner.owner_kind='video_output' AND owner.owner_id=output.id AND owner.generation=0
           WHERE output.id=p_output AND output.dataset_item_id=v_birth.item_id
             AND output.state='building' AND output.lease_token=p_lease AND output.original_ref=v_birth.source
             AND owner.run_id=v_birth.run_id AND owner.dataset_id=v_birth.dataset_id AND owner.item_id=v_birth.item_id
       ) THEN RETURN false; END IF;
    IF v_birth.producer_kind='video_derivatives' AND NOT (v_birth.envelope -> 'args' -> 0 ? p_output::text) THEN
        RETURN false;
    END IF;
    INSERT INTO aap_assets_v1.fixture_independent_video_attempts(output_id,lease_token,delivery_id)
    VALUES(p_output,p_lease,p_delivery) ON CONFLICT DO NOTHING;
    RETURN EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_video_attempts
                   WHERE output_id=p_output AND lease_token=p_lease AND delivery_id=p_delivery);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_fixture_independent_video_attempt(uuid,uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_independent_video_attempt_retired(
    p_run uuid,p_dataset uuid,p_output uuid,p_lease uuid
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_video_attempts AS attempt
        JOIN aap_assets_v1.fixture_independent_media_births AS birth USING(delivery_id)
        JOIN aap_assets_v1.fixture_independent_media_executions AS execution USING(delivery_id)
        WHERE attempt.output_id=p_output AND attempt.lease_token=p_lease
          AND birth.run_id=p_run AND birth.dataset_id=p_dataset AND execution.state='settled'
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_independent_video_attempt_retired(uuid,uuid,uuid,uuid) FROM PUBLIC;
