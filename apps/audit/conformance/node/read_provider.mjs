import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const schemaPath = path.resolve(here, "../../src/aap_audit/read_relations.schema.json");
const dailySchemaPath = path.resolve(here, "../../src/aap_audit/daily_projection.schema.json");
const relationName = "aap_audit_v1.audit_events";
const dailyRelationName = "aap_audit_v1.audit_daily_facts";
const refreshFunctionName = "aap_audit_v1.refresh_audit_bi_mv()";
const protocol = "aap-audit-read-relations/v1";
const dailyProtocol = "aap-audit-daily-projection/v1";
const minAuditId = -9223372036854775808n;
const maxAuditId = 9223372036854775807n;
const columns = [
  "id",
  "actor_id",
  "actor_email",
  "actor_role",
  "action",
  "target_type",
  "target_id",
  "method",
  "path",
  "status_code",
  "ip",
  "detail_json",
  "request_id",
  "created_at",
];
const columnTypes = {
  id: "bigint",
  actor_id: "uuid",
  actor_email: "character varying(255)",
  actor_role: "character varying(32)",
  action: "character varying(64)",
  target_type: "character varying(32)",
  target_id: "character varying(64)",
  method: "character varying(8)",
  path: "character varying(256)",
  status_code: "smallint",
  ip: "inet",
  detail_json: "jsonb",
  request_id: "character varying(36)",
  created_at: "timestamp with time zone",
};
const dailyColumns = ["day", "action", "target_type", "actor_role", "status_family", "event_count"];
const dailyColumnTypes = {
  day: "date",
  action: "character varying(64)",
  target_type: "character varying",
  actor_role: "character varying",
  status_family: "smallint",
  event_count: "bigint",
};

async function loadSchema() {
  return JSON.parse(await readFile(schemaPath, "utf8"));
}

async function loadDailySchema() {
  return JSON.parse(await readFile(dailySchemaPath, "utf8"));
}

function checkReferences(schema) {
  assert.ok(schema.$defs && typeof schema.$defs === "object");
  const visit = (value) => {
    if (Array.isArray(value)) {
      for (const child of value) visit(child);
      return;
    }
    if (value === null || typeof value !== "object") return;
    if (value.$ref !== undefined) {
      assert.ok(value.$ref.startsWith("#/$defs/"));
      assert.ok(schema.$defs[value.$ref.slice("#/$defs/".length)]);
    }
    for (const child of Object.values(value)) visit(child);
  };
  visit(schema);
}

function assertEvent(event) {
  assert.deepEqual(Object.keys(event).sort(), [...columns].sort());
  assert.equal(typeof event.id, "string");
  assert.match(event.id, /^(?:0|-?[1-9][0-9]*)$/);
  assert.ok(event.id.length <= 20);
  const id = BigInt(event.id);
  assert.ok(id >= minAuditId && id <= maxAuditId);
  assert.ok(
    event.actor_id === null ||
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(event.actor_id),
  );
  for (const [field, maxLength] of [
    ["actor_email", 255],
    ["actor_role", 32],
    ["target_type", 32],
    ["target_id", 64],
    ["method", 8],
    ["path", 256],
    ["ip", 64],
    ["request_id", 36],
  ]) {
    assert.ok(event[field] === null || typeof event[field] === "string");
    if (event[field] !== null) assert.ok([...event[field]].length <= maxLength);
  }
  assert.equal(typeof event.action, "string");
  assert.ok([...event.action].length <= 64);
  assert.ok(
    event.status_code === null ||
      (Number.isInteger(event.status_code) &&
        event.status_code >= -32768 &&
        event.status_code <= 32767),
  );
  assert.ok(Object.hasOwn(event, "detail_json"));
  const createdAt =
    event.created_at instanceof Date ? event.created_at.toISOString() : event.created_at;
  assert.equal(typeof createdAt, "string");
  assert.ok(Number.isFinite(Date.parse(createdAt)));
}

function assertDailyFact(fact) {
  assert.deepEqual(Object.keys(fact).sort(), [...dailyColumns].sort());
  const day = fact.day;
  assert.equal(typeof day, "string");
  assert.match(day, /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/);
  const parsedDay = new Date(`${day}T00:00:00Z`);
  assert.ok(Number.isFinite(parsedDay.valueOf()));
  assert.equal(parsedDay.toISOString().slice(0, 10), day);
  for (const [field, maxLength] of [
    ["action", 64],
    ["target_type", 32],
    ["actor_role", 32],
  ]) {
    assert.equal(typeof fact[field], "string");
    assert.ok([...fact[field]].length <= maxLength);
  }
  assert.ok(
    Number.isInteger(fact.status_family) &&
      fact.status_family >= -32768 &&
      fact.status_family <= 32767,
  );
  assert.equal(typeof fact.event_count, "string");
  assert.match(fact.event_count, /^(?:0|[1-9][0-9]*)$/);
  assert.ok(fact.event_count.length <= 19);
  const count = BigInt(fact.event_count);
  assert.ok(count >= 0n && count <= maxAuditId);
}

