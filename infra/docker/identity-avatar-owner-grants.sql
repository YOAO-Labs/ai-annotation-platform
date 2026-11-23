GRANT CREATE ON SCHEMA yoao_identity_v1 TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.reserve_avatar_upload(uuid,text,jsonb,text,integer) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.confirm_avatar_upload_version(uuid,text,jsonb,text,integer,text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.avatar_upload_pending_matches(uuid,text,jsonb,text,integer,text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_ready_avatar_upload(text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.list_avatar_retirements(integer) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.begin_avatar_retirement(text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.finish_avatar_retirement(text,jsonb,text,integer,text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_avatar_binding_history() OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.avatar_saved_target_referenced(uuid) OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_v1 FROM :"identity_owner";

GRANT SELECT, INSERT, UPDATE, DELETE ON public.avatar_uploads TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.matches_avatar_reservation(text,jsonb) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.matches_avatar_reservation(text,jsonb) TO :"identity_owner";
