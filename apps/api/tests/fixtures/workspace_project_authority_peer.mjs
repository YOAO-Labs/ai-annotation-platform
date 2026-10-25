import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url, connectionTimeoutMillis: 5000 });

async function step(phase) {
  const reply = (
    await client.query(
      "SELECT aap_workspace_v1.project_access_step($1::uuid,$2::uuid,$3::text) AS reply",
      [input.actor_id, input.project_id, phase],
    )
  ).rows[0].reply;
  assert.deepEqual(Object.keys(reply).sort(), [
    "actor_id",
    "facts",
    "phase",
    "project_id",
    "schema",
  ]);
  assert.equal(reply.schema, "aap-workspace-project-access-step/v1");
  assert.equal(reply.actor_id, input.actor_id);
  assert.equal(reply.project_id, input.project_id);
  assert.equal(reply.phase, phase);
  return reply.facts;
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
  await client.query("BEGIN");
  const before = (await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid"))
    .rows[0];
  const accounts = (
    await client.query("SELECT aap_identity_v1.lock_accounts_share($1::uuid[],true) AS reply", [
      [input.actor_id],
    ])
  ).rows[0].reply;
  assert.equal(accounts.schema, "aap-identity-account-locks/v1");
  assert.equal(accounts.accounts.length, 1);
  assert.equal(accounts.accounts[0].id, input.actor_id);
  assert.equal(accounts.accounts[0].is_active, true);
  assert.equal(typeof accounts.accounts[0].email, "string");
  const project = JSON.parse((await step("lock_project_key_share")).row_json);
  assert.equal(project.id, input.project_id);
  assert.equal(project.owner_id, input.owner_id);
  const members = JSON.parse((await step("lock_member_share")).rows_json);
  assert.equal(members.length, 1);
  assert.equal(members[0].id, input.membership_id);
  assert.equal(members[0].user_id, input.actor_id);
  assert.equal(members[0].project_id, input.project_id);
  assert.equal(members[0].role, "annotator");
  assert.equal(members[0].version, 1);
  assert.deepEqual(
    (await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid")).rows[0],
    before,
  );
  await client.query("ROLLBACK");
  process.stdout.write(JSON.stringify({ ok: true, transaction_unchanged: true }));
} catch {
  process.stderr.write("Workspace authority peer failed\n");
  process.exitCode = 1;
} finally {
  await client.end();
}
