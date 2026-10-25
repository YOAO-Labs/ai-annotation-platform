import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

const here = path.dirname(fileURLToPath(import.meta.url));
const jobsRoot = path.resolve(here, "../../src/aap_jobs");
const schemaPath = path.join(jobsRoot, "lifecycle.schema.json");
const sqlPath = path.join(jobsRoot, "sql/0327_jobs_lifecycle.sql");
const frozenSchemaPath = path.resolve(
  here,
  "../../../api/alembic/sql/0327_jobs_lifecycle.schema.json",
);
const frozenSqlPath = path.resolve(here, "../../../api/alembic/sql/0327_jobs_lifecycle.sql");
const creationRelation = "aap_jobs_v1.async_job_creation";
const helperSignatures = [
  "aap_jobs_v1._jobs_lifecycle_has_keys(jsonb, text[])",
  "aap_jobs_v1._jobs_lifecycle_is_truthy(jsonb)",
  "aap_jobs_v1._jobs_lifecycle_integer(jsonb)",
];
const rowTypeOids = {
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
const rowTypeNames = {
  id: "uuid",
  kind: "character varying(40)",
  project_id: "uuid",
  retained_project_id: "uuid",
  user_id: "uuid",
  status: "character varying(20)",
  progress_pct: "integer",
  payload: "jsonb",
  result: "jsonb",
  error_message: "text",
  celery_task_id: "character varying(64)",
  started_at: "timestamp with time zone",
  completed_at: "timestamp with time zone",
  created_at: "timestamp with time zone",
  updated_at: "timestamp with time zone",
};
const functionArguments = {
  "aap_jobs_v1.read_job": "uuid",
  "aap_jobs_v1.jobs_by_execution_id": "text",
  "aap_jobs_v1.insert_video_summary": "uuid, text, uuid, uuid, jsonb, text",
  "aap_jobs_v1.apply_lifecycle": "uuid, jsonb",
  "aap_jobs_v1.mark_dispatch_failed": "uuid, timestamp with time zone, text",
  "aap_jobs_v1.cancel_batch_predict_jobs": "uuid, timestamp with time zone",
  "aap_jobs_v1.delete_batch_predict_jobs": "uuid",
  "aap_jobs_v1.delete_terminal_jobs_before": "timestamp with time zone, text[]",
};
const functionRowTypes = {
  ...rowTypeNames,
  kind: "character varying",
  status: "character varying",
  celery_task_id: "character varying",
};
const functionReturns = {
  "aap_jobs_v1.read_job": `TABLE(${Object.entries(functionRowTypes)
    .map(([name, type]) => `${name} ${type}`)
    .join(", ")})`,
  "aap_jobs_v1.jobs_by_execution_id": `TABLE(${Object.entries(functionRowTypes)
    .map(([name, type]) => `${name} ${type}`)
    .join(", ")})`,
  "aap_jobs_v1.insert_video_summary": "boolean",
  "aap_jobs_v1.apply_lifecycle": "TABLE(applied boolean, updated_at timestamp with time zone)",
  "aap_jobs_v1.mark_dispatch_failed": "boolean",
  "aap_jobs_v1.cancel_batch_predict_jobs": "uuid[]",
  "aap_jobs_v1.delete_batch_predict_jobs": "uuid[]",
  "aap_jobs_v1.delete_terminal_jobs_before": "uuid[]",
};

let phase = "startup";

function requireThat(condition, message) {
  if (!condition) throw new Error(message);
}

function normalizeSql(value) {
  return value.replace(/\s+/g, "").toLowerCase();
}

function signatureFromContract(name, contract) {
  return `${name}(${contract.arguments})`;
}

function rowFields(schema) {
  return schema["x-row-columns"];
}

async function loadContract({ requireFrozen = false } = {}) {
  const [schemaText, sql, frozenSchemaResult, frozenSqlResult] = await Promise.all([
    readFile(schemaPath, "utf8"),
    readFile(sqlPath, "utf8"),
    readFile(frozenSchemaPath, "utf8").then(
      (value) => ({ value }),
      (error) => ({ error }),
    ),
    readFile(frozenSqlPath, "utf8").then(
      (value) => ({ value }),
      (error) => ({ error }),
    ),
  ]);
  const schema = JSON.parse(schemaText);
  const hasFrozenSchema = typeof frozenSchemaResult.value === "string";
  const hasFrozenSql = typeof frozenSqlResult.value === "string";
  if (hasFrozenSchema && hasFrozenSql) {
    assert.deepEqual(JSON.parse(frozenSchemaResult.value), schema);
    assert.equal(frozenSqlResult.value, sql);
  } else {
    assert.equal(
      requireFrozen,
      false,
      "frozen API contract files are required for live acceptance",
    );
    assert.ok(
      frozenSchemaResult.error?.code === "ENOENT" && frozenSqlResult.error?.code === "ENOENT",
      "frozen API contract files must be present together",
    );
  }
  assert.equal(schema.$id, "aap-jobs-lifecycle/v1");
  assert.equal(schema["x-creation-relation"].name, creationRelation);
  assert.deepEqual(schema["x-row-types"], rowTypeNames);
  assert.deepEqual(Object.keys(schema["x-row-types"]).sort(), [...rowFields(schema)].sort());
  assert.deepEqual(Object.keys(rowTypeOids).sort(), [...rowFields(schema)].sort());
  assert.deepEqual(
    Object.keys(schema["x-postgresql-functions"]).sort(),
    [
      "aap_jobs_v1.apply_lifecycle",
      "aap_jobs_v1.cancel_batch_predict_jobs",
      "aap_jobs_v1.delete_batch_predict_jobs",
      "aap_jobs_v1.delete_terminal_jobs_before",
      "aap_jobs_v1.insert_video_summary",
      "aap_jobs_v1.jobs_by_execution_id",
      "aap_jobs_v1.mark_dispatch_failed",
      "aap_jobs_v1.read_job",
    ].sort(),
  );
  assert.deepEqual(Object.keys(rowTypeOids).sort(), [...rowFields(schema)].sort());
  for (const [name, fn] of Object.entries(schema["x-postgresql-functions"])) {
    const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    assert.match(sql, new RegExp(`CREATE FUNCTION\\s+${escapedName}\\s*\\(`, "i"));
    assert.equal(normalizeSql(fn.arguments), normalizeSql(functionArguments[name]));
    assert.equal(normalizeSql(fn.returns), normalizeSql(functionReturns[name]));
  }
  assert.match(sql, /CREATE VIEW aap_jobs_v1\.async_job_creation/i);
  assert.match(sql, /WITH LOCAL CHECK OPTION/i);
  return { schema, sql, frozenMatched: hasFrozenSchema && hasFrozenSql };
}

async function selfCheck() {
  phase = "contract self-check";
  const { schema, frozenMatched } = await loadContract();
  assert.deepEqual(schema["x-creation-relation"].columns, rowFields(schema));
  process.stdout.write(
    frozenMatched
      ? "Jobs lifecycle Node contract self-check passed\n"
      : "Jobs lifecycle owner contract self-check passed; frozen API snapshot is absent in this worktree\n",
  );
}

function parseOwnedConnectionString() {
  requireThat(
    process.env.AAP_MODULE_DATA_ROLE_ACCEPTANCE === "1",
    "module-data role acceptance flag is required",
  );
  const connectionString = process.env.AAP_MODULE_DATA_JOBS_LIFECYCLE_URL;
  requireThat(connectionString, "Jobs lifecycle LOGIN URL is required");
  let url;
  try {
    url = new URL(connectionString);
  } catch {
    throw new Error("Jobs lifecycle LOGIN URL is invalid");
  }
  requireThat(["postgres:", "postgresql:"].includes(url.protocol), "URL must use PostgreSQL");
  requireThat(
    ["localhost", "127.0.0.1", "::1", "[::1]"].includes(url.hostname),
    "URL host must be loopback",
  );
  requireThat(
    decodeURIComponent(url.username) === "jobs_lifecycle_client",
    "URL must use the scoped LOGIN",
  );
  const databaseName = decodeURIComponent(url.pathname.replace(/^\//, ""));
  requireThat(databaseName.endsWith("_test"), "URL must target a disposable *_test database");
  return connectionString;
}

function assertFunctionRow(row, contract, signature) {
  assert.equal(
    normalizeSql(signature),
    normalizeSql(`${row.schema_name}.${row.proname}(${row.arguments})`),
  );
  assert.equal(normalizeSql(row.returns), normalizeSql(contract.returns));
  assert.equal(row.prosecdef, true);
  assert.equal(
    row.provolatile,
    ["read_job", "jobs_by_execution_id"].includes(row.proname) ? "s" : "v",
  );
  assert.deepEqual(row.proconfig, ["search_path=pg_catalog, pg_temp"]);
}

function assertNativeRow(result, columns) {
  assert.deepEqual(
    result.fields.map((field) => field.name),
    columns,
  );
  for (const field of result.fields) assert.equal(field.dataTypeID, rowTypeOids[field.name]);
  assert.equal(result.rows.length, 1);
}

function assertJsonPreserved(value, marker) {
  assert.equal(typeof value, "string", "JSONB must use the raw text parser");
  assert.match(value, /"large_integer"\s*:\s*9007199254740993/);
  assert.ok(value.includes(`"unicode":"${marker}"`) || value.includes(`"unicode": "${marker}"`));
  assert.match(value, /"json_null"\s*:\s*null/);
  assert.match(value, /"json_false"\s*:\s*false/);
}

function makeEnvelope(commands, changedFields, extra = {}) {
  return JSON.stringify({
    schema: "aap-jobs-lifecycle/v1",
    commands,
    projection_changes: {},
    changed_fields: changedFields,
    updated_at_override_present: false,
    updated_at_override: null,
    legacy_changes: {},
    ...extra,
  });
}

async function expectSqlState(client, sql, parameters, sqlState) {
  let failure;
  try {
    await client.query(sql, parameters);
  } catch (error) {
    failure = error;
  }
  assert.equal(failure?.code, sqlState);
}

async function verifyLifecycle() {
  const connectionString = parseOwnedConnectionString();
  const { schema } = await loadContract({ requireFrozen: true });
  phase = "load PostgreSQL driver";
  const pg = await import("pg");
  const Client = pg.Client ?? pg.default?.Client;
  const types = pg.types ?? pg.default?.types;
  requireThat(typeof Client === "function" && types, "pg Client/types are unavailable");
  types.setTypeParser(3802, (value) => value);
  types.setTypeParser(1184, (value) => value);

  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  await client.connect();
  try {
    phase = "LOGIN and privilege boundary";
    const identity = await client.query(
      "SELECT current_user AS current_role, session_user AS login_role",
    );
    assert.deepEqual(identity.rows[0], {
      current_role: "jobs_lifecycle_client",
      login_role: "jobs_lifecycle_client",
    });
    const role = await client.query(
      "SELECT r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreatedb, r.rolcreaterole, " +
        "r.rolreplication, r.rolbypassrls, " +
        "NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m " +
        "WHERE m.member=r.oid OR m.roleid=r.oid) AS no_memberships " +
        "FROM pg_catalog.pg_roles r WHERE r.rolname=current_user",
    );
    assert.deepEqual(Object.values(role.rows[0] ?? {}), [
      true,
      false,
      false,
      false,
      false,
      false,
      false,
      true,
    ]);

    const functionRows = await client.query(
      "SELECT n.nspname AS schema_name, p.oid::text AS oid, p.proname, " +
        "pg_catalog.oidvectortypes(p.proargtypes) AS arguments, " +
        "pg_catalog.pg_get_function_result(p.oid) AS returns, p.prosecdef, " +
        "p.provolatile, p.proconfig, " +
        "pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') AS can_execute, " +
        "EXISTS (SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl, " +
        "pg_catalog.acldefault('f',p.proowner))) a " +
        "WHERE a.grantee=0 AND a.privilege_type='EXECUTE') AS public_execute " +
        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace " +
        "WHERE n.nspname='aap_jobs_v1' ORDER BY p.proname",
    );
    const expectedFunctions = Object.entries(schema["x-postgresql-functions"]);
    const bySignature = new Map(
      functionRows.rows.map((row) => [
        normalizeSql(`${row.schema_name}.${row.proname}(${row.arguments})`),
        row,
      ]),
    );
    for (const [name, contract] of expectedFunctions) {
      const signature = signatureFromContract(name, contract);
      const row = bySignature.get(normalizeSql(signature));
      assert.ok(row, `missing native function ${name}`);
      assertFunctionRow(row, contract, signature);
      assert.equal(row.can_execute, true, `${name} is not executable by the LOGIN`);
      assert.equal(row.public_execute, false, `${name} is executable by PUBLIC`);
    }
    for (const signature of helperSignatures) {
      const row = bySignature.get(normalizeSql(signature));
      assert.ok(row, `missing private helper ${signature}`);
      assert.equal(row.can_execute, false, `${signature} must remain private`);
      assert.equal(row.public_execute, false, `${signature} must not be executable by PUBLIC`);
    }
    const executableAapFunctions = await client.query(
      "SELECT n.nspname || '.' || p.proname || '(' || " +
        "pg_catalog.oidvectortypes(p.proargtypes) || ')' AS signature " +
        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace " +
        "WHERE n.nspname LIKE 'aap\\_%' ESCAPE '\\' " +
        "AND pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') " +
        "ORDER BY signature",
    );
    assert.deepEqual(
      executableAapFunctions.rows.map((row) => normalizeSql(row.signature)),
      expectedFunctions
        .map(([name, contract]) => normalizeSql(signatureFromContract(name, contract)))
        .sort(),
    );

    const viewAndRelations = await client.query(
      "SELECT c.oid::text AS oid, n.nspname, c.relname, c.relkind, " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'SELECT') AS can_select, " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'INSERT') AS can_insert, " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'UPDATE') AS can_update, " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'DELETE') AS can_delete, " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'TRUNCATE') AS can_truncate, " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'REFERENCES') AS can_reference, " +
        "pg_catalog.has_table_privilege(current_user,c.oid,'TRIGGER') AS can_trigger, " +
        "(pg_catalog.has_any_column_privilege(current_user,c.oid,'SELECT') OR " +
        "pg_catalog.has_any_column_privilege(current_user,c.oid,'INSERT') OR " +
        "pg_catalog.has_any_column_privilege(current_user,c.oid,'UPDATE') OR " +
        "pg_catalog.has_any_column_privilege(current_user,c.oid,'REFERENCES')) AS any_column " +
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace " +
        "WHERE c.relkind IN ('r','p','v','m','f') " +
        "AND n.nspname NOT IN ('pg_catalog','information_schema') " +
        "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'",
    );
    for (const relation of viewAndRelations.rows) {
      const isCreationView =
        relation.nspname === "aap_jobs_v1" && relation.relname === "async_job_creation";
      assert.deepEqual(
        [
          relation.can_select,
          relation.can_insert,
          relation.can_update,
          relation.can_delete,
          relation.can_truncate,
          relation.can_reference,
          relation.can_trigger,
          relation.any_column,
        ],
        isCreationView
          ? [true, true, false, false, false, false, false, true]
          : [false, false, false, false, false, false, false, false],
        `unexpected relation capability on ${relation.nspname}.${relation.relname}`,
      );
    }
    assert.ok(
      viewAndRelations.rows.some(
        (row) => row.nspname === "aap_jobs_v1" && row.relname === "async_job_creation",
      ),
    );
    const schemas = await client.query(
      "SELECT n.nspname, pg_catalog.has_schema_privilege(current_user,n.oid,'USAGE') AS can_use, " +
        "pg_catalog.has_schema_privilege(current_user,n.oid,'CREATE') AS can_create " +
        "FROM pg_catalog.pg_namespace n " +
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') " +
        "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'",
    );
    assert.deepEqual(
      schemas.rows
        .filter((row) => row.can_use)
        .map((row) => row.nspname)
        .sort(),
      ["aap_jobs_v1", "public"],
    );
    assert.ok(schemas.rows.every((row) => !row.can_create));
    const unsafeRights = await client.query(
      "SELECT pg_catalog.has_database_privilege(current_user,current_database(),'CREATE') AS db_create, " +
        "pg_catalog.has_database_privilege(current_user,current_database(),'TEMP') AS db_temp, " +
        "EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n " +
        "ON n.oid=c.relnamespace WHERE c.relkind='S' " +
        "AND n.nspname NOT IN ('pg_catalog','information_schema') " +
        "AND n.nspname NOT LIKE 'pg_toast%' " +
        "AND (pg_catalog.has_sequence_privilege(current_user,c.oid,'USAGE') OR " +
        "pg_catalog.has_sequence_privilege(current_user,c.oid,'SELECT') OR " +
        "pg_catalog.has_sequence_privilege(current_user,c.oid,'UPDATE'))) AS sequence_rights",
    );
    assert.deepEqual(Object.values(unsafeRights.rows[0]), [false, false, false]);

    phase = "creation view defaults";
    const jobId = randomUUID();
    const executionId = `node-lifecycle-${randomUUID()}`;
    const created = await client.query(
      `INSERT INTO ${creationRelation} (id,kind,project_id,user_id,payload,celery_task_id) ` +
        "VALUES ($1::pg_catalog.uuid,'jobs_lifecycle_node',NULL,NULL,'{}'::pg_catalog.jsonb,NULL) " +
        "RETURNING id,status,progress_pct,payload,result,retained_project_id,error_message,started_at,completed_at",
      [jobId],
    );
    assert.equal(created.rows[0]?.id, jobId);
    assert.equal(created.rows[0]?.status, "pending");
    assert.equal(created.rows[0]?.progress_pct, 0);
    assert.equal(created.rows[0]?.result, "{}");
    assert.equal(created.rows[0]?.retained_project_id, null);
    assert.equal(created.rows[0]?.error_message, null);
    assert.equal(created.rows[0]?.started_at, null);
    assert.equal(created.rows[0]?.completed_at, null);

    phase = "fixed native row ABI";
    const nativeFunctions = [
      ["aap_jobs_v1.read_job", "uuid"],
      ["aap_jobs_v1.jobs_by_execution_id", "text"],
    ];
    for (const [name, argType] of nativeFunctions) {
      const result = await client.query(`SELECT * FROM ${name}($1::pg_catalog.${argType})`, [
        name.endsWith("read_job") ? jobId : executionId,
      ]);
      if (name.endsWith("jobs_by_execution_id")) {
        assert.equal(result.rows.length, 0);
      } else {
        assertNativeRow(result, rowFields(schema));
        assert.equal(result.rows[0].retained_project_id, null);
      }
    }

    const startedAt = new Date().toISOString();
    const running = await client.query(
      "SELECT * FROM aap_jobs_v1.apply_lifecycle($1::pg_catalog.uuid,$2::pg_catalog.jsonb)",
      [
        jobId,
        makeEnvelope(
          [
            {
              operation: "mark_running",
              reads: { status: "pending", celery_task_id: null },
              values: { status: "running", started_at: startedAt, celery_task_id: executionId },
              written_fields: ["status", "started_at", "celery_task_id"],
              arguments: { celery_task_id: executionId },
            },
          ],
          ["status", "celery_task_id", "started_at"],
        ),
      ],
    );
    assert.deepEqual(running.rows[0]?.applied, true);
    assert.equal(running.fields[0]?.dataTypeID, 16);
    assert.equal(running.fields[1]?.dataTypeID, 1184);

    const rawPayload =
      '{"large_integer":9007199254740993,"unicode":"生命周期验证","json_null":null,"json_false":false}';
    const rawResult =
      '{"large_integer":9007199254740993,"unicode":"结果保持原样","json_null":null,"json_false":false}';
    const payloadMarker = "生命周期验证";
    const resultMarker = "结果保持原样";
    const placeholder = (marker) => `__RAW_JSON_${marker}__`;
    const progressCommand = {
      operation: "update_progress",
      reads: { payload: {} },
      values: {
        result: placeholder("result"),
        progress_pct: 37,
        payload: placeholder("payload"),
      },
      written_fields: ["result", "progress_pct", "payload"],
      arguments: {
        pct: 37,
        extra_payload: placeholder("payload"),
        result: placeholder("result"),
      },
    };
    const progressEnvelope = JSON.stringify({
      schema: "aap-jobs-lifecycle/v1",
      commands: [progressCommand],
      projection_changes: { payload: placeholder("payload"), result: placeholder("result") },
      changed_fields: ["progress_pct", "payload", "result"],
      updated_at_override_present: false,
      updated_at_override: null,
      legacy_changes: {},
    })
      .replaceAll(JSON.stringify(placeholder("payload")), rawPayload)
      .replaceAll(JSON.stringify(placeholder("result")), rawResult);
    phase = "JSONB projection and precision";
    const progress = await client.query(
      "SELECT * FROM aap_jobs_v1.apply_lifecycle($1::pg_catalog.uuid,$2::pg_catalog.jsonb)",
      [jobId, progressEnvelope],
    );
    assert.equal(progress.rows[0]?.applied, true);
    const completedAt = new Date().toISOString();
    const completion = await client.query(
      "SELECT * FROM aap_jobs_v1.apply_lifecycle($1::pg_catalog.uuid,$2::pg_catalog.jsonb)",
      [
        jobId,
        makeEnvelope(
          [
            {
              operation: "mark_complete",
              reads: { status: "running" },
              values: { status: "completed", progress_pct: 100, completed_at: completedAt },
              written_fields: ["status", "progress_pct", "completed_at"],
              arguments: { result: null },
            },
          ],
          ["status", "progress_pct", "completed_at"],
        ),
      ],
    );
    assert.equal(completion.rows[0]?.applied, true);
    const read = await client.query("SELECT * FROM aap_jobs_v1.read_job($1::pg_catalog.uuid)", [
      jobId,
    ]);
    assertNativeRow(read, rowFields(schema));
    const row = read.rows[0];
    assert.equal(row.status, "completed");
    assert.equal(row.progress_pct, 100);
    assert.equal(row.celery_task_id, executionId);
    assert.equal(row.retained_project_id, null);
    assert.ok(typeof row.started_at === "string" && typeof row.completed_at === "string");
    assertJsonPreserved(row.payload, payloadMarker);
    assertJsonPreserved(row.result, resultMarker);

    const byExecution = await client.query(
      "SELECT * FROM aap_jobs_v1.jobs_by_execution_id($1::pg_catalog.text)",
      [executionId],
    );
    assertNativeRow(byExecution, rowFields(schema));
    assert.equal(byExecution.rows[0].id, jobId);
    assert.equal(byExecution.rows[0].status, "completed");
    assert.equal(byExecution.rows[0].result, row.result);

    phase = "denied legacy and private writes";
    await expectSqlState(
      client,
      "SELECT * FROM aap_jobs_v1.apply_lifecycle($1::pg_catalog.uuid,$2::pg_catalog.jsonb)",
      [jobId, makeEnvelope([], ["status"], { legacy_changes: { status: "failed" } })],
      "42501",
    );
    await expectSqlState(
      client,
      "SELECT * FROM aap_jobs_v1.apply_lifecycle($1::pg_catalog.uuid,$2::pg_catalog.jsonb)",
      [
        jobId,
        makeEnvelope([], ["updated_at"], {
          updated_at_override_present: true,
          updated_at_override: "2026-01-01T00:00:00Z",
        }),
      ],
      "42501",
    );
    await expectSqlState(
      client,
      "SELECT * FROM aap_jobs_v1.apply_lifecycle($1::pg_catalog.uuid,$2::pg_catalog.jsonb)",
      [jobId, makeEnvelope([], [], { unexpected: true })],
      "22023",
    );
    await expectSqlState(
      client,
      `INSERT INTO ${creationRelation} (id,kind,status) ` +
        "VALUES ($1::pg_catalog.uuid,'jobs_lifecycle_node','completed')",
      [randomUUID()],
      "44000",
    );
    await expectSqlState(
      client,
      "SELECT aap_jobs_v1._jobs_lifecycle_has_keys('{}'::pg_catalog.jsonb,ARRAY[]::pg_catalog.text[])",
      [],
      "42501",
    );
    await expectSqlState(
      client,
      "SELECT id FROM public.async_jobs WHERE id=$1::pg_catalog.uuid",
      [jobId],
      "42501",
    );
    await expectSqlState(
      client,
      "UPDATE public.async_jobs SET kind=kind WHERE id=$1::pg_catalog.uuid",
      [jobId],
      "42501",
    );

    process.stdout.write(
      `${JSON.stringify({
        case: "jobs-lifecycle-node-consumer",
        status: "passed",
        login: "jobs_lifecycle_client",
        functionCapabilities: expectedFunctions.length,
        rowColumns: rowFields(schema).length,
        rawJsonb: "precision-unicode-null-false",
        retainedJobUntilVerifierTeardown: true,
      })}\n`,
    );
  } finally {
    await client.end();
  }
}

async function main() {
  if (process.argv.includes("--self-check")) return selfCheck();
  if (process.argv.includes("--live")) return verifyLifecycle();
  throw new Error("select --self-check or --live");
}

main().catch((error) => {
  const code = typeof error?.code === "string" ? `/${error.code}` : "";
  process.stderr.write(
    `Jobs lifecycle acceptance failed at ${phase}: ${error?.name ?? "Error"}${code}\n`,
  );
  process.exitCode = 1;
});
