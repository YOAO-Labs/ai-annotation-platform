DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM public.export_media_bundles WHERE fixture_run_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM public.fixture_export_zip_retirements) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export retention evidence must be retained';
    END IF;
END;
$guard$;
-- aap-statement-break
DROP FUNCTION public.fixture_export_retired(uuid, uuid, uuid);
-- aap-statement-break
DROP TRIGGER trg_export_output_fixture_creation ON public.export_outputs;
-- aap-statement-break
DROP FUNCTION public.record_fixture_export_zip_creation();
-- aap-statement-break
DROP TRIGGER trg_export_media_fixture_zip_guard ON public.export_media_bundles;
-- aap-statement-break
DROP FUNCTION public.guard_fixture_export_zip_state();
-- aap-statement-break
DROP TRIGGER trg_fixture_export_zip_receipt_immutable ON public.fixture_export_zip_retirements;
-- aap-statement-break
DROP FUNCTION public.guard_fixture_export_zip_receipt();
-- aap-statement-break
DROP TABLE public.fixture_export_zip_retirements;
-- aap-statement-break
ALTER TABLE public.export_media_bundles DROP COLUMN fixture_zip_created;
-- aap-statement-break
DROP TRIGGER trg_export_media_fixture_retention ON public.export_media_bundles;
-- aap-statement-break
DROP FUNCTION public.guard_fixture_export_retention();
-- aap-statement-break
ALTER TABLE public.export_media_bundles DROP CONSTRAINT ck_export_media_expiry;
-- aap-statement-break
ALTER TABLE public.export_media_bundles ADD CONSTRAINT ck_export_media_expiry
CHECK (expires_at = created_at + INTERVAL '168 hours');
-- aap-statement-break
ALTER TABLE public.export_media_bundles DROP COLUMN fixture_run_id;
