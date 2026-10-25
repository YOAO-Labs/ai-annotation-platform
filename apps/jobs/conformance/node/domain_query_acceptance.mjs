import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const schemaPath = path.resolve(here, "../../src/aap_jobs/domain_queries.schema.json");
const rowColumns = [
  "id",
  "kind",
  "project_id",
  "retained_project_id",
  "user_id",
  "status",
  "progress_pct",
  "payload",
  "result",
  "error_message",
  "celery_task_id",
  "started_at",
  "completed_at",
  "created_at",
  "updated_at",
];
const rowOids = [
  2950, 1043, 2950, 2950, 2950, 1043, 23, 3802, 3802, 25, 1043, 1184, 1184, 1184, 1184,
];
const functionNames = [
  "lock_cross_frame_singleflight",
  "active_cross_frame_job",
  "cross_frame_job_page",
  "cross_frame_job_count",
  "latest_batch_prediction_times",
];

let phase = "startup";

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`missing ${name}`);
  return value;
}

function parseJson(name) {
  try {
    return JSON.parse(required(name));
  } catch {
    throw new Error(`invalid ${name}`);
  }
}

function normalize(value) {
  return value.replace(/\s+/g, " ").trim();
}

function assertJobRowFields(result) {
  assert.deepEqual(
    result.fields.map((field) => field.name),
    rowColumns,
  );
  assert.deepEqual(
    result.fields.map((field) => field.dataTypeID),
    rowOids,
  );
}

function timestampSecond(value) {
  assert.equal(typeof value, "string");
  return value.slice(0, 19);
}

