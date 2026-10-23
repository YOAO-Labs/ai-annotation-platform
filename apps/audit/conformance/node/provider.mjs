import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "pg";

const here = path.dirname(fileURLToPath(import.meta.url));
const schemaPath = path.resolve(here, "../../src/aap_audit/append.schema.json");
const protocol = "aap-audit-append/v1";
const maxAuditId = 9223372036854775807n;
const canonicalAuditIdPattern = [
  "^(?:",
  "[1-9][0-9]{0,17}|[1-8][0-9]{18}|9[01][0-9]{17}|92[01][0-9]{16}|",
  "922[0-2][0-9]{15}|9223[0-2][0-9]{14}|92233[0-6][0-9]{13}|",
  "922337[01][0-9]{12}|92233720[0-2][0-9]{10}|922337203[0-5][0-9]{9}|",
  "9223372036[0-7][0-9]{8}|92233720368[0-4][0-9]{7}|",
  "922337203685[0-3][0-9]{6}|9223372036854[0-6][0-9]{5}|",
  "92233720368547[0-6][0-9]{4}|922337203685477[0-4][0-9]{3}|",
  "9223372036854775[0-7][0-9]{2}|922337203685477580[0-7]",
  ")$",
].join("");
const canonicalAuditId = new RegExp(canonicalAuditIdPattern);

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

function assertAppendReply(value) {
  assert.deepEqual(Object.keys(value).sort(), ["data", "ok", "schema"]);
  assert.equal(value.schema, protocol);
  assert.equal(value.ok, true);
  assert.deepEqual(Object.keys(value.data).sort(), ["events"]);
  assert.ok(Array.isArray(value.data.events));
  for (const receipt of value.data.events) {
    assert.deepEqual(Object.keys(receipt).sort(), ["created_at", "id"]);
    assert.match(receipt.id, canonicalAuditId);
    assert.ok(receipt.id.length <= 19);
    assert.ok(BigInt(receipt.id) <= maxAuditId);
    assert.equal(typeof receipt.created_at, "string");
    assert.ok(Number.isFinite(Date.parse(receipt.created_at)));
  }
}

export async function selfCheck() {
  const schema = await loadSchema();
  assert.equal(schema.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(schema.$id, protocol);
  checkReferences(schema);
  const idSchema = schema.$defs.AppendReceipt.properties.id;
  assert.equal(idSchema.pattern, canonicalAuditIdPattern);
  assert.equal(idSchema.maxLength, 19);

  const event = {
    actor_id: null,
    actor_email: null,
    actor_role: null,
    action: "future.owner_specific.action",
    target_type: null,
    target_id: null,
    method: "POST",
    path: "/api/v1/example",
    status_code: 201,
    ip: null,
    detail_json: null,
    request_id: null,
  };
  const request = { schema: protocol, events: [event] };
  assert.deepEqual(Object.keys(request).sort(), ["events", "schema"]);
  assert.ok(request.events[0].action.startsWith("future."));
  assert.equal(Object.hasOwn(request.events[0], "created_at"), false);

  const largeIdReply = {
    schema: protocol,
    ok: true,
    data: {
      events: [{ id: maxAuditId.toString(), created_at: "2026-10-23T00:00:00Z" }],
    },
  };
  assertAppendReply(largeIdReply);
  assert.equal(typeof largeIdReply.data.events[0].id, "string");
  assert.throws(() =>
    assertAppendReply({
      ...largeIdReply,
      data: {
        events: [{ id: "9223372036854775808", created_at: "2026-10-23T00:00:00Z" }],
      },
    }),
  );
  process.stdout.write("Audit append schema self-check passed\n");
}

export async function verifyOwnerCommand() {
  const connectionString = process.env.AUDIT_APPEND_DATABASE_URL;
  assert.ok(connectionString, "missing owned function-only Audit client DSN");
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  await client.connect();
  try {
    const event = {
      actor_id: null,
      actor_email: null,
      actor_role: null,
      action: "audit.append.conformance",
      target_type: null,
      target_id: null,
      method: "POST",
      path: "/audit-conformance",
      status_code: 200,
      ip: null,
      detail_json: null,
      request_id: null,
    };
    const request = { schema: protocol, events: [event] };
    const result = await client.query("SELECT aap_audit_v1.append_events($1::jsonb) AS reply", [
      JSON.stringify(request),
    ]);
    const reply = result.rows[0].reply;
    assertAppendReply(reply);
    assert.equal(reply.data.events.length, 1);

    await assert.rejects(
      client.query("SELECT id FROM public.audit_logs LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT id FROM public.users LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("INSERT INTO public.audit_logs (action) VALUES ('direct.denied')"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT nextval(pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id'))"),
      (error) => error.code === "42501",
    );
    process.stdout.write("Audit append PostgreSQL owner check passed\n");
  } finally {
    await client.end();
  }
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else if (process.argv.includes("--live")) {
  await verifyOwnerCommand();
}
