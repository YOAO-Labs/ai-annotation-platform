import { createHash } from "node:crypto";
import { isIP } from "node:net";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

import {
  parseIdentityReceipt,
  postIdentityRequest,
  uuid,
  validDateTime,
  validateInput,
} from "./identity_admission_peer.mjs";

const schema = "aap-identity-recovery/v1";
const issuer = "aap-identity/v1";
const recoveryPath = "/v1/identity/recovery/execute";
const authenticatePath = "/v1/identity/authenticate";
const operations = new Set([
  "register_open",
  "request_password_reset",
  "resend_email_verification",
  "consume_password_reset",
  "consume_email_verification",
]);
const roles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
const failures = new Map([
  ["registration_disabled", 403],
  ["email_exists", 409],
  ["captcha_required", 400],
  ["policy_rejected", 422],
  ["operation_conflict", 409],
  ["invalid_request", 400],
  ["unavailable", 503],
]);
const maxInt64 = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799n;
const maxInputBytes = 128 * 1024;
const maxRequestBytes = 64 * 1024;

function fail() {
  throw new Error("Invalid Identity recovery request or response");
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
      while (offset < source.length && !/[\t\n\r ,\]}]/.test(source[offset])) offset += 1;
      if (offset === start) fail();
    }
    spans.set(pathKey(path), { start, end: offset });
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

function strictInteger(source, spans, document, path, { max, positive = false, asBigInt = false }) {
  const value = getPath(document, path);
  if (value === null) return null;
  if (typeof value !== "number") fail();
  const raw = rawAtPath(source, spans, path);
  const pattern = positive ? /^[1-9][0-9]*$/ : /^(0|[1-9][0-9]*)$/;
  if (typeof raw !== "string" || !pattern.test(raw)) fail();
  const integer = BigInt(raw);
  if (integer > max) fail();
  if (!asBigInt && integer > BigInt(Number.MAX_SAFE_INTEGER)) fail();
  const replacement = asBigInt ? integer : Number(integer);
  setPath(document, path, replacement);
  return replacement;
}

function parseStrictJson(source, integerPaths = []) {
  if (typeof source !== "string") fail();
  const spans = scanJson(source);
  let document;
  try {
    document = JSON.parse(source);
  } catch {
    fail();
  }
  for (const spec of integerPaths) {
    if (spans.has(pathKey(spec.path))) strictInteger(source, spans, document, spec.path, spec);
  }
  return { document, spans };
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  const limits = { method: 8, path: 256, ip: 64, request_id: 36 };
  for (const [field, max] of Object.entries(limits)) {
    if (value[field] !== null && !boundedString(value[field], 0, max)) fail();
  }
  if (value.ip && isIP(value.ip) === 0) fail();
}

function isNormalizedEmail(value) {
  return boundedString(value, 3, 255) && value === value.trim().toLowerCase();
}

function validateStrongPassword(value, maxLength) {
  return (
    boundedString(value, 8, maxLength) &&
    /[A-Z]/.test(value) &&
    /[a-z]/.test(value) &&
    /\p{Decimal_Number}/u.test(value)
  );
}

export function validateRecoveryRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "audit_context", "payload"]) ||
    value.schema !== schema ||
    !operations.has(value.operation)
  ) {
    fail();
  }
  const operationId = uuid(value.operation_id);
  validateAuditContext(value.audit_context);
  const payload = value.payload;

  if (value.operation === "register_open") {
    if (
      !exact(payload, ["email", "name", "password", "captcha_verified"]) ||
      !isNormalizedEmail(payload.email) ||
      !boundedString(payload.name, 1, 100) ||
      !validateStrongPassword(payload.password, 128) ||
      typeof payload.captcha_verified !== "boolean"
    ) {
      fail();
    }
  } else if (value.operation === "request_password_reset") {
    if (
      !exact(payload, ["email", "captcha_verified"]) ||
      !isNormalizedEmail(payload.email) ||
      typeof payload.captcha_verified !== "boolean"
    ) {
      fail();
    }
  } else if (value.operation === "resend_email_verification") {
    if (!exact(payload, ["email"]) || !isNormalizedEmail(payload.email)) fail();
  } else if (value.operation === "consume_password_reset") {
    if (
      !exact(payload, ["token", "new_password"]) ||
      !boundedString(payload.token, 1, 64) ||
      !validateStrongPassword(payload.new_password, 128)
    ) {
      fail();
    }
  } else if (!exact(payload, ["token"]) || !boundedString(payload.token, 1, 64)) {
    fail();
  }

  return { request: value, operationId };
}

