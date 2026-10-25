import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const jobsRoot = path.resolve(here, "../../src/aap_jobs");
const schemaPath = path.join(jobsRoot, "action_idempotency.schema.json");
const sqlPath = path.join(jobsRoot, "sql/0332_jobs_action_idempotency.sql");
const apiSchemaPath = path.resolve(
  here,
  "../../../api/alembic/sql/0332_jobs_action_idempotency.schema.json",
);
const apiSqlPath = path.resolve(here, "../../../api/alembic/sql/0332_jobs_action_idempotency.sql");
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
const rowResult =
  "TABLE(id uuid, kind character varying, project_id uuid, retained_project_id uuid, " +
  "user_id uuid, status character varying, progress_pct integer, payload jsonb, result jsonb, " +
  "error_message text, celery_task_id character varying, started_at timestamp with time zone, " +
  "completed_at timestamp with time zone, created_at timestamp with time zone, " +
  "updated_at timestamp with time zone)";
const functions = ["lock_action_idempotency", "find_action_idempotent_jobs"];
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

function timestampMicros(value) {
  assert.equal(typeof value, "string");
  const match =
    /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}(?::?\d{2})?)$/.exec(value);
  assert.ok(match, `invalid PostgreSQL timestamp: ${value}`);
  let zone = match[4];
  if (zone !== "Z") {
    const offset = /^([+-])(\d{2})(?::?(\d{2}))?$/.exec(zone);
    assert.ok(offset);
    zone = `${offset[1]}${offset[2]}:${offset[3] ?? "00"}`;
  }
  const epochAtSecond = Date.parse(`${match[1]}T${match[2]}${zone}`);
  assert.ok(Number.isFinite(epochAtSecond));
  const fraction = BigInt((match[3] ?? "").padEnd(6, "0").slice(0, 6) || "0");
  return BigInt(epochAtSecond) * 1000n + fraction;
}

function assertJobResult(result, expectedIds, expectedTimes, expectedScope) {
  assert.deepEqual(
    result.fields.map((field) => field.name),
    rowColumns,
  );
  assert.deepEqual(
    result.fields.map((field) => field.dataTypeID),
    rowOids,
  );
  assert.deepEqual(
    result.rows.map((row) => row.id),
    expectedIds,
  );
  for (const row of result.rows) {
    assert.match(row.id, /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i);
    assert.equal(row.kind, expectedScope.kind);
    assert.equal(row.project_id, expectedScope.projectId);
    assert.equal(row.retained_project_id, null);
    assert.equal(row.user_id, expectedScope.actorId);
    assert.equal(typeof row.kind, "string");
    assert.equal(typeof row.status, "string");
    assert.ok(["pending", "running", "completed", "failed", "cancelled"].includes(row.status));
    assert.equal(row.progress_pct, 0);
    assert.equal(typeof row.payload, "string");
    assert.equal(typeof row.result, "string");
    assert.equal(row.error_message, null);
    assert.equal(row.celery_task_id, null);
    assert.equal(row.started_at, null);
    assert.equal(row.completed_at, null);
    assert.equal(typeof row.created_at, "string");
    assert.equal(typeof row.updated_at, "string");
    assert.equal(timestampMicros(row.created_at), timestampMicros(expectedTimes[row.id]));
    timestampMicros(row.updated_at);
  }
}

