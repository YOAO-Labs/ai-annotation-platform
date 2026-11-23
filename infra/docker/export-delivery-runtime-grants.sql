GRANT USAGE ON SCHEMA yoao_export_v1, yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_export_v1.zip_receipt(uuid,text),
    yoao_export_v1.reserve_zip(uuid,uuid,uuid,uuid,text,text,integer,text,bigint,jsonb,timestamptz,timestamptz),
    yoao_export_v1.save_zip_proof(uuid,jsonb), yoao_export_v1.publish_zip(uuid),
    yoao_export_v1.allocate_media(uuid,uuid,uuid,uuid,jsonb,text,jsonb,jsonb,integer,bigint,jsonb,timestamptz,timestamptz),
    yoao_export_v1.media_delivery(uuid,boolean), yoao_export_v1.media_fence(uuid,text),
    yoao_export_v1.claim_media(uuid,uuid,timestamptz),
    yoao_export_v1.publish_media(uuid,uuid,timestamptz,timestamptz),
    yoao_export_v1.media_version(uuid,uuid), yoao_export_v1.media_versions(uuid),
    yoao_export_v1.find_cache(text,timestamptz), yoao_export_v1.lock_cache(uuid),
    yoao_export_v1.invalidate_cache(uuid,uuid), yoao_export_v1.record_cache(text,uuid,uuid),
    yoao_export_v1.media_attempt_ended(uuid,timestamptz),
    yoao_export_v1.media_has_operations(uuid,uuid),
    yoao_export_v1.lock_media_retirement(uuid,boolean),
    yoao_export_v1.start_media_retirement(uuid,timestamptz),
    yoao_export_v1.finish_media_retirement(uuid,timestamptz),
    yoao_export_v1.media_retirement_queue(timestamptz,integer),
    yoao_export_v1.zip_retirement_bundle(uuid),
    yoao_export_v1.start_zip_retirement(uuid,timestamptz),
    yoao_export_v1.zip_retirement_unreferenced(uuid),
    yoao_export_v1.remove_retired_zip(uuid,timestamptz),
    yoao_export_v1.purge_legacy_cache(timestamptz,integer),
    yoao_export_v1.zip_retirement_queue(timestamptz,integer),
    yoao_export_v1.binding_history(), yoao_export_v1.saved_location_is_referenced(uuid),
    yoao_export_v1.export_zip_inventory(uuid,integer), yoao_export_v1.export_media_inventory(uuid,integer),
    yoao_export_v1.export_media_inventory_objects(uuid,text,integer),
    yoao_export_v1.export_zip_usage(uuid[]), yoao_export_v1.export_media_usage(uuid[])
    TO :"application_role";
GRANT SELECT ON yoao_assets_v1.export_dataset_facts, yoao_assets_v1.export_item_facts,
    yoao_assets_v1.export_scene_facts, yoao_assets_v1.export_calibration_facts,
    yoao_assets_v1.export_pose_facts TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_export_datasets_share(uuid[]),
    yoao_assets_v1.lock_export_items_update(uuid[]), yoao_assets_v1.lock_export_scenes_update(uuid[]),
    yoao_assets_v1.lock_export_calibrations_share(uuid[]), yoao_assets_v1.lock_export_poses_share(uuid[])
    TO :"application_role";
