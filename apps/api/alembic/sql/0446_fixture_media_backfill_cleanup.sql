-- Delete only recorded, physically retired dataset backfills while their
-- Dataset and its independent retirement proof are still present.
CREATE FUNCTION aap_jobs_v1.delete_fixture_media_backfills(p_run uuid, p_dataset uuid)
RETURNS bigint LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_root uuid;
    v_binding aap_jobs_v1.fixture_media_backfills%ROWTYPE;
    v_count bigint := 0;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='0A000', MESSAGE='fixture_dataset_isolation_unsupported';
    END IF;
    IF (pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),5) <> '_test'
        AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),4) <> '_e2e')
       OR NOT COALESCE((
           SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
           FROM pg_catalog.pg_class AS c
           WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
       ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_private_owner_required';
    END IF;
    IF p_run IS NULL OR p_dataset IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_dataset_scope_invalid';
    END IF;
    SELECT COALESCE(parent_run_id,id) INTO v_root
    FROM yoao_fixture_v1.fixture_runs WHERE id=p_run;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs
    WHERE id=v_root AND parent_run_id IS NULL AND state IN ('open','sealing') FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_dataset_scope_changed';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=run.id
    WHERE run.id=p_run AND run.state='sealing'
      AND ((run.parent_run_id IS NOT NULL AND run.family='owned')
        OR (run.parent_run_id IS NULL AND run.family='screenshot'))
      AND resource.kind='dataset' AND resource.state='published' AND resource.resource_id=p_dataset
    FOR SHARE OF run,resource;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_dataset_scope_changed';
    END IF;
    IF aap_assets_v1.lock_dataset_reference(p_dataset) IS DISTINCT FROM p_dataset THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_dataset_scope_changed';
    END IF;
    IF aap_assets_v1.fixture_dataset_media_retired(p_run,p_dataset) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_backfill_unsettled';
    END IF;
    FOR v_binding IN
        SELECT * FROM aap_jobs_v1.fixture_media_backfills
        WHERE run_id=p_run AND dataset_id=p_dataset ORDER BY job_id FOR UPDATE
    LOOP
        PERFORM 1 FROM public.async_jobs WHERE id=v_binding.job_id FOR UPDATE;
        IF NOT FOUND OR aap_jobs_v1.fixture_media_backfill_retired(
            p_run,p_dataset,v_binding.job_id
        ) IS DISTINCT FROM true THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_backfill_unsettled';
        END IF;
        DELETE FROM public.async_jobs WHERE id=v_binding.job_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_backfill_unsettled';
        END IF;
        v_count := v_count + 1;
    END LOOP;
    RETURN v_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.delete_fixture_media_backfills(uuid,uuid) FROM PUBLIC;
