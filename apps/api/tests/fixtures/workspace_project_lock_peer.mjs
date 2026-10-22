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
const lock = "SELECT aap_workspace_v1.lock_project_row($1::uuid,$2::boolean) AS reply";

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
  for (const nowait of [true, false]) {
    await client.query("BEGIN");
    const before = (
      await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid")
    ).rows[0];
    const reply = (await client.query(lock, [input.project_id, nowait])).rows[0].reply;
    assert.deepEqual(Object.keys(reply).sort(), ["nowait", "project_id", "row_json", "schema"]);
    assert.equal(reply.schema, "aap-workspace-project-lock/v1");
    assert.equal(reply.project_id, input.project_id);
    assert.equal(reply.nowait, nowait);
    const project = JSON.parse(reply.row_json);
    assert.equal(project.id, input.project_id);
    assert.equal(project.owner_id, input.owner_id);
    await assert.rejects(contender.query(lock, [input.project_id, true]), { code: "55P03" });
    assert.deepEqual(
      (await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid")).rows[0],
      before,
    );
    await client.query("ROLLBACK");
  }
  process.stdout.write(
    JSON.stringify({ ok: true, modes: [true, false], transaction_unchanged: true }),
  );
} catch (error) {
  process.stderr.write(`Workspace Project lock peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
  await contender.end();
}
