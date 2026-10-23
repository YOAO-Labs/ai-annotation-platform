import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { isIP } from "node:net";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

import {
  principalReceipt,
  uuid,
  validDateTime,
  validateInput,
} from "./identity_admission_peer.mjs";

const accountSchema = "aap-identity-accounts/v1";
const presenceSchema = "aap-identity-presence/v1";
const identitySchema = "aap-identity/v1";
const issuer = "aap-identity/v1";
const accountPath = "/v1/identity/accounts/execute";
const presencePath = "/v1/identity/presence/mark-inactive";
const authPath = "/v1/identity/authenticate";
const operations = new Set([
  "read_self_account",
  "update_name",
  "get_preferences",
  "patch_preferences",
  "change_password",
  "set_avatar_ref",
  "heartbeat",
]);
const roles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
const admissionFailures = new Set([
  "invalid_request",
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
]);
const accountFailures = new Set([
  ...admissionFailures,
  "account_busy",
  "operation_conflict",
  "policy_rejected",
]);
const maxInt64 = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799n;
const passwordDiagnostics = [
  "密码长度至少 8 位",
  "密码需包含大写字母",
  "密码需包含小写字母",
  "密码需包含数字",
];
const maxRequestDeadlineMs = 15_000;

function fail() {
  throw new Error("Invalid Identity account request or response");
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exact(value, keys) {
  return (
    isRecord(value) &&
    Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
  );
}

function codePointLength(value) {
  return Array.from(value).length;
}

function boundedString(value, min, max) {
  return (
    typeof value === "string" && codePointLength(value) >= min && codePointLength(value) <= max
  );
}

function expectedPasswordDiagnostics(value) {
  const errors = [];
  if (codePointLength(value) < 8) errors.push(passwordDiagnostics[0]);
  if (!/[A-Z]/.test(value)) errors.push(passwordDiagnostics[1]);
  if (!/[a-z]/.test(value)) errors.push(passwordDiagnostics[2]);
  if (!/\p{Decimal_Number}/u.test(value)) errors.push(passwordDiagnostics[3]);
  return errors;
}

function pathKey(path) {
  return JSON.stringify(path);
}

function scanJson(source) {
  let offset = 0;
  const spans = new Map();
  const skipWhitespace = () => {
    while (offset < source.length && /[\t\n\r ]/.test(source[offset])) offset += 1;
  };
  const readString = () => {
    if (source[offset] !== '"') fail();
    const start = offset;
    offset += 1;
    while (offset < source.length) {
      const char = source[offset];
      if (char === '"') {
        offset += 1;
        try {
          return JSON.parse(source.slice(start, offset));
        } catch {
          fail();
        }
      }
      if (char === "\\") {
        offset += 1;
        if (source[offset] === "u") offset += 5;
        else offset += 1;
      } else {
        offset += 1;
      }
    }
    fail();
  };
  const readValue = (path) => {
    skipWhitespace();
    const start = offset;
    const char = source[offset];
    if (char === "{") {
      offset += 1;
      skipWhitespace();
      const keys = new Set();
      if (source[offset] !== "}") {
        while (true) {
          skipWhitespace();
          const key = readString();
          if (keys.has(key)) fail();
          keys.add(key);
          skipWhitespace();
          if (source[offset] !== ":") fail();
          offset += 1;
          readValue([...path, key]);
          skipWhitespace();
          if (source[offset] === "}") break;
          if (source[offset] !== ",") fail();
          offset += 1;
        }
      }
      offset += 1;
    } else if (char === "[") {
      offset += 1;
      skipWhitespace();
      let index = 0;
      if (source[offset] !== "]") {
        while (true) {
          readValue([...path, index]);
          index += 1;
          skipWhitespace();
          if (source[offset] === "]") break;
          if (source[offset] !== ",") fail();
          offset += 1;
        }
      }
      offset += 1;
    } else if (char === '"') {
      readString();
    } else {
      while (offset < source.length && !/[\t\n\r ,\]}]/.test(source[offset])) {
        offset += 1;
      }
      if (offset === start) fail();
    }
    const span = { start, end: offset };
    spans.set(pathKey(path), span);
    return span;
  };

  readValue([]);
  skipWhitespace();
  if (offset !== source.length) fail();
  return spans;
}

function getPath(value, path) {
  let current = value;
  for (const key of path) {
    if (current === null || typeof current !== "object") return undefined;
    current = current[key];
  }
  return current;
}

function setPath(value, path, replacement) {
  let parent = value;
  for (const key of path.slice(0, -1)) parent = parent[key];
  parent[path.at(-1)] = replacement;
}

