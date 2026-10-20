import { randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

const protocol = "aap-identity-provisioning/v1";
const clientRole = "identity_provisioning_client";
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function fail() {
  throw new Error("Invalid Identity provisioning peer request or response");
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exact(value, fields) {
  return (
    isRecord(value) &&
    Object.keys(value).length === fields.length &&
    fields.every((field) => Object.hasOwn(value, field))
  );
}

function uuid(value) {
  if (typeof value !== "string" || !uuidPattern.test(value)) fail();
  return value.toLowerCase();
}

function awareTimestamp(value) {
  if (typeof value !== "string") fail();
  const match = value.match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-](\d{2}):(\d{2}))$/,
  );
  if (!match) {
    fail();
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const offsetHours = match[8] === undefined ? 0 : Number(match[8]);
  const offsetMinutes = match[9] === undefined ? 0 : Number(match[9]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    year < 1 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > monthDays[month - 1] ||
    hour > 23 ||
    minute > 59 ||
    second > 59 ||
    offsetHours > 23 ||
    offsetMinutes > 59 ||
    !Number.isFinite(Date.parse(value))
  ) {
    fail();
  }
  return value;
}

function scanJson(source) {
  let offset = 0;
  const whitespace = () => {
    while (offset < source.length && /[\t\n\r ]/.test(source[offset])) offset += 1;
  };
  const string = () => {
    if (source[offset] !== '"') fail();
    const start = offset++;
    while (offset < source.length) {
      if (source[offset] === '"') {
        offset += 1;
        try {
          return JSON.parse(source.slice(start, offset));
        } catch {
          fail();
        }
      }
      if (source[offset] === "\\") {
        offset += 1;
        if (source[offset] === "u") offset += 5;
        else offset += 1;
      } else {
        offset += 1;
      }
    }
    fail();
  };
  const value = () => {
    whitespace();
    if (source[offset] === "{") {
      offset += 1;
      whitespace();
      const keys = new Set();
      if (source[offset] !== "}") {
        while (true) {
          whitespace();
          const key = string();
          if (keys.has(key)) fail();
          keys.add(key);
          whitespace();
          if (source[offset++] !== ":") fail();
          value();
          whitespace();
          if (source[offset] === "}") break;
          if (source[offset++] !== ",") fail();
        }
      }
      offset += 1;
      return;
    }
    if (source[offset] === "[") {
      offset += 1;
      whitespace();
      if (source[offset] !== "]") {
        while (true) {
          value();
          whitespace();
          if (source[offset] === "]") break;
          if (source[offset++] !== ",") fail();
        }
      }
      offset += 1;
      return;
    }
    if (source[offset] === '"') {
      string();
      return;
    }
    const start = offset;
    while (offset < source.length && !/[\t\n\r ,\]}]/.test(source[offset])) offset += 1;
    if (start === offset) fail();
  };

  value();
  whitespace();
  if (offset !== source.length) fail();
}

function parseStrictJson(source) {
  if (typeof source !== "string") fail();
  scanJson(source);
  try {
    return JSON.parse(source);
  } catch {
    fail();
  }
}

function stringifyStrict(value) {
  const result = JSON.stringify(value);
  if (typeof result !== "string") fail();
  return result;
}

function validateLookupRequest(value) {
  if (
    !exact(value, ["schema", "email"]) ||
    value.schema !== protocol ||
    typeof value.email !== "string"
  ) {
    fail();
  }
  return { schema: protocol, email: value.email };
}

function validateCreateRequest(value) {
  if (
    !exact(value, [
      "schema",
      "mode",
      "account_id",
      "email",
      "name",
      "password_hash",
      "now",
      "request_id",
    ]) ||
    value.schema !== protocol ||
    !["bootstrap_admin", "import_admin"].includes(value.mode) ||
    typeof value.email !== "string" ||
    typeof value.name !== "string" ||
    typeof value.password_hash !== "string" ||
    (value.request_id !== null && typeof value.request_id !== "string")
  ) {
    fail();
  }
  const accountId = uuid(value.account_id);
  const now = value.now === null ? null : awareTimestamp(value.now);
  if (value.mode === "bootstrap_admin" && now === null) fail();
  if (
    value.mode === "import_admin" &&
    (value.email !== "admin" ||
      value.name !== "超级管理员" ||
      now !== null ||
      value.request_id !== null)
  ) {
    fail();
  }
  return {
    schema: protocol,
    mode: value.mode,
    account_id: accountId,
    email: value.email,
    name: value.name,
    password_hash: value.password_hash,
    now,
    request_id: value.request_id,
  };
}

