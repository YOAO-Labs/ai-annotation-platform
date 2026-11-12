-- A no-start tombstone is born only after the exact publication was fenced
-- while the run is sealed.  Process absence and Job state are not evidence.
CREATE TABLE aap_assets_v1.fixture_independent_no_start (
    delivery_id uuid PRIMARY KEY REFERENCES aap_assets_v1.fixture_independent_media_births(delivery_id),
    run_id uuid NOT NULL,
    payload_digest text NOT NULL,
    publication_outcome text NOT NULL CHECK (publication_outcome='not_sent')
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_independent_no_start FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_independent_no_start_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_independent_no_start FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_independent_delivery_matches(p_delivery uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_independent_media_births%ROWTYPE; v_digest text;
BEGIN
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=p_delivery;
    IF NOT FOUND THEN RETURN false; END IF;
    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(v_birth.envelope::text,'UTF8')),'hex');
    RETURN aap_jobs_v1.fixture_media_delivery(p_delivery) IS NOT DISTINCT FROM pg_catalog.jsonb_build_object(
        'schema','aap-jobs-fixture-media-delivery/v1','delivery_id',p_delivery,
        'run_id',v_birth.run_id,'dataset_id',v_birth.dataset_id,'item_id',v_birth.item_id,
        'owner_kind',v_birth.owner_kind,'owner_id',v_birth.owner_id,'producer_kind',v_birth.producer_kind,
        'queue',v_birth.queue,'source',v_birth.source,'envelope',v_birth.envelope,'payload_digest',v_digest
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_independent_delivery_matches(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.retire_fixture_independent_publications(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_independent_media_births%ROWTYPE;
    v_digest text; v_outcome text;
BEGIN
    IF aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    FOR v_birth IN SELECT * FROM aap_assets_v1.fixture_independent_media_births
        WHERE run_id=p_run AND dataset_id=p_dataset ORDER BY delivery_id
    LOOP
        IF aap_assets_v1.fixture_independent_delivery_matches(v_birth.delivery_id) IS DISTINCT FROM true THEN
            RETURN false;
        END IF;
        v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(v_birth.envelope::text,'UTF8')),'hex');
        v_outcome := aap_jobs_v1.retire_fixture_media_publication(p_run,v_birth.delivery_id,v_digest);
        IF v_outcome NOT IN ('accepted','not_sent') OR v_outcome IS NULL THEN RETURN false; END IF;
        IF v_outcome='not_sent' AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_media_executions
                       WHERE delivery_id=v_birth.delivery_id) THEN
            INSERT INTO aap_assets_v1.fixture_independent_no_start(delivery_id,run_id,payload_digest,publication_outcome)
            VALUES(v_birth.delivery_id,p_run,v_digest,v_outcome) ON CONFLICT DO NOTHING;
            IF NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_no_start
                           WHERE delivery_id=v_birth.delivery_id AND run_id=p_run
                             AND payload_digest=v_digest AND publication_outcome=v_outcome) THEN RETURN false; END IF;
        END IF;
    END LOOP;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retire_fixture_independent_publications(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_independent_deliveries_retired(p_run uuid,p_dataset uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RETURN NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_media_births AS birth
        LEFT JOIN aap_assets_v1.fixture_independent_media_executions AS execution USING(delivery_id)
        LEFT JOIN aap_assets_v1.fixture_independent_no_start AS no_start USING(delivery_id)
        CROSS JOIN LATERAL (
            SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(birth.envelope::text,'UTF8')),'hex') AS digest
        ) AS payload
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset AND (
            aap_assets_v1.fixture_independent_delivery_matches(birth.delivery_id) IS DISTINCT FROM true
            OR aap_jobs_v1.fixture_media_publication_retirement_outcome(p_run,birth.delivery_id,payload.digest)
               IS DISTINCT FROM CASE WHEN execution.state='settled' THEN 'accepted' ELSE no_start.publication_outcome END
            OR (((execution.state='settled' AND no_start.delivery_id IS NULL) OR
                    (execution.delivery_id IS NULL AND no_start.run_id=p_run
                     AND no_start.payload_digest=payload.digest
                     AND no_start.publication_outcome='not_sent')) IS NOT TRUE)
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_independent_deliveries_retired(uuid,uuid) FROM PUBLIC;
