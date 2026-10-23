import { createHash, createHmac } from "node:crypto";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { isIP } from "node:net";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

import {
  parseIdentityReceipt,
  postIdentityRequest,
  validDateTime,
  validateInput,
  uuid,
} from "./identity_admission_peer.mjs";

const schema = "aap-identity-admin-passwords/v1";
const issuer = "aap-identity/v1";
const endpoint = "/v1/identity/admin-passwords/execute";
const authEndpoint = "/v1/identity/authenticate";
const operation = "admin_reset_password";
const managerRoles = new Set(["super_admin", "project_admin"]);
const authFailures = new Set([
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
]);
const businessFailures = new Set([
  "invalid_request",
  "manager_required",
  "target_not_found",
  "self_reset",
  "role_forbidden",
  "target_out_of_scope",
  "account_busy",
  "operation_conflict",
  "unavailable",
]);
const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*?";
const maxGeneration = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799n;
const masterDomain = "aap-identity-admin-passwords/v1";
const passwordDomain = Buffer.from("aap-identity-admin-passwords/password/v1\0", "ascii");
const maxInputBytes = 64 * 1024;
const maxResponseBytes = 64 * 1024;

class JsonNumber {
  constructor(raw) {
    this.raw = raw;
  }
}

function fail() {
  throw new Error("Invalid Identity administrator-password request or response");
}

function isRecord(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    !(value instanceof JsonNumber)
  );
}

function exact(value, names) {
  return (
    isRecord(value) &&
    Object.keys(value).length === names.length &&
    names.every((name) => Object.hasOwn(value, name))
  );
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

export function parseStrictJson(source) {
  if (typeof source !== "string") fail();
  scanJson(source);
  try {
    return JSON.parse(source, (key, value, context) => {
      if (typeof value !== "number") return value;
      if (typeof context?.source !== "string") fail();
      return new JsonNumber(context.source);
    });
  } catch {
    fail();
  }
}

function stringifyLossless(value) {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (value instanceof JsonNumber) return value.raw;
  if (typeof value === "bigint") return value.toString(10);
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) fail();
    return String(value);
  }
  if (Array.isArray(value)) return `[${value.map(stringifyLossless).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .map((key) => `${JSON.stringify(key)}:${stringifyLossless(value[key])}`)
      .join(",")}}`;
  }
  fail();
}

function unsignedInteger(value, max = maxGeneration) {
  if (!(value instanceof JsonNumber) || !/^(?:0|[1-9][0-9]*)$/.test(value.raw)) fail();
  const number = BigInt(value.raw);
  if (number > max) fail();
  return number;
}

function checkedString(value, max, min = 0) {
  return (
    typeof value === "string" && Array.from(value).length >= min && Array.from(value).length <= max
  );
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  if (
    !(value.method === null || checkedString(value.method, 8)) ||
    !(value.path === null || checkedString(value.path, 256)) ||
    !(value.ip === null || checkedString(value.ip, 64)) ||
    !(value.request_id === null || checkedString(value.request_id, 36)) ||
    (value.ip !== null && value.ip !== "" && isIP(value.ip) === 0)
  ) {
    fail();
  }
}

export function validateAdminPasswordRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "token", "audit_context", "payload"]) ||
    value.schema !== schema ||
    value.operation !== operation
  ) {
    fail();
  }
  const operationId = uuid(value.operation_id);
  if (!checkedString(value.token, 16_384, 1)) fail();
  validateAuditContext(value.audit_context);
  if (!exact(value.payload, ["target_user_id"])) fail();
  const targetId = uuid(value.payload.target_user_id);
  return {
    request: {
      ...value,
      operation_id: operationId,
      payload: { target_user_id: targetId },
    },
    operationId,
    targetId,
  };
}

function decodeTokenPart(part) {
  if (!/^[A-Za-z0-9_-]+$/.test(part) || part.length % 4 === 1) fail();
  const bytes = Buffer.from(part, "base64url");
  if (bytes.toString("base64url") !== part) fail();
  try {
    return parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    fail();
  }
}

