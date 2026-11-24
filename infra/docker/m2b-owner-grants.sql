\ir settings-diagnostics-owner-grants.sql
\ir prediction-runtime-owner-grants.sql
\ir learning-runs-owner-grants.sql
\ir learning-dataset-owner-grants.sql
\ir learning-models-owner-grants.sql
\ir learning-selection-owner-grants.sql

SELECT yoao_assets_storage_v1._adopt_installed_owners();
REVOKE ALL ON SCHEMA yoao_assets_storage_v1 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_assets_storage_v1 FROM PUBLIC;
\ir assets-originals-owner-grants.sql
\ir dataset-sources-owner-grants.sql
\ir storage-inventory-owner-grants.sql
\ir export-delivery-owner-grants.sql
\ir labeling-artifact-owner-grants.sql
\ir legacy-media-owner-grants.sql
\ir identity-avatar-owner-grants.sql
\ir jobs-ledger-owner-grants.sql

\ir source-channel-owner-grants.sql
\ir reference-io-owner-grants.sql
\ir saved-outputs-owner-grants.sql
\ir media-io-owner-grants.sql
\ir training-saved-io-owner-grants.sql
\ir migration-io-owner-grants.sql
\ir import-saved-io-owner-grants.sql
\ir notification-channels-owner-grants.sql
