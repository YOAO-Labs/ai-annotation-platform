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
  connectionString: input.contender_url,
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
  for (const locked of [false, true]) {
    await client.query("BEGIN");
    const before = (
      await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid")
    ).rows[0];
    const rows = JSON.parse(
      (
        await client.query(
          "SELECT aap_workspace_v1.project_member_rows($1::uuid,$2::uuid[],$3::boolean) AS value",
          [input.project_id, input.users, locked],
        )
      ).rows[0].value,
    );
    assert.deepEqual(
      rows.map((row) => row.user_id),
      input.users.toSorted(),
    );
    assert.ok(
      rows.every((row) => row.project_id === input.project_id && Object.keys(row).length === 9),
    );
    for (const member of input.members) {
      await contender.query("BEGIN");
      const probe = contender.query(
        "SELECT id FROM public.project_members WHERE id=$1 FOR UPDATE NOWAIT",
        [member],
      );
      if (locked) await assert.rejects(probe, { code: "55P03" });
      else assert.equal((await probe).rows[0].id, member);
      await contender.query("ROLLBACK");
    }
    assert.deepEqual(
      (await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid")).rows[0],
      before,
    );
    await client.query("ROLLBACK");
  }
  for (const nowait of [false, true]) {
    await client.query("BEGIN");
    assert.equal(
      (
        await client.query(
          "SELECT aap_workspace_v1.lock_project_member_reference($1::uuid,$2::uuid,$3::boolean) AS value",
          [input.project_id, input.users[0], nowait],
        )
      ).rows[0].value,
      input.users[0],
    );
    await contender.query("BEGIN");
    await assert.rejects(
      contender.query("SELECT id FROM public.project_members WHERE id=$1 FOR UPDATE NOWAIT", [
        input.members[0],
      ]),
      { code: "55P03" },
    );
    await contender.query("ROLLBACK");
    await client.query("ROLLBACK");
  }
  assert.deepEqual(
    JSON.parse(
      (
        await client.query(
          "SELECT aap_workspace_v1.project_member_rows($1::uuid,$2::uuid[],true) AS value",
          [input.project_id, []],
        )
      ).rows[0].value,
    ),
    [],
  );
  process.stdout.write(JSON.stringify({ ok: true, transaction_unchanged: true }));
} catch (error) {
  process.stderr.write(`Workspace member peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
  await contender.end();
}
