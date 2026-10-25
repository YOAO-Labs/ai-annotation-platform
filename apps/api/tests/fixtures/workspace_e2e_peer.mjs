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
const project = {
  id: randomUUID(),
  display_id: `P-NE-${randomUUID().slice(0, 8)}`,
  name: "Node literal 雪",
  type_label: "LiDAR",
  type_key: "lidar",
  data_type: "lidar",
  owner_id: input.owner_id,
  tool_bindings: { literal: [false, null] },
  scene_mode: true,
  ai_enabled: false,
};
const template = {
  id: randomUUID(),
  display_id: `TPL-NE-${randomUUID().slice(0, 8)}`,
  name: "Node template",
  description: null,
  type_label: "LiDAR",
  type_key: "lidar",
  data_type: "lidar",
  tool_bindings: project.tool_bindings,
  label_config: {},
  scope: "private",
  created_by: input.owner_id,
  source_project_id: project.id,
};
try {
  await client.connect();
  for (const table of ["projects", "project_members", "project_templates"]) {
    await assert.rejects(client.query(`SELECT id FROM public.${table}`), { code: "42501" });
  }
  await client.query("BEGIN");
  const created = (
    await client.query("SELECT aap_workspace_fixture_v1.create_project($1::jsonb) AS result", [
      project,
    ])
  ).rows[0].result;
  assert.equal(created, project.id);
  const row = (
    await client.query(
      "SELECT name,scene_mode,tool_bindings FROM aap_workspace_v1.project_facts WHERE id=$1",
      [project.id],
    )
  ).rows[0];
  assert.deepEqual(row, {
    name: project.name,
    scene_mode: true,
    tool_bindings: project.tool_bindings,
  });
  assert.equal(
    (
      await client.query("SELECT aap_workspace_fixture_v1.create_templates($1::jsonb) AS result", [
        JSON.stringify([template]),
      ])
    ).rows[0].result,
    1,
  );
  await client.query("SAVEPOINT invalid_record");
  await assert.rejects(
    client.query("SELECT aap_workspace_fixture_v1.create_project($1::jsonb)", [
      { ...project, unexpected: true },
    ]),
    { code: "22023" },
  );
  await client.query("ROLLBACK TO SAVEPOINT invalid_record");
  assert.deepEqual(
    (
      await client.query(
        "SELECT aap_workspace_fixture_v1.guard_project_cleanup($1::uuid[]) AS result",
        [[project.id]],
      )
    ).rows[0].result,
    { schema: "aap-workspace-e2e-fixtures/v1", ok: true, reason: null },
  );
  assert.equal(
    (
      await client.query("SELECT aap_workspace_fixture_v1.delete_projects($1::uuid[]) AS result", [
        [project.id],
      ])
    ).rows[0].result,
    1,
  );
  assert.equal(
    (
      await client.query(
        "SELECT aap_workspace_fixture_v1.delete_templates(false,$1::text[]) AS result",
        [[template.display_id]],
      )
    ).rows[0].result,
    1,
  );
  const rollbackProject = { ...project, id: randomUUID() };
  await client.query("SELECT aap_workspace_fixture_v1.create_project($1::jsonb)", [
    rollbackProject,
  ]);
  await client.query("ROLLBACK");
  assert.equal(
    (
      await client.query(
        "SELECT count(*)::int AS count FROM aap_workspace_v1.project_facts WHERE id=$1",
        [rollbackProject.id],
      )
    ).rows[0].count,
    0,
  );
  assert.equal(
    (
      await client.query(
        "SELECT count(*)::int AS count FROM aap_workspace_v1.project_facts WHERE id=$1",
        [project.id],
      )
    ).rows[0].count,
    0,
  );
  assert.equal(
    (
      await client.query(
        "SELECT count(*)::int AS count FROM aap_workspace_fixture_v1.templates WHERE id=$1",
        [template.id],
      )
    ).rows[0].count,
    0,
  );
  process.stdout.write(
    JSON.stringify({
      ok: true,
      fixed_functions: 5,
      private_tables_denied: true,
      rolled_back: true,
    }),
  );
} finally {
  await client.end();
}
