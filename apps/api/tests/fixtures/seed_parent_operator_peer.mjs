import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url });
const accountId = randomUUID(),
  memberId = randomUUID();
const email = `node-seed-${randomUUID()}`;
try {
  await client.connect();
  for (const table of ["users", "projects", "project_members"]) {
    await assert.rejects(client.query(`SELECT id FROM public.${table}`), { code: "42501" });
  }
  await client.query("BEGIN");
  const account = (
    await client.query("SELECT aap_identity_v1.create_seed_persona_account($1::jsonb) AS reply", [
      {
        schema: "aap-identity-seed-persona/v1",
        account_id: accountId,
        email,
        name: "Node persona",
        password_hash: input.password_hash,
        role: "employee",
        group_name: "Node group",
      },
    ])
  ).rows[0].reply;
  assert.deepEqual(account, {
    schema: "aap-identity-seed-persona/v1",
    account_id: accountId,
    email,
  });
  const result = (
    await client.query("SELECT aap_workspace_v1.write_seed_members($1::jsonb) AS reply", [
      {
        schema: "aap-workspace-seed-members/v1",
        operations: [
          {
            kind: "insert",
            id: memberId,
            project_id: input.project_id,
            user_id: accountId,
            role: "annotator",
            assigned_by: input.actor_id,
          },
          {
            kind: "update",
            id: memberId,
            role: "reviewer",
            assigned_by: input.actor_id,
            write_role: true,
            write_assigned_by: false,
          },
        ],
      },
    ])
  ).rows[0].reply;
  assert.equal(result.schema, "aap-workspace-seed-members/v1");
  assert.deepEqual(
    result.results.map(({ kind, id }) => ({ kind, id })),
    [
      { kind: "insert", id: memberId },
      { kind: "update", id: memberId },
    ],
  );
  assert.equal(result.results[1].found, true);
  assert.ok(result.results.every((reply) => Number.isFinite(Date.parse(reply.updated_at))));
  const fact = (
    await client.query(
      "SELECT role,version,weekly_target FROM aap_workspace_v1.project_member_facts WHERE id=$1",
      [memberId],
    )
  ).rows[0];
  assert.deepEqual(fact, { role: "reviewer", version: 1, weekly_target: null });
  await client.query("ROLLBACK");
  assert.equal(
    (
      await client.query(
        "SELECT count(*)::int AS count FROM aap_identity_v1.account_profiles WHERE id=$1",
        [accountId],
      )
    ).rows[0].count,
    0,
  );
  assert.equal(
    (
      await client.query(
        "SELECT count(*)::int AS count FROM aap_workspace_v1.project_member_facts WHERE id=$1",
        [memberId],
      )
    ).rows[0].count,
    0,
  );
  process.stdout.write(JSON.stringify({ ok: true, capabilities: 2, joint_rollback: true }));
} finally {
  await client.end();
}
