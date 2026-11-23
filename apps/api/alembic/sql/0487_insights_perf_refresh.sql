CREATE FUNCTION yoao_insights_v1.refresh_user_perf_daily()
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_user_perf_daily;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_insights_v1.refresh_user_perf_daily() FROM PUBLIC;