function rawAtPath(source, spans, path) {
  const span = spans.get(pathKey(path));
  return span ? source.slice(span.start, span.end) : undefined;
}

function strictUnsignedInteger(source, spans, document, path, max = null) {
  const value = getPath(document, path);
  if (value === null || value === undefined) return null;
  if (typeof value !== "number") fail();
  const raw = rawAtPath(source, spans, path);
  if (typeof raw !== "string" || !/^(0|[1-9][0-9]*)$/.test(raw)) fail();
  const integer = BigInt(raw);
  if (max !== null && integer > max) fail();
  setPath(document, path, integer);
  return integer;
}

function strictPositiveEpoch(source, spans, document, path) {
  const raw = rawAtPath(source, spans, path);
  if (typeof raw !== "string" || !/^[1-9][0-9]*$/.test(raw)) fail();
  const integer = BigInt(raw);
  if (integer > maxPythonEpochSeconds) fail();
  setPath(document, path, Number(integer));
  return Number(integer);
}

function parseJson(source, { intPaths = [], epochPaths = [] } = {}) {
  if (typeof source !== "string") fail();
  const spans = scanJson(source);
  let document;
  try {
    document = JSON.parse(source);
  } catch {
    fail();
  }
  for (const path of intPaths) {
    const field = path.at(-1);
    strictUnsignedInteger(
      source,
      spans,
      document,
      path,
      ["gen", "generation", "affected"].includes(field) ? maxInt64 : null,
    );
  }
  for (const path of epochPaths) strictPositiveEpoch(source, spans, document, path);
  return { document, spans };
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  const sizes = { method: 8, path: 256, ip: 64, request_id: 36 };
  for (const [field, max] of Object.entries(sizes)) {
    if (value[field] !== null && !boundedString(value[field], 0, max)) fail();
  }
  if (value.ip && isIP(value.ip) === 0) fail();
}

function validateAvatarPayload(payload) {
  if (!exact(payload, ["avatar_ref", "kind", "bytes"])) fail();
  if (payload.avatar_ref !== null && !boundedString(payload.avatar_ref, 0, 80)) {
    fail();
  }
  if (payload.kind === "upload") {
    if (
      typeof payload.avatar_ref !== "string" ||
      !/^upload:[0-9a-f]{32}$/.test(payload.avatar_ref) ||
      typeof payload.bytes !== "bigint" ||
      payload.bytes < 0n
    ) {
      fail();
    }
  } else if (payload.kind === "preset_or_clear") {
    if (
      (payload.avatar_ref !== null && !/^preset:[a-z0-9-]{1,40}$/.test(payload.avatar_ref)) ||
      payload.bytes !== null
    ) {
      fail();
    }
  } else {
    fail();
  }
}

export function validateAccountRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "token", "audit_context", "payload"]) ||
    value.schema !== accountSchema ||
    !operations.has(value.operation)
  ) {
    fail();
  }
  const operationId = uuid(value.operation_id);
  if (!boundedString(value.token, 1, 16384)) fail();
  validateAuditContext(value.audit_context);

  const payload = value.payload;
  if (["read_self_account", "get_preferences", "heartbeat"].includes(value.operation)) {
    if (!exact(payload, [])) fail();
  } else if (value.operation === "update_name") {
    if (!exact(payload, ["name"]) || !boundedString(payload.name, 1, 100)) fail();
  } else if (value.operation === "patch_preferences") {
    if (!exact(payload, ["patch"]) || !isRecord(payload.patch)) fail();
  } else if (value.operation === "change_password") {
    if (
      !exact(payload, ["old_password", "new_password"]) ||
      !boundedString(payload.old_password, 1, 128) ||
      !boundedString(payload.new_password, 8, 128)
    ) {
      fail();
    }
  } else if (value.operation === "set_avatar_ref") {
    validateAvatarPayload(payload);
  } else {
    fail();
  }
  return { request: value, operationId };
}

function decodeBase64Url(value) {
  if (!/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) fail();
  const bytes = Buffer.from(value, "base64url");
  if (bytes.toString("base64url") !== value) fail();
  return bytes;
}