async function selfCheck() {
  phase = "self-check packaged contract and migration copies";
  const [schemaText, apiSchemaText, sql, apiSql] = await Promise.all([
    readFile(schemaPath, "utf8"),
    readFile(apiSchemaPath, "utf8"),
    readFile(sqlPath, "utf8"),
    readFile(apiSqlPath, "utf8"),
  ]);
  const schema = JSON.parse(schemaText);
  const apiSchema = JSON.parse(apiSchemaText);
  assert.deepEqual(apiSchema, schema);
  assert.equal(sql, apiSql);
  assert.equal(schema.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(schema.$id, "aap-jobs-action-idempotency/v1");
  assert.deepEqual(schema["x-row-columns"], rowColumns);
  assert.deepEqual(Object.keys(schema["x-row-types"]).sort(), [...rowColumns].sort());
  assert.deepEqual(Object.keys(schema["x-postgresql-functions"]).sort(), [
    "aap_jobs_v1.find_action_idempotent_jobs",
    "aap_jobs_v1.lock_action_idempotency",
  ]);
  const lockRequest = schema.$defs.ActionIdempotencyLockRequest;
  const queryRequest = schema.$defs.ActionIdempotencyQueryRequest;
  assert.deepEqual(lockRequest.required, ["action", "project_id", "actor_id", "key"]);
  assert.deepEqual(queryRequest.required, ["kind", "project_id", "actor_id", "key"]);
  assert.equal(lockRequest.additionalProperties, false);
  assert.equal(queryRequest.additionalProperties, false);
  for (const request of [lockRequest, queryRequest]) {
    for (const property of Object.values(request.properties)) {
      assert.ok(property.anyOf.some((item) => item.type === "null"));
    }
  }
  assert.deepEqual(schema["x-postgresql-functions"]["aap_jobs_v1.lock_action_idempotency"], {
    arguments: "text, uuid, uuid, text",
    returns: "void",
    volatility: "volatile",
    parallel: "unsafe",
  });
  assert.deepEqual(schema["x-postgresql-functions"]["aap_jobs_v1.find_action_idempotent_jobs"], {
    arguments: "text, uuid, uuid, text",
    returns: rowResult,
    volatility: "volatile",
    parallel: "unsafe",
  });
  assert.deepEqual(schema["x-lock"], {
    key: "aap:data-manager:{action}:{project_id}:{actor_id}:{key}",
    hash: "pg_catalog.hashtextextended(key,0)",
    mode: "pg_advisory_xact_lock; wait until caller transaction ends",
    autoflush: false,
    nulls: "legacy Python None spelling",
  });
  assert.equal(schema["x-query"].ordering, "created_at DESC, id DESC");
  assert.equal(schema["x-query"].lock, "FOR UPDATE, waiting; every matching row locked; no LIMIT");
  assert.equal(schema["x-query"].nulls, "legacy SQLAlchemy equality-to-None IS NULL predicates");

  const normalizedSql = sql
    .replace(/--[^\n]*/g, "")
    .replace(/\s+/g, " ")
    .toLowerCase();
  assert.match(normalizedSql, /create function aap_jobs_v1\.lock_action_idempotency\(/);
  assert.match(normalizedSql, /create function aap_jobs_v1\.find_action_idempotent_jobs\(/);
  assert.match(normalizedSql, /pg_catalog\.pg_advisory_xact_lock/);
  assert.match(normalizedSql, /pg_catalog\.hashtextextended/);
  assert.match(normalizedSql, /aap:data-manager:/);
  assert.match(normalizedSql, /order by job\.created_at desc, job\.id desc for update/);
  assert.match(normalizedSql, /job\.kind = \$1/);
  assert.match(normalizedSql, /job\.project_id = \$2/);
  assert.match(normalizedSql, /job\.user_id = \$3/);
  assert.match(normalizedSql, /job\.payload ->> ''data_manager_idempotency_key'' = \$4/);
  assert.match(normalizedSql, /job\.kind is null/);
  assert.match(normalizedSql, /job\.project_id is null/);
  assert.match(normalizedSql, /job\.user_id is null/);
  assert.match(normalizedSql, /is null/);
  assert.equal(/\blimit\s+\d*/.test(normalizedSql), false);
  assert.equal(/(?:where|and)\s+[^;]*\bstatus\s*=/.test(normalizedSql), false);
  assert.equal(/(?:where|and)\s+[^;]*\bretained_project_id\s*=/.test(normalizedSql), false);
  assert.equal((sql.match(/SECURITY DEFINER/g) ?? []).length, 2);
  assert.equal((sql.match(/REVOKE ALL ON FUNCTION/g) ?? []).length, 2);
  assert.match(normalizedSql, /set search_path = pg_catalog, pg_temp/);
  assert.match(normalizedSql, /set timezone to 'utc'/);
  assert.match(normalizedSql, /set datestyle to 'iso, ymd'/);
  process.stdout.write("Jobs action-idempotency Node contract self-check passed\n");
}

async function live() {
  phase = "load PostgreSQL driver and contract";
  const [pg, schemaText] = await Promise.all([import("pg"), readFile(schemaPath, "utf8")]);
  const Client = pg.Client ?? pg.default?.Client;
  const types = pg.types ?? pg.default?.types;
  assert.equal(typeof Client, "function");
  assert.ok(types);
  const schema = JSON.parse(schemaText);
  assert.equal(schema.$id, "aap-jobs-action-idempotency/v1");
  assert.deepEqual(schema["x-row-columns"], rowColumns);
  types.setTypeParser(20, (value) => value);
  types.setTypeParser(1184, (value) => value);
  types.setTypeParser(2950, (value) => value);
  types.setTypeParser(3802, (value) => value);

  const connectionString = required("JOBS_ACTION_IDEMPOTENCY_DATABASE_URL");
  const expectedRole = required("JOBS_ACTION_IDEMPOTENCY_EXPECTED_ROLE");
  const expectedDatabase = required("JOBS_ACTION_IDEMPOTENCY_EXPECTED_DATABASE");
  const expectedHost = required("JOBS_ACTION_IDEMPOTENCY_EXPECTED_HOST");
  const expectedPort = required("JOBS_ACTION_IDEMPOTENCY_EXPECTED_PORT");
  const url = new URL(connectionString);
  const hostname = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  assert.equal(decodeURIComponent(url.username), expectedRole);
  assert.equal(decodeURIComponent(url.pathname.slice(1)), expectedDatabase);
  assert.ok(expectedDatabase.endsWith("_test") || expectedDatabase.endsWith("_e2e"));
  assert.equal(hostname, expectedHost.replace(/^\[|\]$/g, "").toLowerCase());
  assert.equal(url.port || "5432", expectedPort);

  const kind = required("JOBS_ACTION_IDEMPOTENCY_KIND");
  const action = required("JOBS_ACTION_IDEMPOTENCY_ACTION");
  const key = required("JOBS_ACTION_IDEMPOTENCY_KEY");
  const projectId = required("JOBS_ACTION_IDEMPOTENCY_PROJECT_ID");
  const actorId = required("JOBS_ACTION_IDEMPOTENCY_ACTOR_ID");
  const expectedIds = parseJson("JOBS_ACTION_IDEMPOTENCY_EXPECTED_JOB_IDS");
  const expectedNullIds = parseJson("JOBS_ACTION_IDEMPOTENCY_EXPECTED_NULL_IDS");
  const expectedTimes = parseJson("JOBS_ACTION_IDEMPOTENCY_EXPECTED_TIMES");
  assert.ok(Array.isArray(expectedIds) && Array.isArray(expectedNullIds));

  const clients = [];
  const transactions = new Set();
  const connect = async () => {
    const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
    await client.connect();
    clients.push(client);
    return client;
  };
  try {
    const client = await connect();
    phase = "actual LOGIN identity and private table denial";
    const identity = await client.query(
      "SELECT current_user::text AS current_role, session_user::text AS login_role, " +
        "current_database()::text AS database_name",
    );
    assert.deepEqual(identity.rows[0], {
      current_role: expectedRole,
      login_role: expectedRole,
      database_name: expectedDatabase,
    });
    const rights = await client.query(
      "SELECT pg_catalog.has_table_privilege(current_user,'public.async_jobs','SELECT') " +
        "AS table_select, pg_catalog.has_any_column_privilege(current_user, " +
        "'public.async_jobs','SELECT') AS column_select, " +
        "pg_catalog.has_table_privilege(current_user,'public.async_jobs','UPDATE') " +
        "AS table_update, pg_catalog.has_any_column_privilege(current_user, " +
        "'public.async_jobs','UPDATE') AS column_update",
    );
    assert.deepEqual(
      [
        rights.rows[0].table_select,
        rights.rows[0].column_select,
        rights.rows[0].table_update,
        rights.rows[0].column_update,
      ],
      [false, false, false, false],
    );
    await assert.rejects(
      client.query("SELECT id FROM public.async_jobs LIMIT 1"),
      (error) => error?.code === "42501",
    );

    phase = "function ABI, grants, and fixed execution settings";
    const catalog = await client.query(
      "SELECT p.proname, pg_catalog.oidvectortypes(p.proargtypes) AS arguments, " +
        "pg_catalog.pg_get_function_result(p.oid) AS returns, p.provolatile, " +
        "p.proparallel, p.prosecdef, p.proconfig, " +
        "pg_catalog.pg_get_userbyid(p.proowner) AS owner_name, " +
        "pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') AS can_execute, " +
        "EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(p.proacl, " +
        "pg_catalog.acldefault('f',p.proowner))) acl " +
        "WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') AS public_execute, " +
        "pg_catalog.has_function_privilege('jobs_lifecycle_client',p.oid,'EXECUTE') " +
        "AS lifecycle_execute, CASE WHEN pg_catalog.to_regrole(" +
        "'jobs_list_query_client') IS NULL THEN false ELSE " +
        "pg_catalog.has_function_privilege('jobs_list_query_client',p.oid,'EXECUTE') " +
        "END AS list_execute FROM pg_catalog.pg_proc p " +
        "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace " +
        "WHERE n.nspname='aap_jobs_v1' AND p.proname=ANY($1::pg_catalog.text[]) " +
        "ORDER BY p.proname",
      [functions],
    );
    assert.equal(catalog.rows.length, functions.length);
    for (const row of catalog.rows) {
      const fullName = `aap_jobs_v1.${row.proname}`;
      const contract = schema["x-postgresql-functions"][fullName];
      assert.ok(contract);
      assert.equal(normalize(row.arguments), normalize(contract.arguments));
      assert.equal(normalize(row.returns), normalize(contract.returns));
      assert.equal(row.provolatile, "v");
      assert.equal(row.proparallel, "u");
      assert.equal(row.prosecdef, true);
      assert.deepEqual(row.proconfig, [
        "search_path=pg_catalog, pg_temp",
        "TimeZone=UTC",
        "DateStyle=ISO, YMD",
      ]);
      assert.equal(row.owner_name, "jobs_code");
      assert.equal(row.can_execute, true);
      assert.equal(row.public_execute, false);
      assert.equal(row.lifecycle_execute, false);
      assert.equal(row.list_execute, false);
    }

    phase = "full ordered lookup and nullable legacy predicates";
    await client.query("SET TIME ZONE 'UTC'");
    await client.query("SET DateStyle TO 'ISO, YMD'");
    const found = await client.query(
      "SELECT * FROM aap_jobs_v1.find_action_idempotent_jobs(" +
        "$1::pg_catalog.text,$2::pg_catalog.uuid,$3::pg_catalog.uuid,$4::pg_catalog.text)",
      [kind, projectId, actorId, key],
    );
    const expectedScope = { kind, projectId, actorId };
    assertJobResult(found, expectedIds, expectedTimes, expectedScope);
    assert.equal(found.rows.length, expectedIds.length);
    assert.match(found.rows[0].payload, /"opaque_counter"\s*:\s*9007199254740993(?:,|})/);
    assert.match(found.rows[0].payload, /"enabled"\s*:\s*false/);
    assert.match(found.rows[0].payload, /"missing"\s*:\s*null/);
    assert.match(found.rows[0].payload, /雪🚙/);
    assert.match(found.rows[0].result, /9007199254740993/);

    const nullKey = await client.query(
      "SELECT * FROM aap_jobs_v1.find_action_idempotent_jobs(" +
        "$1::pg_catalog.text,$2::pg_catalog.uuid,$3::pg_catalog.uuid,$4::pg_catalog.text)",
      [kind, projectId, actorId, null],
    );
    assertJobResult(nullKey, expectedNullIds, expectedTimes, expectedScope);

    phase = "legacy advisory namespace, wait, commit, and rollback";
    const legacyKey = `aap:data-manager:${action}:${projectId}:${actorId}:${key}`;
    const waiter = await connect();
    const probe = await connect();
    const lockSql =
      "SELECT aap_jobs_v1.lock_action_idempotency(" +
      "$1::pg_catalog.text,$2::pg_catalog.uuid,$3::pg_catalog.uuid,$4::pg_catalog.text)";
    const trySql =
      "SELECT pg_catalog.pg_try_advisory_xact_lock(" +
      "pg_catalog.hashtextextended($1::pg_catalog.text, 0)) AS acquired";

    await client.query("BEGIN");
    transactions.add(client);
    await client.query(lockSql, [action, projectId, actorId, key]);
    assert.equal((await probe.query(trySql, [legacyKey])).rows[0].acquired, false);
    await client.query("ROLLBACK");
    transactions.delete(client);
    assert.equal((await probe.query(trySql, [legacyKey])).rows[0].acquired, true);

    await client.query("BEGIN");
    transactions.add(client);
    await client.query(lockSql, [action, projectId, actorId, key]);
    await waiter.query("BEGIN");
    transactions.add(waiter);
    const waiting = waiter.query(lockSql, [action, projectId, actorId, key]);
    const early = await Promise.race([
      waiting.then(() => "acquired"),
      new Promise((resolve) => setTimeout(() => resolve("waiting"), 100)),
    ]);
    assert.equal(early, "waiting");
    await client.query("COMMIT");
    transactions.delete(client);
    await waiting;
    assert.equal((await probe.query(trySql, [legacyKey])).rows[0].acquired, false);
    await waiter.query("ROLLBACK");
    transactions.delete(waiter);
    assert.equal((await probe.query(trySql, [legacyKey])).rows[0].acquired, true);

    await client.query("BEGIN");
    transactions.add(client);
    await client.query(lockSql, [action, projectId, actorId, key]);
    await client.query("COMMIT");
    transactions.delete(client);
    assert.equal((await probe.query(trySql, [legacyKey])).rows[0].acquired, true);

    const nullActionKey = `aap:data-manager:None:${projectId}:${actorId}:nullable-lock`;
    await client.query("BEGIN");
    transactions.add(client);
    await client.query(lockSql, [null, projectId, actorId, "nullable-lock"]);
    assert.equal((await probe.query(trySql, [nullActionKey])).rows[0].acquired, false);
    await client.query("ROLLBACK");
    transactions.delete(client);
    assert.equal((await probe.query(trySql, [nullActionKey])).rows[0].acquired, true);

    process.stdout.write(
      `${JSON.stringify({
        status: "passed",
        functions: 2,
        rowColumns: rowColumns.length,
        matchedRows: found.rows.length,
        nullRows: nullKey.rows.length,
      })}\n`,
    );
  } finally {
    for (const client of clients) {
      if (transactions.has(client)) {
        try {
          await client.query("ROLLBACK");
        } catch {
          // The server releases the transaction when the connection closes.
        }
      }
      await client.end();
    }
  }
}

try {
  const mode = process.argv[2];
  if (mode === "--self-check") await selfCheck();
  else if (mode === "--live") await live();
  else throw new Error("expected --self-check or --live");
} catch (error) {
  process.stderr.write(
    `${JSON.stringify({ status: "failed", phase, error: error?.message ?? String(error) })}\n`,
  );
  process.exitCode = 1;
}