function decodeBase64Url(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) {
    fail();
  }
  const bytes = Buffer.from(value, "base64url");
  if (bytes.toString("base64url") !== value) fail();
  return bytes;
}

function parseJwtClaims(token) {
  if (typeof token !== "string" || token.length > 16_384) fail();
  const parts = token.split(".");
  if (parts.length !== 3 || parts[2].length !== 43 || decodeBase64Url(parts[2]).length !== 32) {
    fail();
  }
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const decodeJsonSegment = (segment, integerPaths = []) => {
    let source;
    try {
      source = decoder.decode(decodeBase64Url(segment));
    } catch {
      fail();
    }
    return parseStrictJson(source, integerPaths).document;
  };
  const header = decodeJsonSegment(parts[0]);
  if (!exact(header, ["alg", "typ"]) || header.alg !== "HS256" || header.typ !== "JWT") fail();
  const claims = decodeJsonSegment(parts[1], [
    { path: ["exp"], max: maxPythonEpochSeconds, positive: true },
    { path: ["gen"], max: maxInt64, asBigInt: true },
  ]);
  return validateClaims(claims);
}

function validateClaims(value) {
  if (!exact(value, ["sub", "role", "exp", "jti", "gen", "iss"])) fail();
  if (
    typeof value.role !== "string" ||
    !roles.has(value.role) ||
    typeof value.exp !== "number" ||
    !Number.isSafeInteger(value.exp) ||
    value.exp <= 0 ||
    typeof value.gen !== "bigint" ||
    value.gen < 0n ||
    value.gen > maxInt64 ||
    value.iss !== issuer
  ) {
    fail();
  }
  const sub = uuid(value.sub);
  const jti = uuid(value.jti);
  const expires = new Date(value.exp * 1000);
  if (!Number.isFinite(expires.valueOf()) || !validDateTime(expires.toISOString())) fail();
  return { sub, role: value.role, exp: value.exp, jti, gen: value.gen, iss: issuer };
}