function validateRequest(value, operation) {
  if (operation === "lookup") return validateLookupRequest(value);
  if (operation === "create") return validateCreateRequest(value);
  fail();
}

function validateReply(request, operation, value) {
  if (
    !exact(value, ["schema", "email", "account_id"]) ||
    value.schema !== protocol ||
    value.email !== request.email ||
    (value.account_id !== null && typeof value.account_id !== "string")
  ) {
    fail();
  }
  const accountId = value.account_id === null ? null : uuid(value.account_id);
  if (operation === "create" && accountId !== uuid(request.account_id)) fail();
  return { schema: protocol, email: value.email, account_id: accountId };
}

function validateCallInput(value) {
  if (!exact(value, ["container_id", "database", "requests", "transaction"])) fail();
  if (
    typeof value.container_id !== "string" ||
    !/^[0-9a-f]{64}$/.test(value.container_id) ||
    typeof value.database !== "string" ||
    !/^[a-z0-9_]+_test$/.test(value.database) ||
    !Array.isArray(value.requests) ||
    value.requests.length > 2 ||
    !["commit", "rollback", "create_or_duplicate", "probe_denied"].includes(value.transaction)
  ) {
    fail();
  }
  if (
    (value.transaction === "probe_denied" && value.requests.length !== 0) ||
    (value.transaction !== "probe_denied" && value.requests.length < 1) ||
    (value.transaction === "create_or_duplicate" &&
      (value.requests.length !== 1 || value.requests[0]?.operation !== "create"))
  ) {
    fail();
  }
  const requests = value.requests.map((entry) => {
    if (!exact(entry, ["operation", "request"])) fail();
    const request = validateRequest(entry.request, entry.operation);
    return { operation: entry.operation, request };
  });
  return {
    containerId: value.container_id,
    database: value.database,
    requests,
    transaction: value.transaction,
  };
}

function roleProbeSql() {
  return [
    "SELECT current_user || '|' || session_user || '|' || current_database() || '|' ||",
    "CASE WHEN has_table_privilege(current_user, 'public.users', 'SELECT') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_table_privilege(current_user, 'public.users', 'INSERT') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_table_privilege(current_user, 'public.users', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_table_privilege(current_user, 'public.users', 'DELETE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_any_column_privilege(current_user, 'public.users', 'SELECT') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_table_privilege(current_user, 'public.audit_logs', 'SELECT') OR " +
      "has_table_privilege(current_user, 'public.audit_logs', 'INSERT') OR " +
      "has_table_privilege(current_user, 'public.audit_logs', 'UPDATE') OR " +
      "has_table_privilege(current_user, 'public.audit_logs', 'DELETE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_schema_privilege(current_user, 'public', 'CREATE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_schema_privilege(current_user, 'aap_identity_v1', 'CREATE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_schema_privilege(current_user, 'aap_identity_v1', 'USAGE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_schema_privilege(current_user, 'aap_audit_v1', 'USAGE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_schema_privilege(current_user, 'aap_identity_sessions_v1', 'USAGE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_schema_privilege(current_user, 'aap_identity_keys_v1', 'USAGE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_database_privilege(current_user, current_database(), 'CREATE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_database_privilege(current_user, current_database(), 'TEMP') THEN 'true' ELSE 'false' END || '|' ||",
    "(SELECT count(*)::text FROM pg_catalog.pg_proc AS proc " +
      "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = proc.pronamespace " +
      "WHERE namespace.nspname = 'aap_identity_v1' " +
      "AND has_function_privilege(current_user, proc.oid, 'EXECUTE'));",
  ].join("\n");
}

