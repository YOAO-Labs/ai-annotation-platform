REVOKE ALL ON TABLE public.storage_migration_io_reads, public.storage_migration_io_writes,
    public.storage_migration_io_probes, public.storage_migration_io_flights FROM :"application_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.migration_io_bind_read(uuid,uuid,integer,jsonb)
TO :"application_role";
