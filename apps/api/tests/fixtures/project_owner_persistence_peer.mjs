import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url, connectionTimeoutMillis: 5000 });
const contender = new Client({
  connectionString: input.database_url,
  connectionTimeoutMillis: 5000,
});
const lock = "SELECT aap_workspace_v1.lock_project_references_for_update($1::uuid[]) AS ids";
const read = "SELECT aap_workspace_v1.read_project_row($1::uuid) AS row";
const write = "SELECT aap_labeling_v1.write_project_counter_projection($1::uuid,$2::jsonb) AS id";
const tx = "SELECT pg_backend_pid() AS pid,txid_current()::text AS xid";
try {
  await client.connect();
  await contender.connect();
  assert.equal(
    (await client.query("SELECT current_user AS role")).rows[0].role,
    "identity_api_consumer",
  );
  await assert.rejects(client.query("SELECT id FROM public.projects LIMIT 1"), { code: "42501" });
  await assert.rejects(client.query("UPDATE public.projects SET total_tasks=0 WHERE false"), {
    code: "42501",
  });
  await client.query("BEGIN");
  const before = (await client.query(tx)).rows[0];
  assert.deepEqual((await client.query(lock, [[input.project_id, input.project_id]])).rows[0].ids, [
    input.project_id,
  ]);
  await assert.rejects(contender.query(lock, [[input.project_id]]), { code: "55P03" });
  assert.deepEqual((await client.query(tx)).rows[0], before);
  await client.query("ROLLBACK");
  const original = JSON.parse((await client.query(read, [input.project_id])).rows[0].row);
  await client.query("BEGIN");
  const writeTx = (await client.query(tx)).rows[0];
  assert.equal(
    (await client.query(write, [input.project_id, JSON.stringify({ total_tasks: 41 })])).rows[0].id,
    input.project_id,
  );
  assert.equal(
    JSON.parse((await client.query(read, [input.project_id])).rows[0].row).total_tasks,
    41,
  );
  assert.equal(
    JSON.parse((await contender.query(read, [input.project_id])).rows[0].row).total_tasks,
    original.total_tasks,
  );
  assert.deepEqual((await client.query(tx)).rows[0], writeTx);
  await client.query("ROLLBACK");
  assert.deepEqual(
    JSON.parse((await client.query(read, [input.project_id])).rows[0].row),
    original,
  );
  process.stdout.write(JSON.stringify({ ok: true, transaction_unchanged: true, rollback: true }));
} catch (error) {
  process.stderr.write(`Project owner persistence peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
  await contender.end();
}
