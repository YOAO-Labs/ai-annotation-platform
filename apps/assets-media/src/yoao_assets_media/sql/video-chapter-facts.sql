CREATE VIEW aap_assets_v1.video_chapter_facts
WITH (security_barrier = true)
AS
SELECT id, dataset_item_id, title, start_frame, end_frame
FROM public.video_chapters;
-- aap-statement-break
REVOKE ALL ON TABLE aap_assets_v1.video_chapter_facts FROM PUBLIC;
