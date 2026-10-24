import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { Client } from "pg";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.resolve(here, "../../src/aap_notifications/preferences.schema.json");
const policyPath = path.resolve(here, "../../src/aap_notifications/preferences.policy.json");

async function loadContract() {
  const [contractText, policyText] = await Promise.all([
    readFile(contractPath, "utf8"),
    readFile(policyPath, "utf8"),
  ]);
  return { contract: JSON.parse(contractText), policy: JSON.parse(policyText) };
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
  const { contract, policy } = await loadContract();
  assert.equal(contract.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(contract.title, "AAP notification preferences protocol");
  assertClosedSchemaReferences(contract);
  assert.ok(Array.isArray(policy.known_notification_types));
  assert.ok(Array.isArray(policy.default_toast_types));
  assert.ok(
    policy.default_toast_types.every((type) => policy.known_notification_types.includes(type)),
  );

  const patch = {
    schema: "aap-notification-preferences/v1",
    operation: "patch",
    user_id: "00000000-0000-4000-8000-000000000001",
    type: "task.rejected",
    patch: { in_app: false },
  };
  assert.deepEqual(Object.keys(patch).sort(), ["operation", "patch", "schema", "type", "user_id"]);
  process.stdout.write("Notifications preference schema self-check passed\n");
}

async function verifyOwnerCommand() {
  const connectionString = process.env.NOTIFICATION_PREFERENCES_DATABASE_URL;
  const userId = process.env.NOTIFICATION_PREFERENCE_USER_ID;
  assert.ok(connectionString, "missing owned preference client DSN");
  assert.match(userId ?? "", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  const { policy } = await loadContract();
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });

  async function command(request) {
    const result = await client.query(
      "SELECT aap_notifications_v1.preference_command($1::jsonb) AS reply",
      [JSON.stringify(request)],
    );
    return result.rows[0].reply;
  }

  const request = (operation, fields = {}) => ({
    schema: "aap-notification-preferences/v1",
    operation,
    user_id: userId,
    ...fields,
  });

  await client.connect();
  try {
    const initial = await command(request("get"));
    assert.equal(initial.ok, true);
    assert.deepEqual(
      initial.data.items.map((item) => item.type),
      policy.known_notification_types,
    );
    const toastDefaults = new Set(policy.default_toast_types);
    for (const item of initial.data.items) {
      assert.equal(item.in_app, true);
      assert.equal(item.email, false);
      assert.equal(item.toast, toastDefaults.has(item.type));
    }

    const created = await command(
      request("patch", { type: "task.rejected", patch: { in_app: false } }),
    );
    assert.deepEqual(created, {
      schema: "aap-notification-preferences/v1",
      ok: true,
      data: { operation: "patch", updated: true },
    });
    const muted = await command(request("is_in_app_muted", { type: "task.rejected" }));
    assert.equal(muted.data.muted, true);

    const unknownMute = await command(request("is_in_app_muted", { type: "future.notification" }));
    assert.equal(unknownMute.data.muted, false);

    await client.query("BEGIN");
    const pending = await command(
      request("patch", { type: "task.rejected", patch: { toast: false } }),
    );
    assert.equal(pending.ok, true);
    await client.query("ROLLBACK");
    const afterRollback = await command(request("get"));
    const rejectedAfterRollback = afterRollback.data.items.find(
      (item) => item.type === "task.rejected",
    );
    assert.equal(rejectedAfterRollback.in_app, false);
    assert.equal(rejectedAfterRollback.toast, true);

    const merged = await command(
      request("patch", { type: "task.rejected", patch: { toast: false } }),
    );
    assert.equal(merged.ok, true);
    const afterMerge = await command(request("get"));
    const rejectedAfterMerge = afterMerge.data.items.find((item) => item.type === "task.rejected");
    assert.equal(rejectedAfterMerge.in_app, false);
    assert.equal(rejectedAfterMerge.toast, false);
    assert.equal(rejectedAfterMerge.email, false);

    const unknownPatch = await command(
      request("patch", { type: "future.notification", patch: { in_app: false } }),
    );
    assert.deepEqual(unknownPatch.error, { code: "unknown_type" });

    await assert.rejects(
      client.query("SELECT channels FROM public.notification_preferences LIMIT 1"),
      (error) => error.code === "42501",
    );
  } finally {
    await client.end();
  }
  process.stdout.write("Notifications preference Node owner check passed\n");
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else {
  await verifyOwnerCommand();
}
