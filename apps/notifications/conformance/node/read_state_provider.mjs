import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.resolve(here, "../../src/aap_notifications/read_state.schema.json");

async function loadContract() {
  return JSON.parse(await readFile(contractPath, "utf8"));
}

function assertClosedSchemaReferences(contract) {
  assert.ok(contract.$defs && typeof contract.$defs === "object");
  const visit = (value) => {
    if (Array.isArray(value)) {
      for (const child of value) visit(child);
      return;
    }
    if (value === null || typeof value !== "object") return;
    if (value.$ref !== undefined) {
      assert.ok(value.$ref.startsWith("#/$defs/"));
      assert.ok(contract.$defs[value.$ref.slice("#/$defs/".length)]);
    }
    for (const child of Object.values(value)) visit(child);
  };
  visit(contract);
}

export async function selfCheck() {
  const contract = await loadContract();
  assert.equal(contract.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(contract.$id, "aap-notification-read-state/v1");
  assert.equal(contract.title, "AAP notification read-state protocol");
  assertClosedSchemaReferences(contract);
  for (const name of ["MarkReadRequest", "MarkAllReadRequest"]) {
    const request = contract.$defs[name];
    assert.equal(request.additionalProperties, false);
    assert.ok(request.required.includes("user_id"));
    assert.ok(request.required.includes("read_at"));
  }
  assert.ok(contract.$defs.MarkReadRequest.required.includes("notification_id"));
  process.stdout.write("Notifications read-state schema self-check passed\n");
}

async function verifyOwnerCommands() {
  const { Client } = await import("pg");
  const connectionString = process.env.NOTIFICATION_READ_STATE_DATABASE_URL;
  const userId = process.env.NOTIFICATION_READ_STATE_USER_ID;
  assert.ok(connectionString, "missing owned read-state client DSN");
  assert.match(userId ?? "", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });

  async function command(value) {
    const result = await client.query(
      "SELECT aap_notifications_v1.read_state_command($1::jsonb) AS reply",
      [JSON.stringify(value)],
    );
    return result.rows[0].reply;
  }

  async function append(type) {
    const request = {
      schema: "aap-notification-append/v1",
      operation: "append",
      id: crypto.randomUUID(),
      user_id: userId,
      type,
      target_type: "read-state-conformance",
      target_id: crypto.randomUUID(),
      payload: {},
    };
    const result = await client.query(
      "SELECT aap_notifications_v1.append_notification($1::jsonb) AS reply",
      [JSON.stringify(request)],
    );
    assert.equal(result.rows[0].reply.ok, true);
    return result.rows[0].reply.data.notification.id;
  }

  const request = (operation, fields = {}) => ({
    schema: "aap-notification-read-state/v1",
    operation,
    user_id: userId,
    read_at: new Date().toISOString(),
    ...fields,
  });

  await client.connect();
  try {
    const role = await client.query("SELECT current_user AS role");
    assert.equal(
      role.rows[0].role,
      process.env.NOTIFICATION_READ_STATE_EXPECTED_ROLE ?? "notification_append_client",
    );

    await client.query("BEGIN");
    const baseline = await command(request("mark_all_read"));
    assert.equal(baseline.ok, true);
    const oneId = await append("read-state.one");
    const allId = await append("read-state.all");
    await client.query("COMMIT");

    await client.query("BEGIN");
    const wrongUser = await command(
      request("mark_read", {
        user_id: crypto.randomUUID(),
        notification_id: oneId,
      }),
    );
    assert.deepEqual(wrongUser.data, {
      operation: "mark_read",
      updated: false,
      updated_ids: [],
    });

    const marked = await command(request("mark_read", { notification_id: oneId }));
    assert.deepEqual(marked.data, {
      operation: "mark_read",
      updated: true,
      updated_ids: [oneId],
    });
    const repeated = await command(request("mark_read", { notification_id: oneId }));
    assert.equal(repeated.data.updated, false);
    assert.deepEqual(repeated.data.updated_ids, []);

    const bulk = await command(request("mark_all_read"));
    assert.deepEqual(bulk.data, {
      operation: "mark_all_read",
      updated: 1,
      updated_ids: [allId],
    });
    const noOp = await command(request("mark_all_read"));
    assert.deepEqual(noOp.data, {
      operation: "mark_all_read",
      updated: 0,
      updated_ids: [],
    });
    await client.query("ROLLBACK");

    await client.query("BEGIN");
    const afterRollback = await command(request("mark_read", { notification_id: oneId }));
    assert.equal(afterRollback.data.updated, true);
    await client.query("ROLLBACK");

    const invalid = await command({
      ...request("mark_all_read"),
      unexpected: true,
    });
    assert.deepEqual(invalid.error, { code: "invalid_request" });

    await assert.rejects(
      client.query("SELECT read_at FROM public.notifications LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("UPDATE public.notifications SET read_at=now() WHERE false"),
      (error) => error.code === "42501",
    );
  } finally {
    await client.end();
  }
  process.stdout.write("Notifications read-state owner check passed\n");
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else {
  await verifyOwnerCommands();
}
