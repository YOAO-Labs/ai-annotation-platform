GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"platform_runtime";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.begin_saved_binding_probe(uuid,uuid,uuid,text,text,uuid,integer,integer,integer)
    TO :"platform_runtime";