function incomingJwtClaims(token) {
  if (typeof token !== "string" || token.length > 16_384) fail();
  const parts = token.split(".");
  if (parts.length !== 3) fail();
  const header = decodeTokenPart(parts[0]);
  if (
    !isRecord(header) ||
    header.alg !== "HS256" ||
    (header.typ !== undefined && header.typ !== "JWT")
  )
    fail();
  const signatureBytes = Buffer.from(parts[2], "base64url");
  if (signatureBytes.length !== 32 || signatureBytes.toString("base64url") !== parts[2]) fail();
  const claims = decodeTokenPart(parts[1]);
  if (!isRecord(claims) || claims.iss !== issuer) fail();
  const actorId = uuid(claims.sub);
  const credentialId = uuid(claims.jti);
  const expiry = unsignedInteger(claims.exp, maxPythonEpochSeconds);
  const generation = claims.gen === undefined ? 0n : unsignedInteger(claims.gen);
  return { actorId, credentialId, expiry, generation };
}

function validatePrincipal(value) {
  if (
    !exact(value, [
      "actor_id",
      "name",
      "email",
      "role",
      "is_active",
      "credential_type",
      "credential_id",
      "generation",
      "scopes",
      "expires_at",
    ]) ||
    typeof value.name !== "string" ||
    typeof value.email !== "string" ||
    !managerRoles.has(value.role) ||
    value.is_active !== true ||
    !["jwt", "api_key"].includes(value.credential_type)
  ) {
    fail();
  }
  value.actor_id = uuid(value.actor_id);
  value.credential_id = uuid(value.credential_id);
  if (value.credential_type === "jwt") {
    value.generation = unsignedInteger(value.generation);
    if (value.scopes !== null || !validDateTime(value.expires_at)) fail();
  } else if (
    value.generation !== null ||
    !Array.isArray(value.scopes) ||
    value.scopes.some((scope) => typeof scope !== "string") ||
    (value.expires_at !== null && !validDateTime(value.expires_at))
  ) {
    fail();
  }
  return value;
}

function bindPrincipal(rawPrincipal, principal, request, expectedApiPrincipal) {
  if (!managerRoles.has(principal.role)) fail();
  if (request.token.startsWith("ak_")) {
    if (
      !expectedApiPrincipal ||
      principal.actor_id !== expectedApiPrincipal.actor_id ||
      principal.credential_type !== "api_key" ||
      principal.credential_id !== expectedApiPrincipal.credential_id ||
      principal.generation !== null
    ) {
      fail();
    }
    return;
  }
  const claims = incomingJwtClaims(request.token);
  if (
    principal.actor_id !== claims.actorId ||
    principal.credential_type !== "jwt" ||
    principal.credential_id !== claims.credentialId ||
    principal.generation !== claims.generation ||
    BigInt(Math.floor(Date.parse(rawPrincipal.expires_at) / 1000)) !== claims.expiry
  ) {
    fail();
  }
}

function uuidBytes(value) {
  return Buffer.from(uuid(value).replaceAll("-", ""), "hex");
}

function u32be(value) {
  if (!Number.isInteger(value) || value < 0 || value > 0xffff_ffff) fail();
  const result = Buffer.alloc(4);
  result.writeUInt32BE(value);
  return result;
}

