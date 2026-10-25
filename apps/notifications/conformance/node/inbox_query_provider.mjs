import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const packageRoot = path.resolve(here, "../../src/aap_notifications");
const contractPath = path.join(packageRoot, "inbox_query.schema.json");
const packageSqlPath = path.join(packageRoot, "sql/0324_notifications_inbox_query.sql");
const apiSqlPath = path.resolve(
  here,
  "../../../api/alembic/sql/0324_notifications_inbox_query.sql",
);
const apiSchemaPath = path.resolve(
  here,
  "../../../api/alembic/sql/0324_notifications_inbox_query.schema.json",
);
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

async function selfCheck() {
  const [contractBytes, packageSql, apiSql, apiSchemaBytes] = await Promise.all([
    readFile(contractPath),
    readFile(packageSqlPath, "utf8"),
    readFile(apiSqlPath, "utf8"),
    readFile(apiSchemaPath),
  ]);
  const contract = JSON.parse(contractBytes.toString("utf8"));
  const apiSchema = JSON.parse(apiSchemaBytes.toString("utf8"));
  assert.equal(contract.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(contract.$id, "aap-notifications-inbox-query/v1");
  assert.deepEqual(contract["x-row-columns"], columns);
  assert.deepEqual(contract["x-row-key"], ["id"]);
  assert.equal(contract.properties.payload.type, undefined);
  assert.deepEqual(contract["x-global-async-job-kinds"], ["audit_archive", "dataset_import"]);
  assert.equal(contract["x-trusted-role-snapshot"], "IdentityAccountLockData.read_actor_status");
  assert.equal(
    contract["x-private-helper"],
    "aap_notifications_v1._visible_inbox_rows(uuid, text)",
  );
  assert.match(contract["x-activity-policy"], /No endpoint-wide active-account filter/);
  assert.deepEqual(Object.keys(contract["x-postgresql-functions"]), [
    "aap_notifications_v1.inbox_page",
    "aap_notifications_v1.inbox_total_count",
    "aap_notifications_v1.inbox_unread_count",
  ]);
  assert.equal(contract["x-security"].public_execute, false);
  assert.equal(contract["x-security"].private_helper_execute, false);
  assert.equal(contract["x-bigint-wire"].startsWith("Keep PostgreSQL bigint"), true);
  assert.deepEqual(apiSchema, contract);
  assert.equal(packageSql, apiSql);
  for (const functionName of [
    "_visible_inbox_rows",
    "inbox_page",
    "inbox_total_count",
    "inbox_unread_count",
  ]) {
    assert.match(
      packageSql,
      new RegExp(`CREATE FUNCTION aap_notifications_v1\\.${functionName}\\(`),
    );
  }
  assert.equal((packageSql.match(/SECURITY DEFINER/g) ?? []).length, 4);
  assert.equal((packageSql.match(/SET search_path = pg_catalog, pg_temp/g) ?? []).length, 4);
  assert.equal(packageSql.includes("public.notifications"), false);
  assert.equal(packageSql.includes("public.async_jobs"), false);
  assert.equal(packageSql.includes("public.project_members"), false);
  assert.equal(packageSql.includes("public.projects"), false);
  assert.equal(packageSql.includes("GRANT EXECUTE"), false);
  process.stdout.write("Notifications inbox-query contract self-check passed\n");
}

async function verifyInboxQueryRole() {
  const { Client } = await import("pg");
  const connectionString = process.env.NOTIFICATION_INBOX_DATABASE_URL;
  const userId = process.env.NOTIFICATION_INBOX_USER_ID;
  const notificationId = process.env.NOTIFICATION_INBOX_NOTIFICATION_ID;
  const platformRole = process.env.NOTIFICATION_INBOX_PLATFORM_ROLE;
  const expectedRole = process.env.NOTIFICATION_INBOX_EXPECTED_ROLE;
  assert.ok(connectionString, "missing owned inbox-query client DSN");
  assert.match(userId ?? "", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  assert.match(
    notificationId ?? "",
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
  );
  assert.equal(platformRole, "employee");
  assert.ok(expectedRole, "missing expected inbox-query role");

  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  await client.connect();
  try {
    const identity = await client.query(
      "SELECT current_user AS current_role, session_user AS login_role",
    );
    assert.equal(identity.rows[0].current_role, expectedRole);
    assert.equal(identity.rows[0].login_role, expectedRole);

    const page = await client.query(
      "SELECT * FROM aap_notifications_v1.inbox_page($1::uuid,$2::text,false,1000,0)",
      [userId, platformRole],
    );
    const probe = page.rows.find((row) => row.id === notificationId);
    assert.ok(probe, "owned probe notification is absent from the inbox page");
    assert.deepEqual(Object.keys(probe), columns);
    assert.equal(probe.user_id, userId);
    assert.equal(probe.type, "task.approved");
    assert.equal(probe.target_type, "task");
    assert.deepEqual(probe.payload, { read_relation: true });

    const totalResult = await client.query(
      "SELECT aap_notifications_v1.inbox_total_count($1::uuid,$2::text) AS total",
      [userId, platformRole],
    );
    const unreadResult = await client.query(
      "SELECT aap_notifications_v1.inbox_unread_count($1::uuid,$2::text) AS unread",
      [userId, platformRole],
    );
    const total = totalResult.rows[0].total;
    const unread = unreadResult.rows[0].unread;
    assert.equal(typeof total, "string");
    assert.equal(typeof unread, "string");
    assert.equal(BigInt(total).toString(), total);
    assert.equal(BigInt(unread).toString(), unread);
    assert.ok(BigInt(total) >= BigInt(page.rows.length));
    assert.ok(BigInt(unread) <= BigInt(total));

    async function assertDenied(statement, parameters = []) {
      await assert.rejects(client.query(statement, parameters), (error) => error.code === "42501");
    }
    await assertDenied(
      "SELECT * FROM aap_notifications_v1._visible_inbox_rows($1::uuid,$2::text)",
      [userId, platformRole],
    );
    await assertDenied("SELECT id FROM aap_notifications_v1.notification_facts WHERE id=$1::uuid", [
      notificationId,
    ]);
    await assertDenied("SELECT id FROM public.notifications LIMIT 1");
    await assertDenied(
      "INSERT INTO public.notifications(id,user_id,type,target_type,target_id,payload) " +
        "VALUES ($1::uuid,$2::uuid,'inbox.denied','task',$3::uuid,'{}'::pg_catalog.jsonb)",
      [crypto.randomUUID(), userId, crypto.randomUUID()],
    );
  } finally {
    await client.end();
  }
  process.stdout.write("Notifications inbox-query function-only role check passed\n");
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else if (process.argv.includes("--live")) {
  await verifyInboxQueryRole();
}
