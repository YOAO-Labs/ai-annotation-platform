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
const projectId = randomUUID();
const displayId = `P-NODE-${randomUUID().slice(0, 8)}`;
const schema = "aap-workspace-seed-project/v1";
try {
  await client.connect();
  await assert.rejects(client.query("SELECT id FROM public.projects"), { code: "42501" });
  await client.query("BEGIN");
  const created = (
    await client.query(
      "SELECT aap_workspace_v1.create_point_cloud_seed_project($1,$2,$3,$4,$5::jsonb) AS reply",
      [projectId, displayId, "Node 点云", input.owner_id, { lidar_box_3d: { enabled: true } }],
    )
  ).rows[0].reply;
  assert.deepEqual(created, { schema, display_id: displayId, project_id: projectId });
  const written = (
    await client.query(
      "SELECT aap_workspace_v1.write_nuscenes_seed_defaults($1,$2,$3::jsonb) AS reply",
      [projectId, "Node nuScenes", {}],
    )
  ).rows[0].reply;
  assert.deepEqual(Object.keys(written).sort(), ["project_id", "schema", "updated_at"]);
  assert.equal(written.schema, schema);
  assert.equal(written.project_id, projectId);
  assert.ok(Number.isFinite(Date.parse(written.updated_at)));
  const missing = (
    await client.query(
      "SELECT aap_workspace_v1.write_nuscenes_seed_defaults($1,$2,$3::jsonb) AS reply",
      [randomUUID(), "missing", {}],
    )
  ).rows[0].reply;
  assert.equal(missing.updated_at, null);
  await client.query("ROLLBACK");
  const found = (
    await client.query("SELECT aap_workspace_v1.read_import_project($1) AS reply", [displayId])
  ).rows[0].reply;
  assert.equal(found.project_id, null);
  process.stdout.write(JSON.stringify({ ok: true, seed_functions: 2, rolled_back: true }));
} finally {
  await client.end();
}
