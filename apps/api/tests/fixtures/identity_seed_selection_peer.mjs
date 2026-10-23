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
  await assert.rejects(client.query("SELECT id FROM public.users"), { code: "42501" });
  await client.query("BEGIN READ ONLY");
  const admin = (
    await client.query("SELECT aap_identity_v1.read_scale_seed_super_admin() AS result")
  ).rows[0].result;
  const annotator = (
    await client.query(
      "SELECT aap_identity_v1.read_scale_seed_annotator(ARRAY(SELECT user_id FROM aap_workspace_v1.project_member_facts WHERE role='annotator')) AS result",
    )
  ).rows[0].result;
  const empty = (
    await client.query("SELECT aap_identity_v1.read_scale_seed_annotator($1::uuid[]) AS result", [
      [],
    ])
  ).rows[0].result;
  assert.deepEqual(admin, {
    schema: "aap-identity-scale-seed-actors/v1",
    kind: "super_admin",
    account_id: input.admin_id,
  });
  assert.deepEqual(annotator, {
    schema: "aap-identity-scale-seed-actors/v1",
    kind: "annotator",
    account_id: input.annotator_id,
  });
  assert.deepEqual(empty, {
    schema: "aap-identity-scale-seed-actors/v1",
    kind: "annotator",
    account_id: null,
  });
  await client.query("ROLLBACK");
  process.stdout.write(JSON.stringify({ ok: true, read_only: true, private_user_denied: true }));
} finally {
  await client.end();
}
