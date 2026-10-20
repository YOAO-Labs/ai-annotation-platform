import { timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import {
  ENDPOINT,
  MAX_BODY_BYTES,
  ROLE,
  SCHEMA,
  lockFailure,
  parseJson,
  validatePolicy,
  validateReply,
  validateRequest,
} from "./wire.mjs";

const POLICY_FUNCTION = "aap_workspace_owner_transfer_v1.owner_transfer_policy()";
const COMMAND_FUNCTION = "aap_workspace_owner_transfer_v1.transfer_project_owner(jsonb)";

function unavailable() {
  throw new Error("Workspace owner transfer unavailable");
}

function readSecretFile(name) {
  const path = process.env[name];
  if (typeof path !== "string" || path.length === 0) unavailable();
  try {
    const value = readFileSync(path, "utf8").replace(/\r?\n$/, "");
    if (value.length === 0 || value.includes("\u0000")) unavailable();
    return value;
  } catch {
    unavailable();
  }
}

function settings() {
  const token = readSecretFile("WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE");
  const dsn = readSecretFile("WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE");
  const rawDrop = process.env.AAP_WORKSPACE_OWNER_TRANSFER_DROP_OPERATION_ID;
  let dropOperationId = null;
  if (rawDrop !== undefined && rawDrop !== "") {
    try {
      dropOperationId = validateRequest({
        schema: SCHEMA,
        operation_id: rawDrop,
        actor_id: "00000000-0000-0000-0000-000000000000",
        project_id: "00000000-0000-0000-0000-000000000000",
        new_owner_id: "00000000-0000-0000-0000-000000000000",
      }).operation_id;
    } catch {
      unavailable();
    }
  }
  const host = process.env.WORKSPACE_HOST || "127.0.0.1";
  const port = Number(process.env.WORKSPACE_PORT ?? "8173");
  if (!Number.isSafeInteger(port) || port < 0 || port > 65535) unavailable();
  return { token, dsn, dropOperationId, host, port };
}

function sameSecret(provided, expected) {
  const left = Buffer.from(provided, "utf8");
  const right = Buffer.from(expected, "utf8");
  return left.length === right.length && timingSafeEqual(left, right);
}

function send(res, status, body) {
  if (res.destroyed || res.writableEnded) return;
  const bytes = Buffer.from(JSON.stringify(body), "utf8");
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": bytes.length,
    "cache-control": "no-store",
  });
  res.end(bytes);
}

function sendRaw(res, status, body) {
  if (res.destroyed || res.writableEnded) return;
  const bytes = Buffer.from(body, "utf8");
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": bytes.length,
    "cache-control": "no-store",
  });
  res.end(bytes);
}

