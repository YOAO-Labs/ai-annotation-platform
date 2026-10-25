import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(
  new URL("../../../workspace/conformance/node/package.json", import.meta.url),
);
const { Client } = require("pg");
const input = JSON.parse(readFileSync(0, "utf8"));
const client = new Client({ connectionString: input.database_url, connectionTimeoutMillis: 5000 });

try {
  await client.connect();
  assert.equal(
    (await client.query("SELECT current_user AS role")).rows[0].role,
    "identity_api_consumer",
  );
  for (const table of ["users", "projects", "project_members"]) {
    await assert.rejects(client.query(`SELECT id FROM public.${table} LIMIT 1`), { code: "42501" });
  }
  await client.query("BEGIN READ ONLY");
  const before = (await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid"))
    .rows[0];
  const initial = (
    await client.query(
      "SELECT aap_workspace_v1.project_access_step($1::uuid,$2::uuid,'read_project_row') AS reply",
      [input.actor_id, input.project_id],
    )
  ).rows[0].reply;
  assert.equal(initial.schema, "aap-workspace-project-access-step/v1");
  assert.equal(initial.actor_id, input.actor_id);
  assert.equal(initial.project_id, input.project_id);
  assert.equal(initial.phase, "read_project_row");
  assert.equal(JSON.parse(initial.facts.row_json).id, input.project_id);
  const actor = (
    await client.query("SELECT * FROM aap_identity_v1.read_actor_facts($1::uuid)", [input.actor_id])
  ).rows[0];
  assert.equal(actor.actor_id, input.actor_id);
  assert.equal(actor.is_active, true);
  const request = {
    schema: "aap-workspace-project-query/v1",
    request_id: randomUUID(),
    operation: "get_project_access",
    admitted_actor: { id: actor.actor_id, role: actor.platform_role },
    target_id: input.project_id,
    filters_ascii: null,
    pagination: null,
    projection_evidence: null,
  };
  assert.ok(
    (await client.query("SELECT aap_workspace_project_query_v1.project_query_policy() AS policy"))
      .rows[0].policy,
  );
  for (const action of ["get_row", "access_member_rows"]) {
    const reply = (
      await client.query(
        "SELECT aap_workspace_project_query_v1.project_query_step($1::jsonb,$2::text,NULL::jsonb,NULL::text,NULL::text,NULL::text[],NULL::text[],NULL::uuid,NULL::text,NULL::timestamptz,NULL::timestamptz,NULL::bigint,NULL::integer) AS reply",
        [JSON.stringify(request), action],
      )
    ).rows[0].reply;
    assert.equal(reply.schema, request.schema);
    assert.deepEqual(reply.request, request);
    assert.equal(reply.action, action);
    assert.equal(reply.parameters, null);
    if (action === "get_row") {
      assert.equal(JSON.parse(reply.facts.row_json).id, input.project_id);
    } else {
      const rows = JSON.parse(reply.facts.rows_json);
      assert.equal(rows.length, 1);
      assert.equal(rows[0].id, input.membership_id);
      assert.equal(rows[0].user_id, input.actor_id);
      assert.equal(rows[0].project_id, input.project_id);
      assert.equal(rows[0].role, "annotator");
    }
  }
  assert.deepEqual(
    (await client.query("SELECT pg_backend_pid() AS pid,txid_current()::text AS xid")).rows[0],
    before,
  );
  assert.equal(
    (await client.query("SHOW transaction_read_only")).rows[0].transaction_read_only,
    "on",
  );
  await client.query("ROLLBACK");
  process.stdout.write(JSON.stringify({ ok: true, transaction_unchanged: true, read_only: true }));
} catch (error) {
  process.stderr.write(`Workspace unlocked peer failed (${error.code ?? error.name})\n`);
  process.exitCode = 1;
} finally {
  await client.end();
}