function readJwtClaims(token) {
  const parts = token.split(".");
  if (parts.length !== 3 || parts[2].length !== 43) fail();
  const decode = (segment) => {
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(decodeBase64Url(segment));
    } catch {
      fail();
    }
  };
  const headerJson = decode(parts[0]);
  const header = parseJson(headerJson).document;
  if (!exact(header, ["alg", "typ"]) || header.alg !== "HS256" || header.typ !== "JWT") fail();
  if (decodeBase64Url(parts[2]).length !== 32) fail();
  const payloadJson = decode(parts[1]);
  const parsed = parseJson(payloadJson, {
    intPaths: [["gen"]],
    epochPaths: [["exp"]],
  }).document;
  if (
    !exact(parsed, ["sub", "role", "exp", "jti", "gen", "iss"]) ||
    !roles.has(parsed.role) ||
    parsed.iss !== issuer ||
    typeof parsed.exp !== "number" ||
    typeof parsed.gen !== "bigint"
  ) {
    fail();
  }
  return {
    actor_id: uuid(parsed.sub),
    credential_id: uuid(parsed.jti),
    generation_decimal: parsed.gen.toString(10),
    expires_at_epoch: parsed.exp,
  };
}

function principalExpiryMatches(principal, epoch) {
  if (typeof principal.expires_at !== "string" || !validDateTime(principal.expires_at)) {
    return false;
  }
  return Date.parse(principal.expires_at) === epoch * 1000;
}

function validateAccountRecord(value, actorId) {
  const fields = [
    "id",
    "email",
    "name",
    "role",
    "group_name",
    "group_id",
    "status",
    "is_active",
    "disabled_kind",
    "disabled_at",
    "disabled_by",
    "disabled_reason",
    "email_verified_at",
    "last_login_at",
    "last_seen_at",
    "password_admin_reset_at",
    "deactivation_requested_at",
    "deactivation_scheduled_at",
    "preferences",
    "avatar_ref",
    "created_at",
  ];
  if (!exact(value, fields)) fail();
  const accountId = uuid(value.id);
  if (
    accountId !== actorId ||
    typeof value.email !== "string" ||
    typeof value.name !== "string" ||
    !roles.has(value.role) ||
    (value.group_name !== null && typeof value.group_name !== "string") ||
    typeof value.status !== "string" ||
    typeof value.is_active !== "boolean" ||
    (value.disabled_kind !== null && typeof value.disabled_kind !== "string") ||
    (value.disabled_reason !== null && typeof value.disabled_reason !== "string") ||
    !isRecord(value.preferences) ||
    (value.avatar_ref !== null && !boundedString(value.avatar_ref, 0, 80)) ||
    !validDateTime(value.created_at)
  ) {
    fail();
  }
  if (value.group_id !== null) uuid(value.group_id);
  if (value.disabled_by !== null) uuid(value.disabled_by);
  for (const field of [
    "disabled_at",
    "email_verified_at",
    "last_login_at",
    "last_seen_at",
    "password_admin_reset_at",
    "deactivation_requested_at",
    "deactivation_scheduled_at",
  ]) {
    if (value[field] !== null && !validDateTime(value[field])) fail();
  }
  value.id = accountId;
  return value;
}

function validatePreferenceErrors(detail, source, spans, request) {
  if (!Array.isArray(detail) || detail.length === 0) fail();
  for (let index = 0; index < detail.length; index += 1) {
    const issue = detail[index];
    if (!exact(issue, ["type", "loc", "msg"]) || typeof issue.type !== "string") fail();
    if (
      !Array.isArray(issue.loc) ||
      issue.loc.length === 0 ||
      issue.loc[0] !== "body" ||
      issue.loc.some((part) => typeof part !== "string" && !Number.isSafeInteger(part)) ||
      typeof issue.msg !== "string"
    ) {
      fail();
    }
    for (let part = 0; part < issue.loc.length; part += 1) {
      if (typeof issue.loc[part] === "number") {
        const raw = rawAtPath(source, spans, ["error", "detail", index, "loc", part]);
        if (!raw || !/^-?(0|[1-9][0-9]*)$/.test(raw)) fail();
      }
    }
    assertNoCredentialEcho(issue, request);
  }
}

function assertNoCredentialEcho(value, request) {
  const secrets = [request.token];
  if (request.operation === "change_password") {
    secrets.push(request.payload.old_password, request.payload.new_password);
  }
  const visit = (item) => {
    if (typeof item === "string") {
      for (const secret of secrets) {
        if (secret && (item === secret || (secret.length >= 8 && item.includes(secret)))) fail();
      }
    } else if (Array.isArray(item)) {
      for (const child of item) visit(child);
    } else if (isRecord(item)) {
      for (const child of Object.values(item)) visit(child);
    }
  };
  visit(value);
}

