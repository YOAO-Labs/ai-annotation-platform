import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const schemaPath = path.resolve(here, "../../src/aap_audit/query.schema.json");
const protocol = "aap-audit-query/v1";
const maxAuditId = 9223372036854775807n;
const minAuditId = -9223372036854775808n;
const eventKeys = [
  "action",
  "actor_email",
  "actor_id",
  "actor_role",
  "created_at",
  "detail_json",
  "id",
  "ip",
  "method",
  "path",
  "request_id",
  "status_code",
  "target_id",
  "target_type",
];

async function loadSchema() {
  return JSON.parse(await readFile(schemaPath, "utf8"));
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

function timestampMicroseconds(value) {
  // Date normalizes offsets but drops the submillisecond digits PostgreSQL keeps.
  const fraction = value.match(/\.(\d+)/)?.[1] ?? "";
  return BigInt(Date.parse(value)) * 1000n + BigInt(fraction.padEnd(6, "0").slice(3, 6));
}

function assertQueryReply(reply, operation, pageSize) {
  assert.deepEqual(Object.keys(reply).sort(), ["data", "ok", "schema"]);
  assert.equal(reply.schema, protocol);
  assert.equal(reply.ok, true);
  assert.deepEqual(Object.keys(reply.data).sort(), ["events", "operation", "total"]);
  assert.equal(reply.data.operation, operation);
  assert.equal(typeof reply.data.total, "string");
  assert.match(reply.data.total, /^(?:0|[1-9][0-9]{0,18})$/);
  assert.ok(BigInt(reply.data.total) <= maxAuditId);
  assert.ok(Array.isArray(reply.data.events));
  if (operation === "list") {
    assert.ok(reply.data.events.length <= pageSize);
    assert.ok(BigInt(reply.data.events.length) <= BigInt(reply.data.total));
  }
  for (const event of reply.data.events) {
    assert.deepEqual(Object.keys(event).sort(), eventKeys);
    assert.match(event.id, /^(?:0|-?[1-9][0-9]*)$/);
    assert.ok(event.id.length <= 20);
    assert.ok(BigInt(event.id) >= minAuditId && BigInt(event.id) <= maxAuditId);
    assert.equal(typeof event.action, "string");
    assert.ok(event.action.length <= 64);
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
      if (event[field] !== null) assert.ok(event[field].length <= maxLength);
    }
    assert.ok(
      event.actor_id === null ||
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(event.actor_id),
    );
    assert.ok(
      event.status_code === null ||
        (Number.isInteger(event.status_code) &&
          event.status_code >= -32768 &&
          event.status_code <= 32767),
    );
    assert.equal(typeof event.created_at, "string");
    assert.ok(Number.isFinite(Date.parse(event.created_at)));
  }
  assert.equal(
    new Set(
      reply.data.events.map((event) =>
        JSON.stringify([event.id, timestampMicroseconds(event.created_at).toString()]),
      ),
    ).size,
    reply.data.events.length,
  );
}

function request(operation = "list") {
  return {
    schema: protocol,
    operation,
    filters: {
      action: null,
      target_type: null,
      target_id: null,
      actor_id: null,
      from: null,
      to: null,
      business_only: false,
      detail_key: null,
      detail_value: null,
    },
    page: 1,
    page_size: operation === "export" ? 50000 : 2,
    after: null,
  };
}

export async function selfCheck() {
  const schema = await loadSchema();
  assert.equal(schema.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(schema.$id, protocol);
  checkReferences(schema);

  const query = request();
  assert.deepEqual(Object.keys(query).sort(), [
    "after",
    "filters",
    "operation",
    "page",
    "page_size",
    "schema",
  ]);
  assert.deepEqual(Object.keys(query.filters).sort(), [
    "action",
    "actor_id",
    "business_only",
    "detail_key",
    "detail_value",
    "from",
    "target_id",
    "target_type",
    "to",
  ]);

  const largeIdReply = {
    schema: protocol,
    ok: true,
    data: {
      operation: "list",
      total: maxAuditId.toString(),
      events: [
        {
          id: maxAuditId.toString(),
          actor_id: null,
          actor_email: "audit@example.test",
          actor_role: "super_admin",
          action: "audit.query.conformance",
          target_type: "audit_logs",
          target_id: "9223372036854775807",
          method: "GET",
          path: "/api/v1/audit-logs",
          status_code: 200,
          ip: "2001:db8::1",
          detail_json: null,
          request_id: "request-1",
          created_at: "2026-10-23T12:34:56.123456Z",
        },
      ],
    },
  };
  assertQueryReply(largeIdReply, "list", 2);
  const signedIdReply = {
    schema: protocol,
    ok: true,
    data: {
      operation: "list",
      total: "2",
      events: [
        { ...largeIdReply.data.events[0], id: "0" },
        { ...largeIdReply.data.events[0], id: minAuditId.toString() },
      ],
    },
  };
  assertQueryReply(signedIdReply, "list", 2);
  const sameIdReply = {
    ...largeIdReply,
    data: {
      ...largeIdReply.data,
      total: "2",
      events: [
        largeIdReply.data.events[0],
        { ...largeIdReply.data.events[0], created_at: "2026-10-23T12:34:56.123457Z" },
      ],
    },
  };
  assertQueryReply(sameIdReply, "list", 2);
  for (const createdAt of [
    "2026-10-23T12:34:56.123456+00:00",
    "2026-10-23T14:34:56.123456+02:00",
  ]) {
    assert.throws(() =>
      assertQueryReply(
        {
          ...sameIdReply,
          data: {
            ...sameIdReply.data,
            events: [
              sameIdReply.data.events[0],
              { ...sameIdReply.data.events[1], created_at: createdAt },
            ],
          },
        },
        "list",
        2,
      ),
    );
  }
  assert.throws(() =>
    assertQueryReply(
      {
        ...sameIdReply,
        data: {
          ...sameIdReply.data,
          events: [sameIdReply.data.events[0], sameIdReply.data.events[0]],
        },
      },
      "list",
      2,
    ),
  );
  assert.throws(() =>
    assertQueryReply({ ...largeIdReply, data: { ...largeIdReply.data, total: "0" } }, "list", 2),
  );
  assert.throws(() =>
    assertQueryReply(
      {
        ...largeIdReply,
        data: { ...largeIdReply.data, total: "9223372036854775808" },
      },
      "list",
      2,
    ),
  );
  process.stdout.write("Audit query schema self-check passed\n");
}

export async function verifyQueryCommand() {
  const { Client } = await import("pg");
  const connectionString = process.env.AUDIT_QUERY_DATABASE_URL;
  assert.ok(connectionString, "missing owned function-only Audit query client DSN");
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  await client.connect();
  try {
    const result = await client.query(
      "SELECT aap_audit_v1.query_events($1::pg_catalog.jsonb) AS reply",
      [JSON.stringify(request())],
    );
    assertQueryReply(result.rows[0].reply, "list", 2);

    await assert.rejects(
      client.query("SELECT id FROM public.audit_logs LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT id FROM aap_identity_v1.account_profiles LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT aap_audit_v1.append_events($1::pg_catalog.jsonb)", [
        JSON.stringify({ schema: "aap-audit-append/v1", events: [] }),
      ]),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("INSERT INTO public.audit_logs (action) VALUES ('audit.denied')"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT nextval(pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id'))"),
      (error) => error.code === "42501",
    );
    process.stdout.write("Audit query PostgreSQL owner check passed\n");
  } finally {
    await client.end();
  }
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else if (process.argv.includes("--live")) {
  await verifyQueryCommand();
}
