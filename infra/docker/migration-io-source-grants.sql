GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_source_saved_io_login";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.migration_io_open_flight(uuid,uuid,text),
    yoao_assets_v1.migration_io_close_flight(uuid,uuid),
    yoao_assets_v1.migration_io_reconcile_flights(uuid),
    yoao_assets_v1.migration_io_copy(uuid,uuid,text,uuid,text,integer,integer,text),
    yoao_assets_v1.migration_io_baseline(uuid,uuid,text,uuid,text,integer,integer,text),
    yoao_assets_v1.migration_io_claim_write(uuid,uuid,text,uuid,text,integer,integer,text,integer,text),
    yoao_assets_v1.migration_io_read(uuid,uuid,integer,text,text,integer,integer,boolean),
    yoao_assets_v1.migration_io_cutover(uuid,integer,integer,integer,uuid,text,integer,uuid,integer,integer)
TO :"assets_source_saved_io_login";