function functionSql(operation, request) {
  const encoded = Buffer.from(stringifyStrict(request), "utf8").toString("hex");
  if (operation === "lookup") {
    const email = Buffer.from(request.email, "utf8").toString("hex");
    return `SELECT aap_identity_v1.read_provisioning_account(convert_from(decode('${email}', 'hex'), 'UTF8'));`;
  }
  return `SELECT aap_identity_v1.create_provisioning_account(convert_from(decode('${encoded}', 'hex'), 'UTF8')::jsonb);`;
}

function buildSql(input) {
  const lines = ["\\set VERBOSITY verbose", "BEGIN;", roleProbeSql()];
  if (input.transaction === "probe_denied") {
    const probeTable = `provisioning_peer_probe_${randomUUID().replaceAll("-", "")}`;
    const deniedStatements = [
      "SELECT id FROM public.users LIMIT 0;",
      "SELECT id FROM public.api_keys LIMIT 0;",
      "SELECT id FROM public.audit_logs LIMIT 0;",
      "UPDATE public.users SET password_hash = 'forbidden-probe' WHERE false;",
      `CREATE TABLE public.${probeTable} (id integer);`,
      `CREATE TABLE aap_identity_v1.${probeTable} (id integer);`,
      `CREATE TEMP TABLE ${probeTable} (id integer);`,
      "SELECT aap_identity_v1.lock_accounts_share(ARRAY[]::uuid[], true);",
      "SELECT aap_identity_sessions_v1.read_login_candidate('forbidden@example.test');",
      "SET ROLE identity_code;",
      "SET ROLE audit_code;",
    ];
    deniedStatements.forEach((statement, index) => {
      const savepoint = `provisioning_probe_${index}`;
      lines.push(`SAVEPOINT ${savepoint};`);
      lines.push(statement);
      lines.push(`ROLLBACK TO SAVEPOINT ${savepoint};`);
      lines.push(`RELEASE SAVEPOINT ${savepoint};`);
    });
    lines.push("ROLLBACK;");
    return lines.join("\n");
  }
  for (const entry of input.requests) lines.push(functionSql(entry.operation, entry.request));
  if (input.transaction === "create_or_duplicate") {
    lines.push("SELECT 1;");
    lines.push("COMMIT;");
    return lines.join("\n");
  }
  lines.push(input.transaction === "commit" ? "COMMIT;" : "ROLLBACK;");
  return lines.join("\n");
}

function validateRoleProbe(value, database) {
  const fields = value.split("|");
  if (
    fields.length !== 18 ||
    fields[0] !== clientRole ||
    fields[1] !== clientRole ||
    fields[2] !== database ||
    [3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16].some((index) => fields[index] !== "false") ||
    fields[11] !== "true" ||
    fields[17] !== "2"
  ) {
    fail();
  }
  return { current_user: fields[0], session_user: fields[1], database: fields[2] };
}

function extractSqlStates(stderr) {
  return [...stderr.matchAll(/^(?:psql:[^\r\n]*?:\s*)?(?:ERROR|FATAL):\s+([0-9A-Z]{5}):/gm)].map(
    (match) => match[1],
  );
}

