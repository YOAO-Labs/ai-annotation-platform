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
  for (const sql of [
    "SELECT id FROM public.users",
    "UPDATE public.users SET preferences='{}'::jsonb WHERE false",
  ])
    await assert.rejects(client.query(sql), { code: "42501" });
  await client.query("BEGIN");
  const write = async (id, prefs) =>
    (
      await client.query(
        "SELECT aap_identity_v1.replace_fixture_preferences($1::uuid,$2::jsonb) AS updated",
        [id, JSON.stringify(prefs)],
      )
    ).rows[0].updated;
  assert.equal(await write(input.user_id, { node: { unknown: ["完整替换", null, false] } }), 1);
  assert.equal(await write(input.user_id, { node: { unknown: ["完整替换", null, false] } }), 1);
  assert.equal(await write(null, {}), 0);
  assert.equal(await write("00000000-0000-0000-0000-000000000000", {}), 0);
  await client.query("SAVEPOINT invalid_object");
  await assert.rejects(write(input.user_id, []), { code: "22023" });
  await client.query("ROLLBACK TO SAVEPOINT invalid_object");
  await client.query("ROLLBACK");
  process.stdout.write(
    JSON.stringify({
      ok: true,
      private_user_denied: true,
      rolled_back: true,
      updated: [1, 1, 0, 0],
    }),
  );
} finally {
  await client.end();
}