export async function selfCheck() {
  const schema = await loadSchema();
  assert.equal(schema.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(schema.$id, protocol);
  checkReferences(schema);

  const relation = schema["x-postgresql-relations"]?.[relationName];
  assert.deepEqual(relation?.columns, columns);
  assert.deepEqual(relation?.column_types, columnTypes);
  assert.equal(relation?.row_key?.join(","), "id,created_at");
  assert.equal(relation?.read_only, true);
  assert.equal(relation?.security_barrier, true);
  assert.deepEqual(relation?.nullable_columns, [
    "actor_id",
    "actor_email",
    "actor_role",
    "target_type",
    "target_id",
    "method",
    "path",
    "status_code",
    "ip",
    "detail_json",
    "request_id",
  ]);

  const event = {
    id: maxAuditId.toString(),
    actor_id: "00000000-0000-4000-8000-000000000320",
    actor_email: "audit@example.test",
    actor_role: "super_admin",
    action: "audit.read.conformance",
    target_type: "audit_logs",
    target_id: "9223372036854775807",
    method: "GET",
    path: "/api/v1/audit-logs",
    status_code: 200,
    ip: "2001:db8::1",
    detail_json: { source: "audit_events", nested: [null, true, 4] },
    request_id: "request-320",
    created_at: "2026-10-24T12:34:56.123456Z",
  };
  assertEvent(event);
  assertEvent({ ...event, action: "😀".repeat(64) });
  assertEvent({ ...event, id: "0", ip: null, detail_json: null });
  assertEvent({ ...event, id: minAuditId.toString(), ip: "2001:db8::2/64" });
  assert.throws(() => assertEvent({ ...event, id: "9223372036854775808" }));
  assert.throws(() => assertEvent({ ...event, status_code: 32768 }));

  const dailySchema = await loadDailySchema();
  assert.equal(dailySchema.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(dailySchema.$id, dailyProtocol);
  checkReferences(dailySchema);
  const dailyRelation = dailySchema["x-postgresql-relations"]?.[dailyRelationName];
  assert.deepEqual(dailyRelation?.columns, dailyColumns);
  assert.deepEqual(dailyRelation?.column_types, dailyColumnTypes);
  assert.deepEqual(dailyRelation?.row_key, [
    "day",
    "action",
    "target_type",
    "actor_role",
    "status_family",
  ]);
  assert.deepEqual(dailyRelation?.row, { $ref: "#/$defs/AuditDailyFact" });
  assert.deepEqual(dailyRelation?.nullable_columns, []);
  assert.equal(dailyRelation?.read_only, true);
  assert.equal(dailyRelation?.security_barrier, true);
  const refresh = dailySchema["x-postgresql-functions"]?.[refreshFunctionName];
  assert.deepEqual(refresh?.argument_types, []);
  assert.equal(refresh?.returns, "void");
  assert.deepEqual(refresh?.required_owner_database_privileges, ["TEMPORARY"]);
  assert.equal(refresh?.security_definer, true);
  assert.equal(refresh?.concurrent, true);
  assert.equal(refresh?.caller_commit_required, true);
  const dailyFact = {
    day: "2026-10-24",
    action: "task.submit",
    target_type: "task",
    actor_role: "reviewer",
    status_family: 2,
    event_count: maxAuditId.toString(),
  };
  assertDailyFact(dailyFact);
  assertDailyFact({ ...dailyFact, action: "😀".repeat(64) });
  assert.throws(() => assertDailyFact({ ...dailyFact, event_count: "9223372036854775808" }));
  assert.throws(() =>
    assertDailyFact({ ...dailyFact, day: new Date("2026-10-24T00:00:00+08:00") }),
  );

  process.stdout.write("Audit read and daily-projection schema self-check passed\n");
}

export async function verifyReadRelation() {
  const { Client, types } = await import("pg");
  const connectionString = process.env.AUDIT_READ_DATABASE_URL;
  assert.ok(connectionString, "missing owned read-only Audit relation LOGIN DSN");
  const client = new Client({
    connectionString,
    connectionTimeoutMillis: 5000,
    types: {
      getTypeParser(oid, format) {
        // PostgreSQL DATE is a calendar value, not a timezone-dependent instant.
        return oid === 1082 ? (value) => value : types.getTypeParser(oid, format);
      },
    },
  });
  await client.connect();
  try {
    const relation = await client.query(
      `SELECT * FROM ${relationName} ORDER BY created_at DESC, id DESC LIMIT 1`,
    );
    if (relation.rows.length) assertEvent(relation.rows[0]);
    const columnNames = relation.fields.map((field) => field.name);
    assert.deepEqual(columnNames, columns);

    const dailyRelation = await client.query(
      `SELECT * FROM ${dailyRelationName} ORDER BY day DESC, action, target_type, actor_role, status_family LIMIT 1`,
    );
    if (dailyRelation.rows.length) assertDailyFact(dailyRelation.rows[0]);
    assert.deepEqual(
      dailyRelation.fields.map((field) => field.name),
      dailyColumns,
    );

    await assert.rejects(
      client.query("SELECT id FROM public.audit_logs LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT * FROM aap_identity_v1.account_profiles LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT aap_audit_v1.query_events('{}'::pg_catalog.jsonb)"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT aap_audit_v1.refresh_audit_bi_mv()"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT * FROM public.mv_audit_bi_daily LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT aap_audit_v1.append_events('{}'::pg_catalog.jsonb)"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("INSERT INTO public.audit_logs (action) VALUES ('audit.denied')"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("INSERT INTO aap_audit_v1.audit_events (action) VALUES ('audit.denied')"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("UPDATE aap_audit_v1.audit_events SET action='audit.denied'"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("DELETE FROM aap_audit_v1.audit_events"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT nextval(pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id'))"),
      (error) => error.code === "42501",
    );
    process.stdout.write("Audit read-only PostgreSQL owner check passed\n");
  } finally {
    await client.end();
  }
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else if (process.argv.includes("--live")) {
  await verifyReadRelation();
}
