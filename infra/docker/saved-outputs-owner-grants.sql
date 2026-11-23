GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_audit_v1 TO :"audit_owner";

ALTER TABLE yoao_assets_v1.saved_output_admissions OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.saved_output_writes OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._saved_output_gate(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._saved_output_origin(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_output_origin(jsonb,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.admit_saved_output_read(uuid,text,uuid,uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_avatar_subject(text,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_bug_subject(uuid,uuid,text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_archive_subject(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_saved_output_write(uuid,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_saved_output_write(uuid,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_output_write_status(uuid,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._guard_saved_output_write() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_audit_v1.saved_archive_output_facts(uuid,text,uuid) OWNER TO :"audit_owner";

GRANT SELECT (id, location_id, purpose, revision) ON public.storage_operations TO :"assets_owner";
GRANT SELECT (location_id, state, revision) ON public.storage_maintenance_gates TO :"assets_owner";
GRANT SELECT (id, connection_id, kind, enabled, config_revision) ON public.storage_locations TO :"assets_owner";
GRANT SELECT (id, kind, scope, project_id, config, revision) ON public.storage_connections TO :"assets_owner";
GRANT SELECT (token, owner_id, storage_ref, sha256, size_bytes, object_version_id, state)
ON public.avatar_uploads TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"assets_owner", :"audit_owner";
GRANT SELECT (avatar_ref) ON yoao_identity_v1.account_profiles TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid) TO :"assets_owner", :"audit_owner";
GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"assets_owner";
GRANT SELECT (id, job_id, year, month, storage_ref, sha256, size_bytes, state, published_at)
ON public.audit_archive_outputs TO :"audit_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.saved_archive_output_facts(uuid,text,uuid) TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"audit_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.read_job(uuid) TO :"audit_owner";

REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_audit_v1 FROM :"audit_owner";