function hasAllPasswordClasses(password) {
  return (
    /[A-Z]/.test(password) &&
    /[a-z]/.test(password) &&
    /[0-9]/.test(password) &&
    /[!@#$%^&*?]/.test(password)
  );
}

export function deriveAdminPassword(signingSecret, actorId, operationId, fingerprint) {
  if (
    typeof signingSecret !== "string" ||
    signingSecret.length === 0 ||
    Buffer.byteLength(signingSecret, "utf8") > 4096
  )
    fail();
  if (!Buffer.isBuffer(fingerprint) || fingerprint.length !== 32) fail();
  const master = createHmac("sha256", Buffer.from(signingSecret, "utf8"))
    .update(masterDomain, "ascii")
    .digest();
  for (let candidate = 0; candidate <= 0xffff_ffff; candidate += 1) {
    let password = "";
    let blockCounter = 0;
    while (password.length < 16) {
      const block = createHmac("sha256", master)
        .update(
          Buffer.concat([
            passwordDomain,
            uuidBytes(actorId),
            uuidBytes(operationId),
            fingerprint,
            u32be(candidate),
            u32be(blockCounter),
          ]),
        )
        .digest();
      for (const byte of block) {
        if (byte < 213) password += alphabet[byte % alphabet.length];
        if (password.length === 16) break;
      }
      if (password.length < 16) {
        if (blockCounter === 0xffff_ffff) fail();
        blockCounter += 1;
      }
    }
    if (hasAllPasswordClasses(password)) return password;
  }
  fail();
}

function requestFingerprint(signingSecret, request) {
  const master = createHmac("sha256", Buffer.from(signingSecret, "utf8"))
    .update(masterDomain, "ascii")
    .digest();
  const canonical = JSON.stringify({
    operation: operation,
    payload: { target_user_id: request.payload.target_user_id },
  });
  const fingerprint = createHmac("sha256", master).update(canonical, "ascii").digest();
  return { master, fingerprint };
}

function expectedFailure(request, error, expectedActorId = null) {
  if (
    !exact(error, ["code", "status_code", "detail"]) ||
    typeof error.code !== "string" ||
    (!authFailures.has(error.code) && !businessFailures.has(error.code)) ||
    (error.detail !== null && typeof error.detail !== "string")
  ) {
    fail();
  }
  const status = error.status_code === null ? null : unsignedInteger(error.status_code, 599n);
  if (status !== null && status < 100n) fail();
  if (authFailures.has(error.code)) {
    if (status !== null || error.detail !== null) fail();
  } else if (error.code === "invalid_request") {
    if (status !== 400n || error.detail !== null) fail();
  } else if (error.code === "operation_conflict") {
    if (status !== 409n || error.detail !== null) fail();
  } else if (error.code === "manager_required") {
    if (status !== 403n || error.detail !== "当前账号已无管理权限") fail();
  } else if (error.code === "target_not_found") {
    if (status !== 404n || error.detail !== "用户不存在") fail();
  } else if (error.code === "self_reset") {
    if (status !== 400n || error.detail !== "不能给自己重置密码（请用「修改密码」）") fail();
  } else if (error.code === "role_forbidden") {
    if (status !== 403n || error.detail !== "只能重置等级低于你的用户的密码") fail();
  } else if (error.code === "target_out_of_scope") {
    if (status !== 403n || error.detail !== "该用户不在你管理的项目内") fail();
  } else if (error.code === "account_busy") {
    if (status !== 409n || error.detail !== "账号正在更新，请刷新后重试") fail();
  } else if (error.code === "unavailable") {
    if (status !== 503n || error.detail !== null) fail();
  }
  if (
    error.code === "self_reset" &&
    (expectedActorId === null || request.payload.target_user_id !== expectedActorId)
  ) {
    fail();
  }
  return error;
}

function checkedRequestActor(request) {
  if (request.token.startsWith("ak_")) return null;
  return incomingJwtClaims(request.token).actorId;
}

export function decodeAdminPasswordResponse(
  checkedRequest,
  source,
  status,
  contentType,
  signingSecret,
  expectedApiPrincipal = null,
) {
  if (
    typeof source !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const document = parseStrictJson(source);
  if (!isRecord(document) || document.schema !== schema || typeof document.ok !== "boolean") fail();
  const request = checkedRequest.request;
  if (document.ok === false) {
    if (!exact(document, ["schema", "ok", "error"])) fail();
    const expectedActorId =
      document.error?.code === "self_reset"
        ? request.token.startsWith("ak_")
          ? (expectedApiPrincipal?.actor_id ?? null)
          : checkedRequestActor(request)
        : null;
    return { ok: false, error: expectedFailure(request, document.error, expectedActorId) };
  }
  if (
    !exact(document, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    document.operation !== operation ||
    uuid(document.operation_id) !== checkedRequest.operationId ||
    !exact(document.data, [
      "target_user_id",
      "target_email",
      "temp_password",
      "password_admin_reset_at",
    ])
  ) {
    fail();
  }
  document.operation_id = uuid(document.operation_id);
  const principal = validatePrincipal(document.principal);
  bindPrincipal(document.principal, principal, request, expectedApiPrincipal);
  const targetId = uuid(document.data.target_user_id);
  if (
    targetId !== checkedRequest.targetId ||
    targetId === principal.actor_id ||
    typeof document.data.target_email !== "string" ||
    document.data.target_email.length < 1 ||
    document.data.target_email.length > 320 ||
    typeof document.data.temp_password !== "string" ||
    !/^[A-Za-z0-9!@#$%^&*?]{16}$/.test(document.data.temp_password) ||
    !hasAllPasswordClasses(document.data.temp_password) ||
    !validDateTime(document.data.password_admin_reset_at)
  ) {
    fail();
  }
  const { fingerprint } = requestFingerprint(signingSecret, request);
  const expectedPassword = deriveAdminPassword(
    signingSecret,
    principal.actor_id,
    checkedRequest.operationId,
    fingerprint,
  );
  if (document.data.temp_password !== expectedPassword) fail();
  return document;
}

export function validatePublicResetResponse(body, status, expectedTargetId, expectedTargetEmail) {
  if (typeof body !== "string" || status !== 200) fail();
  const value = parseStrictJson(body);
  if (!exact(value, ["temp_password", "message", "target_email"])) fail();
  if (
    value.target_email !== expectedTargetEmail ||
    typeof value.temp_password !== "string" ||
    !/^[A-Za-z0-9!@#$%^&*?]{16}$/.test(value.temp_password) ||
    !hasAllPasswordClasses(value.temp_password) ||
    value.message !== "请通过安全渠道告知用户首次登录后立即修改密码"
  ) {
    fail();
  }
  return {
    target_user_id: uuid(expectedTargetId),
    target_email: value.target_email,
    password_length: value.temp_password.length,
    has_required_classes: true,
  };
}

function validatePeerInput(value) {
  if (
    !exact(value, ["mode", "url", "control_token", "signing_secret", "request"]) ||
    !["call", "discard"].includes(value.mode)
  )
    fail();
  const checkedRequest = validateAdminPasswordRequest(value.request);
  const checked = validateInput({
    url: value.url,
    control_token: value.control_token,
    credential: checkedRequest.request.token,
  });
  if (
    typeof value.signing_secret !== "string" ||
    value.signing_secret.length < 1 ||
    value.signing_secret.length > 4096
  )
    fail();
  return {
    checkedRequest,
    origin: checked.origin,
    controlToken: checked.controlToken,
    signingSecret: value.signing_secret,
  };
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > maxInputBytes) fail();
    chunks.push(chunk);
  }
  try {
    return parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
  } catch {
    fail();
  }
}

function postAdminPasswordRequest(origin, controlToken, request) {
  const target = new URL(endpoint, origin);
  const transport = target.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(JSON.stringify(request), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const outgoing = transport(
      target,
      {
        method: "POST",
        agent: false,
        signal: AbortSignal.timeout(90_000),
        headers: {
          authorization: `Bearer ${controlToken}`,
          "content-type": "application/json",
          accept: "application/json",
          "content-length": body.length,
        },
      },
      (response) => {
        const chunks = [];
        let size = 0;
        response.on("data", (chunk) => {
          size += chunk.length;
          if (size > maxResponseBytes) {
            response.destroy(new Error("response too large"));
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          try {
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              text: new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
            });
          } catch {
            rejectResponse(new Error("Invalid Identity administrator-password response"));
          }
        });
        response.on("error", () => rejectResponse(new Error("Identity request failed")));
      },
    );
    outgoing.on("error", () => rejectResponse(new Error("Identity request failed")));
    outgoing.end(body);
  });
}

async function resolveApiKeyPrincipal(origin, controlToken, token) {
  const response = await postIdentityRequest(origin, controlToken, authEndpoint, { token });
  const receipt = parseIdentityReceipt(response.text, response.status, response.contentType);
  if (!receipt.ok) return null;
  if (receipt.principal.credential_type !== "api_key") fail();
  return receipt.principal;
}

function normalizedApiPrincipal(value) {
  return {
    actor_id: value.actor_id,
    credential_id: value.credential_id,
  };
}

function safeReport(document, request) {
  if (!document.ok) {
    return {
      schema,
      ok: false,
      operation,
      operation_id: request.operationId,
      error: document.error,
    };
  }
  return {
    schema,
    ok: true,
    operation,
    operation_id: request.operationId,
    principal: {
      actor_id: document.principal.actor_id,
      role: document.principal.role,
      credential_type: document.principal.credential_type,
      credential_id: document.principal.credential_id,
      generation:
        document.principal.credential_type === "jwt"
          ? document.principal.generation.toString(10)
          : null,
    },
    data: {
      target_user_id: document.data.target_user_id,
      target_email: document.data.target_email,
      password_admin_reset_at: document.data.password_admin_reset_at,
      temp_password_verified: true,
    },
  };
}

function testAuditContext() {
  return {
    method: "POST",
    path: "/api/v1/users/33333333-3333-4333-8333-333333333333/admin-reset-password",
    ip: "127.0.0.1",
    request_id: "44444444-4444-4444-8444-444444444444",
  };
}

function makeToken(actorId, credentialId, generation = 7n) {
  const header = Buffer.from('{"alg":"HS256","typ":"JWT"}').toString("base64url");
  const payload = Buffer.from(
    stringifyLossless({
      sub: actorId,
      role: "super_admin",
      exp: 1893456000n,
      jti: credentialId,
      gen: generation,
      iss: issuer,
    }),
  ).toString("base64url");
  return `${header}.${payload}.${Buffer.alloc(32, 9).toString("base64url")}`;
}

function testPrincipal(actorId, credentialId, generation = 7n) {
  return {
    actor_id: actorId,
    name: "Administrator",
    email: "admin@example.test",
    role: "super_admin",
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation,
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
  };
}

function testRequest(operationId, actorId, credentialId, targetId, generation = 7n) {
  return validateAdminPasswordRequest({
    schema,
    operation,
    operation_id: operationId,
    token: makeToken(actorId, credentialId, generation),
    audit_context: testAuditContext(),
    payload: { target_user_id: targetId },
  });
}

function testReply(request, data, principal) {
  return stringifyLossless({
    schema,
    ok: true,
    operation,
    operation_id: request.operationId,
    principal,
    data,
  });
}

function expectFailure(callback) {
  let failed = false;
  try {
    callback();
  } catch {
    failed = true;
  }
  if (!failed) fail();
}

function selfCheck() {
  let checks = 0;
  const actorId = "01234567-89ab-4cde-8123-456789abcdef";
  const operationId = "fedcba98-7654-4321-8fed-cba987654321";
  const credentialId = "22222222-2222-4222-8222-222222222222";
  const targetId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const secret = "vector-only-admin-password-secret";
  const request = testRequest(operationId, actorId, credentialId, targetId);
  const { master, fingerprint } = requestFingerprint(secret, request.request);
  const derivationId = createHash("sha256").update(master).digest("hex");
  if (
    fingerprint.toString("hex") !==
      "2191983825cb1874e18c884b5a2ea064e35fcc5eb256c2f948ed275bb1c1cf67" ||
    derivationId !== "c04ac69f06e9ef556e7fc70c72da2949bb9fd88f8764dda1029920a2abd9e039" ||
    deriveAdminPassword(secret, actorId, operationId, fingerprint) !== "GLwLvE4ri9v0V!#y"
  ) {
    fail();
  }
  checks += 1;

  const password = deriveAdminPassword(secret, actorId, operationId, fingerprint);
  const validReply = testReply(
    request,
    {
      target_user_id: targetId,
      target_email: "employee@example.test",
      temp_password: password,
      password_admin_reset_at: "2030-01-01T00:00:00Z",
    },
    testPrincipal(actorId, credentialId),
  );
  const decoded = decodeAdminPasswordResponse(request, validReply, 200, "application/json", secret);
  if (decoded.data.temp_password !== password) fail();
  checks += 1;

  const wideGeneration = 9_007_199_254_740_993n;
  const wideRequest = testRequest(
    "11111111-1111-4111-8111-111111111111",
    actorId,
    credentialId,
    targetId,
    wideGeneration,
  );
  const wideFingerprint = requestFingerprint(secret, wideRequest.request).fingerprint;
  const widePassword = deriveAdminPassword(
    secret,
    actorId,
    wideRequest.operationId,
    wideFingerprint,
  );
  const wideReply = testReply(
    wideRequest,
    {
      target_user_id: targetId,
      target_email: "employee@example.test",
      temp_password: widePassword,
      password_admin_reset_at: "2030-01-01T00:00:00Z",
    },
    testPrincipal(actorId, credentialId, wideGeneration),
  );
  if (
    decodeAdminPasswordResponse(wideRequest, wideReply, 200, "application/json", secret).principal
      .generation !== wideGeneration
  ) {
    fail();
  }
  checks += 1;

  const publicBody = JSON.stringify({
    temp_password: password,
    message: "请通过安全渠道告知用户首次登录后立即修改密码",
    target_email: "employee@example.test",
  });
  if (
    !validatePublicResetResponse(publicBody, 200, targetId, "employee@example.test")
      .has_required_classes
  )
    fail();
  checks += 1;

  expectFailure(() => parseStrictJson('{"schema":"a","schema":"b"}'));
  expectFailure(() =>
    validateAdminPasswordRequest(
      parseStrictJson(
        `{"schema":"${schema}","operation":"${operation}","operation_id":"${operationId}","token":"x","audit_context":{"method":"POST","path":"/","ip":"127.0.0.1","request_id":"44444444-4444-4444-8444-444444444444"},"payload":{"target_user_id":"${targetId}","user_id":"${targetId}"}}`,
      ),
    ),
  );
  expectFailure(() => parseStrictJson('{"schema":'));
  expectFailure(() =>
    validateAdminPasswordRequest({
      ...request.request,
      schema: undefined,
      protocol: schema,
    }),
  );
  checks += 4;

  expectFailure(() =>
    decodeAdminPasswordResponse(
      request,
      validReply.replace(operationId, credentialId),
      200,
      "application/json",
      secret,
    ),
  );
  expectFailure(() =>
    decodeAdminPasswordResponse(
      request,
      validReply.replace(actorId, targetId),
      200,
      "application/json",
      secret,
    ),
  );
  expectFailure(() =>
    decodeAdminPasswordResponse(
      request,
      validReply.replace(password, "X".repeat(16)),
      200,
      "application/json",
      secret,
    ),
  );
  expectFailure(() =>
    decodeAdminPasswordResponse(
      request,
      validReply.replace(
        '"target_email":"employee@example.test"',
        '"target_email":"employee@example.test","plaintext":"leak"',
      ),
      200,
      "application/json",
      secret,
    ),
  );
  expectFailure(() =>
    decodeAdminPasswordResponse(
      request,
      '{"schema":"aap-identity-admin-passwords/v1","schema":"aap-identity-admin-passwords/v1"}',
      200,
      "application/json",
      secret,
    ),
  );
  checks += 5;

  const unavailable = stringifyLossless({
    schema,
    ok: false,
    error: { code: "unavailable", status_code: 503, detail: null },
  });
  if (
    decodeAdminPasswordResponse(request, unavailable, 200, "application/json", secret).error
      .code !== "unavailable"
  )
    fail();
  expectFailure(() =>
    decodeAdminPasswordResponse(
      request,
      unavailable.replace('"detail":null', '"detail":"' + secret + '"'),
      200,
      "application/json",
      secret,
    ),
  );
  checks += 2;

  const shortTokenRequest = validateAdminPasswordRequest({
    ...request.request,
    token: "x",
  });
  const credentialFailure = stringifyLossless({
    schema,
    ok: false,
    error: { code: "credential_invalid", status_code: null, detail: null },
  });
  if (
    decodeAdminPasswordResponse(
      shortTokenRequest,
      credentialFailure,
      200,
      "application/json",
      secret,
    ).error.code !== "credential_invalid"
  )
    fail();
  checks += 1;

  const invalidRequestFailure = stringifyLossless({
    schema,
    ok: false,
    error: { code: "invalid_request", status_code: 400, detail: null },
  });
  const conflictFailure = stringifyLossless({
    schema,
    ok: false,
    error: { code: "operation_conflict", status_code: 409, detail: null },
  });
  if (
    decodeAdminPasswordResponse(request, invalidRequestFailure, 200, "application/json", secret)
      .error.code !== "invalid_request" ||
    decodeAdminPasswordResponse(request, conflictFailure, 200, "application/json", secret).error
      .code !== "operation_conflict"
  )
    fail();
  expectFailure(() =>
    decodeAdminPasswordResponse(
      request,
      invalidRequestFailure.replace('"status_code":400', '"status_code":400.0'),
      200,
      "application/json",
      secret,
    ),
  );
  const selfRequest = testRequest(operationId, actorId, credentialId, actorId);
  const selfFailure = stringifyLossless({
    schema,
    ok: false,
    error: {
      code: "self_reset",
      status_code: 400,
      detail: "不能给自己重置密码（请用「修改密码」）",
    },
  });
  if (
    decodeAdminPasswordResponse(selfRequest, selfFailure, 200, "application/json", secret).error
      .code !== "self_reset"
  )
    fail();
  expectFailure(() =>
    decodeAdminPasswordResponse(request, selfFailure, 200, "application/json", secret),
  );
  checks += 4;

  expectFailure(() =>
    validatePublicResetResponse(
      publicBody.replace(
        '"target_email":"employee@example.test"',
        '"target_email":"employee@example.test","extra":true',
      ),
      200,
      targetId,
      "employee@example.test",
    ),
  );
  expectFailure(() =>
    validatePublicResetResponse(publicBody, 201, targetId, "employee@example.test"),
  );
  checks += 2;
  return { ok: true, checks };
}

function isDirectExecution() {
  return (
    process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])
  );
}