function validatePolicyDetail(request, statusCode, detail, source, spans) {
  if (request.operation === "update_name") {
    if (statusCode !== 400n || request.payload.name.trim() !== "" || detail !== "姓名不能为空") {
      fail();
    }
  } else if (request.operation === "change_password") {
    if (statusCode === 400n) {
      const accepted = new Set(["原密码不正确", "新密码不能与原密码相同"]);
      if (typeof detail !== "string" || !accepted.has(detail)) fail();
      if (
        request.payload.old_password !== request.payload.new_password &&
        detail !== "原密码不正确"
      )
        fail();
    } else if (statusCode === 422n) {
      if (!Array.isArray(detail) || detail.length !== 1) fail();
      const issue = detail[0];
      if (
        !exact(issue, ["type", "loc", "msg"]) ||
        issue.type !== "value_error" ||
        JSON.stringify(issue.loc) !== JSON.stringify(["body", "new_password"]) ||
        typeof issue.msg !== "string" ||
        !issue.msg.startsWith("Value error, ")
      ) {
        fail();
      }
      const parts = issue.msg.slice("Value error, ".length).split("; ");
      const expected = expectedPasswordDiagnostics(request.payload.new_password);
      if (
        expected.length === 0 ||
        JSON.stringify(parts) !== JSON.stringify(expected) ||
        new Set(parts).size !== parts.length ||
        parts.some((part) => !passwordDiagnostics.includes(part))
      ) {
        fail();
      }
      assertNoCredentialEcho(detail, request);
    } else {
      fail();
    }
  } else if (request.operation === "patch_preferences") {
    if (statusCode === 422n && Array.isArray(detail)) {
      validatePreferenceErrors(detail, source, spans, request);
    } else if (statusCode === 422n && detail === "named_presets_revision_read_only") {
      // Frozen public policy detail.
    } else if (
      statusCode === 409n &&
      (detail === "layout_engine_downgrade" || detail === "layout_schema_downgrade")
    ) {
      // Frozen public policy detail.
    } else if (statusCode === 409n && isRecord(detail)) {
      const required = ["code", "message"];
      const fields = Object.keys(detail);
      if (
        !fields.every((key) => required.includes(key) || key === "currentRevision") ||
        !required.every((key) => Object.hasOwn(detail, key)) ||
        detail.code !== "named_presets_conflict" ||
        detail.message !== "命名布局预设已在其他会话更新，请刷新后重试" ||
        (Object.hasOwn(detail, "currentRevision") && typeof detail.currentRevision !== "string")
      ) {
        fail();
      }
    } else {
      fail();
    }
  } else {
    fail();
  }
  assertNoCredentialEcho(detail, request);
}

