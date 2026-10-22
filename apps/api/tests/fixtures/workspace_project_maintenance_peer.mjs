import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url });
const schema = "aap-workspace-project-maintenance/v1";
const displayId = `P-NU-${randomUUID().slice(0, 10)}`;
const projectId = randomUUID();
const lookup = async () =>
  (await client.query("SELECT aap_workspace_v1.read_import_project($1) AS reply", [displayId]))
    .rows[0].reply;

try {
  await client.connect();
  assert.equal(
    (await client.query("SELECT current_user AS role")).rows[0].role,
    "workspace_project_maintenance_client",
  );
  assert.deepEqual(await lookup(), {
    schema,
    display_id: displayId,
    project_id: null,
  });
  await client.query("BEGIN");
  const created = (
    await client.query(
      "SELECT aap_workspace_v1.create_import_project($1,$2,$3,$4,$5::jsonb) AS reply",
      [projectId, displayId, "Node 项目", input.owner_id, { lidar_box_3d: {} }],
    )
  ).rows[0].reply;
  assert.deepEqual(created, { schema, display_id: displayId, project_id: projectId });
  assert.deepEqual(await lookup(), created);
  const written = (
    await client.query(
      "SELECT aap_workspace_v1.write_point_cloud_quality_config($1,$2::jsonb) AS reply",
      [input.project_id, { config_revision: 2 }],
    )
  ).rows[0].reply;
  assert.deepEqual(Object.keys(written).sort(), ["project_id", "schema", "updated_at"]);
  assert.equal(written.schema, schema);
  assert.equal(written.project_id, input.project_id);
  assert.ok(Number.isFinite(Date.parse(written.updated_at)));
  await client.query("ROLLBACK");
  assert.deepEqual(await lookup(), { schema, display_id: displayId, project_id: null });
  process.stdout.write(JSON.stringify({ ok: true, functions: 3, rollback_preserved: true }));
} finally {
  await client.end();
}
