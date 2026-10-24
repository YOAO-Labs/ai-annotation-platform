import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const jobsRoot = path.resolve(here, "../../src/aap_jobs");
const schemaPath = path.join(jobsRoot, "fixture_cleanup.schema.json");
const sqlPath = path.join(jobsRoot, "sql/0330_jobs_fixture_cleanup.sql");
const apiSchemaPath = path.resolve(
  here,
  "../../../api/alembic/sql/0330_jobs_fixture_cleanup.schema.json",
);
const apiSqlPath = path.resolve(here, "../../../api/alembic/sql/0330_jobs_fixture_cleanup.sql");
const functionName = "aap_jobs_v1.delete_fixture_project_jobs";
const jobColumns = ["id", "project_id", "retained_project_id", "kind", "status", "payload"];

let phase = "startup";

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`missing ${name}`);
  return value;
}

function parseArray(name) {
  let value;
  try {
    value = JSON.parse(required(name));
  } catch {
    throw new Error(`${name} must be a JSON array`);
  }
  assert.ok(Array.isArray(value), `${name} must be a JSON array`);
  return value;
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
  assert.equal(schema.$id, "aap-jobs-fixture-cleanup/v1");
  const fn = schema["x-postgresql-functions"][functionName];
  assert.equal(fn.arguments, "uuid[]");
  assert.equal(fn.returns, "bigint");
  assert.equal(fn.volatility, "volatile");
  assert.match(sql, /CREATE FUNCTION aap_jobs_v1\.delete_fixture_project_jobs\(/);
  assert.match(sql, /REVOKE ALL ON FUNCTION aap_jobs_v1\.delete_fixture_project_jobs\(/);
  process.stdout.write("Jobs fixture-cleanup contract self-check passed\n");
}

async function liveAcceptance() {
  phase = "load PostgreSQL driver and frozen contract";
  const pg = await import("pg");
  const Client = pg.Client ?? pg.default?.Client;
  const types = pg.types ?? pg.default?.types;
  assert.equal(typeof Client, "function");
  assert.ok(types);
  types.setTypeParser(20, (value) => value);
  types.setTypeParser(3802, (value) => value);
  await selfCheck();

  phase = "validate independent LOGIN target";
  const connectionString = required("JOBS_FIXTURE_CLEANUP_DATABASE_URL");
  const expectedRole = required("JOBS_FIXTURE_CLEANUP_EXPECTED_ROLE");
  const expectedDatabase = required("JOBS_FIXTURE_CLEANUP_EXPECTED_DATABASE");
  const expectedHost = required("JOBS_FIXTURE_CLEANUP_EXPECTED_HOST");
  const expectedPort = required("JOBS_FIXTURE_CLEANUP_EXPECTED_PORT");
  const url = new URL(connectionString);
  assert.equal(decodeURIComponent(url.username), expectedRole);
  assert.equal(decodeURIComponent(url.pathname.slice(1)), expectedDatabase);
  assert.ok(expectedDatabase.endsWith("_test") || expectedDatabase.endsWith("_e2e"));
  assert.equal(url.hostname.replace(/^\[|\]$/g, "").toLowerCase(), expectedHost.toLowerCase());
  assert.equal(url.port || "5432", expectedPort);
  const projectId = required("JOBS_FIXTURE_CLEANUP_PROJECT_ID");
  const targetIds = parseArray("JOBS_FIXTURE_CLEANUP_TARGET_IDS");
  const unrelatedIds = parseArray("JOBS_FIXTURE_CLEANUP_UNRELATED_IDS");
  const retainedIds = parseArray("JOBS_FIXTURE_CLEANUP_RETAINED_IDS");
  const allIds = parseArray("JOBS_FIXTURE_CLEANUP_ALL_IDS");
  assert.deepEqual(new Set([...targetIds, ...unrelatedIds, ...retainedIds]), new Set(allIds));
  assert.equal(targetIds.length + unrelatedIds.length + retainedIds.length, allIds.length);

  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  let connected = false;
  let inTransaction = false;
  try {
    await client.connect();
    connected = true;
    phase = "prove actual LOGIN and ordinary private ACLs";
    const identity = await client.query(
      "SELECT current_user::text AS current_role,session_user::text AS login_role, " +
        "current_database()::text AS database_name",
    );
    assert.deepEqual(identity.rows[0], {
      current_role: expectedRole,
      login_role: expectedRole,
      database_name: expectedDatabase,
    });
    const rights = await client.query(
      "SELECT pg_catalog.has_table_privilege(current_user,'public.async_jobs','SELECT') AS can_select, " +
        "pg_catalog.has_table_privilege(current_user,'public.async_jobs','INSERT') AS can_insert, " +
        "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','INSERT') AS can_column_insert, " +
        "pg_catalog.has_table_privilege(current_user,'public.async_jobs','UPDATE') AS can_update, " +
        "pg_catalog.has_table_privilege(current_user,'public.async_jobs','DELETE') AS can_delete, " +
        "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','DELETE') AS can_column_delete",
    );
    assert.deepEqual(Object.values(rights.rows[0]), [true, false, false, true, false, false]);

    phase = "inspect owner ABI and function grants";
    const catalog = await client.query(
      "SELECT pg_catalog.oidvectortypes(p.proargtypes) AS arguments, " +
        "pg_catalog.pg_get_function_result(p.oid) AS returns,p.provolatile::text AS volatility, " +
        "p.prosecdef,p.proconfig,pg_catalog.pg_get_userbyid(p.proowner) AS owner_name, " +
        "pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') AS can_execute, " +
        "EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(p.proacl, " +
        "pg_catalog.acldefault('f',p.proowner))) acl " +
        "WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') AS public_execute " +
        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace " +
        "WHERE n.nspname='aap_jobs_v1' AND p.proname='delete_fixture_project_jobs'",
    );
    assert.equal(catalog.rows.length, 1);
    assert.deepEqual(Object.values(catalog.rows[0]), [
      "uuid[]",
      "bigint",
      "v",
      true,
      ["search_path=pg_catalog, pg_temp"],
      "jobs_code",
      true,
      false,
    ]);

    async function readRows(ids) {
      const result = await client.query(
        "SELECT id::text,project_id::text,retained_project_id::text,kind,status,payload " +
          "FROM public.async_jobs WHERE id=ANY($1::pg_catalog.uuid[]) ORDER BY id",
        [ids],
      );
      assert.deepEqual(
        result.fields.map((field) => field.name),
        jobColumns,
      );
      return result.rows;
    }

    phase = "verify trusted fixture scope";
    const originalRows = await readRows(allIds);
    assert.deepEqual(
      originalRows.map((row) => row.id),
      [...allIds].sort(),
    );
    const targets = new Set(targetIds);
    const unrelated = new Set(unrelatedIds);
    const retained = new Set(retainedIds);
    for (const row of originalRows) {
      if (targets.has(row.id)) assert.equal(row.project_id, projectId);
      if (unrelated.has(row.id)) assert.notEqual(row.project_id, projectId);
      if (retained.has(row.id)) {
        assert.equal(row.project_id, null);
        assert.equal(row.retained_project_id, projectId);
      }
    }
    const targetByKind = new Map(
      originalRows.filter((row) => targets.has(row.id)).map((row) => [row.kind, row]),
    );
    assert.deepEqual([...targetByKind.keys()].sort(), [
      "batch_predict",
      "export",
      "future_fixture_kind",
      "mask_qc",
    ]);
    assert.equal(targetByKind.get("export").status, "completed");
    assert.equal(targetByKind.get("batch_predict").status, "failed");
    assert.equal(targetByKind.get("future_fixture_kind").status, "cancelled");
    assert.equal(targetByKind.get("mask_qc").status, "pending");
    assert.equal(typeof JSON.parse(targetByKind.get("batch_predict").payload), "string");
    assert.ok(Array.isArray(JSON.parse(targetByKind.get("future_fixture_kind").payload)));
    assert.equal(typeof JSON.parse(targetByKind.get("mask_qc").payload), "number");

    phase = "deny direct private DELETE";
    await client.query("BEGIN");
    inTransaction = true;
    await client.query("SAVEPOINT private_delete");
    let denied;
    try {
      await client.query("DELETE FROM public.async_jobs WHERE id=ANY($1::pg_catalog.uuid[])", [
        targetIds,
      ]);
    } catch (error) {
      denied = error;
    }
    assert.equal(denied?.code, "42501");
    await client.query("ROLLBACK TO SAVEPOINT private_delete");
    await client.query("RELEASE SAVEPOINT private_delete");
    await client.query("ROLLBACK");
    inTransaction = false;

    phase = "preserve caller rollback";
    await client.query("BEGIN");
    inTransaction = true;
    const rollbackDelete = await client.query(
      `SELECT ${functionName}($1::pg_catalog.uuid[]) AS deleted`,
      [[projectId]],
    );
    assert.equal(rollbackDelete.fields[0].dataTypeID, 20);
    assert.equal(typeof rollbackDelete.rows[0].deleted, "string");
    assert.equal(rollbackDelete.rows[0].deleted, String(targetIds.length));
    assert.deepEqual(
      (await readRows(allIds)).map((row) => row.id),
      [...unrelatedIds, ...retainedIds].sort(),
    );
    await client.query("ROLLBACK");
    inTransaction = false;
    assert.deepEqual(
      (await readRows(allIds)).map((row) => row.id),
      [...allIds].sort(),
    );

    phase = "preserve savepoint rollback and commit exact scope";
    await client.query("BEGIN");
    inTransaction = true;
    await client.query("SAVEPOINT scoped_delete");
    const savepointDelete = await client.query(
      `SELECT ${functionName}($1::pg_catalog.uuid[]) AS deleted`,
      [[projectId]],
    );
    assert.equal(savepointDelete.fields[0].dataTypeID, 20);
    assert.equal(savepointDelete.rows[0].deleted, String(targetIds.length));
    await client.query("ROLLBACK TO SAVEPOINT scoped_delete");
    assert.deepEqual(
      (await readRows(allIds)).map((row) => row.id),
      [...allIds].sort(),
    );
    await client.query("RELEASE SAVEPOINT scoped_delete");
    const committedDelete = await client.query(
      `SELECT ${functionName}($1::pg_catalog.uuid[]) AS deleted`,
      [[projectId]],
    );
    assert.equal(committedDelete.fields[0].dataTypeID, 20);
    assert.equal(committedDelete.rows[0].deleted, String(targetIds.length));
    const inTransactionRows = await readRows(allIds);
    assert.deepEqual(
      inTransactionRows.map((row) => row.id),
      [...unrelatedIds, ...retainedIds].sort(),
    );
    await client.query("COMMIT");
    inTransaction = false;
    assert.deepEqual(
      (await readRows(allIds)).map((row) => row.id),
      [...unrelatedIds, ...retainedIds].sort(),
    );

    phase = "verify empty scope ABI";
    const empty = await client.query(`SELECT ${functionName}($1::pg_catalog.uuid[]) AS deleted`, [
      [],
    ]);
    assert.equal(empty.fields[0].dataTypeID, 20);
    assert.equal(empty.rows[0].deleted, "0");
    process.stdout.write(
      `Jobs fixture-cleanup Node acceptance passed (${targetIds.length} rows, int8, rollback/savepoint/commit)\n`,
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
  process.stderr.write(`Jobs fixture-cleanup Node acceptance failed at ${phase} (${code})\n`);
  process.exitCode = 1;
});