function decodeAccountResponse(requestContext, source, status, contentType, apiKeyPrincipal) {
  const { request, operationId } = requestContext;
  if (
    typeof source !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const { document, spans } = parseJson(source, {
    intPaths: [
      ["principal", "generation"],
      ["error", "status_code"],
    ],
    epochPaths: [],
  });
  if (
    !isRecord(document) ||
    document.schema !== accountSchema ||
    typeof document.ok !== "boolean"
  ) {
    fail();
  }
  if (document.ok === false) {
    if (
      !exact(document, ["schema", "ok", "error"]) ||
      !exact(document.error, ["code", "status_code", "detail"])
    )
      fail();
    const error = document.error;
    if (!accountFailures.has(error.code)) fail();
    if (error.code !== "policy_rejected") {
      if (error.status_code !== null || error.detail !== null) fail();
      return {
        status,
        schema: accountSchema,
        ok: false,
        error: { code: error.code, status_code: null, detail: null },
      };
    }
    if (typeof error.status_code !== "bigint" || ![400n, 409n, 422n].includes(error.status_code)) {
      fail();
    }
    validatePolicyDetail(request, error.status_code, error.detail, source, spans);
    return {
      status,
      schema: accountSchema,
      ok: false,
      error: {
        code: "policy_rejected",
        status_code: Number(error.status_code),
        detail: error.detail,
      },
    };
  }

  if (
    !exact(document, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    document.operation !== request.operation ||
    uuid(document.operation_id) !== operationId
  ) {
    fail();
  }
  const principal = principalReceipt(document.principal);
  bindPrincipal(principal, request, apiKeyPrincipal);
  if (!exact(document.data, ["account", "preferences", "previous_avatar_ref", "changed"])) fail();
  const data = document.data;
  if (request.operation === "read_self_account") {
    if (
      data.account === null ||
      data.preferences !== null ||
      data.previous_avatar_ref !== null ||
      data.changed !== null
    )
      fail();
    validateAccountRecord(data.account, principal.actor_id);
  } else if (request.operation === "update_name") {
    if (
      data.account === null ||
      data.preferences !== null ||
      data.previous_avatar_ref !== null ||
      typeof data.changed !== "boolean"
    )
      fail();
    validateAccountRecord(data.account, principal.actor_id);
    if (data.account.name !== request.payload.name.trim()) fail();
  } else if (request.operation === "set_avatar_ref") {
    if (data.account === null || data.preferences !== null || typeof data.changed !== "boolean")
      fail();
    if (data.previous_avatar_ref !== null && !boundedString(data.previous_avatar_ref, 0, 80))
      fail();
    validateAccountRecord(data.account, principal.actor_id);
    if (data.account.avatar_ref !== request.payload.avatar_ref) fail();
    if (
      (data.changed && data.previous_avatar_ref === request.payload.avatar_ref) ||
      (!data.changed && data.previous_avatar_ref !== null)
    ) {
      fail();
    }
  } else if (["get_preferences", "patch_preferences"].includes(request.operation)) {
    if (
      data.account !== null ||
      !isRecord(data.preferences) ||
      data.previous_avatar_ref !== null ||
      data.changed !== null
    )
      fail();
  } else if (request.operation === "heartbeat" || request.operation === "change_password") {
    if (
      data.account !== null ||
      data.preferences !== null ||
      data.previous_avatar_ref !== null ||
      data.changed !== null
    )
      fail();
  } else {
    fail();
  }
  const receipt = {
    status,
    schema: accountSchema,
    ok: true,
    operation: request.operation,
    operation_id: operationId,
    principal,
    data,
  };
  assertNoCredentialEcho(receipt, request);
  return receipt;
}

function parseAdmissionPrincipal(source, status, contentType) {
  if (
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const { document } = parseJson(source, { intPaths: [["data", "generation"]] });
  if (
    !isRecord(document) ||
    document.schema !== identitySchema ||
    typeof document.ok !== "boolean"
  ) {
    fail();
  }
  if (document.ok === false) {
    if (
      !exact(document, ["schema", "ok", "error"]) ||
      !exact(document.error, ["code"]) ||
      !admissionFailures.has(document.error.code)
    ) {
      fail();
    }
    return null;
  }
  if (!exact(document, ["schema", "ok", "data"])) fail();
  return principalReceipt(document.data);
}

function bindPrincipal(principal, request, apiKeyPrincipal) {
  if (request.token.startsWith("ak_")) {
    if (
      apiKeyPrincipal === null ||
      apiKeyPrincipal.credential_type !== "api_key" ||
      principal.actor_id !== apiKeyPrincipal.actor_id ||
      principal.credential_type !== "api_key" ||
      principal.credential_id !== apiKeyPrincipal.credential_id
    ) {
      fail();
    }
    return;
  }
  const claims = readJwtClaims(request.token);
  if (
    principal.actor_id !== claims.actor_id ||
    principal.credential_type !== "jwt" ||
    principal.credential_id !== claims.credential_id ||
    principal.generation_decimal !== claims.generation_decimal ||
    !principalExpiryMatches(principal, claims.expires_at_epoch)
  ) {
    fail();
  }
}

function validatePresenceRequest(value) {
  if (
    !exact(value, ["schema", "cutoff"]) ||
    value.schema !== presenceSchema ||
    typeof value.cutoff !== "string" ||
    !validDateTime(value.cutoff)
  ) {
    fail();
  }
  return value;
}

function decodePresenceResponse(source, status, contentType) {
  if (
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const { document } = parseJson(source, { intPaths: [["affected"]] });
  if (
    !exact(document, ["schema", "affected"]) ||
    document.schema !== presenceSchema ||
    typeof document.affected !== "bigint"
  )
    fail();
  return { status, schema: presenceSchema, affected_decimal: document.affected.toString(10) };
}

function postIdentityRequest(origin, controlToken, path, body) {
  const endpoint = new URL(path, origin);
  const transport = endpoint.protocol === "https:" ? httpsRequest : httpRequest;
  const bytes = Buffer.isBuffer(body) ? body : Buffer.from(JSON.stringify(body), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const request = transport(
      endpoint,
      {
        method: "POST",
        agent: false,
        signal: AbortSignal.timeout(maxRequestDeadlineMs),
        headers: {
          authorization: `Bearer ${controlToken}`,
          "content-type": "application/json",
          accept: "application/json",
          "content-length": bytes.length,
        },
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          try {
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              text: new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
            });
          } catch {
            rejectResponse(new Error("Identity response invalid"));
          }
        });
        response.on("error", () => rejectResponse(new Error("Identity response failed")));
      },
    );
    request.on("error", () => rejectResponse(new Error("Identity request failed")));
    request.end(bytes);
  });
}

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch {
    fail();
  }
}

