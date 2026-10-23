import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { Client } from "pg";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.resolve(here, "../../src/aap_notifications/terminal_append.schema.json");

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
  assert.equal(contract.$id, "aap-notification-terminal-append/v1");
  assertClosedSchemaReferences(contract);
  const request = contract.$defs.TerminalNotificationRequest;
  assert.equal(request.additionalProperties, false);
  assert.deepEqual(request.properties.type.enum, ["job.completed", "job.failed", "job.cancelled"]);
  assert.equal(contract.$defs.TerminalNotificationData.properties.created.type, "boolean");
  process.stdout.write("Notifications terminal-append schema self-check passed\n");
}

async function verifyOwnerAppend() {
  const connectionString = process.env.NOTIFICATION_APPEND_DATABASE_URL;
  const userId = process.env.NOTIFICATION_APPEND_USER_ID;
  assert.ok(connectionString, "missing owned append client DSN");
  assert.match(userId ?? "", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  const contract = await loadContract();
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });

  const request = (status, fields = {}) => ({
    schema: contract.$id,
    operation: "append_job_terminal",
    id: crypto.randomUUID(),
    user_id: userId,
    type: `job.${status}`,
    target_id: crypto.randomUUID(),
    payload: { kind: "batch_predict", status },
    ...fields,
  });

  async function append(value) {
    const result = await client.query(
      "SELECT aap_notifications_v1.append_job_terminal_notification($1::jsonb) AS reply",
      [JSON.stringify(value)],
    );
    return result.rows[0].reply;
  }

  await client.connect();
  try {
    const role = await client.query("SELECT current_user AS role");
    assert.equal(role.rows[0].role, "notification_append_client");

    const firstRequest = request("completed");
    const first = await append(firstRequest);
    assert.equal(first.ok, true);
    assert.equal(first.data.operation, "append_job_terminal");
    assert.equal(first.data.created, true);
    assert.equal(first.data.notification.id, firstRequest.id);
    assert.equal(first.data.notification.user_id, userId);
    assert.equal(first.data.notification.target_type, "async_job");
    assert.equal(first.data.notification.target_id, firstRequest.target_id);
    assert.deepEqual(first.data.notification.payload, firstRequest.payload);
    assert.equal(first.data.notification.read_at, null);
    assert.match(first.data.notification.created_at, /^\d{4}-\d\d-\d\dT.*Z$/);

    const competingRequest = request("completed", {
      id: crypto.randomUUID(),
      target_id: firstRequest.target_id,
      payload: { kind: "other", status: "completed" },
    });
    const existing = await append(competingRequest);
    assert.equal(existing.ok, true);
    assert.equal(existing.data.created, false);
    assert.equal(existing.data.notification.id, firstRequest.id);
    assert.deepEqual(existing.data.notification.payload, firstRequest.payload);

    const rollbackRequest = request("failed");
    await client.query("BEGIN");
    const beforeRollback = await append(rollbackRequest);
    assert.equal(beforeRollback.data.created, true);
    await client.query("ROLLBACK");
    const afterRollback = await append(rollbackRequest);
    assert.equal(afterRollback.data.created, true);

    const invalid = await append(request("completed", { unexpected: true }));
    assert.deepEqual(invalid.error, { code: "invalid_request" });

    await assert.rejects(
      client.query("SELECT id FROM public.notifications LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query("SELECT channels FROM public.notification_preferences LIMIT 1"),
      (error) => error.code === "42501",
    );
    await assert.rejects(
      client.query(
        "INSERT INTO public.notifications " +
          "(id,user_id,type,target_type,target_id,payload) VALUES " +
          "($1::uuid,$2::uuid,'job.completed','async_job',$3::uuid,'{}'::jsonb)",
        [crypto.randomUUID(), userId, crypto.randomUUID()],
      ),
      (error) => error.code === "42501",
    );
  } finally {
    await client.end();
  }
  process.stdout.write("Notifications terminal-append owner check passed\n");
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else {
  await verifyOwnerAppend();
}
