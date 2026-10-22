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
const transaction = "SELECT pg_backend_pid() AS pid,txid_current()::text AS xid";
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
  const before = (await client.query(transaction)).rows[0];
  const rows = await client.query(
    `
    SELECT scope.project_id::text AS id
    FROM aap_workspace_v1.actor_project_memberships($1::uuid) scope
    JOIN aap_identity_v1.account_profiles profile ON profile.id=$1::uuid
    WHERE profile.is_active IS TRUE
      AND profile.role IN ('super_admin','project_admin','employee','viewer')
      AND scope.member_role IN ('annotator','reviewer','viewer')
      AND (profile.role<>'viewer' OR scope.member_role='viewer')
      AND scope.member_role='reviewer'
    ORDER BY scope.project_id`,
    [input.actor_id],
  );
  assert.deepEqual(
    rows.rows.map((row) => row.id),
    input.expected,
  );
  const projects = await client.query(
    "SELECT project_id::text AS id FROM aap_workspace_project_query_v1.project_search_scope($1::uuid,'employee')",
    [input.actor_id],
  );
  assert.ok(projects.rows.some((row) => row.id === input.project_id));
  await contender.query("SELECT aap_workspace_v1.lock_project_references_for_update($1::uuid[])", [
    [input.project_id],
  ]);
  assert.deepEqual((await client.query(transaction)).rows[0], before);
  assert.equal(
    (await client.query("SHOW transaction_read_only")).rows[0].transaction_read_only,
    "on",
  );
  await client.query("ROLLBACK");
  process.stdout.write(JSON.stringify({ ok: true, read_only: true, transaction_unchanged: true }));
} catch (error) {
  process.stderr.write(`Workspace actor scope peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
  await contender.end();
}
