DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_export_executions) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export execution evidence must be retained';
    END IF;
END;
$guard$;
-- aap-statement-break
DROP FUNCTION public.fixture_export_execution_retired(uuid, uuid, uuid);
-- aap-statement-break
DROP FUNCTION public.retire_fixture_export_execution(uuid, uuid, uuid);
-- aap-statement-break
DROP FUNCTION public.finish_fixture_export_execution(uuid, uuid);
-- aap-statement-break
DROP FUNCTION public.begin_fixture_export_execution(uuid, uuid, uuid);
-- aap-statement-break
DROP FUNCTION public.fixture_export_execution_admitted(uuid);
-- aap-statement-break
DROP FUNCTION public.fixture_export_session_owns_job(uuid);
-- aap-statement-break
DROP TRIGGER trg_fixture_export_job_identity ON public.async_jobs;
-- aap-statement-break
DROP FUNCTION public.guard_fixture_export_job_identity();
-- aap-statement-break
DROP TRIGGER trg_fixture_export_execution_birth ON public.export_media_bundles;
-- aap-statement-break
DROP FUNCTION public.record_fixture_export_execution();
-- aap-statement-break
DROP TRIGGER trg_fixture_export_execution_immutable ON yoao_fixture_v1.fixture_export_executions;
-- aap-statement-break
DROP FUNCTION public.guard_fixture_export_execution();
-- aap-statement-break
DROP TABLE yoao_fixture_v1.fixture_export_executions;