async function main() {
  phase = "load PostgreSQL driver and contract";
  const [pg, schemaText] = await Promise.all([import("pg"), readFile(schemaPath, "utf8")]);
  const Client = pg.Client ?? pg.default?.Client;
  const types = pg.types ?? pg.default?.types;
  assert.equal(typeof Client, "function");
  assert.ok(types);
  const schema = JSON.parse(schemaText);
  assert.equal(schema["$id"], "aap-jobs-domain-queries/v1");
  assert.deepEqual(schema["x-row-columns"], rowColumns);
  assert.deepEqual(
    Object.keys(schema["x-postgresql-functions"])
      .map((name) => name.split(".").at(-1))
      .sort(),
    [...functionNames].sort(),
  );
  types.setTypeParser(20, (value) => value);
  types.setTypeParser(1184, (value) => value);
  types.setTypeParser(2950, (value) => value);
  types.setTypeParser(3802, (value) => value);

  const connectionString = required("JOBS_DOMAIN_QUERY_DATABASE_URL");
  const expectedRole = required("JOBS_DOMAIN_QUERY_EXPECTED_ROLE");
  const expectedDatabase = required("JOBS_DOMAIN_QUERY_EXPECTED_DATABASE");
  const expectedHost = required("JOBS_DOMAIN_QUERY_EXPECTED_HOST");
  const expectedPort = required("JOBS_DOMAIN_QUERY_EXPECTED_PORT");
  const url = new URL(connectionString);
  const hostname = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  assert.equal(decodeURIComponent(url.username), expectedRole);
  assert.equal(decodeURIComponent(url.pathname.slice(1)), expectedDatabase);
  assert.ok(expectedDatabase.endsWith("_test"));
  assert.equal(hostname, expectedHost.replace(/^\[|\]$/g, "").toLowerCase());
  assert.equal(url.port || "5432", expectedPort);

  const actorId = required("JOBS_DOMAIN_QUERY_ACTOR_ID");
  const singleflightKey = required("JOBS_DOMAIN_QUERY_SINGLEFLIGHT_KEY");
  const sceneId = required("JOBS_DOMAIN_QUERY_SCENE_ID");
  const activeId = required("JOBS_DOMAIN_QUERY_EXPECTED_ACTIVE_ID");
  const pageIds = parseJson("JOBS_DOMAIN_QUERY_EXPECTED_PAGE_IDS");
  const projectIds = parseJson("JOBS_DOMAIN_QUERY_PROJECT_IDS");
  const expectedTimes = parseJson("JOBS_DOMAIN_QUERY_EXPECTED_BATCH_TIMES");
  assert.ok(Array.isArray(pageIds) && Array.isArray(projectIds));

  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  let connected = false;
  let inTransaction = false;
  try {
    await client.connect();
    connected = true;

    phase = "LOGIN and database identity";
    const identity = await client.query(
      "SELECT current_user::text AS current_role, session_user::text AS login_role, " +
        "current_database()::text AS database_name",
    );
    assert.deepEqual(identity.rows[0], {
      current_role: expectedRole,
      login_role: expectedRole,
      database_name: expectedDatabase,
    });
    const privateRights = await client.query(
      "SELECT pg_catalog.has_table_privilege(current_user,'public.async_jobs','SELECT') " +
        "AS table_select, pg_catalog.has_any_column_privilege(current_user, " +
        "'public.async_jobs','SELECT') AS column_select, " +
        "pg_catalog.has_table_privilege(current_user,'public.async_jobs','UPDATE') " +
        "AS table_update, pg_catalog.has_any_column_privilege(current_user, " +
        "'public.async_jobs','UPDATE') AS column_update",
    );
    assert.deepEqual(
      [
        privateRights.rows[0].table_select,
        privateRights.rows[0].column_select,
        privateRights.rows[0].table_update,
        privateRights.rows[0].column_update,
      ],
      [false, false, false, false],
    );

    phase = "function ABI and grants";
    const catalog = await client.query(
      "SELECT p.proname, pg_catalog.oidvectortypes(p.proargtypes) AS arguments, " +
        "pg_catalog.pg_get_function_result(p.oid) AS returns, p.provolatile, " +
        "p.prosecdef, p.proconfig, pg_catalog.pg_get_userbyid(p.proowner) AS owner_name, " +
        "pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') AS can_execute, " +
        "EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(p.proacl, " +
        "pg_catalog.acldefault('f',p.proowner))) AS acl " +
        "WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') AS public_execute " +
        "FROM pg_catalog.pg_proc AS p JOIN pg_catalog.pg_namespace AS n " +
        "ON n.oid=p.pronamespace WHERE n.nspname='aap_jobs_v1' " +
        "AND p.proname = ANY($1::pg_catalog.text[]) ORDER BY p.proname",
      [functionNames],
    );
    assert.equal(catalog.rows.length, 5);
    for (const row of catalog.rows) {
      const signature = `aap_jobs_v1.${row.proname}`;
      const expected = schema["x-postgresql-functions"][signature];
      assert.ok(expected);
      assert.equal(normalize(row.arguments), normalize(expected.arguments));
      assert.equal(normalize(row.returns), normalize(expected.returns));
      assert.equal(row.provolatile, expected.volatility === "stable" ? "s" : "v");
      assert.equal(row.prosecdef, true);
      assert.ok(row.proconfig?.includes("search_path=pg_catalog, pg_temp"));
      assert.equal(row.owner_name, "jobs_code");
      assert.equal(row.can_execute, true);
      assert.equal(row.public_execute, false);
    }

    phase = "transactional singleflight lock";
    await client.query("BEGIN");
    inTransaction = true;
    const lock = await client.query(
      "SELECT aap_jobs_v1.lock_cross_frame_singleflight($1::pg_catalog.uuid, $2::pg_catalog.text)",
      [actorId, singleflightKey],
    );
    assert.equal(lock.fields[0].dataTypeID, 2278);

    phase = "active cross-frame result";
    const active = await client.query(
      "SELECT * FROM aap_jobs_v1.active_cross_frame_job($1::pg_catalog.uuid, $2::pg_catalog.text)",
      [actorId, singleflightKey],
    );
    assertJobRowFields(active);
    assert.equal(active.rows.length, 1);
    assert.equal(active.rows[0].id, activeId);
    assert.equal(active.rows[0].status, "running");
    assert.equal(typeof active.rows[0].payload, "string");
    assert.match(active.rows[0].payload, /"opaque_counter"\s*:\s*9007199254740993(?:,|})/);
    assert.match(active.rows[0].payload, new RegExp(`"scene_id"\\s*:\\s*"${sceneId}"`));

    phase = "cross-frame page and count";
    const page = await client.query(
      "SELECT * FROM aap_jobs_v1.cross_frame_job_page(" +
        "$1::pg_catalog.uuid, $2::pg_catalog.text, $3::pg_catalog.int4)",
      [actorId, sceneId, 50],
    );
    assertJobRowFields(page);
    assert.deepEqual(
      page.rows.map((row) => row.id),
      pageIds,
    );
    for (const row of page.rows) {
      assert.equal(typeof row.payload, "string");
      assert.equal(typeof row.result, "string");
      assert.equal(typeof row.created_at, "string");
      assert.equal(typeof row.updated_at, "string");
    }
    const count = await client.query(
      "SELECT aap_jobs_v1.cross_frame_job_count($1::pg_catalog.uuid, $2::pg_catalog.text)",
      [actorId, sceneId],
    );
    assert.equal(count.fields[0].dataTypeID, 20);
    assert.equal(typeof count.rows[0].cross_frame_job_count, "string");
    assert.equal(count.rows[0].cross_frame_job_count, String(pageIds.length));

    phase = "latest batch prediction tuples";
    const latest = await client.query(
      "SELECT * FROM aap_jobs_v1.latest_batch_prediction_times($1::pg_catalog.uuid[])",
      [projectIds],
    );
    assert.deepEqual(
      latest.fields.map((field) => field.name),
      ["project_id", "latest_created_at"],
    );
    assert.deepEqual(
      latest.fields.map((field) => field.dataTypeID),
      [2950, 1184],
    );
    const actualTimes = latest.rows
      .map((row) => [row.project_id, timestampSecond(row.latest_created_at)])
      .sort(([left], [right]) => left.localeCompare(right));
    const expectedTimeRows = Object.entries(expectedTimes)
      .map(([id, value]) => [id, value.slice(0, 19).replace("T", " ")])
      .sort(([left], [right]) => left.localeCompare(right));
    assert.deepEqual(actualTimes, expectedTimeRows);

    await client.query("ROLLBACK");
    inTransaction = false;
    process.stdout.write(
      `${JSON.stringify({ status: "passed", functions: 5, rowColumns: rowColumns.length, rows: page.rows.length })}\n`,
    );
  } finally {
    if (connected && inTransaction) {
      try {
        await client.query("ROLLBACK");
      } catch {
        // The failed transaction is discarded when the client disconnects.
      }
    }
    if (connected) await client.end();
  }
}

main().catch((error) => {
  const code = typeof error?.code === "string" ? error.code : (error?.name ?? "error");
  process.stderr.write(`Jobs domain-query Node acceptance failed at ${phase} (${code})\n`);
  process.exitCode = 1;
});