async function bodyBytes(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new RangeError("Request body too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, size);
}

function validateContentType(value) {
  return (
    typeof value === "string" && value.split(";", 1)[0].trim().toLowerCase() === "application/json"
  );
}

async function driver() {
  return await import("pg");
}

async function connected(config) {
  const pg = await driver();
  const Client = pg.Client || pg.default?.Client;
  if (typeof Client !== "function") unavailable();
  const client = new Client({
    connectionString: config.dsn,
    connectionTimeoutMillis: 5000,
    statement_timeout: 30000,
    application_name: "aap-workspace-owner-transfer",
  });
  await client.connect();
  return client;
}

const CAPABILITY_QUERY = `
  SELECT
    current_user::text AS current_user,
    session_user::text AS session_user,
    current_user = $1 AND session_user = $1 AS expected_login,
    EXISTS (
      SELECT 1 FROM pg_catalog.pg_roles AS role
      WHERE role.rolname = current_user
        AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
             OR role.rolreplication OR role.rolbypassrls OR NOT role.rolcanlogin)
    ) AS unsafe_role,
    EXISTS (
      SELECT 1 FROM pg_catalog.pg_auth_members AS membership
      WHERE membership.member = current_user::pg_catalog.regrole
    ) AS inherited_role,
    pg_catalog.has_database_privilege(current_user, current_database(), 'CREATE')
      OR pg_catalog.has_database_privilege(current_user, current_database(), 'TEMP') AS database_write,
    EXISTS (
      SELECT 1 FROM pg_catalog.pg_namespace AS namespace
      WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        AND namespace.nspname NOT LIKE 'pg_toast%'
        AND namespace.nspname NOT LIKE 'pg_temp_%'
        AND (
          pg_catalog.has_schema_privilege(current_user, namespace.oid, 'CREATE')
          OR (namespace.nspname NOT IN ('public', 'aap_workspace_owner_transfer_v1')
              AND pg_catalog.has_schema_privilege(current_user, namespace.oid, 'USAGE'))
        )
    ) AS broad_schema_access,
    EXISTS (
      SELECT 1
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        AND namespace.nspname NOT LIKE 'pg_toast%'
        AND namespace.nspname NOT LIKE 'pg_temp_%'
        AND (
          (relation.relkind IN ('r', 'p', 'v', 'm', 'f') AND (
            pg_catalog.has_table_privilege(current_user, relation.oid,
              'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR pg_catalog.has_any_column_privilege(current_user, relation.oid,
              'SELECT,INSERT,UPDATE,REFERENCES')
          ))
          OR (relation.relkind = 'S' AND pg_catalog.has_sequence_privilege(
            current_user, relation.oid, 'SELECT,UPDATE,USAGE'))
        )
    ) AS raw_relation_access
`;

const EXECUTABLE_FUNCTIONS_QUERY = `
  SELECT namespace.nspname || '.' || function.proname || '(' ||
         pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
  FROM pg_catalog.pg_proc AS function
  JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
  JOIN pg_catalog.pg_type AS return_type ON return_type.oid = function.prorettype
  WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
    AND namespace.nspname NOT LIKE 'pg_toast%'
    AND namespace.nspname NOT LIKE 'pg_temp_%'
    AND function.prokind IN ('f', 'p', 'a', 'w')
    AND NOT (return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
             AND return_type.typname IN ('trigger', 'event_trigger'))
    AND NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_depend AS dependency
      WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
        AND dependency.objid = function.oid
        AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
        AND dependency.deptype = 'e'
    )
    AND (
      pg_catalog.left(namespace.nspname, 4) = 'aap_'
      OR function.prosecdef
      OR EXISTS (
        SELECT 1 FROM pg_catalog.aclexplode(COALESCE(function.proacl,
          pg_catalog.acldefault('f', function.proowner))) AS acl
        WHERE acl.privilege_type = 'EXECUTE' AND acl.grantee <> 0
          AND pg_catalog.pg_has_role(current_user, acl.grantee, 'USAGE')
      )
    )
    AND pg_catalog.has_function_privilege(current_user, function.oid, 'EXECUTE')
  ORDER BY signature
`;

async function verifyRole(client) {
  const identity = await client.query(CAPABILITY_QUERY, [ROLE]);
  if (identity.rowCount !== 1) unavailable();
  const row = identity.rows[0];
  if (
    row.expected_login !== true ||
    row.unsafe_role !== false ||
    row.inherited_role !== false ||
    row.database_write !== false ||
    row.broad_schema_access !== false ||
    row.raw_relation_access !== false
  )
    unavailable();
  const functions = await client.query(EXECUTABLE_FUNCTIONS_QUERY);
  const signatures = functions.rows.map(({ signature }) => signature);
  const expected = [POLICY_FUNCTION, COMMAND_FUNCTION].sort();
  if (
    signatures.length !== expected.length ||
    signatures.some((value, index) => value !== expected[index])
  )
    unavailable();
}

async function verifyPolicy(client) {
  const result = await client.query(
    `SELECT aap_workspace_owner_transfer_v1.owner_transfer_policy()::text AS policy`,
  );
  if (result.rowCount !== 1) unavailable();
  validatePolicy(parseJson(result.rows[0].policy));
}

function decodeNativeReply(request, raw) {
  try {
    return validateReply(request, parseJson(raw));
  } catch {
    unavailable();
  }
}

async function readiness(config) {
  let client;
  try {
    client = await connected(config);
    await verifyRole(client);
    await verifyPolicy(client);
    return true;
  } catch {
    return false;
  } finally {
    if (client) await client.end().catch(() => {});
  }
}

function pgCode(error) {
  return typeof error?.code === "string" ? error.code : "";
}

async function execute(config, request) {
  let client;
  let transaction = false;
  let stage = "connect";
  try {
    client = await connected(config);
    await client.query("BEGIN");
    transaction = true;
    stage = "policy";
    await verifyRole(client);
    await verifyPolicy(client);
    stage = "command";
    const result = await client.query(
      "SELECT aap_workspace_owner_transfer_v1.transfer_project_owner($1::jsonb)::text AS reply",
      [JSON.stringify(request)],
    );
    stage = "decode";
    if (result.rowCount !== 1 || typeof result.rows[0].reply !== "string") unavailable();
    const reply = decodeNativeReply(request, result.rows[0].reply);
    if (reply.ok === true) {
      stage = "commit";
      await client.query("COMMIT");
      transaction = false;
      if (config.dropOperationId === request.operation_id && !config.dropped) {
        config.dropped = true;
        return { dropped: true };
      }
    } else {
      await client.query("ROLLBACK");
      transaction = false;
    }
    return { raw: result.rows[0].reply, reply };
  } catch (error) {
    if (transaction && client) {
      await client.query("ROLLBACK").catch(() => {});
      transaction = false;
    }
    if (stage === "command") {
      const mapped = lockFailure(error);
      if (mapped) return { reply: mapped };
      const state = pgCode(error);
      if (state.startsWith("08") || ["57P01", "57P02", "57P03"].includes(state)) {
        unavailable();
      }
      throw new Error("Workspace owner transfer data failure");
    }
    if (stage === "commit") {
      const state = pgCode(error);
      if (state.startsWith("08") || ["57P01", "57P02", "57P03"].includes(state)) {
        unavailable();
      }
      throw new Error("Workspace owner transfer data failure");
    }
    unavailable();
  } finally {
    if (client) await client.end().catch(() => {});
  }
}

function responseForFailure(res, status, message) {
  send(res, status, { detail: message });
}

function createHandler(config) {
  return async (req, res) => {
    if (req.method === "GET" && req.url === "/health") {
      send(res, 200, { status: "ok" });
      return;
    }
    if (req.method === "GET" && req.url === "/ready") {
      if (await readiness(config)) send(res, 200, { status: "ready" });
      else responseForFailure(res, 503, "Workspace owner transfer unavailable");
      return;
    }
    if (req.url !== ENDPOINT) {
      responseForFailure(res, 404, "Not found");
      return;
    }
    if (req.method !== "POST") {
      responseForFailure(res, 405, "Method not allowed");
      return;
    }
    if (
      !sameSecret(
        (req.headers.authorization || "").startsWith("Bearer ")
          ? req.headers.authorization.slice(7)
          : "",
        config.token,
      )
    ) {
      responseForFailure(res, 401, "Invalid internal credential");
      return;
    }
    if (!validateContentType(req.headers["content-type"])) {
      responseForFailure(res, 400, "Expected application/json");
      return;
    }
    let request;
    try {
      request = validateRequest(parseJson(await bodyBytes(req)));
    } catch {
      responseForFailure(res, 400, "Invalid Workspace owner transfer request");
      return;
    }
    try {
      const result = await execute(config, request);
      if (result.dropped) {
        res.destroy();
        return;
      }
      if (result.raw) sendRaw(res, 200, result.raw);
      else send(res, 200, result.reply);
    } catch (error) {
      const status = error.message === "Workspace owner transfer unavailable" ? 503 : 500;
      responseForFailure(
        res,
        status,
        status === 503 ? "Workspace owner transfer unavailable" : "Workspace owner transfer failed",
      );
    }
  };
}

function rejects(callback) {
  try {
    callback();
  } catch {
    return true;
  }
  return false;
}

function rejectsUnavailable(callback) {
  try {
    callback();
  } catch (error) {
    return error.message === "Workspace owner transfer unavailable";
  }
  return false;
}

function selfCheckFailed() {
  throw new Error("Workspace owner transfer provider self-check failed");
}

function selfCheck() {
  const request = {
    schema: SCHEMA,
    operation_id: "11111111-1111-4111-8111-111111111111",
    actor_id: "22222222-2222-4222-8222-222222222222",
    project_id: "33333333-3333-4333-8333-333333333333",
    new_owner_id: "44444444-4444-4444-8444-444444444444",
  };
  const valid = {
    schema: SCHEMA,
    ok: true,
    operation_id: request.operation_id,
    actor_id: request.actor_id,
    project_id: request.project_id,
    new_owner_id: request.new_owner_id,
    data: {
      previous_owner_id: request.actor_id,
      updated_at: "2026-10-21T01:02:03.123456+00:00",
      changed: true,
    },
  };
  validatePolicy({ schema: SCHEMA });
  validateReply(request, valid);
  const busy = lockFailure({
    code: "55P03",
    hint: "aap.workspace.owner_transfer.account_lock\nprivate",
  });
  if (!busy || busy.error.rollback !== true) selfCheckFailed();
  const projectBusy = lockFailure({
    code: "40P01",
    hint: "aap.workspace.owner_transfer.project_lock\nprivate",
  });
  if (!projectBusy || projectBusy.error.rollback !== false) selfCheckFailed();
  if (
    lockFailure({
      code: "23505",
      hint: "aap.workspace.owner_transfer.account_lock\nprivate",
    }) !== null ||
    lockFailure({
      code: "55P03",
      message: "aap.workspace.owner_transfer.project_lock",
    }) !== null
  )
    selfCheckFailed();
  const checks = [
    rejects(() => validateRequest({ ...request, extra: true })),
    rejects(() => validateRequest({ ...request, project_id: "bad" })),
    rejects(() => validatePolicy({ schema: SCHEMA, extra: true })),
    rejects(() => validateReply(request, { ...valid, actor_id: request.project_id })),
    rejects(() => validateReply(request, { ...valid, data: { ...valid.data, changed: false } })),
    rejects(() =>
      validateReply(request, {
        ...valid,
        data: { ...valid.data, updated_at: "2026-10-21T01:02:03" },
      }),
    ),
    rejects(() =>
      validateReply(request, {
        ...valid,
        data: { ...valid.data, updated_at: "2026-02-29T01:02:03Z" },
      }),
    ),
    rejects(() =>
      validateReply(
        request,
        parseJson(
          '{"schema":"aap-workspace-owner-transfer/v1","ok":false,"error":{"status_code":409.0,"detail":null,"rollback":false}}',
        ),
      ),
    ),
    rejectsUnavailable(() =>
      decodeNativeReply(request, '{"schema":"aap-workspace-owner-transfer/v1","ok":true}'),
    ),
    rejects(() => parseJson('{"operation_id":"a","operation_id":"b"}')),
    rejects(() => parseJson('{"x":NaN}')),
    rejects(() => parseJson(Buffer.from([0xc3, 0x28]))),
  ];
  if (!checks.every(Boolean)) selfCheckFailed();
  return { ok: true, checks: checks.length + 7 };
}

export async function main() {
  if (process.argv[2] === "--self-check") {
    process.stdout.write(`${JSON.stringify(selfCheck())}\n`);
    return;
  }
  const config = settings();
  config.dropped = false;
  const server = createServer((req, res) => {
    void createHandler(config)(req, res).catch(() => {
      responseForFailure(res, 500, "Workspace owner transfer failed");
    });
  });
  const port = config.port;
  server.listen(port, config.host, () => {
    const address = server.address();
    process.stdout.write(`${JSON.stringify({ event: "listening", port: address.port })}\n`);
  });
  const stop = () => server.close(() => process.exit(0));
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
}

if (process.argv[1] && new URL(import.meta.url).pathname === process.argv[1]) {
  main().catch(() => {
    process.stderr.write("Workspace owner transfer provider startup failed\n");
    process.exitCode = 1;
  });
}
