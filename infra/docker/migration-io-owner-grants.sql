ALTER TABLE public.storage_migration_io_reads OWNER TO :"assets_owner";
ALTER TABLE public.storage_migration_io_writes OWNER TO :"assets_owner";
ALTER TABLE public.storage_migration_io_probes OWNER TO :"assets_owner";
ALTER TABLE public.storage_migration_io_flights OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._migration_io_target(uuid,jsonb,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_copy(uuid,uuid,text,uuid,text,integer,integer,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_baseline(uuid,uuid,text,uuid,text,integer,integer,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_claim_write(uuid,uuid,text,uuid,text,integer,integer,text,integer,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_bind_read(uuid,uuid,integer,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_read(uuid,uuid,integer,text,text,integer,integer,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_cutover(uuid,integer,integer,integer,uuid,text,integer,uuid,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_open_flight(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_close_flight(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_reconcile_flights(uuid) OWNER TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_identity_v1, yoao_jobs_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid),
    yoao_identity_v1.read_actor_facts(uuid), yoao_jobs_v1.read_job(uuid) TO :"assets_owner";
GRANT SELECT (target) ON public.storage_migration_finalizations TO :"assets_owner";
GRANT SELECT (window_id, finalization_revision, window_revision)
    ON public.storage_migration_read_publications TO :"assets_owner";
GRANT SELECT (id) ON public.storage_migration_cutovers TO :"assets_owner";