async function main() {
  const source = await readInput();
  const { document, spans } = parseJson(source, {
    intPaths: [["request", "payload", "bytes"]],
  });
  if (!exact(document, ["url", "control_token", "request"])) fail();
  const requestSpan = spans.get(pathKey(["request"]));
  if (!requestSpan) fail();
  const rawRequest = Buffer.from(source.slice(requestSpan.start, requestSpan.end), "utf8");
  const request = document.request;
  const presence = isRecord(request) && request.schema === presenceSchema;
  const validation = presence ? validatePresenceRequest(request) : validateAccountRequest(request);
  const { origin, controlToken } = validateInput({
    url: document.url,
    control_token: document.control_token,
    credential: presence ? "presence-operation" : validation.request.token,
  });

  if (presence) {
    const response = await postIdentityRequest(origin, controlToken, presencePath, rawRequest);
    process.stdout.write(
      `${JSON.stringify(decodePresenceResponse(response.text, response.status, response.contentType))}\n`,
    );
    return;
  }

  let apiKeyPrincipal = null;
  if (validation.request.token.startsWith("ak_")) {
    const admission = await postIdentityRequest(origin, controlToken, authPath, {
      token: validation.request.token,
    });
    apiKeyPrincipal = parseAdmissionPrincipal(
      admission.text,
      admission.status,
      admission.contentType,
    );
  }
  const response = await postIdentityRequest(origin, controlToken, accountPath, rawRequest);
  const receipt = decodeAccountResponse(
    validation,
    response.text,
    response.status,
    response.contentType,
    apiKeyPrincipal,
  );
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
}

