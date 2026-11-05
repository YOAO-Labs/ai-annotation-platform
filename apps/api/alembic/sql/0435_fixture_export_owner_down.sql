DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM public.export_media_bundles WHERE fixture_run_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM public.fixture_export_zip_retirements)
       OR EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_native_exports)
       OR EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_native_export_outputs) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export owner evidence must be retained';
    END IF;
END;
$guard$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.retire_fixture_native_export_output(p_output_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT public.fixture_native_export_cleanup_eligible(p_output_id) THEN RETURN false; END IF;
    UPDATE yoao_fixture_v1.fixture_native_export_outputs AS receipt SET retired_at = pg_catalog.clock_timestamp()
    FROM public.export_outputs AS output
    WHERE receipt.output_id = p_output_id AND receipt.output_id = output.id
      AND receipt.job_id = output.job_id AND receipt.project_id = output.project_id
      AND receipt.sha256 = output.sha256 AND receipt.size_bytes = output.size_bytes
      AND output.media_bundle_id IS NULL AND output.state = 'retired'
      AND NOT EXISTS (SELECT 1 FROM public.storage_operations AS operation WHERE operation.purpose IN (
          'export_write_' || pg_catalog.replace(output.id::text, '-', ''),
          'export_read_' || pg_catalog.replace(output.id::text, '-', ''),
          'export_delete_' || pg_catalog.replace(output.id::text, '-', '')
      ));
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
DROP TRIGGER trg_fixture_native_export_receipt_owner ON yoao_fixture_v1.fixture_native_export_outputs;
-- aap-statement-break
DROP TRIGGER trg_fixture_export_output_retirement_owner ON public.export_outputs;
-- aap-statement-break
DROP TRIGGER trg_fixture_export_media_retirement_owner ON public.export_media_bundles;
-- aap-statement-break
DROP TRIGGER trg_fixture_export_zip_receipt_owner ON public.fixture_export_zip_retirements;
-- aap-statement-break
DROP FUNCTION public.guard_fixture_export_private_retirement();
-- aap-statement-break
DROP FUNCTION public.fixture_export_owner_session();
