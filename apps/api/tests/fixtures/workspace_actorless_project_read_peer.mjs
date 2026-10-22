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
try {
  await client.connect();
  await contender.connect();
  assert.equal(
    (await client.query("SELECT current_user AS role")).rows[0].role,
    "identity_api_consumer",
  );
  for (const table of ["users", "projects", "project_members"]) {
    await assert.rejects(client.query(`SELECT id FROM public.${table} LIMIT 1`), { code: "42501" });
  }
  await client.query("BEGIN READ ONLY");
  const before = (await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid"))
    .rows[0];
  const statement = "SELECT aap_workspace_v1.read_project_row($1::uuid) AS value";
  const row = JSON.parse((await client.query(statement, [input.project_id])).rows[0].value);
  assert.equal(row.id, input.project_id);
  assert.equal(Object.keys(row).length, 43);
  assert.equal((await client.query(statement, [null])).rows[0].value, null);
  await contender.query("SELECT aap_workspace_v1.lock_project_row($1::uuid,true)", [
    input.project_id,
  ]);
  assert.deepEqual(
    (await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid")).rows[0],
    before,
  );
  await client.query("ROLLBACK");
  process.stdout.write(JSON.stringify({ ok: true, transaction_unchanged: true }));
} catch (error) {
  process.stderr.write(`Actor-free Project peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
  await contender.end();
}
