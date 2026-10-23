import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.resolve(
  here,
  "../../../jobs/src/aap_jobs/notification_scope.schema.json",
);
const columns = ["id", "kind", "scope_project_id"];

async function loadContract() {
  return JSON.parse(await readFile(contractPath, "utf8"));
}

export async function selfCheck() {
  const contract = await loadContract();
  const relation = contract["x-postgresql-relations"]?.["aap_jobs_v1.notification_scope_facts"];
  assert.equal(contract.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(contract.$id, "aap-jobs-notification-scope-facts/v1");
  assert.equal(contract.additionalProperties, false);
  assert.deepEqual(Object.keys(contract.properties), columns);
  assert.deepEqual(contract.required, columns);
  assert.equal(contract.properties.kind.type, "string");
  assert.equal(contract.properties.kind.maxLength, 40);
  assert.equal(contract.properties.scope_project_id.anyOf[1].type, "null");
  assert.deepEqual(relation.columns, columns);
  assert.deepEqual(relation.column_types, {
    id: "uuid",
    kind: "character varying(40)",
    scope_project_id: "uuid",
  });
  assert.deepEqual(relation.nullable_columns, ["scope_project_id"]);
  assert.deepEqual(relation.row_key, ["id"]);
  assert.equal(relation.read_only, true);
  assert.equal(relation.security_barrier, true);
  process.stdout.write("Jobs notification-scope schema self-check passed\n");
}

async function verifyRelation() {
  const { Client } = await import("pg");
  const connectionString = process.env.JOBS_NOTIFICATION_SCOPE_DATABASE_URL;
  const jobId = process.env.JOBS_NOTIFICATION_SCOPE_JOB_ID;
  const expectedKind = process.env.JOBS_NOTIFICATION_SCOPE_EXPECTED_KIND;
  const expectedProjectId = process.env.JOBS_NOTIFICATION_SCOPE_EXPECTED_PROJECT_ID;
  const expectedRole = process.env.JOBS_NOTIFICATION_SCOPE_EXPECTED_ROLE;
  const setRole = process.env.JOBS_NOTIFICATION_SCOPE_SET_ROLE;
  assert.ok(connectionString, "missing capability-only Jobs relation DSN");
  assert.ok(expectedRole, "missing expected capability-only role");
  assert.match(jobId ?? "", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  assert.ok(expectedKind && expectedKind.length <= 40);
  assert.ok(expectedProjectId !== undefined, "missing expected project-scope value");
  if (expectedProjectId !== "null") {
    assert.match(
      expectedProjectId,
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
    );
  }
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  await client.connect();
  try {
    if (setRole) {
      assert.match(setRole, /^[a-z_][a-z0-9_]{0,62}$/);
      await client.query(`SET ROLE ${setRole}`);
    }
    const role = await client.query("SELECT current_user AS role");
    assert.equal(role.rows[0].role, expectedRole);
    await client.query("BEGIN");
    const metadata = await client.query(
      "SELECT column_name, data_type, character_maximum_length " +
        "FROM information_schema.columns " +
        "WHERE table_schema='aap_jobs_v1' AND table_name='notification_scope_facts' " +
        "ORDER BY ordinal_position",
    );
    assert.deepEqual(
      metadata.rows.map((row) => [
        row.column_name,
        row.data_type,
        row.character_maximum_length === null ? null : Number(row.character_maximum_length),
      ]),
      [
        ["id", "uuid", null],
        ["kind", "character varying", 40],
        ["scope_project_id", "uuid", null],
      ],
    );
    const viewOptions = await client.query(
      "SELECT reloptions FROM pg_catalog.pg_class " +
        "WHERE oid='aap_jobs_v1.notification_scope_facts'::pg_catalog.regclass",
    );
    assert.ok(viewOptions.rows[0]?.reloptions?.includes("security_barrier=true"));
    const result = await client.query(
      "SELECT id, kind, scope_project_id FROM aap_jobs_v1.notification_scope_facts WHERE id=$1",
      [jobId],
    );
    assert.equal(result.rows.length, 1);
    assert.deepEqual(Object.keys(result.rows[0]), columns);
    assert.equal(result.rows[0].id, jobId);
    assert.equal(result.rows[0].kind, expectedKind);
    assert.equal(
      result.rows[0].scope_project_id,
      expectedProjectId === "null" ? null : expectedProjectId,
    );

    async function assertDenied(statement, parameters = []) {
      const savepoint = `denied_jobs_scope_${randomUUID().replaceAll("-", "")}`;
      await client.query(`SAVEPOINT ${savepoint}`);
      await assert.rejects(client.query(statement, parameters), (error) => error.code === "42501");
      await client.query(`ROLLBACK TO SAVEPOINT ${savepoint}`);
      await client.query(`RELEASE SAVEPOINT ${savepoint}`);
    }

    await assertDenied("SELECT id FROM public.async_jobs WHERE id=$1", [jobId]);
    await assertDenied("INSERT INTO public.async_jobs (id, kind) VALUES ($1, 'scope_probe')", [
      randomUUID(),
    ]);
    await assertDenied("UPDATE public.async_jobs SET kind='scope_probe' WHERE id=$1", [jobId]);
    await assertDenied("DELETE FROM public.async_jobs WHERE id=$1", [jobId]);
    await assertDenied(
      "INSERT INTO aap_jobs_v1.notification_scope_facts (id, kind) VALUES ($1, 'future_probe')",
      [randomUUID()],
    );
    await assertDenied(
      "UPDATE aap_jobs_v1.notification_scope_facts SET kind='changed' WHERE id=$1",
      [jobId],
    );
    await assertDenied("DELETE FROM aap_jobs_v1.notification_scope_facts WHERE id=$1", [jobId]);
    await client.query("ROLLBACK");
  } finally {
    await client.end();
  }
  process.stdout.write("Jobs notification-scope role check passed\n");
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else {
  await verifyRelation();
}
