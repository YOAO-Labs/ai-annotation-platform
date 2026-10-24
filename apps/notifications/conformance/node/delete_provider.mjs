import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.resolve(here, "../../src/aap_notifications/delete.schema.json");

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
  assert.equal(contract.$id, "aap-notification-delete/v1");
  assert.equal(contract.title, "AAP notification deletion protocol");
  assertClosedSchemaReferences(contract);
  for (const name of ["DeleteOneRequest", "ClearReadRequest"]) {
    const request = contract.$defs[name];
    assert.equal(request.additionalProperties, false);
    assert.ok(request.required.includes("user_id"));
  }
  assert.ok(contract.$defs.DeleteOneRequest.required.includes("notification_id"));
  process.stdout.write("Notifications deletion schema self-check passed\n");
}

async function verifyOwnerCommands() {
  const { Client } = await import("pg");
  const connectionString = process.env.NOTIFICATION_DELETE_DATABASE_URL;
  const userId = process.env.NOTIFICATION_DELETE_USER_ID;
  assert.ok(connectionString, "missing owned delete client DSN");
  assert.match(userId ?? "", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });

  async function command(value) {
    const result = await client.query(
      "SELECT aap_notifications_v1.delete_command($1::pg_catalog.jsonb) AS reply",
      [JSON.stringify(value)],
    );
    return result.rows[0].reply;
  }

  async function readStateCommand(value) {
    const result = await client.query(
      "SELECT aap_notifications_v1.read_state_command($1::pg_catalog.jsonb) AS reply",
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
      target_type: "delete-conformance",
      target_id: crypto.randomUUID(),
      payload: {},
    };
    const result = await client.query(
      "SELECT aap_notifications_v1.append_notification($1::pg_catalog.jsonb) AS reply",
      [JSON.stringify(request)],
    );
    assert.equal(result.rows[0].reply.ok, true);
    return result.rows[0].reply.data.notification.id;
  }

  async function markRead(notificationId) {
    return readStateCommand({
      schema: "aap-notification-read-state/v1",
      operation: "mark_read",
      user_id: userId,
      notification_id: notificationId,
      read_at: new Date().toISOString(),
    });
  }

  await client.connect();
  try {
    const role = await client.query("SELECT current_user AS role");
    assert.equal(
      role.rows[0].role,
      process.env.NOTIFICATION_DELETE_EXPECTED_ROLE ?? "notification_append_client",
    );

    await client.query("BEGIN");
    await command({
      schema: "aap-notification-delete/v1",
      operation: "clear_read",
      user_id: userId,
    });
    const readId = await append("delete.read");
    const unreadId = await append("delete.unread");
    assert.equal((await markRead(readId)).data.updated, true);
    assert.deepEqual(
      (
        await command({
          schema: "aap-notification-delete/v1",
          operation: "clear_read",
          user_id: userId,
        })
      ).data,
      { operation: "clear_read", deleted: 1 },
    );
    assert.deepEqual(
      (
        await command({
          schema: "aap-notification-delete/v1",
          operation: "clear_read",
          user_id: userId,
        })
      ).data,
      { operation: "clear_read", deleted: 0 },
    );
    assert.deepEqual(
      (
        await command({
          schema: "aap-notification-delete/v1",
          operation: "delete_one",
          user_id: crypto.randomUUID(),
          notification_id: unreadId,
        })
      ).data,
      { operation: "delete_one", deleted: false },
    );
    assert.deepEqual(
      (
        await command({
          schema: "aap-notification-delete/v1",
          operation: "delete_one",
          user_id: userId,
          notification_id: unreadId,
        })
      ).data,
      { operation: "delete_one", deleted: true },
    );
    assert.deepEqual(
      (
        await command({
          schema: "aap-notification-delete/v1",
          operation: "delete_one",
          user_id: userId,
          notification_id: unreadId,
        })
      ).data,
      { operation: "delete_one", deleted: false },
    );

    await client.query("ROLLBACK");

    await client.query("BEGIN");
    const rollbackId = await append("delete.rollback");
    await client.query("SAVEPOINT before_delete");
    assert.equal(
      (
        await command({
          schema: "aap-notification-delete/v1",
          operation: "delete_one",
          user_id: userId,
          notification_id: rollbackId,
        })
      ).data.deleted,
      true,
    );
    await client.query("ROLLBACK TO SAVEPOINT before_delete");
    assert.equal(
      (
        await command({
          schema: "aap-notification-delete/v1",
          operation: "delete_one",
          user_id: userId,
          notification_id: rollbackId,
        })
      ).data.deleted,
      true,
    );
    await client.query("ROLLBACK");

    const invalid = await command({
      schema: "aap-notification-delete/v1",
      operation: "clear_read",
      user_id: userId,
      unexpected: true,
    });
    assert.deepEqual(invalid.error, { code: "invalid_request" });
    await assert.rejects(
      client.query("DELETE FROM public.notifications WHERE false"),
      (error) => error.code === "42501",
    );
  } finally {
    await client.end();
  }
  process.stdout.write("Notifications deletion owner check passed\n");
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else {
  await verifyOwnerCommands();
}