async function main() {
  const input = await readInput();
  if (
    exact(input, ["mode", "response", "expected_target_user_id", "expected_target_email"]) &&
    input.mode === "public"
  ) {
    const response = input.response;
    if (!exact(response, ["status", "content_type", "body"])) fail();
    const report = validatePublicResetResponse(
      response.body,
      Number(unsignedInteger(response.status, 599n)),
      input.expected_target_user_id,
      input.expected_target_email,
    );
    process.stdout.write(`${JSON.stringify({ ok: true, ...report })}\n`);
    return;
  }
  const { checkedRequest, origin, controlToken, signingSecret } = validatePeerInput(input);
  let expectedApiPrincipal = null;
  if (checkedRequest.request.token.startsWith("ak_")) {
    expectedApiPrincipal = normalizedApiPrincipal(
      await resolveApiKeyPrincipal(origin, controlToken, checkedRequest.request.token),
    );
  }
  const response = await postAdminPasswordRequest(origin, controlToken, checkedRequest.request);
  const decoded = decodeAdminPasswordResponse(
    checkedRequest,
    response.text,
    response.status,
    response.contentType,
    signingSecret,
    expectedApiPrincipal,
  );
  if (input.mode === "discard") {
    process.stdout.write(`${JSON.stringify({ ok: true, response_discarded: true })}\n`);
    return;
  }
  process.stdout.write(`${stringifyLossless(safeReport(decoded, checkedRequest))}\n`);
}

if (isDirectExecution()) {
  if (process.argv[2] === "--self-check") {
    try {
      process.stdout.write(`${JSON.stringify(selfCheck())}\n`);
    } catch {
      process.stderr.write("Identity administrator-password peer self-check failed\n");
      process.exitCode = 1;
    }
  } else {
    main().catch(() => {
      process.stderr.write("Identity administrator-password peer failed\n");
      process.exitCode = 1;
    });
  }
}
