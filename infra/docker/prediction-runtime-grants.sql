GRANT USAGE ON SCHEMA yoao_inference_topology_v1, yoao_inference_pipelines_v1,
    yoao_assets_v1, yoao_labeling_v1
TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_inference_topology_v1.lock_video_prediction_registry(uuid),
    yoao_inference_topology_v1.lock_video_prediction_pool(uuid,uuid,uuid),
    yoao_inference_topology_v1.lock_native_prediction_route(uuid,uuid,uuid),
    yoao_inference_pipelines_v1.create_prediction_execution_snapshot(uuid,text,jsonb),
    yoao_inference_pipelines_v1.read_prediction_execution_snapshot(uuid,uuid),
    yoao_inference_pipelines_v1.prune_prediction_execution_snapshots(timestamptz),
    yoao_labeling_v1.finish_prediction_retry_pipeline_failure(jsonb,jsonb),
    yoao_assets_v1.create_prediction_input_lease(uuid,uuid,text,uuid,integer,jsonb,integer,jsonb,timestamptz),
    yoao_assets_v1.prediction_input_lease_facts(uuid,boolean,timestamptz),
    yoao_assets_v1.prediction_input_objects_pending(uuid),
    yoao_assets_v1.read_prediction_input_inventory(uuid),
    yoao_assets_v1.read_prediction_input_object(uuid,text),
    yoao_assets_v1.reserve_prediction_input_object(uuid,text,jsonb),
    yoao_assets_v1.verify_prediction_input_object(uuid,text,jsonb,text,text),
    yoao_assets_v1.seal_prediction_input_lease(uuid,boolean,timestamptz),
    yoao_assets_v1.prepare_prediction_input_cleanup(uuid,timestamptz),
    yoao_assets_v1.record_prediction_input_cleanup_failure(uuid,text,boolean),
    yoao_assets_v1.finish_prediction_input_cleanup(uuid,uuid,integer,timestamptz),
    yoao_assets_v1.list_due_prediction_input_cleanups(timestamptz,integer),
    yoao_assets_v1.prediction_input_binding_history()
TO :"application_role";
