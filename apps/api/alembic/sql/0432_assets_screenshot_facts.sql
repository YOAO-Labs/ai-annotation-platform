-- Labeling may inspect only published screenshot-run items through Assets.
CREATE VIEW aap_assets_v1.fixture_screenshot_item_facts
WITH (security_barrier = true)
AS
SELECT resource.run_id, item.id, item.dataset_id, item.file_path,
       item.metadata ->> 'storage_relative_path' AS relative_path
FROM public.dataset_items AS item
JOIN yoao_fixture_v1.fixture_run_resources AS resource
  ON resource.kind='item' AND resource.resource_id=item.id AND resource.state='published'
JOIN yoao_fixture_v1.fixture_runs AS run
  ON run.id=resource.run_id AND run.family='screenshot'
  AND run.parent_run_id IS NULL AND run.state='open';
-- aap-statement-break
REVOKE ALL ON TABLE aap_assets_v1.fixture_screenshot_item_facts FROM PUBLIC;
