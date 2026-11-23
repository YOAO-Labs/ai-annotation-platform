GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.prepare_reference_io(uuid,text,uuid,uuid,text,jsonb)
    TO :"application_role";