function validateAccountRecord(value) {
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
    !boundedString(value.email, 3, 255) ||
    !boundedString(value.name, 1, 100) ||
    !roles.has(value.role) ||
    (value.group_name !== null && !boundedString(value.group_name, 0, 255)) ||
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

function assertNoSecretEcho(detail, request) {
  if (typeof detail !== "string") return;
  const secrets = [];
  if (request.operation === "register_open") secrets.push(request.payload.password);
  if (request.operation === "consume_password_reset") {
    secrets.push(request.payload.token, request.payload.new_password);
  }
  if (request.operation === "consume_email_verification") secrets.push(request.payload.token);
  for (const secret of secrets) if (secret.length >= 8 && detail.includes(secret)) fail();
}

function normalizedClaims(claims) {
  return {
    sub: claims.sub,
    role: claims.role,
    exp: claims.exp,
    jti: claims.jti,
    generation_decimal: claims.gen.toString(10),
    iss: claims.iss,
  };
}

function validateRegistration(request, data) {
  const { operationId } = request;
  const payload = request.request.payload;
  if (
    !data.account ||
    typeof data.email_verification_required !== "boolean" ||
    data.completed !== null
  ) {
    fail();
  }
  const account = validateAccountRecord(data.account);
  if (
    account.id !== operationId ||
    account.email !== payload.email ||
    account.name !== payload.name ||
    account.role !== "viewer" ||
    account.group_name !== null ||
    account.group_id !== null ||
    account.is_active !== true ||
    account.status !== (data.email_verification_required ? "offline" : "online") ||
    account.last_login_at !== null ||
    (data.email_verification_required && account.email_verified_at !== null) ||
    (!data.email_verification_required && account.email_verified_at === null)
  ) {
    fail();
  }
  if (data.email_verification_required) {
    if (data.claims !== null || data.access_token !== null) fail();
    return { account, claims: null, accessToken: null, verificationRequired: true };
  }
  if (data.claims === null || typeof data.access_token !== "string") fail();
  const claims = validateClaims(data.claims);
  const signedClaims = parseJwtClaims(data.access_token);
  if (
    claims.sub !== account.id ||
    claims.jti !== operationId ||
    claims.role !== account.role ||
    claims.gen !== 0n ||
    signedClaims.sub !== claims.sub ||
    signedClaims.role !== claims.role ||
    signedClaims.exp !== claims.exp ||
    signedClaims.jti !== claims.jti ||
    signedClaims.gen !== claims.gen ||
    signedClaims.iss !== claims.iss
  ) {
    fail();
  }
  return { account, claims, accessToken: data.access_token, verificationRequired: false };
}

function validateOperationData(request, source, spans, data) {
  if (
    !exact(data, ["account", "claims", "access_token", "email_verification_required", "completed"])
  ) {
    fail();
  }
  if (request.request.operation === "register_open") return validateRegistration(request, data);
  if (["request_password_reset", "resend_email_verification"].includes(request.request.operation)) {
    if (
      data.account !== null ||
      data.claims !== null ||
      data.access_token !== null ||
      data.email_verification_required !== null ||
      data.completed !== null
    ) {
      fail();
    }
    return { account: null, claims: null, accessToken: null, completed: null };
  }
  const completedPath = ["data", "completed"];
  if (
    data.account !== null ||
    data.claims !== null ||
    data.access_token !== null ||
    data.email_verification_required !== null ||
    typeof data.completed !== "boolean"
  ) {
    fail();
  }
  if (!rawAtPath(source, spans, completedPath)) fail();
  return { account: null, claims: null, accessToken: null, completed: data.completed };
}

export function decodeRecoveryResponse(request, source, status, contentType) {
  if (
    typeof source !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const { document, spans } = parseStrictJson(source, [
    { path: ["error", "status_code"], max: 599 },
    { path: ["data", "claims", "exp"], max: maxPythonEpochSeconds, positive: true },
    { path: ["data", "claims", "gen"], max: maxInt64, asBigInt: true },
  ]);
  if (!isRecord(document) || document.schema !== schema || typeof document.ok !== "boolean") fail();

  if (document.ok === false) {
    if (
      !exact(document, ["schema", "ok", "error"]) ||
      !exact(document.error, ["code", "status_code", "detail"]) ||
      typeof document.error.code !== "string" ||
      !failures.has(document.error.code) ||
      document.error.status_code !== failures.get(document.error.code) ||
      (document.error.detail !== null && !boundedString(document.error.detail, 0, 2048))
    ) {
      fail();
    }
    assertNoSecretEcho(document.error.detail, request.request);
    return {
      receipt: {
        status,
        schema,
        ok: false,
        error: {
          code: document.error.code,
          status_code: document.error.status_code,
          detail: document.error.detail,
        },
      },
      account: null,
      claims: null,
      accessToken: null,
    };
  }

  if (
    !exact(document, ["schema", "ok", "operation", "operation_id", "data"]) ||
    document.operation !== request.request.operation ||
    uuid(document.operation_id) !== request.operationId
  ) {
    fail();
  }
  const result = validateOperationData(request, source, spans, document.data);
  if (document.operation === "register_open") {
    const receipt = {
      status,
      schema,
      ok: true,
      operation: document.operation,
      operation_id: request.operationId,
      account: result.account,
      claims: result.claims ? normalizedClaims(result.claims) : null,
      access_token_sha256: result.accessToken
        ? createHash("sha256").update(result.accessToken, "utf8").digest("hex")
        : null,
      email_verification_required: result.verificationRequired,
      completed: null,
    };
    return { receipt, ...result };
  }
  const receipt = {
    status,
    schema,
    ok: true,
    operation: document.operation,
    operation_id: request.operationId,
    account: null,
    claims: null,
    access_token_sha256: null,
    email_verification_required: null,
    completed: result.completed,
  };
  return { receipt, ...result };
}

function stringifyLossless(value) {
  if (typeof value === "bigint") return value.toString(10);
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stringifyLossless).join(",")}]`;
  return `{${Object.keys(value)
    .map((key) => `${JSON.stringify(key)}:${stringifyLossless(value[key])}`)
    .join(",")}}`;
}

function makeJwt(claims) {
  const header = Buffer.from('{"alg":"HS256","typ":"JWT"}').toString("base64url");
  const payload = Buffer.from(stringifyLossless(claims), "utf8").toString("base64url");
  return `${header}.${payload}.${Buffer.alloc(32, 7).toString("base64url")}`;
}

function makeAuditContext() {
  return {
    method: "POST",
    path: "/api/v1/auth/register-open",
    ip: "127.0.0.1",
    request_id: null,
  };
}

function makeRequest(operation, operationId, payload) {
  return validateRecoveryRequest({
    schema,
    operation,
    operation_id: operationId,
    audit_context: makeAuditContext(),
    payload,
  });
}

function testAccount(actorId, email, name, overrides = {}) {
  return {
    id: actorId,
    email,
    name,
    role: "viewer",
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
    last_seen_at: null,
    password_admin_reset_at: null,
    deactivation_requested_at: null,
    deactivation_scheduled_at: null,
    preferences: {},
    avatar_ref: null,
    created_at: "2025-01-01T00:00:00Z",
    ...overrides,
  };
}

function testReply(request, data) {
  return stringifyLossless({
    schema,
    ok: true,
    operation: request.request.operation,
    operation_id: request.operationId,
    data,
  });
}

function registrationData(request, verificationRequired = false, overrides = {}) {
  const actorId = request.operationId;
  const payload = request.request.payload;
  const claims = {
    sub: actorId,
    role: "viewer",
    exp: 1893456000,
    jti: actorId,
    gen: 0n,
    iss: issuer,
  };
  return {
    account: testAccount(actorId, payload.email, payload.name, {
      email_verified_at: verificationRequired ? null : "2025-01-01T00:00:00Z",
      status: verificationRequired ? "offline" : "online",
    }),
    claims: verificationRequired ? null : claims,
    access_token: verificationRequired ? null : makeJwt(claims),
    email_verification_required: verificationRequired,
    completed: null,
    ...overrides,
  };
}

function expectFailure(callback) {
  let rejected = false;
  try {
    callback();
  } catch {
    rejected = true;
  }
  if (!rejected) fail();
}

function selfCheck() {
  let checks = 0;
  const actorId = "11111111-1111-4111-8111-111111111111";
  const register = makeRequest("register_open", actorId, {
    email: "new-user@example.test",
    name: "New User",
    password: "StrongPass123",
    captcha_verified: true,
  });
  const issued = decodeRecoveryResponse(
    register,
    testReply(register, registrationData(register)),
    200,
    "application/json; charset=utf-8",
  );
  if (
    issued.receipt.account.id !== actorId ||
    issued.receipt.account.role !== "viewer" ||
    issued.receipt.account.status !== "online" ||
    issued.receipt.account.last_login_at !== null ||
    issued.receipt.claims.generation_decimal !== "0" ||
    issued.receipt.access_token_sha256.length !== 64
  ) {
    fail();
  }
  checks += 1;

  const sharedPasswordName = "StrongPass123";
  const sharedNameRequest = makeRequest("register_open", actorId, {
    email: "same-name@example.test",
    name: sharedPasswordName,
    password: sharedPasswordName,
    captcha_verified: true,
  });
  const sharedNameResult = decodeRecoveryResponse(
    sharedNameRequest,
    testReply(sharedNameRequest, registrationData(sharedNameRequest)),
    200,
    "application/json",
  );
  if (sharedNameResult.receipt.account.name !== sharedPasswordName) fail();
  checks += 1;

  const verifiedRegister = makeRequest("register_open", actorId, {
    email: "new-user@example.test",
    name: "New User",
    password: "StrongPass123",
    captcha_verified: true,
  });
  const pending = decodeRecoveryResponse(
    verifiedRegister,
    testReply(verifiedRegister, registrationData(verifiedRegister, true)),
    200,
    "application/json",
  );
  if (
    !pending.receipt.email_verification_required ||
    pending.receipt.access_token_sha256 !== null ||
    pending.receipt.account.status !== "offline" ||
    pending.receipt.account.role !== "viewer"
  ) {
    fail();
  }
  checks += 1;

  const opaquePreferences = registrationData(register);
  opaquePreferences.account.preferences = {
    token: "opaque-preference-value",
    password: "opaque-preference-password",
    protocol: "aap-identity-recovery/v1",
  };
  const opaquePreferenceResult = decodeRecoveryResponse(
    register,
    testReply(register, opaquePreferences),
    200,
    "application/json",
  );
  if (
    opaquePreferenceResult.receipt.account.preferences.token !== "opaque-preference-value" ||
    opaquePreferenceResult.receipt.account.preferences.protocol !== "aap-identity-recovery/v1"
  ) {
    fail();
  }
  checks += 1;

  for (const operation of ["request_password_reset", "resend_email_verification"]) {
    const payload =
      operation === "request_password_reset"
        ? { email: "new-user@example.test", captcha_verified: true }
        : { email: "new-user@example.test" };
    const request = makeRequest(operation, actorId, payload);
    const response = decodeRecoveryResponse(
      request,
      testReply(request, {
        account: null,
        claims: null,
        access_token: null,
        email_verification_required: null,
        completed: null,
      }),
      200,
      "application/json",
    );
    if (response.receipt.account !== null || response.receipt.completed !== null) fail();
    checks += 1;
  }

  for (const [operation, payload, completed] of [
    [
      "consume_password_reset",
      { token: "captured-reset-token", new_password: "ResetPass123" },
      false,
    ],
    ["consume_email_verification", { token: "captured-verify-token" }, true],
  ]) {
    const request = makeRequest(operation, actorId, payload);
    const response = decodeRecoveryResponse(
      request,
      testReply(request, {
        account: null,
        claims: null,
        access_token: null,
        email_verification_required: null,
        completed,
      }),
      200,
      "application/json",
    );
    if (response.receipt.completed !== completed) fail();
    checks += 1;
  }

  const reservedLookingToken = makeRequest("consume_email_verification", actorId, {
    token: "password_reset",
  });
  const reservedLookingResult = decodeRecoveryResponse(
    reservedLookingToken,
    testReply(reservedLookingToken, {
      account: null,
      claims: null,
      access_token: null,
      email_verification_required: null,
      completed: true,
    }),
    200,
    "application/json",
  );
  if (reservedLookingResult.receipt.completed !== true) fail();
  checks += 1;

  const legacyToken = "legacy-token_" + "a".repeat(51);
  const legacyConsume = makeRequest("consume_email_verification", actorId, {
    token: legacyToken,
  });
  if (legacyConsume.request.payload.token !== legacyToken) fail();
  checks += 1;
  expectFailure(() =>
    validateRecoveryRequest({
      schema,
      operation: "consume_email_verification",
      operation_id: actorId,
      audit_context: makeAuditContext(),
      payload: { token: "x".repeat(65) },
    }),
  );
  checks += 1;

  const whitespaceName = makeRequest("register_open", actorId, {
    email: "new-user@example.test",
    name: " ",
    password: "StrongPass123",
    captcha_verified: true,
  });
  if (whitespaceName.request.payload.name !== " ") fail();
  checks += 1;

  expectFailure(() =>
    validateRecoveryRequest({
      schema: "aap-identity-recovery/v1",
      protocol: "aap-identity-recovery/v1",
      operation: "resend_email_verification",
      operation_id: actorId,
      audit_context: makeAuditContext(),
      payload: { email: "new-user@example.test" },
    }),
  );
  checks += 1;
  expectFailure(() =>
    validateRecoveryRequest({
      schema,
      operation: "resend_email_verification",
      operation_id: actorId,
      audit_context: makeAuditContext(),
      payload: { email: "New-User@example.test" },
    }),
  );
  checks += 1;
  expectFailure(() =>
    validateRecoveryRequest({
      schema,
      operation: "register_open",
      operation_id: actorId,
      audit_context: makeAuditContext(),
      payload: {
        email: "new-user@example.test",
        name: " New User ",
        password: "weak",
        captcha_verified: true,
      },
      extra: true,
    }),
  );
  checks += 1;
  expectFailure(() =>
    parseStrictJson(
      [
        '{"schema":"aap-identity-recovery/v1","operation":"register_open",',
        '"operation":"request_password_reset"}',
      ].join(""),
    ),
  );
  checks += 1;
  expectFailure(() =>
    parseStrictJson('{"data":{"claims":{"gen":1.0}}}', [
      { path: ["data", "claims", "gen"], max: maxInt64, asBigInt: true },
    ]),
  );
  checks += 1;
  const wideGeneration = parseStrictJson('{"data":{"claims":{"gen":9007199254740993}}}', [
    { path: ["data", "claims", "gen"], max: maxInt64, asBigInt: true },
  ]).document.data.claims.gen;
  if (wideGeneration !== 9007199254740993n) fail();
  checks += 1;

  const wrongJti = registrationData(register);
  wrongJti.claims.jti = "22222222-2222-4222-8222-222222222222";
  wrongJti.access_token = makeJwt(wrongJti.claims);
  expectFailure(() =>
    decodeRecoveryResponse(register, testReply(register, wrongJti), 200, "application/json"),
  );
  checks += 1;
  const wrongRole = registrationData(register);
  wrongRole.claims.role = "employee";
  wrongRole.access_token = makeJwt(wrongRole.claims);
  expectFailure(() =>
    decodeRecoveryResponse(register, testReply(register, wrongRole), 200, "application/json"),
  );
  checks += 1;
  const mismatchedJws = registrationData(register);
  mismatchedJws.access_token = makeJwt({
    ...mismatchedJws.claims,
    jti: "22222222-2222-4222-8222-222222222222",
  });
  expectFailure(() =>
    decodeRecoveryResponse(register, testReply(register, mismatchedJws), 200, "application/json"),
  );
  checks += 1;
  expectFailure(() =>
    decodeRecoveryResponse(
      register,
      stringifyLossless({
        schema,
        ok: true,
        operation: "request_password_reset",
        operation_id: register.operationId,
        data: registrationData(register),
      }),
      200,
      "application/json",
    ),
  );
  checks += 1;
  const mismatchedAccount = registrationData(register);
  mismatchedAccount.account.email = "other@example.test";
  expectFailure(() =>
    decodeRecoveryResponse(
      register,
      testReply(register, mismatchedAccount),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    decodeRecoveryResponse(
      register,
      testReply(register, { ...registrationData(register), token_ciphertext: "ciphertext" }),
      200,
      "application/json",
    ),
  );
  checks += 1;
  const accountWithHash = registrationData(register);
  accountWithHash.account.password_hash = "must-not-be-public";
  expectFailure(() =>
    decodeRecoveryResponse(register, testReply(register, accountWithHash), 200, "application/json"),
  );
  checks += 1;
  expectFailure(() =>
    decodeRecoveryResponse(
      register,
      testReply(register, { ...registrationData(register), smtp_password: "private" }),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    decodeRecoveryResponse(
      register,
      testReply(register, registrationData(register)),
      201,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    decodeRecoveryResponse(
      register,
      [
        '{"schema":"aap-identity-recovery/v1","ok":true,',
        '"operation":"register_open","operation_id":"11111111-1111-4111-8111-111111111111",',
        '"data":{},"data":{}}',
      ].join(""),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const resetToken = "captured-reset-token-1234567890";
  const reset = makeRequest("consume_password_reset", actorId, {
    token: resetToken,
    new_password: "ResetPass123",
  });
  expectFailure(() =>
    decodeRecoveryResponse(
      reset,
      JSON.stringify({
        schema,
        ok: false,
        error: { code: "invalid_request", status_code: 400, detail: resetToken },
      }),
      200,
      "application/json",
    ),
  );
  checks += 1;
  expectFailure(() =>
    decodeRecoveryResponse(
      reset,
      JSON.stringify({
        schema,
        ok: false,
        error: { code: "invalid_request", status_code: 422, detail: null },
      }),
      200,
      "application/json",
    ),
  );
  checks += 1;

  return { ok: true, checks };
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > maxInputBytes) fail();
    chunks.push(chunk);
  }
  let source;
  try {
    source = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch {
    fail();
  }
  return parseStrictJson(source).document;
}

function validatePeerInput(value) {
  if (!exact(value, ["url", "control_token", "request"])) fail();
  const request = validateRecoveryRequest(value.request);
  const encoded = Buffer.from(JSON.stringify(request.request), "utf8");
  if (encoded.length > maxRequestBytes) fail();
  const { origin, controlToken } = validateInput({
    url: value.url,
    control_token: value.control_token,
    credential: "identity-recovery-command",
  });
  return { request, origin, controlToken };
}

function validateIssuedPrincipal(source, status, contentType, account, claims) {
  parseStrictJson(source, [{ path: ["data", "generation"], max: maxInt64, asBigInt: true }]);
  const issued = parseIdentityReceipt(source, status, contentType);
  if (!issued.ok) {
    if (
      !new Set(["generation_outdated", "token_expired", "credential_revoked", "user_inactive"]).has(
        issued.error.code,
      )
    )
      fail();
    return { ok: false, error: { code: issued.error.code } };
  }
  if (
    issued.principal.actor_id !== account.id ||
    issued.principal.credential_type !== "jwt" ||
    issued.principal.credential_id !== claims.jti ||
    issued.principal.generation_decimal !== claims.gen.toString(10)
  ) {
    fail();
  }
  return {
    actor_id: issued.principal.actor_id,
    credential_type: issued.principal.credential_type,
    credential_id: issued.principal.credential_id,
    generation_decimal: issued.principal.generation_decimal,
    role: issued.principal.role,
  };
}

async function main() {
  const input = await readInput();
  const { request, origin, controlToken } = validatePeerInput(input);
  const response = await postIdentityRequest(origin, controlToken, recoveryPath, request.request);
  const result = decodeRecoveryResponse(
    request,
    response.text,
    response.status,
    response.contentType,
  );
  if (result.accessToken !== null) {
    const issuedResponse = await postIdentityRequest(origin, controlToken, authenticatePath, {
      token: result.accessToken,
    });
    result.receipt.issued = validateIssuedPrincipal(
      issuedResponse.text,
      issuedResponse.status,
      issuedResponse.contentType,
      result.account,
      result.claims,
    );
  }
  process.stdout.write(`${stringifyLossless(result.receipt)}\n`);
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
      process.stderr.write("Identity recovery peer self-check failed\n");
      process.exitCode = 1;
    }
  } else {
    main().catch(() => {
      process.stderr.write("Identity recovery peer failed\n");
      process.exitCode = 1;
    });
  }
}
