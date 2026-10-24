import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.resolve(here, "../../src/aap_notifications/read_relations.schema.json");
const columns = [
  "id",
  "user_id",
  "type",
  "target_type",
  "target_id",
  "payload",
  "read_at",
  "created_at",
];

async function loadContract() {
  return JSON.parse(await readFile(contractPath, "utf8"));
}

export async function selfCheck() {
  const contract = await loadContract();
  assert.equal(contract.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(contract.$id, "aap-notifications-read-relations/v1");
  assert.equal(contract.additionalProperties, false);
  assert.deepEqual(
    contract["x-postgresql-relations"]["aap_notifications_v1.notification_facts"].columns,
    columns,
  );
  assert.equal(
    contract["x-postgresql-relations"]["aap_notifications_v1.notification_facts"].read_only,
    true,
  );
  assert.equal(
    contract["x-postgresql-relations"]["aap_notifications_v1.notification_facts"].security_barrier,
    true,
  );
  assert.deepEqual(Object.keys(contract.properties), columns);
  assert.deepEqual(contract.required, columns);
  process.stdout.write("Notifications read-relation schema self-check passed\n");
}

async function verifyReadRelation() {
  const { Client } = await import("pg");
  const connectionString = process.env.NOTIFICATION_READ_RELATIONS_DATABASE_URL;
  const userId = process.env.NOTIFICATION_READ_RELATIONS_USER_ID;
  const notificationId = process.env.NOTIFICATION_READ_RELATIONS_NOTIFICATION_ID;
  assert.ok(connectionString, "missing owned read-relation client DSN");
  assert.match(userId ?? "", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  assert.match(
    notificationId ?? "",
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
  );
  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });

  await client.connect();
  try {
    const role = await client.query("SELECT current_user AS role");
    assert.equal(role.rows[0].role, process.env.NOTIFICATION_READ_RELATIONS_EXPECTED_ROLE);

    await client.query("BEGIN");
    const result = await client.query(
      "SELECT * FROM aap_notifications_v1.notification_facts " + "WHERE user_id=$1 AND id=$2",
      [userId, notificationId],
    );
    assert.equal(result.rows.length, 1);
    const row = result.rows[0];
    assert.deepEqual(Object.keys(row), columns);
    assert.equal(row.id, notificationId);
    assert.equal(row.user_id, userId);
    assert.deepEqual(row.payload, { read_relation: true });

    const wrongUser = await client.query(
      "SELECT id FROM aap_notifications_v1.notification_facts " + "WHERE user_id=$1 AND id=$2",
      [crypto.randomUUID(), notificationId],
    );
    assert.equal(wrongUser.rows.length, 0);

    async function assertDenied(statement, parameters = []) {
      await client.query("SAVEPOINT denied_read_relation_write");
      await assert.rejects(client.query(statement, parameters), (error) => error.code === "42501");
      await client.query("ROLLBACK TO SAVEPOINT denied_read_relation_write");
      await client.query("RELEASE SAVEPOINT denied_read_relation_write");
    }

    await assertDenied("SELECT id FROM public.notifications LIMIT 1");
    await assertDenied(
      "INSERT INTO aap_notifications_v1.notification_facts " +
        "(id,user_id,type,target_type,target_id,payload) " +
        "VALUES ($1,$2,'read_relation.write','task',$3,'{}'::pg_catalog.jsonb)",
      [crypto.randomUUID(), userId, crypto.randomUUID()],
    );
    await assertDenied(
      "UPDATE aap_notifications_v1.notification_facts SET read_at=now() WHERE id=$1",
      [notificationId],
    );
    await assertDenied("DELETE FROM aap_notifications_v1.notification_facts WHERE id=$1", [
      notificationId,
    ]);
    await client.query("ROLLBACK");
  } finally {
    await client.end();
  }
  process.stdout.write("Notifications read-relation role check passed\n");
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else {
  await verifyReadRelation();
}
