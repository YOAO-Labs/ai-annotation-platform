-- Retire direct fixture health mutation; managed providers publish real observations.
DROP FUNCTION aap_inference_topology_v1.seed_workbench_health(uuid);
