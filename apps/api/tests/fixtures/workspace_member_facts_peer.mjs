import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url, connectionTimeoutMillis: 5000 });
try {
  await client.connect();
  assert.equal(
    (await client.query("SELECT current_user AS role")).rows[0].role,
    "identity_api_consumer",
  );
  await assert.rejects(client.query("SELECT id FROM public.project_members LIMIT 1"), {
    code: "42501",
  });
  await assert.rejects(
    client.query("UPDATE aap_workspace_v1.project_member_facts SET role='viewer' WHERE false"),
    { code: "42501" },
  );
  await client.query("BEGIN READ ONLY");
  const tx = "SELECT pg_backend_pid() AS pid,txid_current()::text AS xid";
  const before = (await client.query(tx)).rows[0];
  const result = await client.query(
    "SELECT id,project_id,user_id,role,assigned_by,assigned_at,version,updated_at,weekly_target FROM aap_workspace_v1.project_member_facts WHERE id=$1::uuid",
    [input.membership_id],
  );
  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].project_id, input.project_id);
  assert.equal(result.rows[0].user_id, input.member_id);
  assert.equal(result.rows[0].role, "annotator");
  assert.equal(result.rows[0].version, 1);
  assert.equal(result.rows[0].weekly_target, null);
  assert.ok(result.rows[0].assigned_at instanceof Date);
  assert.deepEqual((await client.query(tx)).rows[0], before);
  assert.equal(
    (await client.query("SHOW transaction_read_only")).rows[0].transaction_read_only,
    "on",
  );
  await client.query("ROLLBACK");
  process.stdout.write(JSON.stringify({ ok: true, read_only: true, same_transaction: true }));
} catch (error) {
  process.stderr.write(`Workspace member facts peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
}
