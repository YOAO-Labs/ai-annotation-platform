import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const jobsRoot = path.resolve(here, "../../src/aap_jobs");
const schemaPath = path.join(jobsRoot, "async_job_list_query.schema.json");
const sqlPath = path.join(jobsRoot, "sql/0326_jobs_list_query.sql");
const apiSchemaPath = path.resolve(
  here,
  "../../../api/alembic/sql/0326_jobs_list_query.schema.json",
);
const apiSqlPath = path.resolve(here, "../../../api/alembic/sql/0326_jobs_list_query.sql");
const pageSignature =
  "aap_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)";
const totalSignature = "aap_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)";
const helperSignature = "aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)";
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const statusValues = new Set(["pending", "running", "completed", "failed", "cancelled"]);
const nativeColumnOids = {
  id: 2950,
  kind: 1043,
  project_id: 2950,
  retained_project_id: 2950,
  user_id: 2950,
  status: 1043,
  progress_pct: 23,
  payload: 3802,
  result: 3802,
  error_message: 25,
  celery_task_id: 1043,
  started_at: 1184,
  completed_at: 1184,
  created_at: 1184,
  updated_at: 1184,
};

let phase = "startup";

function failIfNot(condition, message) {
  if (!condition) throw new Error(message);
}

function parseJsonArray(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  let value;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new Error(`${name} must be a JSON array`);
  }
  failIfNot(Array.isArray(value), `${name} must be a JSON array`);
  return value;
}

function parseOptionalUuid(name) {
  const raw = process.env[name];
  if (raw === undefined || raw === "" || raw === "null") return null;
  failIfNot(uuidPattern.test(raw), `${name} must be a UUID or null`);
  return raw.toLowerCase();
}

function parseInteger(name, fallback, { minimum, maximum }) {
  const raw = process.env[name] ?? String(fallback);
  failIfNot(/^(0|[1-9][0-9]*)$/.test(raw), `${name} must be a decimal integer`);
  const value = Number(raw);
  failIfNot(Number.isSafeInteger(value), `${name} exceeds the safe integer range`);
  failIfNot(value >= minimum && value <= maximum, `${name} is outside its allowed range`);
  return value;
}

function normalizeSql(value) {
  return value.replace(/\s+/g, "").toLowerCase();
}

function timestampMicros(value) {
  failIfNot(typeof value === "string", "created_at must retain its timestamptz text value");
  const match =
    /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}(?::?\d{2})?)$/.exec(value);
  failIfNot(match, "created_at is not a PostgreSQL timestamptz value");
  let zone = match[4];
  if (zone !== "Z") {
    const offset = /^([+-])(\d{2})(?::?(\d{2}))?$/.exec(zone);
    failIfNot(offset, "created_at has an invalid UTC offset");
    zone = `${offset[1]}${offset[2]}:${offset[3] ?? "00"}`;
  }
  const epochAtSecond = Date.parse(`${match[1]}T${match[2]}${zone}`);
  failIfNot(Number.isFinite(epochAtSecond), "created_at is not a valid timestamp");
  const fraction = BigInt((match[3] ?? "").padEnd(6, "0").slice(0, 6) || "0");
  return BigInt(epochAtSecond) * 1000n + fraction;
}

function assertUuid(value, nullable = false) {
  if (nullable && value === null) return;
  assert.equal(typeof value, "string");
  assert.match(value, uuidPattern);
}

function assertPageRow(row, columns) {
  assert.deepEqual(Object.keys(row), columns);
  assertUuid(row.id);
  assertUuid(row.project_id, true);
  assertUuid(row.retained_project_id, true);
  assertUuid(row.user_id, true);
  assert.equal(typeof row.kind, "string");
  assert.ok([...row.kind].length <= 40);
  assert.equal(typeof row.status, "string");
  assert.ok([...row.status].length <= 20);
  assert.ok(Number.isSafeInteger(row.progress_pct));
  assert.ok(row.error_message === null || typeof row.error_message === "string");
  assert.ok(row.celery_task_id === null || typeof row.celery_task_id === "string");
  for (const field of ["started_at", "completed_at"]) {
    assert.ok(row[field] === null || typeof row[field] === "string");
  }
  assert.equal(typeof row.created_at, "string");
  assert.equal(typeof row.updated_at, "string");
  timestampMicros(row.created_at);
  timestampMicros(row.updated_at);
}

