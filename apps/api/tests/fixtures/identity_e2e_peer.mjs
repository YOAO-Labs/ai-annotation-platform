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
  for (const name of [
    "users",
    "user_invitations",
    "password_reset_tokens",
    "organization_members",
  ]) {
    await assert.rejects(client.query(`SELECT * FROM public.${name}`), { code: "42501" });
  }
  await assert.rejects(client.query("UPDATE public.users SET status='offline' WHERE false"), {
    code: "42501",
  });
  await client.query("BEGIN");
  const create = () =>
    client.query(
      "SELECT aap_identity_fixture_v1.create_account($1::uuid,$2::text,$3::text,$4::text,$5::text) AS id",
      [input.peer_id, "peer-source@e2e.test", "原样 🚙", "opaque-fixture-hash", "employee"],
    );
  assert.equal((await create()).rows[0].id, input.peer_id);
  await client.query("SAVEPOINT duplicate_account");
  await assert.rejects(create(), { code: "23505" });
  await client.query("ROLLBACK TO SAVEPOINT duplicate_account");
  const row = (
    await client.query("SELECT * FROM aap_identity_fixture_v1.account_profiles WHERE id=$1::uuid", [
      input.peer_id,
    ])
  ).rows[0];
  assert.deepEqual(Object.keys(row).sort(), [...input.fields].sort());
  assert.equal(row.name, "原样 🚙");
  assert.equal(row.role, "employee");
  assert.equal(row.status, "offline");
  assert.equal(row.is_active, true);
  assert.deepEqual(row.preferences, {});
  for (const key of input.fields.filter(
    (key) =>
      !["id", "email", "name", "role", "status", "is_active", "preferences", "created_at"].includes(
        key,
      ),
  ))
    assert.equal(row[key], null, key);
  for (let repeat = 0; repeat < 2; repeat++) {
    assert.equal(
      (
        await client.query(
          "SELECT aap_identity_fixture_v1.mark_manual_inactive($1::uuid,'2025-02-03T04:05:06Z'::timestamptz,$2::uuid) AS changed",
          [input.peer_id, input.target_id],
        )
      ).rows[0].changed,
      1,
    );
  }
  const invitation = {
    id: input.invitation_id,
    email: "peer-invite@example.test",
    role: "reviewer",
    token: "peer-e2e-invitation",
    expires_at: "2025-02-03T04:05:06Z",
    invited_by: input.peer_id,
    accepted_at: "2025-02-02T00:00:00Z",
    accepted_user_id: input.target_id,
  };
  await client.query("SAVEPOINT invalid_invitation");
  await assert.rejects(
    client.query("SELECT aap_identity_fixture_v1.create_invitations($1::jsonb)", [
      JSON.stringify([{ ...invitation, unexpected: true }]),
    ]),
    { code: "22023" },
  );
  await client.query("ROLLBACK TO SAVEPOINT invalid_invitation");
  assert.equal(
    (
      await client.query(
        "SELECT aap_identity_fixture_v1.create_invitations($1::jsonb) AS changed",
        [JSON.stringify([invitation])],
      )
    ).rows[0].changed,
    1,
  );
  assert.equal(
    (
      await client.query(
        "SELECT aap_identity_fixture_v1.delete_reset_tokens($1::uuid[]) AS changed",
        [[input.target_id]],
      )
    ).rows[0].changed,
    1,
  );
  assert.equal(
    (
      await client.query(
        "SELECT aap_identity_fixture_v1.delete_organization_members($1::uuid[]) AS changed",
        [[input.target_id]],
      )
    ).rows[0].changed,
    1,
  );
  assert.equal(
    (
      await client.query(
        "SELECT aap_identity_fixture_v1.delete_invitations($1::uuid[]) AS changed",
        [[input.target_id, input.peer_id]],
      )
    ).rows[0].changed,
    2,
  );
  assert.equal(
    (
      await client.query(
        "SELECT aap_identity_fixture_v1.delete_accounts(false,$1::text[]) AS changed",
        [["peer-source@e2e.test"]],
      )
    ).rows[0].changed,
    1,
  );
  await client.query("ROLLBACK");
  process.stdout.write(
    JSON.stringify({
      ok: true,
      private_identity_denied: true,
      duplicate_preserved: true,
      full_profile_fields: input.fields.length,
      rolled_back: true,
    }),
  );
} finally {
  await client.end();
}
