CREATE VIEW aap_audit_v1.audit_events
WITH (security_barrier = true)
AS
SELECT
    log.id,
    log.actor_id,
    log.actor_email,
    log.actor_role,
    log.action,
    log.target_type,
    log.target_id,
    log.method,
    log.path,
    log.status_code,
    log.ip,
    log.detail_json,
    log.request_id,
    log.created_at
FROM public.audit_logs AS log;
-- aap-statement-break
REVOKE ALL ON TABLE aap_audit_v1.audit_events FROM PUBLIC;
