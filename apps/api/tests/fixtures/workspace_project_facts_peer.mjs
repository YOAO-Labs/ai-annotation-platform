import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({
  connectionString: input.database_url,
  connectionTimeoutMillis: 5000,
});

function assertColumns(row, expected, label) {
  assert.deepEqual(Object.keys(row).sort(), [...expected].sort(), label);
}

try {
  await client.connect();
  assert.equal(
    (await client.query("SELECT current_user AS role")).rows[0].role,
    "identity_api_consumer",
  );
  for (const table of ["users", "projects", "project_members"]) {
    await assert.rejects(client.query(`SELECT id FROM public.${table} LIMIT 1`), { code: "42501" });
  }
  await assert.rejects(
    client.query("UPDATE aap_workspace_v1.project_facts SET name=name WHERE id=$1::uuid", [
      input.project_id,
    ]),
    { code: "42501" },
  );
  await assert.rejects(
    client.query(
      "UPDATE aap_labeling_v1.project_catalog_counters " +
        "SET project_id=project_id WHERE project_id=$1::uuid",
      [input.project_id],
    ),
    { code: "42501" },
  );

  await client.query("BEGIN READ ONLY");
  const transaction = "SELECT pg_backend_pid() AS pid,txid_current()::text AS xid";
  const before = (await client.query(transaction)).rows[0];
  const workspace = await client.query(
    "SELECT * FROM aap_workspace_v1.project_facts WHERE id=$1::uuid",
    [input.project_id],
  );
  assert.equal(workspace.rows.length, 1);
  assertColumns(workspace.rows[0], input.workspace_columns, "Workspace facts shape");
  assert.equal(workspace.rows[0].id, input.project_id);
  assert.equal(workspace.rows[0].owner_id, input.owner_id);

  const counters = await client.query(
    "SELECT * FROM aap_labeling_v1.project_catalog_counters WHERE project_id=$1::uuid",
    [input.project_id],
  );
  assert.equal(counters.rows.length, 1);
  assertColumns(counters.rows[0], input.counter_columns, "Labeling counter shape");
  for (const column of input.counter_columns) {
    assert.notEqual(counters.rows[0][column], null, `counter ${column} is present`);
  }

  const combined = await client.query(
    `SELECT project.*,
            counters.total_tasks,
            counters.completed_tasks,
            counters.review_tasks,
            counters.in_progress_tasks,
            counters.batch_summary
       FROM aap_workspace_v1.project_facts AS project
       JOIN aap_labeling_v1.project_catalog_counters AS counters
         ON counters.project_id=project.id
      WHERE project.id=$1::uuid`,
    [input.project_id],
  );
  assert.equal(combined.rows.length, 1);
  assertColumns(combined.rows[0], input.project_columns, "Joined Project row shape");
  assert.deepEqual((await client.query(transaction)).rows[0], before);
  assert.equal(
    (await client.query("SHOW transaction_read_only")).rows[0].transaction_read_only,
    "on",
  );
  await client.query("ROLLBACK");
  process.stdout.write(
    JSON.stringify({
      ok: true,
      workspace_columns: Object.keys(workspace.rows[0]).length,
      counter_columns: Object.keys(counters.rows[0]).length,
      project_columns: Object.keys(combined.rows[0]).length,
      read_only: true,
      same_transaction: true,
    }),
  );
} catch (error) {
  process.stderr.write(
    `Workspace Project facts peer failed (${error.code ?? error.name}; actual=${error.actual?.code ?? "none"})\n`,
  );
  process.exitCode = 1;
} finally {
  await client.end();
}
