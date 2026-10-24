import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const packageRoot = path.resolve(here, "../../src/aap_notifications");
const contractPath = path.join(packageRoot, "delivery_read.schema.json");
const packageSqlPath = path.join(packageRoot, "sql/0325_notifications_delivery_read.sql");
const apiSqlPath = path.resolve(
  here,
  "../../../api/alembic/sql/0325_notifications_delivery_read.sql",
);
const apiSchemaPath = path.resolve(
  here,
  "../../../api/alembic/sql/0325_notifications_delivery_read.schema.json",
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
const functions = [
  "read_notification",
  "find_terminal_notification",
  "delivery_job_scopes",
  "delivery_member_pairs",
  "delivery_reviewer_pairs",
  "delivery_owner_pairs",
  "delivery_super_pairs",
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
  assert.equal(contract.$id, "aap-notifications-delivery-read/v1");
  assert.deepEqual(contract["x-row-columns"], columns);
  assert.deepEqual(contract["x-row-key"], ["id"]);
  assert.equal(contract.properties.payload.type, undefined);
  assert.deepEqual(
    Object.keys(contract["x-postgresql-functions"])
      .map((name) => name.split(".").at(-1))
      .sort(),
    [...functions].sort(),
  );
  assert.deepEqual(contract["x-pair-input"].exact_keys, ["project_id", "user_id"]);
  assert.equal(contract["x-pair-input"].cardinality_cap, null);
  assert.equal(contract["x-security"].public_execute, false);
  assert.deepEqual(contract["x-security"].search_path, ["pg_catalog", "pg_temp"]);
  assert.deepEqual(apiSchema, contract);
  assert.equal(packageSql, apiSql);
  for (const name of functions) {
    assert.match(packageSql, new RegExp(`CREATE FUNCTION aap_notifications_v1\\.${name}\\(`));
    assert.match(
      packageSql,
      new RegExp(`REVOKE ALL ON FUNCTION\\s+aap_notifications_v1\\.${name}\\(`),
    );
  }
  assert.equal((packageSql.match(/SECURITY DEFINER/g) ?? []).length, functions.length);
  assert.equal(
    (packageSql.match(/SET search_path = pg_catalog, pg_temp/g) ?? []).length,
    functions.length,
  );
  assert.equal(
    (
      packageSql.match(
        /\(item\.value - 'project_id' - 'user_id'\)\s+<> '\{\}'::pg_catalog\.jsonb/g,
      ) ?? []
    ).length,
    4,
  );
  assert.equal(packageSql.includes("jsonb_object_length"), false);
  for (const privateRelation of [
    "public.notifications",
    "public.async_jobs",
    "public.project_members",
    "public.projects",
  ]) {
    assert.equal(packageSql.includes(privateRelation), false);
  }
  assert.equal(packageSql.includes("GRANT EXECUTE"), false);
  process.stdout.write("Notifications delivery-read contract self-check passed\n");
}

async function verifyDeliveryReadRole() {
  const { Client } = await import("pg");
  const connectionString = process.env.NOTIFICATION_DELIVERY_DATABASE_URL;
  const userId = process.env.NOTIFICATION_DELIVERY_USER_ID;
  const notificationId = process.env.NOTIFICATION_DELIVERY_NOTIFICATION_ID;
  const expectedRole = process.env.NOTIFICATION_DELIVERY_EXPECTED_ROLE;
  const projectId = process.env.NOTIFICATION_DELIVERY_PROJECT_ID;
  const jobId = process.env.NOTIFICATION_DELIVERY_JOB_ID;
  assert.ok(connectionString, "missing owned delivery-read client DSN");
  assert.match(userId ?? "", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  assert.match(
    notificationId ?? "",
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
  );
  assert.ok(expectedRole, "missing expected delivery-read LOGIN");
  if (projectId !== undefined) {
    assert.match(projectId, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  }
  if (jobId !== undefined) {
    assert.match(jobId, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  }

  const client = new Client({ connectionString, connectionTimeoutMillis: 5000 });
  await client.connect();
  try {
    const identity = await client.query(
      "SELECT current_user AS current_role, session_user AS login_role",
    );
    assert.equal(identity.rows[0].current_role, expectedRole);
    assert.equal(identity.rows[0].login_role, expectedRole);

    const executePrivileges = await client.query(
      "SELECT p.proname, pg_catalog.oidvectortypes(p.proargtypes) AS arguments, " +
        "p.prosecdef, p.provolatile, p.proconfig, " +
        "pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') AS can_execute " +
        "FROM pg_catalog.pg_proc AS p " +
        "JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace " +
        "WHERE n.nspname='aap_notifications_v1' " +
        "AND pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE') " +
        "ORDER BY p.proname, arguments",
    );
    const granted = executePrivileges.rows.map((row) => `${row.proname}(${row.arguments})`);
    const expected = [
      "read_notification(uuid)",
      "find_terminal_notification(uuid, text, uuid)",
      "delivery_job_scopes(uuid[])",
      "delivery_member_pairs(jsonb)",
      "delivery_reviewer_pairs(jsonb)",
      "delivery_owner_pairs(jsonb)",
      "delivery_super_pairs(jsonb)",
    ].sort();
    assert.deepEqual(granted, expected);
    for (const fn of executePrivileges.rows) {
      assert.equal(fn.prosecdef, true);
      assert.equal(fn.provolatile, "s");
      assert.ok(fn.proconfig?.includes("search_path=pg_catalog, pg_temp"));
      assert.equal(fn.can_execute, true);
    }

    const notificationResult = await client.query(
      "SELECT * FROM aap_notifications_v1.read_notification($1::uuid)",
      [notificationId],
    );
    assert.equal(notificationResult.rows.length, 1);
    const notification = notificationResult.rows[0];
    assert.deepEqual(Object.keys(notification), columns);
    assert.equal(notification.id, notificationId);
    assert.equal(notification.user_id, userId);
    assert.equal(notification.type, "task.approved");
    assert.equal(notification.target_type, "task");
    assert.deepEqual(notification.payload, { read_relation: true });

    const terminal = await client.query(
      "SELECT aap_notifications_v1.find_terminal_notification(" +
        "$1::uuid,'job.completed',$2::uuid) AS id",
      [userId, randomUUID()],
    );
    assert.equal(terminal.rows[0].id, null);

    const jobs = await client.query(
      "SELECT id, scope_project_id, kind FROM " +
        "aap_notifications_v1.delivery_job_scopes($1::uuid[])",
      [jobId === undefined ? [] : [jobId]],
    );
    if (jobId === undefined) {
      assert.deepEqual(jobs.rows, []);
    } else {
      assert.equal(jobs.rows.length, 1);
      assert.deepEqual(Object.keys(jobs.rows[0]), ["id", "scope_project_id", "kind"]);
      assert.equal(jobs.rows[0].id, jobId);
      assert.equal(jobs.rows[0].kind, "🧪".repeat(40));
    }

    const pairInput = JSON.stringify(
      projectId === undefined ? [] : [{ project_id: projectId, user_id: userId }],
    );
    for (const name of [
      "delivery_member_pairs",
      "delivery_reviewer_pairs",
      "delivery_owner_pairs",
      "delivery_super_pairs",
    ]) {
      const result = await client.query(
        `SELECT project_id, user_id FROM aap_notifications_v1.${name}($1::pg_catalog.jsonb)`,
        [pairInput],
      );
      const pairs = result.rows
        .map((row) => [row.project_id, row.user_id])
        .sort((left, right) => `${left[0]}:${left[1]}`.localeCompare(`${right[0]}:${right[1]}`));
      const expectedPairs =
        projectId !== undefined &&
        ["delivery_member_pairs", "delivery_reviewer_pairs"].includes(name)
          ? [[projectId, userId]]
          : [];
      assert.deepEqual(pairs, expectedPairs);
    }

    await assert.rejects(
      client.query(
        "SELECT * FROM aap_notifications_v1.delivery_member_pairs($1::pg_catalog.jsonb)",
        ['[{"project_id":1,"user_id":2}]'],
      ),
      (error) => error.code === "22023",
    );

    async function assertDenied(statement, parameters = []) {
      await assert.rejects(client.query(statement, parameters), (error) => error.code === "42501");
    }
    await assertDenied("SELECT id FROM aap_notifications_v1.notification_facts LIMIT 0");
    await assertDenied("SELECT id FROM public.notifications LIMIT 0");
    await assertDenied("SELECT id FROM public.async_jobs LIMIT 0");
    await assertDenied("SELECT id FROM aap_jobs_v1.notification_scope_facts LIMIT 0");
    await assertDenied("SELECT id FROM aap_identity_v1.account_profiles LIMIT 0");
    await assertDenied(
      "UPDATE aap_notifications_v1.notification_facts SET read_at=now() WHERE id=$1::uuid",
      [notificationId],
    );
    await assertDenied("DELETE FROM public.notifications WHERE id=$1::uuid", [notificationId]);
    await assertDenied(
      "INSERT INTO public.notifications(id,user_id,type,target_type,target_id,payload) " +
        "VALUES ($1::uuid,$2::uuid,'delivery.denied','task',$3::uuid,'{}'::pg_catalog.jsonb)",
      [randomUUID(), userId, randomUUID()],
    );
  } finally {
    await client.end();
  }
  process.stdout.write("Notifications delivery-read function-only role check passed\n");
}

if (process.argv.includes("--self-check")) {
  await selfCheck();
} else if (process.argv.includes("--live")) {
  await verifyDeliveryReadRole();
}
