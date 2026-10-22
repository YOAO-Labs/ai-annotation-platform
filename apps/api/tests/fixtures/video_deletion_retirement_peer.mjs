import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url, connectionTimeoutMillis: 5000 });
const command = input.command;
const tx = "SELECT pg_backend_pid() AS pid,txid_current()::text AS xid";
try {
  await client.connect();
  assert.equal((await client.query("SELECT current_user AS role")).rows[0].role, input.role);
  await assert.rejects(client.query("SELECT id FROM public.projects LIMIT 1"), { code: "42501" });
  await assert.rejects(
    client.query("SELECT * FROM aap_jobs_v1.video_deletion_clearances LIMIT 1"),
    { code: "42501" },
  );
  await client.query("BEGIN");
  const before = (await client.query(tx)).rows[0];
  const sql =
    input.kind === "project"
      ? "SELECT aap_workspace_v1.delete_project($1::uuid,$2::uuid,$3::uuid,NULL)::text AS reply"
      : "SELECT aap_workspace_v1.unlink_dataset($1::uuid,$2::uuid,$4::uuid,$3::uuid,'video-delete-test',NULL)::text AS reply";
  const args = [command.operation_id, command.actor_id, command.project_id];
  if (input.kind !== "project") args.push(command.dataset_id);
  const raw = (await client.query(sql, args)).rows[0].reply;
  assert.deepEqual(JSON.parse(raw), {
    schema: input.kind === "project" ? "aap-project-delete/v1" : "aap-dataset-unlink/v1",
    ok: false,
    error: { code: "video_execution_not_retired" },
  });
  assert.deepEqual((await client.query(tx)).rows[0], before);
  // A rejected business envelope must leave nothing destructive to commit.
  await client.query("COMMIT");
  process.stdout.write(raw);
} catch (error) {
  process.stderr.write(`Video deletion peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
}