function callOwner(value) {
  const input = validateCallInput(value);
  const args = [
    "exec",
    "-i",
    input.containerId,
    "psql",
    "--no-psqlrc",
    "-q",
    "-A",
    "-t",
    "-U",
    clientRole,
    "-d",
    input.database,
    "-f",
    "-",
  ];
  if (!["create_or_duplicate", "probe_denied"].includes(input.transaction)) {
    args.splice(args.length - 2, 0, "-v", "ON_ERROR_STOP=1");
  }
  const result = spawnSync("docker", args, {
    input: buildSql(input),
    encoding: "utf8",
    timeout: 30_000,
    maxBuffer: 4 * 1024 * 1024,
    env: { PATH: process.env.PATH ?? "" },
  });
  if (result.error) fail();
  const lines = result.stdout.split(/\r?\n/).filter((line) => line.length > 0);
  const identity = validateRoleProbe(lines[0] ?? "", input.database);
  if (input.transaction === "probe_denied") {
    const sqlStates = extractSqlStates(result.stderr ?? "");
    if (sqlStates.filter((state) => state === "42501").length !== 11 || lines.length !== 1) {
      fail();
    }
    return {
      ok: true,
      ...identity,
      raw_user_access: false,
      denied_capabilities: 11,
    };
  }
  if (input.transaction === "create_or_duplicate") {
    const sqlStates = extractSqlStates(result.stderr ?? "");
    if (sqlStates.length === 0 && lines.length === 3 && lines[2] === "1") {
      const entry = input.requests[0];
      return {
        ok: true,
        ...identity,
        raw_user_access: false,
        outcome: "created",
        result: validateReply(entry.request, entry.operation, parseStrictJson(lines[1])),
      };
    }
    if (sqlStates.includes("23505") && sqlStates.includes("25P02") && lines.length === 1) {
      return {
        ok: true,
        ...identity,
        raw_user_access: false,
        outcome: "duplicate_aborted",
        sqlstates: ["23505", "25P02"],
      };
    }
    fail();
  }
  if (result.status !== 0 || lines.length !== input.requests.length + 1) fail();
  const results = input.requests.map((entry, index) =>
    validateReply(entry.request, entry.operation, parseStrictJson(lines[index + 1])),
  );
  return {
    ok: true,
    ...identity,
    raw_user_access: false,
    transaction: input.transaction,
    results,
  };
}

function expectFailure(callback) {
  try {
    callback();
  } catch {
    return;
  }
  fail();
}

