import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url });
const command = input.command;
let commitFailure = null;

try {
  await client.connect();
  assert.equal(
    (await client.query("SELECT current_user AS role")).rows[0].role,
    "workspace_project_deletion_client",
  );
  await client.query("BEGIN");
  // Keep JSON text intact so receipt integers retain full PostgreSQL precision.
  const raw = (
    await client.query(
      "SELECT aap_workspace_v1.delete_project($1::uuid,$2::uuid,$3::uuid,$4::jsonb)::text AS reply",
      [command.operation_id, command.actor_id, command.project_id, command.audit_context],
    )
  ).rows[0].reply;
  const reply = JSON.parse(raw);
  assert.equal(reply.schema, "aap-project-delete/v1");
  assert.equal(reply.ok, true);
  for (const field of ["operation_id", "actor_id", "project_id"]) {
    assert.equal(reply.data[field], command[field]);
  }
  for (const field of ["deleted_tasks", "deleted_annotations", "audit_id"]) {
    const matched = raw.match(new RegExp(`"${field}"\\s*:\\s*([0-9]+)`));
    assert.ok(matched);
    assert.ok(BigInt(matched[1]) >= (field === "audit_id" ? 1n : 0n));
  }
  if (input.finish === "rollback") {
    await client.query("ROLLBACK");
  } else {
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
  process.stdout.write(
    commitFailure === null ? `{"reply":${raw}}` : JSON.stringify({ commit_failed: commitFailure }),
  );
} finally {
  await client.end();
}
