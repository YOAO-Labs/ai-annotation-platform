import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const jobsRoot = path.resolve(here, "../../src/aap_jobs");
const schemaPath = path.join(jobsRoot, "fixture_filter.schema.json");
const sqlPath = path.join(jobsRoot, "sql/0331_jobs_filter_fixtures.sql");
const apiSchemaPath = path.resolve(
  here,
  "../../../api/alembic/sql/0331_jobs_filter_fixtures.schema.json",
);
const apiSqlPath = path.resolve(here, "../../../api/alembic/sql/0331_jobs_filter_fixtures.sql");
const functionName = "aap_jobs_v1.set_filter_fixture_job_outcomes";
const columns = [
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

let phase = "startup";

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`missing ${name}`);
  return value;
}

function uuidText(value, name) {
  assert.match(value, /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i, name);
  return value.toLowerCase();
}

async function selfCheck() {
  phase = "read language-neutral contract";
  const [schemaText, apiSchemaText, sql, apiSql] = await Promise.all([
    readFile(schemaPath, "utf8"),
    readFile(apiSchemaPath, "utf8"),
    readFile(sqlPath, "utf8"),
    readFile(apiSqlPath, "utf8"),
  ]);
  const schema = JSON.parse(schemaText);
  assert.deepEqual(JSON.parse(apiSchemaText), schema);
  assert.equal(sql, apiSql);
  assert.equal(schema.$id, "aap-jobs-filter-fixtures/v1");
  assert.deepEqual(schema.$defs.FilterFixtureJobsRequest.required.sort(), [
    "completed_job_id",
    "failed_job_id",
  ]);
  assert.equal(schema.$defs.FilterFixtureJobsCount.minimum, 2);
  assert.equal(schema.$defs.FilterFixtureJobsCount.maximum, 2);
  const fn = schema["x-postgresql-functions"][functionName];
  assert.deepEqual(fn, {
    arguments: "uuid, uuid",
    returns: "bigint",
    volatility: "volatile",
    parallel: "unsafe",
  });
  assert.deepEqual(schema["x-projections"], {
    completed: {
      status: "completed",
      progress_pct: 100,
      result: { success_count: 2 },
    },
    failed: {
      status: "failed",
      progress_pct: 40,
      error_message: "fixture failure",
    },
  });
  assert.match(sql, /CREATE FUNCTION aap_jobs_v1\.set_filter_fixture_job_outcomes\(/);
  assert.match(sql, /ORDER BY item\.id/);
  assert.match(sql, /REVOKE ALL ON FUNCTION aap_jobs_v1\.set_filter_fixture_job_outcomes\(/);
  process.stdout.write("Jobs filter-fixture Node contract self-check passed\n");
  return schema;
}

function assertRows(rows) {
  assert.equal(rows.length, 2);
  return rows;
}

function assertInitial(rows, completedId, failedId) {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const completed = byId.get(completedId);
  const failed = byId.get(failedId);
  assert.ok(completed && failed);
  assert.equal(completed.kind, "batch_predict");
  assert.equal(failed.kind, "prediction_retry");
  for (const row of [completed, failed]) {
    assert.equal(row.status, "pending");
    assert.equal(row.progress_pct, 0);
    assert.equal(row.project_id, null);
    assert.equal(row.retained_project_id, null);
    assert.equal(row.user_id, null);
    assert.equal(row.error_message, null);
    assert.equal(row.started_at, null);
    assert.equal(row.completed_at, null);
    assert.equal(Date.parse(row.updated_at), Date.parse("2001-01-01T00:00:00Z"));
    assert.deepEqual(JSON.parse(row.payload), {
      fixture: row.id === completedId ? "completed" : "failed",
      preserve: true,
    });
  }
  assert.deepEqual(JSON.parse(completed.result), {});
  assert.match(failed.result, /"large_integer"\s*:\s*9007199254740993/);
  const failedResult = JSON.parse(failed.result);
  assert.equal(failedResult.keep, "failed result");
  assert.equal(failedResult.json_null, null);
  assert.equal(failedResult.json_false, false);
  return { byId, completed, failed };
}

function assertOutcomes(rows, original, completedId, failedId) {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const completed = byId.get(completedId);
  const failed = byId.get(failedId);
  assert.ok(completed && failed);
  assert.equal(completed.status, "completed");
  assert.equal(completed.progress_pct, 100);
  assert.deepEqual(JSON.parse(completed.result), { success_count: 2 });
  assert.equal(completed.error_message, original.completed.error_message);
  assert.ok(Date.parse(completed.updated_at) > Date.parse(original.completed.updated_at));
  assert.equal(failed.status, "failed");
  assert.equal(failed.progress_pct, 40);
  assert.equal(failed.error_message, "fixture failure");
  assert.equal(failed.result, original.failed.result);
  assert.ok(Date.parse(failed.updated_at) > Date.parse(original.failed.updated_at));
  for (const [row, before] of [
    [completed, original.completed],
    [failed, original.failed],
  ]) {
    for (const field of [
      "id",
      "kind",
      "project_id",
      "retained_project_id",
      "user_id",
      "payload",
      "celery_task_id",
      "started_at",
      "completed_at",
      "created_at",
    ]) {
      assert.equal(row[field], before[field], `${field} changed for ${row.id}`);
    }
  }
}

async function expectSqlState(client, sql, parameters, code) {
  await client.query("SAVEPOINT expected_error");
  let failure;
  try {
    await client.query(sql, parameters);
  } catch (error) {
    failure = error;
  }
  assert.equal(failure?.code, code);
  await client.query("ROLLBACK TO SAVEPOINT expected_error");
  await client.query("RELEASE SAVEPOINT expected_error");
}

function assertFunctionCatalog(row, expectedRole) {
  assert.deepEqual(Object.values(row), [
    "uuid, uuid",
    "bigint",
    "v",
    "u",
    true,
    ["search_path=pg_catalog, pg_temp", "TimeZone=UTC", "DateStyle=ISO, YMD"],
    "jobs_code",
    true,
    false,
  ]);
  assert.equal(expectedRole, "identity_api_consumer");
}

async function liveAcceptance() {
  phase = "load PostgreSQL driver and frozen contract";
  const schema = await selfCheck();
  const pg = await import("pg");
  const Client = pg.Client ?? pg.default?.Client;
  const types = pg.types ?? pg.default?.types;
  assert.equal(typeof Client, "function");
  assert.ok(types);
  types.setTypeParser(20, (value) => value);
  types.setTypeParser(3802, (value) => value);
  types.setTypeParser(1184, (value) => value);

  phase = "validate independent LOGIN target";
  const connectionString = required("JOBS_FILTER_FIXTURE_DATABASE_URL");
  const expectedRole = required("JOBS_FILTER_FIXTURE_EXPECTED_ROLE");
  const expectedDatabase = required("JOBS_FILTER_FIXTURE_EXPECTED_DATABASE");
  const expectedHost = required("JOBS_FILTER_FIXTURE_EXPECTED_HOST");
  const expectedPort = required("JOBS_FILTER_FIXTURE_EXPECTED_PORT");
  const completedId = uuidText(
    required("JOBS_FILTER_FIXTURE_COMPLETED_JOB_ID"),
    "completed job id",
  );
  const failedId = uuidText(required("JOBS_FILTER_FIXTURE_FAILED_JOB_ID"), "failed job id");
  assert.notEqual(completedId, failedId);
  const url = new URL(connectionString);
  assert.equal(decodeURIComponent(url.username), expectedRole);
  assert.equal(decodeURIComponent(url.pathname.slice(1)), expectedDatabase);
  assert.ok(expectedDatabase.endsWith("_test") || expectedDatabase.endsWith("_e2e"));
  assert.equal(url.hostname.replace(/^\[|\]$/g, "").toLowerCase(), expectedHost.toLowerCase());
  assert.equal(url.port || "5432", expectedPort);

  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  let connected = false;
  let inTransaction = false;
  try {
    await client.connect();
    connected = true;
    phase = "prove actual LOGIN and ordinary ACLs";
    const identity = await client.query(
      "SELECT current_user::text AS current_role,session_user::text AS login_role, " +
        "current_database()::text AS database_name",
    );
    assert.deepEqual(identity.rows[0], {
      current_role: expectedRole,
      login_role: expectedRole,
      database_name: expectedDatabase,
    });
    const privileges = await client.query(
      "SELECT pg_catalog.has_table_privilege(current_user,'public.async_jobs','SELECT') AS can_select, " +
        "pg_catalog.has_table_privilege(current_user,'public.async_jobs','INSERT') AS can_insert, " +
        "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','INSERT') AS can_column_insert, " +
        "pg_catalog.has_table_privilege(current_user,'public.async_jobs','UPDATE') AS can_update, " +
        "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','UPDATE') AS can_column_update, " +
        "pg_catalog.has_table_privilege(current_user,'public.async_jobs','DELETE') AS can_delete, " +
        "pg_catalog.has_function_privilege(current_user,$1::text,'EXECUTE') AS can_execute",
      [`${functionName}(uuid,uuid)`],
    );
    assert.deepEqual(Object.values(privileges.rows[0]), [
      true,
      false,
      false,
      false,
      false,
      false,
      true,
    ]);

    phase = "inspect function ABI and grants";
    const catalog = await client.query(
      "SELECT pg_catalog.oidvectortypes(p.proargtypes) AS arguments, " +
        "pg_catalog.pg_get_function_result(p.oid) AS returns,p.provolatile::text AS volatility, " +
        "p.proparallel::text AS parallel,p.prosecdef,p.proconfig, " +
        "pg_catalog.pg_get_userbyid(p.proowner) AS owner_name, " +
        "pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') AS can_execute, " +
        "EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(p.proacl, " +
        "pg_catalog.acldefault('f',p.proowner)) ) acl " +
        "WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') AS public_execute " +
        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace " +
        "WHERE n.nspname='aap_jobs_v1' AND p.proname='set_filter_fixture_job_outcomes'",
    );
    assert.equal(catalog.rows.length, 1);
    assertFunctionCatalog(catalog.rows[0], expectedRole);
    assert.equal(schema["x-postgresql-functions"][functionName].returns, "bigint");

    async function readRows(ids) {
      const result = await client.query(
        "SELECT id::text,kind,project_id::text,retained_project_id::text,user_id::text, " +
          "status,progress_pct,payload,result,error_message,celery_task_id,started_at, " +
          "completed_at,created_at,updated_at FROM public.async_jobs " +
          "WHERE id=ANY($1::pg_catalog.uuid[]) ORDER BY id",
        [ids],
      );
      assert.deepEqual(
        result.fields.map((field) => field.name),
        columns,
      );
      return result.rows;
    }

    phase = "verify test fixture and invalid requests";
    const initialRows = assertRows(await readRows([completedId, failedId]));
    const original = assertInitial(initialRows, completedId, failedId);
    await client.query("BEGIN");
    inTransaction = true;
    const call = `SELECT ${functionName}($1::pg_catalog.uuid,$2::pg_catalog.uuid) AS changed`;
    await expectSqlState(client, call, [null, failedId], "22023");
    await expectSqlState(client, call, [completedId, completedId], "22023");
    await expectSqlState(
      client,
      call,
      [completedId, "ffffffff-ffff-ffff-ffff-ffffffffffff"],
      "P0002",
    );
    assert.deepEqual(assertRows(await readRows([completedId, failedId])), initialRows);
    await client.query("ROLLBACK");
    inTransaction = false;

    phase = "deny direct private updates";
    await client.query("BEGIN");
    inTransaction = true;
    await expectSqlState(
      client,
      "UPDATE public.async_jobs SET status='running',updated_at=pg_catalog.now() WHERE id=$1::uuid",
      [completedId],
      "42501",
    );
    await expectSqlState(
      client,
      "UPDATE public.async_jobs SET progress_pct=9 WHERE id=$1::uuid",
      [completedId],
      "42501",
    );
    await client.query("ROLLBACK");
    inTransaction = false;

    phase = "preserve caller rollback";
    await client.query("BEGIN");
    inTransaction = true;
    const rolledBack = await client.query(call, [completedId, failedId]);
    assert.equal(rolledBack.fields[0].dataTypeID, 20);
    assert.equal(rolledBack.rows[0].changed, "2");
    assertOutcomes(
      assertRows(await readRows([completedId, failedId])),
      original,
      completedId,
      failedId,
    );
    await client.query("ROLLBACK");
    inTransaction = false;
    assert.deepEqual(assertRows(await readRows([completedId, failedId])), initialRows);

    phase = "preserve savepoint rollback and commit";
    await client.query("BEGIN");
    inTransaction = true;
    await client.query("SAVEPOINT fixture_outcomes");
    const savepoint = await client.query(call, [completedId, failedId]);
    assert.equal(savepoint.fields[0].dataTypeID, 20);
    assert.equal(savepoint.rows[0].changed, "2");
    assertOutcomes(
      assertRows(await readRows([completedId, failedId])),
      original,
      completedId,
      failedId,
    );
    await client.query("ROLLBACK TO SAVEPOINT fixture_outcomes");
    assert.deepEqual(assertRows(await readRows([completedId, failedId])), initialRows);
    await client.query("RELEASE SAVEPOINT fixture_outcomes");
    const committed = await client.query(call, [completedId, failedId]);
    assert.equal(committed.fields[0].dataTypeID, 20);
    assert.equal(committed.rows[0].changed, "2");
    assertOutcomes(
      assertRows(await readRows([completedId, failedId])),
      original,
      completedId,
      failedId,
    );
    await client.query("COMMIT");
    inTransaction = false;
    assertOutcomes(
      assertRows(await readRows([completedId, failedId])),
      original,
      completedId,
      failedId,
    );

    process.stdout.write(
      `${JSON.stringify({
        status: "passed",
        function: functionName,
        nativeBigint: committed.rows[0].changed,
        rowColumns: columns.length,
        unitOfWork: "rollback/savepoint/commit",
      })}\n`,
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

const live = process.argv.includes("--live");
(live ? liveAcceptance() : selfCheck()).catch((error) => {
  const code = typeof error?.code === "string" ? error.code : (error?.name ?? "error");
  process.stderr.write(`Jobs filter-fixture Node acceptance failed at ${phase} (${code})\n`);
  process.exitCode = 1;
});
