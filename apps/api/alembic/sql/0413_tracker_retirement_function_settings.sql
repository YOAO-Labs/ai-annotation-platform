-- Match the fixed execution settings required for all Routing Native functions.
ALTER FUNCTION aap_inference_routing_v1._tracker_scopes_physically_retired(uuid)
    SET TimeZone TO 'UTC';
-- aap-statement-break
ALTER FUNCTION aap_inference_routing_v1._tracker_scopes_physically_retired(uuid)
    SET DateStyle TO 'ISO, YMD';
-- aap-statement-break
ALTER FUNCTION aap_inference_routing_v1._tracker_scopes_physically_retired(uuid)
    SET extra_float_digits TO 3;
-- aap-statement-break
ALTER FUNCTION aap_inference_routing_v1._guard_disabled_plugin_tracker_scope()
    SET TimeZone TO 'UTC';
-- aap-statement-break
ALTER FUNCTION aap_inference_routing_v1._guard_disabled_plugin_tracker_scope()
    SET DateStyle TO 'ISO, YMD';
-- aap-statement-break
ALTER FUNCTION aap_inference_routing_v1._guard_disabled_plugin_tracker_scope()
    SET extra_float_digits TO 3;
