-- Narrow owner projections preserve relational filter/count/sort semantics.
CREATE VIEW aap_assets_v1.spatial_scene_references
WITH (security_barrier = true)
AS
SELECT scene.id AS scene_id, scene.dataset_id, scene.name AS scene_name
FROM public.scenes AS scene;
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.spatial_scene_references FROM PUBLIC;
-- aap-statement-break
CREATE VIEW aap_assets_v1.spatial_item_references
WITH (security_barrier = true)
AS
SELECT item.id AS item_id, item.dataset_id, item.scene_id, item.frame_index,
       item.file_type, dataset.metadata ->> 'axis_convention' AS axis_convention,
       scene.name AS scene_name,
       item.metadata ? 'calibration' AS calibration_metadata_present
FROM public.dataset_items AS item
JOIN public.datasets AS dataset ON dataset.id = item.dataset_id
LEFT JOIN public.scenes AS scene ON scene.id = item.scene_id;
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.spatial_item_references FROM PUBLIC;
