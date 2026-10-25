CREATE VIEW aap_audit_v1.audit_daily_facts
WITH (security_barrier = true)
AS
SELECT
    day,
    action,
    target_type,
    actor_role,
    status_family,
    event_count
FROM public.mv_audit_bi_daily;
-- aap-statement-break
REVOKE ALL ON TABLE aap_audit_v1.audit_daily_facts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.refresh_audit_bi_mv()
RETURNS pg_catalog.void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_audit_bi_daily;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.refresh_audit_bi_mv() FROM PUBLIC;