function selfCheck() {
  let checks = 0;
  const sqlStates = extractSqlStates(
    "psql:<stdin>:5: ERROR: 42501: permission denied for table users\n" +
      "LOCATION: aclcheck_error, aclchk.c:2845\n" +
      "psql:<stdin>:8: ERROR: 23505: duplicate key value violates unique constraint\n" +
      "psql:<stdin>:9: ERROR: 25P02: current transaction is aborted\n",
  );
  if (JSON.stringify(sqlStates) !== JSON.stringify(["42501", "23505", "25P02"])) fail();
  checks += 1;
  const accountId = "11111111-1111-4111-8111-111111111111";
  const secretHash = "bcrypt-hash-that-must-never-return";
  const lookup = validateLookupRequest({ schema: protocol, email: "owner@example.test" });
  if (
    validateReply(lookup, "lookup", {
      schema: protocol,
      email: lookup.email,
      account_id: null,
    }).account_id !== null
  ) {
    fail();
  }
  checks += 1;

  const bootstrap = validateCreateRequest({
    schema: protocol,
    mode: "bootstrap_admin",
    account_id: accountId,
    email: "owner@example.test",
    name: "Owner",
    password_hash: secretHash,
    now: "2026-10-21T00:00:00.000000+00:00",
    request_id: "request-id",
  });
  const createReply = validateReply(bootstrap, "create", {
    schema: protocol,
    email: bootstrap.email,
    account_id: accountId,
  });
  if (JSON.stringify(createReply).includes(secretHash)) fail();
  checks += 1;

  const importRequest = validateCreateRequest({
    schema: protocol,
    mode: "import_admin",
    account_id: accountId,
    email: "admin",
    name: "超级管理员",
    password_hash: secretHash,
    now: null,
    request_id: null,
  });
  if (importRequest.now !== null || importRequest.request_id !== null) fail();
  checks += 1;

  const invalidRequests = [
    { protocol, email: "owner@example.test" },
    { schema: protocol, email: "owner@example.test", extra: true },
    { schema: protocol, email: 1 },
    {
      schema: protocol,
      mode: "bootstrap_admin",
      account_id: "not-a-uuid",
      email: "owner@example.test",
      name: "Owner",
      password_hash: secretHash,
      now: "2026-10-21T00:00:00+00:00",
      request_id: null,
    },
    {
      schema: protocol,
      mode: "bootstrap_admin",
      account_id: accountId,
      email: "owner@example.test",
      name: "Owner",
      password_hash: secretHash,
      now: null,
      request_id: null,
    },
    {
      schema: protocol,
      mode: "bootstrap_admin",
      account_id: accountId,
      email: "owner@example.test",
      name: "Owner",
      password_hash: secretHash,
      now: "2026-10-21T00:00:00",
      request_id: null,
    },
    {
      schema: protocol,
      mode: "bootstrap_admin",
      account_id: accountId,
      email: "owner@example.test",
      name: "Owner",
      password_hash: secretHash,
      now: "2026-02-30T00:00:00+00:00",
      request_id: null,
    },
    {
      schema: protocol,
      mode: "import_admin",
      account_id: accountId,
      email: "other@example.test",
      name: "超级管理员",
      password_hash: secretHash,
      now: null,
      request_id: null,
    },
    {
      schema: protocol,
      mode: "import_admin",
      account_id: accountId,
      email: "admin",
      name: "超级管理员",
      password_hash: secretHash,
      now: null,
      request_id: "unexpected",
    },
  ];
  for (const request of invalidRequests) {
    expectFailure(() =>
      Object.hasOwn(request, "mode")
        ? validateCreateRequest(request)
        : validateLookupRequest(request),
    );
    checks += 1;
  }

  for (const reply of [
    { schema: protocol, email: "other@example.test", account_id: null },
    { schema: protocol, email: "owner@example.test", account_id: null, password_hash: secretHash },
    {
      schema: protocol,
      email: "owner@example.test",
      account_id: "22222222-2222-4222-8222-222222222222",
    },
  ]) {
    expectFailure(() => validateReply(bootstrap, "create", reply));
    checks += 1;
  }
  expectFailure(() => parseStrictJson(`{"schema":"${protocol}","email":"a","email":"b"}`));
  checks += 1;
  expectFailure(() => parseStrictJson(`{"schema":"${protocol}","email":"a","x":NaN}`));
  checks += 1;

  const adversarialEmail = "owner@example.test'); SELECT pg_sleep(9); --";
  const lookupSql = functionSql("lookup", { schema: protocol, email: adversarialEmail });
  if (lookupSql.includes(adversarialEmail) || !/decode\('[0-9a-f]*', 'hex'\)/.test(lookupSql))
    fail();
  checks += 1;

  const call = validateCallInput({
    container_id: "a".repeat(64),
    database: "identity_test",
    requests: [
      { operation: "lookup", request: { schema: protocol, email: "owner@example.test" } },
      { operation: "create", request: bootstrap },
    ],
    transaction: "commit",
  });
  if (call.requests.length !== 2 || call.database !== "identity_test") fail();
  if (buildSql(call).includes(secretHash)) fail();
  checks += 1;
  const raceCall = validateCallInput({
    container_id: "a".repeat(64),
    database: "identity_test",
    requests: [{ operation: "create", request: bootstrap }],
    transaction: "create_or_duplicate",
  });
  if (raceCall.requests.length !== 1) fail();
  checks += 1;
  const probeCall = validateCallInput({
    container_id: "a".repeat(64),
    database: "identity_test",
    requests: [],
    transaction: "probe_denied",
  });
  if (buildSql(probeCall).includes(secretHash)) fail();
  checks += 1;
  expectFailure(() =>
    validateCallInput({
      container_id: "a".repeat(64),
      database: "production",
      requests: [{ operation: "lookup", request: lookup }],
      transaction: "commit",
    }),
  );
  checks += 1;
  expectFailure(() =>
    validateCallInput({
      container_id: "a".repeat(64),
      database: "identity_test",
      requests: [{ operation: "lookup", request: lookup }],
      transaction: "probe_denied",
    }),
  );
  checks += 1;

  return checks;
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 1024 * 1024) fail();
    chunks.push(chunk);
  }
  try {
    return parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
  } catch {
    fail();
  }
}

async function main() {
  if (process.argv[2] === "--self-check") {
    process.stdout.write(`${JSON.stringify({ ok: true, checks: selfCheck() })}\n`);
    return;
  }
  process.stdout.write(`${stringifyStrict(callOwner(await readInput()))}\n`);
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch(() => {
    process.stderr.write("Identity provisioning peer validation failed\n");
    process.exitCode = 1;
  });
}
