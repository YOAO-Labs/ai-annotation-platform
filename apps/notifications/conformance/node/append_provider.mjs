import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { Client } from "pg";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.resolve(here, "../../src/aap_notifications/append.schema.json");

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
  assert.equal(contract.$id, "aap-notification-append/v1");
  assert.equal(contract.title, "AAP notification append protocol");
  assertClosedSchemaReferences(contract);
  const request = contract.$defs.AppendNotificationRequest;
  assert.equal(request.additionalProperties, false);
  assert.ok(request.required.includes("payload"));
  process.stdout.write("Notifications append schema self-check passed\n");
}

async function verifyOwnerAppend() {
  const connectionString = process.env.NOTIFICATION_APPEND_DATABASE_URL;
  const userId = process.env.NOTIFICATION_APPEND_USER_ID;
  assert.ok(connectionString, "missing owned append client DSN");
  assert.match(userId ?? "", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  const contract = await loadContract();
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });

  const request = (type, fields = {}) => ({
    schema: contract.$id,
    operation: "append",
    id: crypto.randomUUID(),
    user_id: userId,
    type,
    target_type: "task",
    target_id: crypto.randomUUID(),
    payload: {},
    ...fields,
  });

  async function append(value) {
    const result = await client.query(
      "SELECT aap_notifications_v1.append_notification($1::jsonb) AS reply",
      [JSON.stringify(value)],
    );
    return result.rows[0].reply;
  }

  async function isMuted(type) {
    const result = await client.query(
      "SELECT aap_notifications_v1.preference_command($1::jsonb) AS reply",
      [
        JSON.stringify({
          schema: "aap-notification-preferences/v1",
          operation: "is_in_app_muted",
          user_id: userId,
          type,
        }),
      ],
    );
    assert.equal(result.rows[0].reply.ok, true);
    return result.rows[0].reply.data.muted;
  }

  async function composeAppend(type, value = request(type)) {
    await client.query("BEGIN");
    try {
      if (await isMuted(type)) {
        await client.query("COMMIT");
        return null;
      }
      const reply = await append(value);
      await client.query("COMMIT");
      return reply;
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    }
  }

  await client.connect();
  try {
    const role = await client.query("SELECT current_user AS role");
    assert.equal(role.rows[0].role, "notification_append_client");

    const unmutedRequest = request("task.approved", {
      payload: { target_id: "caller payload", enabled: true, empty: [] },
    });
    const unmuted = await composeAppend(unmutedRequest.type, unmutedRequest);
    assert.ok(unmuted);
    assert.equal(unmuted.ok, true);
    assert.equal(unmuted.data.notification.id, unmutedRequest.id);
    assert.equal(unmuted.data.notification.user_id, userId);
    assert.deepEqual(unmuted.data.notification.payload, unmutedRequest.payload);
    assert.equal(unmuted.data.notification.read_at, null);
    assert.match(unmuted.data.notification.created_at, /^\d{4}-\d\d-\d\dT.*Z$/);

    const unknownType = await composeAppend("future.notification");
    assert.equal(unknownType.ok, true);
    assert.ok(unknownType.data.notification);

    assert.equal(await composeAppend("task.rejected"), null);

    const invalid = await append(request("task.approved", { unexpected: true }));
    assert.deepEqual(invalid.error, { code: "invalid_request" });

    const rollbackRequest = request("future.rollback");
    await client.query("BEGIN");
    assert.equal(await isMuted(rollbackRequest.type), false);
    const beforeRollback = await append(rollbackRequest);
    assert.equal(beforeRollback.ok, true);
    assert.equal(beforeRollback.data.notification.id, rollbackRequest.id);
    await client.query("ROLLBACK");
    await client.query("BEGIN");
    assert.equal(await isMuted(rollbackRequest.type), false);
    const afterRollback = await append(rollbackRequest);
    assert.equal(afterRollback.ok, true);
    assert.equal(afterRollback.data.notification.id, rollbackRequest.id);
    await client.query("ROLLBACK");

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
          "($1::uuid,$2::uuid,'task.approved','task',$3::uuid,'{}'::jsonb)",
        [crypto.randomUUID(), userId, crypto.randomUUID()],
      ),
      (error) => error.code === "42501",
    );
  } finally {
    await client.end();
  }
  process.stdout.write("Notifications append owner check passed\n");
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else {
  await verifyOwnerAppend();
}