function assertPageOrder(rows) {
  for (let index = 1; index < rows.length; index += 1) {
    const previous = rows[index - 1];
    const current = rows[index];
    const previousTime = timestampMicros(previous.created_at);
    const currentTime = timestampMicros(current.created_at);
    assert.ok(previousTime >= currentTime, "page is not ordered by created_at DESC");
    if (previousTime === currentTime) {
      assert.ok(
        previous.id.toLowerCase() > current.id.toLowerCase(),
        "tied created_at rows are not ordered by id DESC",
      );
    }
  }
}

async function selfCheck() {
  phase = "self-check resources";
  const [schemaText, apiSchemaText, sql, apiSql] = await Promise.all([
    readFile(schemaPath, "utf8"),
    readFile(apiSchemaPath, "utf8"),
    readFile(sqlPath, "utf8"),
    readFile(apiSqlPath, "utf8"),
  ]);
  const schema = JSON.parse(schemaText);
  const apiSchema = JSON.parse(apiSchemaText);
  const columns = schema["x-row-columns"];
  assert.equal(schema.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(schema.$id, "aap-jobs-async-job-list-query/v1");
  assert.deepEqual(apiSchema, schema);
  assert.equal(sql, apiSql);
  assert.deepEqual(Object.keys(nativeColumnOids).sort(), [...schema["x-row-columns"]].sort());
  assert.deepEqual(
    [
      "aap_jobs_v1.async_job_list_page(uuid, text, text[], text[], uuid, text, integer, integer)",
      "aap_jobs_v1.async_job_list_total(uuid, text, text[], text[], uuid, text)",
    ]
      .map(normalizeSql)
      .sort(),
    [pageSignature, totalSignature].map(normalizeSql).sort(),
  );
  assert.deepEqual(schema["x-page-semantics"].order, ["created_at DESC", "id DESC"]);
  assert.equal(schema["x-page-semantics"].separate_statements, true);
  assert.equal(schema["x-identity-snapshot"].preflight_reads_per_list_request, 1);
  assert.match(schema["x-identity-snapshot"].same_statement_membership_facts, /read_actor_facts/);
  assert.deepEqual(schema["x-row-columns"], columns);
  assert.deepEqual(
    Object.keys(schema.$defs.AsyncJobListRow.properties).sort(),
    [...columns].sort(),
  );
  assert.equal(
    schema["x-postgresql-functions"]["aap_jobs_v1.async_job_list_page"].arguments,
    "uuid, text, text[], text[], uuid, text, integer, integer",
  );
  assert.equal(
    schema["x-postgresql-functions"]["aap_jobs_v1.async_job_list_total"].returns,
    "bigint",
  );
  assert.match(sql, /CREATE FUNCTION aap_jobs_v1\._async_job_list_rows\(/);
  assert.match(sql, /CREATE FUNCTION aap_jobs_v1\.async_job_list_page\(/);
  assert.match(sql, /CREATE FUNCTION aap_jobs_v1\.async_job_list_total\(/);
  assert.match(sql, /aap_identity_v1\.read_actor_facts\(p_actor_id\)/);
  assert.match(sql, /SET search_path = pg_catalog, pg_temp/g);
  assert.equal((sql.match(/SECURITY DEFINER/g) ?? []).length, 3);
  assert.equal((sql.match(/STABLE/g) ?? []).length, 3);
  for (const functionName of [
    "_async_job_list_rows",
    "async_job_list_page",
    "async_job_list_total",
  ]) {
    assert.match(sql, new RegExp(`REVOKE ALL ON FUNCTION aap_jobs_v1\\.${functionName}\\(`));
  }
  assert.equal(sql.includes("FROM public.users"), false);
  assert.equal(sql.includes("aap_identity_v1.account_profiles"), false);
  assert.equal(sql.includes("GRANT EXECUTE"), false);
  process.stdout.write("Jobs list-query Node contract self-check passed\n");
}

async function verifyListQueryRole() {
  phase = "load PostgreSQL driver";
  const pg = await import("pg");
  const Client = pg.Client ?? pg.default?.Client;
  const types = pg.types ?? pg.default?.types;
  failIfNot(typeof Client === "function" && types, "pg Client/types are unavailable");
  types.setTypeParser(1184, (value) => value);

  const connectionString = process.env.JOBS_LIST_QUERY_DATABASE_URL;
  const expectedRole = process.env.JOBS_LIST_QUERY_EXPECTED_ROLE;
  const actorId = process.env.JOBS_LIST_QUERY_ACTOR_ID;
  const platformRole = process.env.JOBS_LIST_QUERY_PLATFORM_ROLE;
  const expectedTotal = process.env.JOBS_LIST_QUERY_EXPECTED_TOTAL;
  failIfNot(connectionString, "missing function-only Jobs list database URL");
  failIfNot(expectedRole, "missing expected function-only LOGIN role");
  failIfNot(uuidPattern.test(actorId ?? ""), "actor ID must be a UUID");
  failIfNot(platformRole, "missing platform-role snapshot");
  failIfNot(/^(0|[1-9][0-9]*)$/.test(expectedTotal ?? ""), "expected total must be decimal text");

  const statuses = parseJsonArray("JOBS_LIST_QUERY_STATUSES", []);
  const kinds = parseJsonArray("JOBS_LIST_QUERY_KINDS", []);
  const expectedPageIds = parseJsonArray("JOBS_LIST_QUERY_EXPECTED_PAGE_IDS", null);
  failIfNot(Array.isArray(expectedPageIds), "expected page IDs must be supplied as JSON");
  for (const status of statuses) {
    failIfNot(statusValues.has(status), "status filter contains an unknown value");
  }
  for (const kind of kinds) failIfNot(typeof kind === "string", "kind filter must contain strings");
  for (const id of expectedPageIds) failIfNot(uuidPattern.test(id), "expected page ID is invalid");

  const projectId = parseOptionalUuid("JOBS_LIST_QUERY_PROJECT_ID");
  const searchPattern = process.env.JOBS_LIST_QUERY_SEARCH_PATTERN ?? null;
  const limit = parseInteger("JOBS_LIST_QUERY_LIMIT", 50, { minimum: 1, maximum: 200 });
  const offset = parseInteger("JOBS_LIST_QUERY_OFFSET", 0, {
    minimum: 0,
    maximum: 2147483647,
  });
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  await client.connect();
  try {
    phase = "LOGIN identity";
    const identity = await client.query(
      "SELECT current_user AS current_role, session_user AS login_role",
    );
    assert.equal(identity.rows[0]?.current_role, expectedRole);
    assert.equal(identity.rows[0]?.login_role, expectedRole);
    const loginRole = await client.query(
      "SELECT role.rolcanlogin, role.rolinherit, role.rolsuper, role.rolcreatedb, " +
        "role.rolcreaterole, role.rolreplication, role.rolbypassrls, " +
        "NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members AS membership " +
        "WHERE membership.member=role.oid OR membership.roleid=role.oid) AS no_memberships " +
        "FROM pg_catalog.pg_roles AS role WHERE role.rolname=current_user",
    );
    assert.deepEqual(Object.values(loginRole.rows[0] ?? {}), [
      true,
      false,
      false,
      false,
      false,
      false,
      false,
      true,
    ]);

    phase = "function ABI and grants";
    const functions = await client.query(
      "SELECT p.oid, p.proname, pg_catalog.oidvectortypes(p.proargtypes) AS arguments, " +
        "pg_catalog.pg_get_function_result(p.oid) AS returns, p.prosecdef, " +
        "p.provolatile, p.proconfig, " +
        "pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') AS can_execute " +
        "FROM pg_catalog.pg_proc AS p " +
        "JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace " +
        "WHERE n.nspname='aap_jobs_v1' " +
        "AND p.proname IN ('async_job_list_page','async_job_list_total','_async_job_list_rows') " +
        "ORDER BY p.proname",
    );
    const contract = JSON.parse(await readFile(schemaPath, "utf8"));
    const page = functions.rows.find((row) => row.proname === "async_job_list_page");
    const total = functions.rows.find((row) => row.proname === "async_job_list_total");
    const helper = functions.rows.find((row) => row.proname === "_async_job_list_rows");
    assert.ok(page && total && helper);
    assert.equal(
      page.arguments.replaceAll(", ", ","),
      pageSignature.slice(pageSignature.indexOf("(") + 1, -1),
    );
    assert.equal(
      total.arguments.replaceAll(", ", ","),
      totalSignature.slice(totalSignature.indexOf("(") + 1, -1),
    );
    assert.equal(
      `aap_jobs_v1._async_job_list_rows(${helper.arguments.replaceAll(", ", ",")})`,
      helperSignature,
    );
    assert.equal(
      normalizeSql(page.returns),
      normalizeSql(contract["x-postgresql-functions"]["aap_jobs_v1.async_job_list_page"].returns),
    );
    assert.equal(normalizeSql(total.returns), "bigint");
    for (const fn of [page, total, helper]) {
      assert.equal(fn.prosecdef, true);
      assert.equal(fn.provolatile, "s");
      assert.ok(fn.proconfig?.includes("search_path=pg_catalog, pg_temp"));
    }
    assert.equal(page.can_execute, true);
    assert.equal(total.can_execute, true);
    assert.equal(helper.can_execute, false);
    const publicExecute = await client.query(
      "SELECT EXISTS (" +
        "SELECT 1 FROM pg_catalog.pg_proc AS p " +
        "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(p.proacl, " +
        "pg_catalog.acldefault('f',p.proowner))) AS acl " +
        "WHERE p.oid IN ($1::pg_catalog.oid,$2::pg_catalog.oid,$3::pg_catalog.oid) " +
        "AND acl.grantee=0 AND acl.privilege_type='EXECUTE') AS public_execute",
      [page.oid, total.oid, helper.oid],
    );
    assert.equal(publicExecute.rows[0].public_execute, false);

    const executableAapFunctions = await client.query(
      "SELECT n.nspname || '.' || p.proname || '(' || " +
        "pg_catalog.oidvectortypes(p.proargtypes) || ')' AS signature " +
        "FROM pg_catalog.pg_proc AS p " +
        "JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace " +
        "WHERE n.nspname LIKE 'aap\\_%' ESCAPE '\\' " +
        "AND pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') " +
        "ORDER BY signature",
    );
    assert.deepEqual(
      executableAapFunctions.rows.map((row) => normalizeSql(row.signature)),
      [pageSignature, totalSignature].map(normalizeSql).sort(),
    );

    phase = "page result types and order";
    const args = [actorId, platformRole, statuses, kinds, projectId, searchPattern, limit, offset];
    const pageResult = await client.query(
      "SELECT * FROM aap_jobs_v1.async_job_list_page(" +
        "$1::pg_catalog.uuid,$2::pg_catalog.text,$3::pg_catalog.text[],$4::pg_catalog.text[]," +
        "$5::pg_catalog.uuid,$6::pg_catalog.text,$7::pg_catalog.int4,$8::pg_catalog.int4)",
      args,
    );
    const columns = contract["x-row-columns"];
    assert.deepEqual(
      pageResult.fields.map((field) => field.name),
      columns,
    );
    for (const field of pageResult.fields) {
      assert.equal(field.dataTypeID, nativeColumnOids[field.name]);
    }
    for (const row of pageResult.rows) assertPageRow(row, columns);
    assertPageOrder(pageResult.rows);
    assert.deepEqual(
      pageResult.rows.map((row) => row.id.toLowerCase()),
      expectedPageIds.map((id) => id.toLowerCase()),
    );
    assert.ok(pageResult.rows.length <= limit);

    phase = "separate total result";
    const totalResult = await client.query(
      "SELECT aap_jobs_v1.async_job_list_total(" +
        "$1::pg_catalog.uuid,$2::pg_catalog.text,$3::pg_catalog.text[],$4::pg_catalog.text[]," +
        "$5::pg_catalog.uuid,$6::pg_catalog.text) AS total",
      args.slice(0, 6),
    );
    assert.equal(totalResult.fields[0]?.name, "total");
    assert.equal(totalResult.fields[0]?.dataTypeID, 20);
    assert.equal(totalResult.rows.length, 1);
    const totalValue = totalResult.rows[0].total;
    assert.equal(typeof totalValue, "string");
    assert.equal(BigInt(totalValue).toString(), totalValue);
    assert.equal(totalValue, expectedTotal);
    assert.ok(BigInt(totalValue) >= BigInt(pageResult.rows.length));

    phase = "effective capability boundary";
    const rights = await client.query(
      "SELECT " +
        "pg_catalog.has_table_privilege(current_user,'public.async_jobs','SELECT') AS jobs_read, " +
        "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','SELECT') AS jobs_column_read, " +
        "pg_catalog.has_table_privilege(current_user,'public.users','SELECT') AS users_read, " +
        "pg_catalog.has_database_privilege(current_user,current_database(),'CREATE') AS database_create, " +
        "pg_catalog.has_database_privilege(current_user,current_database(),'TEMP') AS database_temp",
    );
    assert.deepEqual(Object.values(rights.rows[0]), [false, false, false, false, false]);

    const aapSchemas = await client.query(
      "SELECT n.nspname FROM pg_catalog.pg_namespace AS n " +
        "WHERE n.nspname LIKE 'aap\\_%' ESCAPE '\\' " +
        "AND pg_catalog.has_schema_privilege(current_user,n.oid,'USAGE') ORDER BY n.nspname",
    );
    assert.deepEqual(
      aapSchemas.rows.map((row) => row.nspname),
      ["aap_jobs_v1"],
    );

    const extraSchemas = await client.query(
      "SELECT n.nspname FROM pg_catalog.pg_namespace AS n " +
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') " +
        "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%' " +
        "AND (pg_catalog.has_schema_privilege(current_user,n.oid,'CREATE') OR " +
        "(n.nspname NOT IN ('public','aap_jobs_v1') AND " +
        "pg_catalog.has_schema_privilege(current_user,n.oid,'USAGE'))) " +
        "ORDER BY n.nspname",
    );
    assert.deepEqual(extraSchemas.rows, []);

    const protectedRelations = await client.query(
      "SELECT EXISTS (" +
        "SELECT 1 FROM pg_catalog.pg_class AS c " +
        "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace " +
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') " +
        "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%' " +
        "AND c.relkind IN ('r','p','v','m','f') AND (" +
        "pg_catalog.has_table_privilege(current_user,c.oid,'SELECT') OR " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'INSERT') OR " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'UPDATE') OR " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'DELETE') OR " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'TRUNCATE') OR " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'REFERENCES') OR " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'TRIGGER') OR " +
        "pg_catalog.has_any_column_privilege(current_user,c.oid,'SELECT') OR " +
        "pg_catalog.has_any_column_privilege(current_user,c.oid,'INSERT') OR " +
        "pg_catalog.has_any_column_privilege(current_user,c.oid,'UPDATE') OR " +
        "pg_catalog.has_any_column_privilege(current_user,c.oid,'REFERENCES'))) " +
        "AS any_relation_privilege",
    );
    assert.equal(protectedRelations.rows[0].any_relation_privilege, false);

    const sequenceRights = await client.query(
      "SELECT EXISTS (" +
        "SELECT 1 FROM pg_catalog.pg_class AS c " +
        "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace " +
        "WHERE c.relkind='S' AND n.nspname NOT IN ('pg_catalog','information_schema') " +
        "AND (pg_catalog.has_sequence_privilege(current_user,c.oid,'USAGE') OR " +
        "pg_catalog.has_sequence_privilege(current_user,c.oid,'SELECT') OR " +
        "pg_catalog.has_sequence_privilege(current_user,c.oid,'UPDATE'))) " +
        "AS any_sequence_privilege",
    );
    assert.equal(sequenceRights.rows[0].any_sequence_privilege, false);

    async function assertDenied(statement, parameters = []) {
      let failure;
      try {
        await client.query(statement, parameters);
      } catch (error) {
        failure = error;
      }
      assert.equal(failure?.code, "42501");
    }
    await assertDenied(
      "SELECT * FROM aap_jobs_v1._async_job_list_rows(" +
        "$1::pg_catalog.uuid,$2::pg_catalog.text,$3::pg_catalog.text[],$4::pg_catalog.text[]," +
        "$5::pg_catalog.uuid,$6::pg_catalog.text)",
      args.slice(0, 6),
    );
    await assertDenied("SELECT id FROM public.async_jobs WHERE id=$1::pg_catalog.uuid", [actorId]);
    await assertDenied(
      "SELECT id FROM aap_jobs_v1.notification_scope_facts WHERE id=$1::pg_catalog.uuid",
      [actorId],
    );
    await assertDenied("SELECT * FROM aap_identity_v1.read_actor_facts($1::pg_catalog.uuid)", [
      actorId,
    ]);
    await assertDenied("UPDATE public.async_jobs SET kind=kind WHERE id=$1::pg_catalog.uuid", [
      actorId,
    ]);
  } finally {
    await client.end();
  }
  process.stdout.write("Jobs list-query function-only LOGIN acceptance passed\n");
}

async function main() {
  if (process.argv.includes("--self-check")) return selfCheck();
  if (process.argv.includes("--live")) return verifyListQueryRole();
  throw new Error("select --self-check or --live");
}

main().catch((error) => {
  const code = typeof error?.code === "string" ? `/${error.code}` : "";
  process.stderr.write(
    `Jobs list-query acceptance failed at ${phase}: ${error?.name ?? "Error"}${code}\n`,
  );
  process.exitCode = 1;
});
