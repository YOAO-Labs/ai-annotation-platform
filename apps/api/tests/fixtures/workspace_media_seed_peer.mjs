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
const schema = "aap-workspace-media-seed-project/v1";
const assignedAt = "2026-06-01T12:00:00.000Z";
const projectIds = [];
const displayIds = [];
try {
  await client.connect();
  for (const relation of ["projects", "project_members"]) {
    await assert.rejects(client.query(`SELECT id FROM public.${relation}`), { code: "42501" });
  }
  await client.query("BEGIN");
  for (const kind of ["coco8", "video", "ocr", "large_image"]) {
    const projectId = randomUUID();
    const displayId = `P-NM-${randomUUID().slice(0, 8)}`;
    const created = (
      await client.query(
        "SELECT aap_workspace_v1.create_media_seed_project($1,$2,$3,$4,$5,$6::jsonb) AS reply",
        [projectId, displayId, "Node media", input.owner_id, kind, {}],
      )
    ).rows[0].reply;
    assert.deepEqual(created, { schema, kind, project_id: projectId, display_id: displayId });
    projectIds.push(projectId);
    displayIds.push(displayId);
  }
  const patched = (
    await client.query(
      "SELECT aap_workspace_v1.write_seed_project_projection($1,$2::jsonb) AS reply",
      [projectIds[0], { name: "Node changed", updated_at: assignedAt }],
    )
  ).rows[0].reply;
  assert.deepEqual(Object.keys(patched).sort(), ["found", "project_id", "schema", "updated_at"]);
  assert.equal(patched.schema, schema);
  assert.equal(patched.project_id, projectIds[0]);
  assert.equal(patched.found, true);
  assert.equal(Date.parse(patched.updated_at), Date.parse(assignedAt));

  const memberId = randomUUID();
  await client.query("SELECT aap_workspace_v1.write_seed_members($1::jsonb)", [
    {
      schema: "aap-workspace-seed-members/v1",
      operations: [
        {
          kind: "insert",
          id: memberId,
          project_id: projectIds[0],
          user_id: input.member_user_id,
          role: "annotator",
          assigned_by: input.owner_id,
        },
      ],
    },
  ]);
  const member = (
    await client.query(
      "SELECT aap_workspace_v1.pin_seed_member_assignment($1,$2::timestamptz) AS reply",
      [memberId, assignedAt],
    )
  ).rows[0].reply;
  assert.deepEqual(Object.keys(member).sort(), [
    "assigned_at",
    "found",
    "member_id",
    "schema",
    "updated_at",
  ]);
  assert.equal(member.schema, "aap-workspace-seed-member-assignment/v1");
  assert.equal(member.member_id, memberId);
  assert.equal(member.found, true);
  assert.equal(Date.parse(member.assigned_at), Date.parse(assignedAt));
  assert.ok(Number.isFinite(Date.parse(member.updated_at)));
  const missingId = randomUUID();
  const missing = (
    await client.query(
      "SELECT aap_workspace_v1.pin_seed_member_assignment($1,$2::timestamptz) AS reply",
      [missingId, assignedAt],
    )
  ).rows[0].reply;
  assert.deepEqual(missing, {
    schema: member.schema,
    member_id: missingId,
    found: false,
    assigned_at: null,
    updated_at: null,
  });
  await client.query("SAVEPOINT malformed");
  await assert.rejects(
    client.query("SELECT aap_workspace_v1.write_seed_project_projection($1,$2::jsonb)", [
      projectIds[0],
      { total_tasks: 99 },
    ]),
    { code: "22023" },
  );
  await client.query("ROLLBACK TO SAVEPOINT malformed");
  await client.query("ROLLBACK");
  for (const displayId of displayIds) {
    const found = (
      await client.query("SELECT aap_workspace_v1.read_import_project($1) AS reply", [displayId])
    ).rows[0].reply;
    assert.equal(found.project_id, null);
  }
  process.stdout.write(
    JSON.stringify({
      ok: true,
      media_kinds: 4,
      new_functions: 3,
      private_tables_denied: true,
      rolled_back: true,
    }),
  );
} finally {
  await client.end();
}
