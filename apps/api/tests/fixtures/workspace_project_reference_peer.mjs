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
  for (const name of ["lock_project_reference", "lock_project_reference_nowait"]) {
    await client.query("BEGIN");
    const before = (
      await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid")
    ).rows[0];
    const query = `SELECT aap_workspace_v1.${name}($1::uuid) AS reference`;
    assert.equal(
      (await client.query(query, [input.project_id])).rows[0].reference,
      input.project_id,
    );
    assert.equal((await client.query(query, [null])).rows[0].reference, null);
    await assert.rejects(
      contender.query("SELECT aap_workspace_v1.lock_project_row($1::uuid,true)", [
        input.project_id,
      ]),
      { code: "55P03" },
    );
    const snapshot = (
      await client.query(
        "SELECT aap_workspace_v1.project_access_step($1::uuid,$2::uuid,'read_project_row') AS reply",
        [input.actor_id, input.project_id],
      )
    ).rows[0].reply;
    assert.equal(snapshot.schema, "aap-workspace-project-access-step/v1");
    assert.equal(snapshot.actor_id, input.actor_id);
    assert.equal(snapshot.project_id, input.project_id);
    assert.equal(JSON.parse(snapshot.facts.row_json).id, input.project_id);
    assert.deepEqual(
      (await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid")).rows[0],
      before,
    );
    await client.query("ROLLBACK");
  }
  process.stdout.write(JSON.stringify({ ok: true, transaction_unchanged: true }));
} catch (error) {
  process.stderr.write(`Workspace Project reference peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
  await contender.end();
}
