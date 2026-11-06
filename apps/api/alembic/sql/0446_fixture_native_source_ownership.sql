-- An ordinary native producer may read a fixture original without owning its
-- physical outputs.  Preserve its real source/Job identity independently.
CREATE TABLE aap_assets_v1.fixture_external_media_jobs (
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid,
    job_kind text NOT NULL,
    owner_kind text NOT NULL,
    owner_id uuid NOT NULL,
    selection jsonb,
    producer_oid oid NOT NULL,
    PRIMARY KEY(family,job_id)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_external_media_jobs FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_external_media_job_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_external_media_jobs FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_image_admission_current(p_run uuid,p_dataset uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_image_work_intents AS intent
        JOIN yoao_fixture_v1.fixture_run_admissions AS admission
          ON admission.run_id=intent.run_id AND admission.id=intent.admission_id AND admission.state='active'
        CROSS JOIN LATERAL (
            SELECT pg_catalog.hashtextextended('aap-assets-images:' || intent.operation_id::text,0) AS key
        ) AS lock_key
        JOIN pg_catalog.pg_locks AS held ON held.locktype='advisory'
          AND held.pid=pg_catalog.pg_backend_pid() AND held.mode='ExclusiveLock' AND held.granted
          AND held.objsubid=1 AND held.classid::bigint=((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint=(lock_key.key & 4294967295::bigint)
        WHERE intent.run_id=p_run AND intent.dataset_id=p_dataset AND intent.state='prepared'
    ) OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_scan_admissions AS scan
        WHERE scan.run_id=p_run AND scan.dataset_id=p_dataset AND scan.family='image'
          AND scan.state='admitted' AND scan.business_pid=pg_catalog.pg_backend_pid()
          AND aap_assets_v1.fixture_native_source_lock_held(scan.business_pid,scan.token)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_image_admission_current(uuid,uuid) FROM PUBLIC;
