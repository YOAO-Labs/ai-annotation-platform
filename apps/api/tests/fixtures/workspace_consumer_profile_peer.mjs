import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");

let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const input = JSON.parse(raw);
const client = new Client({ connectionString: input.database_url });
try {
  await client.connect();
  await client.query("BEGIN");
  const before = (await client.query("SELECT pg_backend_pid(),txid_current()::text AS xid"))
    .rows[0];
  const functions = await client.query(
    "SELECT count(*)::int AS count FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$' AND has_function_privilege(current_user,p.oid,'EXECUTE')",
  );
  assert.equal(functions.rows[0].count, 32);
  const project = await client.query("SELECT id FROM aap_workspace_v1.project_facts WHERE id=$1", [
    input.project_id,
  ]);
  assert.equal(project.rows[0].id, input.project_id);
  await client.query("SELECT aap_workspace_v1.lock_project_reference($1)", [input.project_id]);
  await client.query("SAVEPOINT denied");
  await assert.rejects(client.query("SELECT id FROM public.projects LIMIT 1"), { code: "42501" });
  await client.query("ROLLBACK TO SAVEPOINT denied");
  const after = (await client.query("SELECT pg_backend_pid(),txid_current()::text AS xid")).rows[0];
  assert.deepEqual(after, before);
  await client.query("ROLLBACK");
  process.stdout.write(
    JSON.stringify({ ok: true, functions: 32, private_denied: true, same_transaction: true }),
  );
} catch {
  process.stderr.write("Workspace consumer conformance failed\n");
  process.exitCode = 1;
} finally {
  await client.end().catch(() => {});
}
