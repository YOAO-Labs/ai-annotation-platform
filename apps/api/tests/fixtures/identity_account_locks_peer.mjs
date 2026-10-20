import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

const schema = "aap-identity-account-locks/v1";
const modes = new Map([
  ["share", "lock_accounts_share"],
  ["update", "lock_accounts_update"],
  ["key_share", "lock_accounts_key_share"],
]);
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const accountFields = ["id", "name", "email", "role", "is_active"];

function fail() {
  throw new Error("Invalid Identity account-lock request or response");
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

function validateRequest(value) {
  if (
    !exact(value, ["schema", "account_ids", "mode", "nowait"]) ||
    value.schema !== schema ||
    !modes.has(value.mode) ||
    typeof value.nowait !== "boolean" ||
    !Array.isArray(value.account_ids)
  ) {
    fail();
  }
  const accountIds = value.account_ids.map(uuid);
  return { schema, account_ids: accountIds, mode: value.mode, nowait: value.nowait };
}

function validateReply(request, value) {
  if (!exact(value, ["schema", "accounts"]) || value.schema !== schema) fail();
  if (!Array.isArray(value.accounts)) fail();
  const requested = new Set(request.account_ids);
  const accounts = value.accounts.map((record) => {
    if (
      !exact(record, accountFields) ||
      typeof record.name !== "string" ||
      typeof record.email !== "string" ||
      typeof record.role !== "string" ||
      typeof record.is_active !== "boolean"
    ) {
      fail();
    }
    const id = uuid(record.id);
    if (!requested.has(id)) fail();
    return { ...record, id };
  });
  const ids = accounts.map((account) => account.id);
  if (new Set(ids).size !== ids.length || ids.some((id, index) => id !== [...ids].sort()[index])) {
    fail();
  }
  return { schema, accounts };
}

function validateCallInput(value) {
  if (!exact(value, ["container_id", "database", "request"])) fail();
  if (
    typeof value.container_id !== "string" ||
    !/^[0-9a-f]{64}$/.test(value.container_id) ||
    typeof value.database !== "string" ||
    !/^[a-z0-9_]+_test$/.test(value.database)
  ) {
    fail();
  }
  return {
    containerId: value.container_id,
    database: value.database,
    request: validateRequest(value.request),
  };
}

function roleProbeSql(request) {
  const ids = request.account_ids.map((id) => `'${id}'::uuid`).join(",");
  const array = `ARRAY[${ids}]::uuid[]`;
  const lock = modes.get(request.mode);
  return [
    "BEGIN;",
    "SELECT current_user || '|' || session_user || '|' || current_database() || '|' ||",
    "CASE WHEN has_table_privilege(current_user, 'public.users', 'SELECT') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_table_privilege(current_user, 'public.users', 'INSERT') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_table_privilege(current_user, 'public.users', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_table_privilege(current_user, 'public.users', 'DELETE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_database_privilege(current_user, current_database(), 'TEMP') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_any_column_privilege(current_user, 'public.users', 'SELECT') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_schema_privilege(current_user, 'aap_identity_v1', 'CREATE') THEN 'true' ELSE 'false' END || '|' ||",
    "CASE WHEN has_schema_privilege(current_user, 'aap_identity_v1', 'USAGE') THEN 'true' ELSE 'false' END;",
    `SELECT aap_identity_v1.${lock}(${array}, ${request.nowait ? "true" : "false"});`,
    "ROLLBACK;",
  ].join("\n");
}

function callOwner(value) {
  const { containerId, database, request } = validateCallInput(value);
  const result = spawnSync(
    "docker",
    [
      "exec",
      "-i",
      containerId,
      "psql",
      "--no-psqlrc",
      "-q",
      "-A",
      "-t",
      "-U",
      "append_client",
      "-d",
      database,
      "-v",
      "ON_ERROR_STOP=1",
      "-f",
      "-",
    ],
    {
      input: roleProbeSql(request),
      encoding: "utf8",
      timeout: 30_000,
      maxBuffer: 4 * 1024 * 1024,
      env: { PATH: process.env.PATH ?? "" },
    },
  );
  if (result.error || result.status !== 0) fail();
  const lines = result.stdout.split(/\r?\n/).filter((line) => line.length > 0);
  if (lines.length !== 2) fail();
  const role = lines[0].split("|");
  if (
    role.length !== 11 ||
    role[0] !== "append_client" ||
    role[1] !== "append_client" ||
    role[2] !== value.database ||
    role.slice(3, 10).some((privilege) => privilege !== "false") ||
    role[10] !== "true"
  ) {
    fail();
  }
  const reply = validateReply(request, parseStrictJson(lines[1]));
  return {
    ok: true,
    current_user: role[0],
    session_user: role[1],
    database: role[2],
    raw_user_privileges: false,
    request: {
      account_ids: request.account_ids,
      mode: request.mode,
      nowait: request.nowait,
    },
    accounts: reply.accounts,
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
  const first = "11111111-1111-4111-8111-111111111111";
  const second = "22222222-2222-4222-8222-222222222222";
  const request = validateRequest({
    schema,
    account_ids: [second, first, second],
    mode: "share",
    nowait: true,
  });
  const reply = validateReply(request, {
    schema,
    accounts: [
      {
        id: first,
        name: "  Owner ß  ",
        email: " owner@example.test ",
        role: "historical/reviewer",
        is_active: false,
      },
    ],
  });
  if (
    reply.accounts.length !== 1 ||
    reply.accounts[0].role !== "historical/reviewer" ||
    reply.accounts[0].is_active !== false
  ) {
    fail();
  }
  checks += 1;

  expectFailure(() =>
    validateRequest({
      schema,
      account_ids: [first],
      mode: "share",
      nowait: 1,
    }),
  );
  checks += 1;
  expectFailure(() =>
    validateRequest({
      schema,
      account_ids: [first],
      mode: "update",
      nowait: false,
      token: "unexpected",
    }),
  );
  checks += 1;
  for (const accounts of [
    [reply.accounts[0], reply.accounts[0]],
    [
      { ...reply.accounts[0], id: second },
      { ...reply.accounts[0], id: first },
    ],
    [{ ...reply.accounts[0], is_active: 0 }],
    [{ ...reply.accounts[0], password_hash: "must reject" }],
  ]) {
    expectFailure(() => validateReply(request, { schema, accounts }));
    checks += 1;
  }
  expectFailure(() =>
    parseStrictJson('{"schema":"aap-identity-account-locks/v1","accounts":[],"accounts":[]}'),
  );
  checks += 1;
  expectFailure(() =>
    parseStrictJson('{"schema":"aap-identity-account-locks/v1","accounts":[],"x":NaN}'),
  );
  checks += 1;
  if (!stringifyStrict({ schema, accounts: [] }).includes(schema)) fail();
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
    process.stderr.write("Identity account-lock peer validation failed\n");
    process.exitCode = 1;
  });
}
