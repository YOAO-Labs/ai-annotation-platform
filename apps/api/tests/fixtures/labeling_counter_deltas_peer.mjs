import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url, connectionTimeoutMillis: 5000 });
const tx = "SELECT pg_backend_pid() AS pid,txid_current()::text AS xid";
const counters =
  "SELECT completed_tasks,review_tasks FROM aap_labeling_v1.project_catalog_counters WHERE project_id=$1::uuid";
try {
  await client.connect();
  assert.equal(
    (await client.query("SELECT current_user AS role")).rows[0].role,
    "identity_api_consumer",
  );
  await assert.rejects(client.query("SELECT id FROM public.projects LIMIT 1"), { code: "42501" });
  await assert.rejects(client.query("UPDATE public.projects SET total_tasks=0 WHERE false"), {
    code: "42501",
  });
  await client.query("BEGIN");
  const before = (await client.query(tx)).rows[0];
  assert.equal(
    (
      await client.query(
        "SELECT aap_labeling_v1.decrement_project_review_count($1::uuid) AS count",
        [input.project_id],
      )
    ).rows[0].count,
    6,
  );
  assert.equal(
    (
      await client.query(
        "SELECT aap_labeling_v1.decrement_project_completed_count($1::uuid) AS count",
        [input.project_id],
      )
    ).rows[0].count,
    10,
  );
  assert.deepEqual(
    (
      await client.query("SELECT * FROM aap_labeling_v1.record_project_review_approval($1::uuid)", [
        input.project_id,
      ])
    ).rows,
    [{ completed_tasks: 11, review_tasks: 5 }],
  );
  assert.deepEqual((await client.query(counters, [input.project_id])).rows, [
    { completed_tasks: 11, review_tasks: 5 },
  ]);
  assert.equal(
    (
      await client.query(
        "SELECT updated_at=now() AS changed FROM aap_workspace_v1.project_facts WHERE id=$1::uuid",
        [input.project_id],
      )
    ).rows[0].changed,
    true,
  );
  assert.deepEqual((await client.query(tx)).rows[0], before);
  await client.query("ROLLBACK");
  assert.deepEqual((await client.query(counters, [input.project_id])).rows, [
    { completed_tasks: 11, review_tasks: 7 },
  ]);
  process.stdout.write(JSON.stringify({ ok: true, transaction_unchanged: true, rollback: true }));
} catch (error) {
  process.stderr.write(`Labeling counter delta peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
}