function stringifyLossless(value) {
  if (typeof value === "bigint") return value.toString(10);
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stringifyLossless).join(",")}]`;
  return `{${Object.keys(value)
    .map((key) => `${JSON.stringify(key)}:${stringifyLossless(value[key])}`)
    .join(",")}}`;
}

function testPrincipal(actorId, credentialId) {
  return {
    actor_id: actorId,
    name: "Current owner",
    email: "current@example.test",
    role: "employee",
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation: maxInt64,
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
  };
}

function testAccount(actorId, overrides = {}) {
  return {
    id: actorId,
    email: "current@example.test",
    name: "Current owner",
    role: "employee",
    group_name: null,
    group_id: null,
    status: "online",
    is_active: true,
    disabled_kind: null,
    disabled_at: null,
    disabled_by: null,
    disabled_reason: null,
    email_verified_at: "2025-01-01T00:00:00Z",
    last_login_at: null,
    last_seen_at: "2026-10-20T00:00:00Z",
    password_admin_reset_at: null,
    deactivation_requested_at: null,
    deactivation_scheduled_at: null,
    preferences: {},
    avatar_ref: null,
    created_at: "2024-01-01T00:00:00Z",
    ...overrides,
  };
}

function testRequest(operation, operationId, token, payload = {}) {
  return validateAccountRequest({
    schema: accountSchema,
    operation,
    operation_id: operationId,
    token,
    audit_context: { method: "PATCH", path: "/api/v1/auth/me", ip: "127.0.0.1", request_id: null },
    payload,
  });
}

function testReply(requestContext, principal, data, overrides = {}) {
  return stringifyLossless({
    schema: accountSchema,
    ok: true,
    operation: requestContext.request.operation,
    operation_id: requestContext.operationId,
    principal,
    data,
    ...overrides,
  });
}

function expectFailure(callback) {
  let failed = false;
  try {
    callback();
  } catch {
    failed = true;
  }
  if (!failed) throw new Error("Account peer self-check accepted invalid input");
}

function selfCheck() {
  const actorId = "11111111-1111-4111-8111-111111111111";
  const jti = "22222222-2222-4222-8222-222222222222";
  const operationId = "33333333-3333-4333-8333-333333333333";
  const token = `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.${Buffer.from(
    `{"sub":"${actorId}","role":"employee","exp":1893456000,"jti":"${jti}","gen":${maxInt64},"iss":"${issuer}"}`,
  ).toString("base64url")}.${Buffer.alloc(32, 9).toString("base64url")}`;
  const principal = testPrincipal(actorId, jti);

  const read = testRequest("read_self_account", operationId, token);
  const account = testAccount(actorId);
  const readData = { account, preferences: null, previous_avatar_ref: null, changed: null };
  const readReceipt = decodeAccountResponse(
    read,
    testReply(read, principal, readData),
    200,
    "application/json",
    null,
  );
  if (readReceipt.data.account.id !== actorId) fail();

  const nameRequest = testRequest("update_name", operationId, token, { name: " New name " });
  const renamed = testAccount(actorId, { name: "New name" });
  const nameReceipt = decodeAccountResponse(
    nameRequest,
    testReply(nameRequest, principal, {
      account: renamed,
      preferences: null,
      previous_avatar_ref: null,
      changed: true,
    }),
    200,
    "application/json",
    null,
  );
  if (nameReceipt.data.account.name !== "New name") fail();

  const getPrefs = testRequest("get_preferences", operationId, token);
  const prefs = { workbench: { layout: { engine: "dockview@8" } } };
  decodeAccountResponse(
    getPrefs,
    testReply(getPrefs, principal, {
      account: null,
      preferences: prefs,
      previous_avatar_ref: null,
      changed: null,
    }),
    200,
    "application/json",
    null,
  );

  const largeValue = "x".repeat(70 * 1024);
  const patchPrefs = testRequest("patch_preferences", operationId, token, {
    patch: { ai: { opaque: largeValue }, workspaceA: largeValue, workspaceB: largeValue },
  });
  const largeOuter = stringifyLossless({
    url: "http://127.0.0.1",
    control_token: "control-secret",
    request: patchPrefs.request,
  });
  if (Buffer.byteLength(largeOuter) <= 65 * 1024) fail();
  const largeInput = parseJson(largeOuter);
  if (validateAccountRequest(largeInput.document.request).request.operation !== "patch_preferences")
    fail();
  const rawRequestSpan = largeInput.spans.get(pathKey(["request"]));
  if (
    !rawRequestSpan ||
    largeOuter.slice(rawRequestSpan.start, rawRequestSpan.end).length <= 65 * 1024
  )
    fail();
  const largePrefsReply = decodeAccountResponse(
    patchPrefs,
    testReply(patchPrefs, principal, {
      account: null,
      preferences: patchPrefs.request.payload.patch,
      previous_avatar_ref: null,
      changed: null,
    }),
    200,
    "application/json",
    null,
  );
  if (largePrefsReply.data.preferences.workspaceB.length !== largeValue.length) fail();

  const passwordRequest = testRequest("change_password", operationId, token, {
    old_password: "old-secret-password",
    new_password: "NewStrong1Password",
  });
  const passwordReceipt = decodeAccountResponse(
    passwordRequest,
    testReply(passwordRequest, principal, {
      account: null,
      preferences: null,
      previous_avatar_ref: null,
      changed: null,
    }),
    200,
    "application/json",
    null,
  );
  const safePasswordOutput = JSON.stringify(passwordReceipt);
  if (
    safePasswordOutput.includes(passwordRequest.request.payload.old_password) ||
    safePasswordOutput.includes(passwordRequest.request.payload.new_password) ||
    safePasswordOutput.includes(token)
  )
    fail();
  const weakPasswordRequest = testRequest("change_password", operationId, token, {
    old_password: "old-secret-password",
    new_password: "weakpass",
  });
  const weakPasswordError = {
    schema: accountSchema,
    ok: false,
    error: {
      code: "policy_rejected",
      status_code: 422,
      detail: [
        {
          type: "value_error",
          loc: ["body", "new_password"],
          msg: "Value error, 密码需包含大写字母; 密码需包含数字",
        },
      ],
    },
  };
  if (
    decodeAccountResponse(
      weakPasswordRequest,
      stringifyLossless(weakPasswordError),
      200,
      "application/json",
      null,
    ).ok !== false
  ) {
    fail();
  }

  const avatarRequest = testRequest("set_avatar_ref", operationId, token, {
    avatar_ref: "preset:star-blue",
    kind: "preset_or_clear",
    bytes: null,
  });
  const avatarReceipt = decodeAccountResponse(
    avatarRequest,
    testReply(avatarRequest, principal, {
      account: testAccount(actorId, { avatar_ref: "preset:star-blue" }),
      preferences: null,
      previous_avatar_ref: null,
      changed: false,
    }),
    200,
    "application/json",
    null,
  );
  if (avatarReceipt.data.account.avatar_ref !== "preset:star-blue") fail();

  const replacementAvatar = "upload:0123456789abcdef0123456789abcdef";
  const changedAvatarRequest = testRequest("set_avatar_ref", operationId, token, {
    avatar_ref: replacementAvatar,
    kind: "upload",
    bytes: 4096n,
  });
  const changedAvatar = decodeAccountResponse(
    changedAvatarRequest,
    testReply(changedAvatarRequest, principal, {
      account: testAccount(actorId, { avatar_ref: replacementAvatar }),
      preferences: null,
      previous_avatar_ref: "preset:star-blue",
      changed: true,
    }),
    200,
    "application/json",
    null,
  );
  if (changedAvatar.data.previous_avatar_ref !== "preset:star-blue") fail();

  const heartbeat = testRequest("heartbeat", operationId, token);
  decodeAccountResponse(
    heartbeat,
    testReply(heartbeat, principal, {
      account: null,
      preferences: null,
      previous_avatar_ref: null,
      changed: null,
    }),
    200,
    "application/json",
    null,
  );

  const avatarUpload = testRequest("set_avatar_ref", operationId, token, {
    avatar_ref: "upload:0123456789abcdef0123456789abcdef",
    kind: "upload",
    bytes: 0n,
  });
  if (avatarUpload.request.payload.bytes !== 0n) fail();

  const badOwner = { ...readData, account: testAccount("44444444-4444-4444-8444-444444444444") };
  expectFailure(() =>
    decodeAccountResponse(
      read,
      testReply(read, principal, badOwner),
      200,
      "application/json",
      null,
    ),
  );
  expectFailure(() =>
    decodeAccountResponse(
      read,
      testReply(read, principal, { ...readData, password_hash: "secret" }),
      200,
      "application/json",
      null,
    ),
  );
  expectFailure(() =>
    parseJson(
      '{"request":{"schema":"aap-identity-accounts/v1","operation":"heartbeat","operation":"read_self_account"}}',
    ),
  );
  expectFailure(() =>
    decodePresenceResponse(
      '{"schema":"aap-identity-presence/v1","affected":1.0}',
      200,
      "application/json",
    ),
  );
  const cutoff = "2026-10-20T00:00:00Z";
  if (validatePresenceRequest({ schema: presenceSchema, cutoff }).cutoff !== cutoff) fail();
  expectFailure(() => validatePresenceRequest({ schema: presenceSchema, cutoff, extra: true }));
  const presence = decodePresenceResponse(
    `{"schema":"${presenceSchema}","affected":${maxInt64}}`,
    200,
    "application/json",
  );
  if (presence.affected_decimal !== maxInt64.toString(10)) fail();

  const apiKeyToken = `ak_${"a".repeat(32)}`;
  const apiKeyId = "77777777-7777-4777-8777-777777777777";
  const admittedApiKey = principalReceipt({
    actor_id: actorId,
    name: "Current owner",
    email: "current@example.test",
    role: "employee",
    is_active: true,
    credential_type: "api_key",
    credential_id: apiKeyId,
    generation: null,
    scopes: ["datasets:read"],
    expires_at: null,
  });
  const apiKeyRequest = testRequest("heartbeat", operationId, apiKeyToken);
  const apiKeyPrincipal = {
    actor_id: actorId,
    name: "Current owner",
    email: "current@example.test",
    role: "viewer",
    is_active: true,
    credential_type: "api_key",
    credential_id: apiKeyId,
    generation: null,
    scopes: [],
    expires_at: null,
  };
  const apiKeyData = {
    account: null,
    preferences: null,
    previous_avatar_ref: null,
    changed: null,
  };
  decodeAccountResponse(
    apiKeyRequest,
    testReply(apiKeyRequest, apiKeyPrincipal, apiKeyData),
    200,
    "application/json",
    admittedApiKey,
  );
  expectFailure(() =>
    decodeAccountResponse(
      apiKeyRequest,
      testReply(apiKeyRequest, { ...apiKeyPrincipal, credential_id: jti }, apiKeyData),
      200,
      "application/json",
      admittedApiKey,
    ),
  );

  const blankName = testRequest("update_name", operationId, token, { name: "   " });
  const blankError = {
    schema: accountSchema,
    ok: false,
    error: { code: "policy_rejected", status_code: 400, detail: "姓名不能为空" },
  };
  if (
    decodeAccountResponse(blankName, stringifyLossless(blankError), 200, "application/json", null)
      .ok !== false
  )
    fail();

  const failureRequest = testRequest("change_password", operationId, token, {
    old_password: "wrong-old-password",
    new_password: "NewStrong1Password",
  });
  const invalid = {
    schema: accountSchema,
    ok: false,
    error: { code: "credential_invalid", status_code: null, detail: null },
  };
  const failed = decodeAccountResponse(
    failureRequest,
    stringifyLossless(invalid),
    200,
    "application/json",
    null,
  );
  if (failed.ok !== false || failed.error.detail !== null) fail();
  return { ok: true, checks: 21 };
}

function isDirectExecution() {
  return (
    process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])
  );
}

if (isDirectExecution()) {
  if (process.argv[2] === "--self-check") {
    try {
      process.stdout.write(`${JSON.stringify(selfCheck())}\n`);
    } catch {
      process.stderr.write("Identity account peer self-check failed\n");
      process.exitCode = 1;
    }
  } else {
    main().catch(() => {
      process.stderr.write("Identity account peer failed\n");
      process.exitCode = 1;
    });
  }
}
