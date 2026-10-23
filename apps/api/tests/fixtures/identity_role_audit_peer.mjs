import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url });
try {
  await client.connect();
  for (const table of ["users", "user_invitations"])
    await assert.rejects(client.query(`SELECT id FROM public.${table}`), { code: "42501" });
  await client.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY");
  const accounts = (
    await client.query("SELECT * FROM aap_identity_audit_v1.account_roles ORDER BY id")
  ).rows;
  assert.deepEqual(
    accounts.map((r) => [r.role, r.is_active]),
    [
      ["project_admin", true],
      ["annotator", false],
      ["mystery", true],
    ],
  );
  assert.deepEqual(Object.keys(accounts[0]), ["id", "role", "is_active"]);
  const invitations = (
    await client.query("SELECT * FROM aap_identity_audit_v1.invitation_facts ORDER BY id")
  ).rows;
  assert.equal(invitations.length, 1);
  assert.deepEqual(Object.keys(invitations[0]), [
    "id",
    "project_id",
    "role",
    "project_role",
    "expires_at",
    "accepted_at",
    "revoked_at",
    "invited_by",
  ]);
  assert.equal(invitations[0].project_role, input.project_role);
  assert.deepEqual(
    (await client.query("SELECT * FROM aap_identity_audit_v1.schema_capabilities")).rows,
    [{ invitation_project_role: input.prepared }],
  );
  await client.query("ROLLBACK");
  process.stdout.write(
    JSON.stringify({
      ok: true,
      read_only: true,
      private_identity_denied: true,
      prepared: input.prepared,
    }),
  );
} finally {
  await client.end();
}
