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
  assert.equal(
    (await client.query("SELECT current_user AS role")).rows[0].role,
    "workspace_dataset_unlink_client",
  );
  await assert.rejects(client.query("SELECT id FROM public.projects LIMIT 1"), { code: "42501" });
  await assert.rejects(client.query("DELETE FROM public.tasks WHERE false"), { code: "42501" });
  await client.query("BEGIN");
  const before = (await client.query(tx)).rows[0];
  const preview = (
    await client.query(
      "SELECT aap_workspace_v1.preview_dataset_unlink($1::uuid,$2::uuid,$3::uuid) AS reply",
      [command.actor_id, command.dataset_id, command.project_id],
    )
  ).rows[0].reply;
  assert.equal(preview.schema, "aap-dataset-unlink-preview/v1");
  assert.equal(preview.ok, true);
  assert.deepEqual(preview.data, {
    will_delete_tasks: 1,
    will_delete_annotations: 1,
    will_delete_batches: 1,
  });
  const raw = (
    await client.query(
      "SELECT aap_workspace_v1.unlink_dataset($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::text,$6::jsonb)::text AS reply",
      [
        command.operation_id,
        command.actor_id,
        command.dataset_id,
        command.project_id,
        command.cleanup_bucket,
        command.audit_context,
      ],
    )
  ).rows[0].reply;
  const reply = JSON.parse(raw);
  assert.equal(reply.schema, "aap-dataset-unlink/v1");
  assert.equal(reply.ok, true);
  for (const field of ["operation_id", "actor_id", "dataset_id", "project_id", "cleanup_bucket"])
    assert.equal(reply.data[field], command[field]);
  assert.equal(reply.data.soft, false);
  for (const field of ["deleted_tasks", "deleted_annotations", "deleted_batches", "audit_id"]) {
    const matched = raw.match(new RegExp(`"${field}"\\s*:\\s*([0-9]+)`));
    assert.ok(matched);
    assert.ok(BigInt(matched[1]) >= (field === "audit_id" ? 1n : 0n));
    if (field === "audit_id") assert.ok(BigInt(matched[1]) > 2n ** 53n);
  }
  assert.deepEqual((await client.query(tx)).rows[0], before);
  let commitFailure = null;
  if (input.finish === "rollback") await client.query("ROLLBACK");
  else {
    try {
      await client.query("COMMIT");
    } catch (error) {
      if (input.finish !== "failed_commit") throw error;
      assert.equal(error.code, "23514");
      await client.query("ROLLBACK");
      commitFailure = error.code;
    }
    if (commitFailure === null) assert.notEqual(input.finish, "failed_commit");
  }
  // Forward original JSON text; JavaScript numbers cannot represent the audit ID.
  process.stdout.write(
    commitFailure === null ? `{"reply":${raw}}` : JSON.stringify({ commit_failed: commitFailure }),
  );
} catch (error) {
  process.stderr.write(`Dataset unlink peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
}
