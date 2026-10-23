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
const marker = `node-scale-${randomUUID()}`;
const datasetId = randomUUID();
try {
  await client.connect();
  for (const table of ["projects", "project_members", "project_templates"]) {
    await assert.rejects(client.query(`SELECT id FROM public.${table}`), { code: "42501" });
  }
  await client.query("BEGIN");
  const projectId = (
    await client.query("SELECT aap_workspace_v1.create_scale_seed_project($1,$2) AS result", [
      input.owner_id,
      marker,
    ])
  ).rows[0].result;
  await client.query("SELECT aap_labeling_v1.write_project_counter_projection($1,$2::jsonb)", [
    projectId,
    { total_tasks: 17 },
  ]);
  const fact = (
    await client.query(
      "SELECT p.display_id,p.type_key,c.total_tasks FROM aap_workspace_v1.project_facts p JOIN aap_labeling_v1.project_catalog_counters c ON c.project_id=p.id WHERE p.id=$1",
      [projectId],
    )
  ).rows[0];
  assert.equal(fact.type_key, "image_detection");
  assert.equal(fact.total_tasks, 17);
  for (const count of [1, 0]) {
    assert.equal(
      (
        await client.query("SELECT aap_workspace_v1.ensure_scale_seed_member($1,$2,$3) AS result", [
          projectId,
          input.annotator_id,
          input.owner_id,
        ])
      ).rows[0].result,
      count,
    );
  }
  assert.deepEqual(
    (await client.query("SELECT aap_workspace_v1.guard_scale_seed_cleanup($1) AS result", [marker]))
      .rows[0].result,
    {
      schema: "aap-workspace-scale-seed/v1",
      marker,
      project_ids: [projectId],
      ok: true,
      error: null,
    },
  );
  await client.query("INSERT INTO datasets(id,display_id,name,created_by) VALUES($1,$2,$3,$4)", [
    datasetId,
    `D-N-${randomUUID().slice(0, 8)}`,
    marker,
    input.owner_id,
  ]);
  for (const phase of [false, true]) {
    assert.deepEqual(
      (
        await client.query(
          "SELECT aap_assets_v1.guard_scale_seed_dataset_cleanup($1,$2) AS result",
          [datasetId, phase],
        )
      ).rows[0].result,
      {
        schema: "aap-workspace-scale-source-guard/v1",
        dataset_id: datasetId,
        after_task_cleanup: phase,
        ok: true,
        error: null,
      },
    );
  }
  assert.equal(
    (
      await client.query(
        "SELECT aap_workspace_v1.delete_scale_seed_projects($1,$2::uuid[]) AS result",
        [marker, [projectId]],
      )
    ).rows[0].result,
    1,
  );
  const rollbackId = (
    await client.query("SELECT aap_workspace_v1.create_scale_seed_project($1,$2) AS result", [
      input.owner_id,
      marker,
    ])
  ).rows[0].result;
  const second = (
    await client.query("SELECT display_id FROM aap_workspace_v1.project_facts WHERE id=$1", [
      rollbackId,
    ])
  ).rows[0].display_id;
  assert.equal(Number(second.slice(2)), Number(fact.display_id.slice(2)) + 1);
  await client.query("ROLLBACK");
  assert.equal(
    (
      await client.query(
        "SELECT count(*)::int AS count FROM aap_workspace_v1.project_facts WHERE name=$1",
        [marker],
      )
    ).rows[0].count,
    0,
  );
  assert.equal(
    (await client.query("SELECT count(*)::int AS count FROM datasets WHERE id=$1", [datasetId]))
      .rows[0].count,
    0,
  );
  process.stdout.write(
    JSON.stringify({
      ok: true,
      fixed_capabilities: 5,
      conflict_unchanged: true,
      sequence_preserved: true,
      rolled_back: true,
    }),
  );
} finally {
  await client.end();
}
