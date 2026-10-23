import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
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

const schema = "aap-identity-platform-roles/v1";
const issuer = "aap-identity/v1";
const endpoint = "/v1/identity/platform-roles/execute";
const authEndpoint = "/v1/identity/authenticate";
const operations = new Set(["preview_role_change", "change_platform_role"]);
const roles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
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
  "super_admin_required",
  "target_not_found",
  "invalid_platform_role",
  "self_change",
  "last_super_admin",
  "account_busy",
  "project_busy",
  "platform_role_change_blocked",
  "operation_conflict",
  "unavailable",
]);
const maxInt64 = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799n;
const roleBlockers = [
  "账号仍持有标注/质检项目职责，请先在项目中变更职责",
  "账号仍是项目负责人，请先转移项目后变更平台角色",
  "账号仍有未完成的标注/质检工作，请先交接",
];
const previewWarnings = [
  "平台角色对所有项目生效；已有项目成员身份和批次负责人不会自动改变。",
  "若新平台角色与现有项目身份不匹配，成员可能无法继续标注或审核；请检查并重新分派。",
];
const userFields = [
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
const previewFields = [
  "user_id",
  "email",
  "current_role",
  "requested_role",
  "can_change",
  "blockers",
  "projects",
  "assigned_batch_count",
  "assigned_task_count",
  "review_task_count",
  "other_project_count",
  "warnings",
];
const projectFields = [
  "project_id",
  "project_name",
  "membership_role",
  "annotator_batch_count",
  "reviewer_batch_count",
  "assigned_task_count",
  "review_task_count",
];
const nullableDateFields = [
  "disabled_at",
  "email_verified_at",
  "last_login_at",
  "last_seen_at",
  "password_admin_reset_at",
  "deactivation_requested_at",
  "deactivation_scheduled_at",
];
const jwtHeader = { alg: "HS256", typ: "JWT" };

class JsonNumber {
  constructor(raw) {
    this.raw = raw;
  }
}

function fail() {
  throw new Error("Invalid Identity platform-role request or response");
}

function isRecord(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    !(value instanceof JsonNumber)
  );
}

function exact(value, fields) {
  return (
    isRecord(value) &&
    Object.keys(value).length === fields.length &&
    fields.every((field) => Object.hasOwn(value, field))
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

function parseStrictJson(source) {
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

function int64(value) {
  if (!(value instanceof JsonNumber) || !/^(?:0|[1-9][0-9]*)$/.test(value.raw)) fail();
  const number = BigInt(value.raw);
  if (number > maxInt64) fail();
  return number;
}

function nullableString(value, max = Number.MAX_SAFE_INTEGER) {
  return value === null || (typeof value === "string" && Array.from(value).length <= max);
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  if (
    !nullableString(value.method, 8) ||
    !nullableString(value.path, 256) ||
    !nullableString(value.ip, 64) ||
    !nullableString(value.request_id, 36) ||
    (value.ip !== null && value.ip !== "" && isIP(value.ip) === 0)
  ) {
    fail();
  }
}

function validatePlatformRoleRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "token", "audit_context", "payload"]) ||
    value.schema !== schema ||
    !operations.has(value.operation)
  ) {
    fail();
  }
  const operationId = uuid(value.operation_id);
  if (typeof value.token !== "string" || value.token.length < 1 || value.token.length > 16_384)
    fail();
  validateAuditContext(value.audit_context);
  if (!exact(value.payload, ["target_user_id", "role"]) || typeof value.payload.role !== "string")
    fail();
  const targetId = uuid(value.payload.target_user_id);
  return {
    request: {
      ...value,
      operation_id: operationId,
      payload: { target_user_id: targetId, role: value.payload.role },
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
  const signature = Buffer.from(parts[2], "base64url");
  if (signature.length !== 32 || signature.toString("base64url") !== parts[2]) fail();
  const claims = decodeTokenPart(parts[1]);
  if (!isRecord(claims) || claims.iss !== issuer) fail();
  const actorId = uuid(claims.sub);
  const credentialId = uuid(claims.jti);
  const expiry = int64(claims.exp);
  if (expiry < 1n || expiry > 253402300799n) fail();
  const generation = claims.gen === undefined ? 0n : int64(claims.gen);
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
    !roles.has(value.role) ||
    value.is_active !== true ||
    !["jwt", "api_key"].includes(value.credential_type)
  ) {
    fail();
  }
  value.actor_id = uuid(value.actor_id);
  value.credential_id = uuid(value.credential_id);
  if (value.credential_type === "jwt") {
    value.generation = int64(value.generation);
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

function bindPrincipal(principal, request, expectedApiPrincipal) {
  if (principal.role !== "super_admin") fail();
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
    BigInt(Math.floor(Date.parse(principal.expires_at) / 1000)) !== claims.expiry
  ) {
    fail();
  }
}

function validateUserRecord(value, fields = userFields) {
  if (!exact(value, fields)) fail();
  value.id = uuid(value.id);
  value.group_id = value.group_id === null ? null : uuid(value.group_id);
  if (
    typeof value.email !== "string" ||
    typeof value.name !== "string" ||
    typeof value.role !== "string" ||
    !nullableString(value.group_name) ||
    typeof value.status !== "string" ||
    typeof value.is_active !== "boolean" ||
    !nullableString(value.disabled_kind) ||
    !nullableString(value.disabled_reason) ||
    !isRecord(value.preferences) ||
    !nullableString(value.avatar_ref) ||
    !validDateTime(value.created_at)
  ) {
    fail();
  }
  if (value.disabled_by !== null) value.disabled_by = uuid(value.disabled_by);
  for (const field of nullableDateFields) {
    if (value[field] !== null && !validDateTime(value[field])) fail();
  }
  return value;
}

function roleWorkBlockersValid(role, blockers) {
  const allowed = [];
  if (["super_admin", "project_admin", "viewer"].includes(role)) allowed.push(roleBlockers[0]);
  if (["employee", "viewer"].includes(role)) allowed.push(roleBlockers[1]);
  if (role === "viewer") allowed.push(roleBlockers[2]);
  return (
    blockers.length > 0 &&
    blockers.every((blocker) => allowed.includes(blocker)) &&
    new Set(blockers).size === blockers.length &&
    blockers
      .slice()
      .sort((a, b) => allowed.indexOf(a) - allowed.indexOf(b))
      .join("\0") === blockers.join("\0")
  );
}

function validatePreviewData(data, request, actorId) {
  if (
    !exact(data, previewFields) ||
    !Array.isArray(data.projects) ||
    !Array.isArray(data.blockers) ||
    !Array.isArray(data.warnings)
  )
    fail();
  data.user_id = uuid(data.user_id);
  if (
    data.user_id !== request.payload.target_user_id ||
    typeof data.email !== "string" ||
    typeof data.current_role !== "string" ||
    data.requested_role !== request.payload.role ||
    typeof data.can_change !== "boolean" ||
    data.blockers.some((blocker) => typeof blocker !== "string") ||
    data.warnings.length !== previewWarnings.length ||
    data.warnings.some((warning, index) => warning !== previewWarnings[index]) ||
    data.can_change !== (data.blockers.length === 0)
  ) {
    fail();
  }
  const ids = new Set();
  let assignedBatches = 0n;
  let assignedTasks = 0n;
  let reviewTasks = 0n;
  for (const item of data.projects) {
    if (!exact(item, projectFields)) fail();
    item.project_id = uuid(item.project_id);
    if (ids.has(item.project_id)) fail();
    ids.add(item.project_id);
    if (typeof item.project_name !== "string" || !nullableString(item.membership_role)) {
      fail();
    }
    item.annotator_batch_count = int64(item.annotator_batch_count);
    item.reviewer_batch_count = int64(item.reviewer_batch_count);
    item.assigned_task_count = int64(item.assigned_task_count);
    item.review_task_count = int64(item.review_task_count);
    assignedBatches += item.annotator_batch_count + item.reviewer_batch_count;
    assignedTasks += item.assigned_task_count;
    reviewTasks += item.review_task_count;
  }
  data.assigned_batch_count = int64(data.assigned_batch_count);
  data.assigned_task_count = int64(data.assigned_task_count);
  data.review_task_count = int64(data.review_task_count);
  data.other_project_count = int64(data.other_project_count);
  if (
    data.other_project_count !== 0n ||
    assignedBatches !== data.assigned_batch_count ||
    assignedTasks !== data.assigned_task_count ||
    reviewTasks !== data.review_task_count
  ) {
    fail();
  }
  const expectedPrefix = [];
  if (data.user_id === actorId) expectedPrefix.push("不能修改自己的角色");
  if (data.current_role === request.payload.role) expectedPrefix.push("目标角色与当前角色相同");
  if (data.blockers.slice(0, expectedPrefix.length).join("\0") !== expectedPrefix.join("\0"))
    fail();
  const remaining = data.blockers.slice(expectedPrefix.length);
  const lastIndex = remaining.indexOf("不能降级最后一名超级管理员");
  if (lastIndex >= 0) {
    if (
      data.current_role !== "super_admin" ||
      request.payload.role === "super_admin" ||
      lastIndex !== 0
    ) {
      fail();
    }
    remaining.shift();
  }
  if (
    (request.payload.role === data.current_role && remaining.length !== 0) ||
    (remaining.length > 0 && !roleWorkBlockersValid(request.payload.role, remaining))
  ) {
    fail();
  }
  return data;
}

function validateFailure(request, error, actorId) {
  if (
    !exact(error, ["code", "status_code", "detail"]) ||
    typeof error.code !== "string" ||
    (!authFailures.has(error.code) && !businessFailures.has(error.code))
  ) {
    fail();
  }
  const status = error.status_code === null ? null : int64(error.status_code);
  const detail = error.detail;
  if (authFailures.has(error.code)) {
    if (status !== null || detail !== null) fail();
  } else if (error.code === "invalid_request") {
    if (status !== 400n || detail !== null) fail();
  } else if (error.code === "super_admin_required") {
    if (status !== 403n || detail !== "当前账号已无管理权限") fail();
  } else if (error.code === "target_not_found") {
    if (status !== 404n || detail !== "用户不存在") fail();
  } else if (error.code === "invalid_platform_role") {
    if (
      roles.has(request.payload.role) ||
      status !== 400n ||
      detail !== `非法平台角色: ${request.payload.role}`
    )
      fail();
  } else if (error.code === "self_change") {
    if (
      !roles.has(request.payload.role) ||
      actorId === null ||
      request.payload.target_user_id !== actorId ||
      status !== 400n ||
      detail !== "不能修改自己的角色"
    )
      fail();
  } else if (error.code === "last_super_admin") {
    if (
      request.operation !== "change_platform_role" ||
      !roles.has(request.payload.role) ||
      request.payload.role === "super_admin" ||
      actorId === null ||
      request.payload.target_user_id === actorId ||
      status !== 400n ||
      detail !== "不能降级最后一名超级管理员"
    )
      fail();
  } else if (error.code === "account_busy") {
    if (status !== 409n || detail !== "账号正在更新，请刷新后重试") fail();
  } else if (error.code === "project_busy") {
    if (
      request.operation !== "change_platform_role" ||
      !roles.has(request.payload.role) ||
      actorId === null ||
      request.payload.target_user_id === actorId ||
      status !== 409n ||
      !exact(detail, ["reason"]) ||
      detail.reason !== "project_busy"
    )
      fail();
  } else if (error.code === "platform_role_change_blocked") {
    if (
      request.operation !== "change_platform_role" ||
      !roles.has(request.payload.role) ||
      actorId === null ||
      request.payload.target_user_id === actorId ||
      status !== 409n ||
      !exact(detail, ["reason", "blockers"]) ||
      detail.reason !== "platform_role_change_blocked" ||
      !Array.isArray(detail.blockers) ||
      detail.blockers.some((blocker) => typeof blocker !== "string") ||
      !roleWorkBlockersValid(request.payload.role, detail.blockers)
    )
      fail();
  } else if (error.code === "operation_conflict") {
    if (request.operation !== "change_platform_role" || status !== 409n || detail !== null) fail();
  } else if (error.code === "unavailable") {
    if (status !== 503n || detail !== null) fail();
  }
  if (detail !== null && stringifyLossless(detail).includes(request.token)) fail();
  return error;
}

function decodePlatformRoleResponse(
  checked,
  source,
  status,
  contentType,
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
  const request = checked.request;
  if (document.ok === false) {
    if (!exact(document, ["schema", "ok", "error"])) fail();
    let actorId = null;
    if (
      document.error?.code === "self_change" ||
      document.error?.code === "last_super_admin" ||
      document.error?.code === "project_busy" ||
      document.error?.code === "platform_role_change_blocked"
    ) {
      actorId = request.token.startsWith("ak_")
        ? (expectedApiPrincipal?.actor_id ?? null)
        : incomingJwtClaims(request.token).actorId;
    }
    return {
      schema,
      ok: false,
      operation: request.operation,
      operation_id: checked.operationId,
      error: validateFailure(request, document.error, actorId),
    };
  }
  if (
    !exact(document, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    document.operation !== request.operation ||
    uuid(document.operation_id) !== checked.operationId
  ) {
    fail();
  }
  document.operation_id = uuid(document.operation_id);
  const principal = validatePrincipal(document.principal);
  bindPrincipal(principal, request, expectedApiPrincipal);
  if (request.operation === "preview_role_change") {
    document.data = validatePreviewData(document.data, request, principal.actor_id);
  } else {
    if (!exact(document.data, ["user"])) fail();
    document.data.user = validateUserRecord(document.data.user);
    if (
      document.data.user.id !== checked.targetId ||
      document.data.user.role !== request.payload.role ||
      document.data.user.is_active !== true ||
      document.data.user.id === principal.actor_id
    ) {
      fail();
    }
  }
  return document;
}

function validatePublicPreview(body, status, contentType, targetId, role, actorId) {
  if (
    typeof body !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  )
    fail();
  const checked = {
    request: {
      operation: "preview_role_change",
      payload: { target_user_id: uuid(targetId), role },
    },
  };
  const data = validatePreviewData(parseStrictJson(body), checked.request, uuid(actorId));
  return data;
}

function validatePublicUser(body, status, contentType, targetId, role) {
  if (
    typeof body !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  )
    fail();
  const user = validateUserRecord(parseStrictJson(body));
  if (user.id !== uuid(targetId) || user.role !== role || user.is_active !== true) fail();
  return user;
}

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  try {
    return parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
  } catch {
    fail();
  }
}

function postPlatformRoleRequest(origin, controlToken, payload) {
  const endpointUrl = new URL(endpoint, origin);
  const transport = endpointUrl.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const outgoing = transport(
      endpointUrl,
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
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          try {
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              text: new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
            });
          } catch {
            rejectResponse(new Error("Invalid Identity platform-role response"));
          }
        });
        response.on("error", () => rejectResponse(new Error("Identity request failed")));
      },
    );
    outgoing.on("error", () => rejectResponse(new Error("Identity request failed")));
    outgoing.end(body);
  });
}

async function apiKeyPrincipal(origin, controlToken, token) {
  const response = await postIdentityRequest(origin, controlToken, authEndpoint, { token });
  const admission = parseIdentityReceipt(response.text, response.status, response.contentType);
  return admission.ok && admission.principal.credential_type === "api_key"
    ? admission.principal
    : null;
}

function testAuditContext() {
  return {
    method: "POST",
    path: "/api/v1/users/33333333-3333-4333-8333-333333333333/role",
    ip: "127.0.0.1",
    request_id: "44444444-4444-4444-8444-444444444444",
  };
}

function testToken(actorId, credentialId, generation = 9_007_199_254_740_993n) {
  const header = Buffer.from(JSON.stringify(jwtHeader)).toString("base64url");
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
  return `${header}.${payload}.${Buffer.alloc(32, 5).toString("base64url")}`;
}

function testPrincipal(actorId, credentialId, generation = 9_007_199_254_740_993n) {
  return {
    actor_id: actorId,
    name: "Role administrator",
    email: "role-admin@example.test",
    role: "super_admin",
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation,
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
  };
}

function testRequest(operation, operationId, actorId, credentialId, targetId, role) {
  return validatePlatformRoleRequest({
    schema,
    operation,
    operation_id: operationId,
    token: testToken(actorId, credentialId),
    audit_context: testAuditContext(),
    payload: { target_user_id: targetId, role },
  });
}

function testReply(request, data, principal) {
  return stringifyLossless({
    schema,
    ok: true,
    operation: request.request.operation,
    operation_id: request.operationId,
    principal,
    data,
  });
}

function testUserRecord(id, role, preferences = {}) {
  return {
    id,
    email: "legacy-role@example.test",
    name: "Historical Account",
    role,
    group_name: null,
    group_id: null,
    status: "legacy-offline",
    is_active: true,
    disabled_kind: null,
    disabled_at: null,
    disabled_by: null,
    disabled_reason: null,
    email_verified_at: null,
    last_login_at: null,
    last_seen_at: null,
    password_admin_reset_at: null,
    deactivation_requested_at: null,
    deactivation_scheduled_at: null,
    preferences,
    avatar_ref: null,
    created_at: "2030-01-01T00:00:00Z",
  };
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
  const actorId = "11111111-1111-4111-8111-111111111111";
  const credentialId = "22222222-2222-4222-8222-222222222222";
  const targetId = "33333333-3333-4333-8333-333333333333";
  const operationId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const principal = testPrincipal(actorId, credentialId);
  const previewRequest = testRequest(
    "preview_role_change",
    operationId,
    actorId,
    credentialId,
    targetId,
    "viewer",
  );
  const preview = {
    user_id: targetId,
    email: "legacy-role@example.test",
    current_role: "annotator",
    requested_role: "viewer",
    can_change: false,
    blockers: roleBlockers,
    projects: [
      {
        project_id: "44444444-4444-4444-8444-444444444444",
        project_name: "Archived project",
        membership_role: "reviewer",
        annotator_batch_count: new JsonNumber(maxInt64.toString()),
        reviewer_batch_count: new JsonNumber("0"),
        assigned_task_count: new JsonNumber("0"),
        review_task_count: new JsonNumber("0"),
      },
    ],
    assigned_batch_count: new JsonNumber(maxInt64.toString()),
    assigned_task_count: new JsonNumber("0"),
    review_task_count: new JsonNumber("0"),
    other_project_count: new JsonNumber("0"),
    warnings: previewWarnings,
  };
  const checkedPreview = decodePlatformRoleResponse(
    previewRequest,
    testReply(previewRequest, preview, principal),
    200,
    "application/json",
  );
  if (
    checkedPreview.data.assigned_batch_count !== maxInt64 ||
    !stringifyLossless(checkedPreview).includes(maxInt64.toString())
  )
    fail();
  checks += 1;

  const preferenceValue = {
    token: "opaque user preference",
    password: new JsonNumber("123456789012345678901"),
    nested: {
      wide: new JsonNumber("9007199254740993"),
      decimal: new JsonNumber("0.1234567890123456789"),
      future: [true, null, "keep", new JsonNumber("-9223372036854775808")],
    },
  };
  const mutationRequest = testRequest(
    "change_platform_role",
    operationId,
    actorId,
    credentialId,
    targetId,
    "employee",
  );
  const mutated = testReply(
    mutationRequest,
    { user: testUserRecord(targetId, "employee", preferenceValue) },
    principal,
  );
  const changed = decodePlatformRoleResponse(mutationRequest, mutated, 200, "application/json");
  const changedJson = stringifyLossless(changed);
  for (const literal of [
    '"wide":9007199254740993',
    '"decimal":0.1234567890123456789',
    '"password":123456789012345678901',
    '"token":"opaque user preference"',
  ]) {
    if (!changedJson.includes(literal)) fail();
  }
  checks += 1;

  const legacyClaims = Buffer.from(
    stringifyLossless({
      sub: actorId,
      role: "annotator",
      exp: 1893456000n,
      jti: credentialId,
      iss: issuer,
      legacy_extension: true,
    }),
  ).toString("base64url");
  const legacyToken = `${Buffer.from(JSON.stringify(jwtHeader)).toString("base64url")}.${legacyClaims}.${Buffer.alloc(32, 5).toString("base64url")}`;
  const legacyRequest = validatePlatformRoleRequest({
    ...mutationRequest.request,
    token: legacyToken,
  });
  const legacyPrincipal = { ...principal, generation: new JsonNumber("0") };
  const legacyReply = decodePlatformRoleResponse(
    legacyRequest,
    testReply(legacyRequest, { user: testUserRecord(targetId, "employee") }, legacyPrincipal),
    200,
    "application/json",
  );
  if (legacyReply.principal.generation !== 0n) fail();
  checks += 1;

  const selfRequest = testRequest(
    "change_platform_role",
    operationId,
    actorId,
    credentialId,
    actorId,
    "viewer",
  );
  const selfFailure = stringifyLossless({
    schema,
    ok: false,
    error: { code: "self_change", status_code: 400, detail: "不能修改自己的角色" },
  });
  if (
    decodePlatformRoleResponse(selfRequest, selfFailure, 200, "application/json").error.code !==
    "self_change"
  )
    fail();
  expectFailure(() =>
    decodePlatformRoleResponse(mutationRequest, selfFailure, 200, "application/json"),
  );
  checks += 2;

  const invalidRoleFailure = stringifyLossless({
    schema,
    ok: false,
    error: {
      code: "invalid_platform_role",
      status_code: 400,
      detail: "非法平台角色: future_role",
    },
  });
  const invalidRoleRequest = testRequest(
    "preview_role_change",
    operationId,
    actorId,
    credentialId,
    targetId,
    "future_role",
  );
  if (
    decodePlatformRoleResponse(invalidRoleRequest, invalidRoleFailure, 200, "application/json")
      .error.code !== "invalid_platform_role"
  )
    fail();
  expectFailure(() =>
    decodePlatformRoleResponse(
      invalidRoleRequest,
      invalidRoleFailure.replace("future_role", "Future_role"),
      200,
      "application/json",
    ),
  );
  checks += 2;

  expectFailure(() => parseStrictJson('{"schema":"a","schema":"b"}'));
  expectFailure(() => parseStrictJson('{"schema":'));
  expectFailure(() =>
    validatePlatformRoleRequest(
      parseStrictJson(
        `{"schema":"${schema}","operation":"preview_role_change","operation_id":"${operationId}","token":"x","audit_context":{"method":"GET","path":"/","ip":"127.0.0.1","request_id":"44444444-4444-4444-8444-444444444444"},"payload":{"target_user_id":"${targetId}","role":"viewer","extra":true}}`,
      ),
    ),
  );
  expectFailure(() =>
    validatePlatformRoleRequest({ ...mutationRequest.request, protocol: schema }),
  );
  checks += 4;

  expectFailure(() =>
    decodePlatformRoleResponse(
      mutationRequest,
      mutated.replace(operationId, credentialId),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodePlatformRoleResponse(
      mutationRequest,
      mutated.replace(actorId, targetId),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodePlatformRoleResponse(
      mutationRequest,
      mutated.replace('"role":"employee"', '"role":"viewer"'),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodePlatformRoleResponse(
      mutationRequest,
      mutated.replace(
        '"created_at":"2030-01-01T00:00:00Z"',
        '"created_at":"2030-01-01T00:00:00Z","password_hash":"leak"',
      ),
      200,
      "application/json",
    ),
  );
  checks += 4;

  expectFailure(() =>
    decodePlatformRoleResponse(
      previewRequest,
      testReply(
        previewRequest,
        { ...preview, assigned_batch_count: new JsonNumber("9223372036854775808") },
        principal,
      ),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodePlatformRoleResponse(
      previewRequest,
      testReply(
        previewRequest,
        { ...preview, assigned_batch_count: new JsonNumber("1.0") },
        principal,
      ),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodePlatformRoleResponse(
      previewRequest,
      testReply(
        previewRequest,
        { ...preview, blockers: [roleBlockers[1], roleBlockers[0], roleBlockers[2]] },
        principal,
      ),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodePlatformRoleResponse(
      previewRequest,
      testReply(previewRequest, { ...preview, can_change: true }, principal),
      200,
      "application/json",
    ),
  );
  checks += 4;

  const blockedFailure = stringifyLossless({
    schema,
    ok: false,
    error: {
      code: "platform_role_change_blocked",
      status_code: 409,
      detail: { reason: "platform_role_change_blocked", blockers: roleBlockers },
    },
  });
  const blockedRequest = testRequest(
    "change_platform_role",
    operationId,
    actorId,
    credentialId,
    targetId,
    "viewer",
  );
  if (
    decodePlatformRoleResponse(blockedRequest, blockedFailure, 200, "application/json").error
      .code !== "platform_role_change_blocked"
  )
    fail();
  expectFailure(() =>
    decodePlatformRoleResponse(
      blockedRequest,
      blockedFailure.replace(
        '"detail":{"reason":"platform_role_change_blocked"',
        '"detail":{"reason":"unknown"',
      ),
      200,
      "application/json",
    ),
  );
  const echoedToken = stringifyLossless({
    schema,
    ok: false,
    error: { code: "unavailable", status_code: 503, detail: null },
  });
  const tokenRequest = validatePlatformRoleRequest({
    ...mutationRequest.request,
    token: "x",
  });
  expectFailure(() =>
    decodePlatformRoleResponse(
      tokenRequest,
      echoedToken.replace('"detail":null', '"detail":"x"'),
      200,
      "application/json",
    ),
  );
  checks += 3;
  return { ok: true, checks };
}

function validateCallInput(value) {
  if (
    !exact(value, ["mode", "url", "control_token", "request"]) ||
    !["call", "discard"].includes(value.mode)
  )
    fail();
  const checkedRequest = validatePlatformRoleRequest(value.request);
  const checked = validateInput({
    url: value.url,
    control_token: value.control_token,
    credential: checkedRequest.request.token,
  });
  return {
    mode: value.mode,
    request: checkedRequest,
    origin: checked.origin,
    controlToken: checked.controlToken,
  };
}

async function callPeer() {
  const input = await readInput();
  const checked = validateCallInput(input);
  let expectedApiPrincipal = null;
  if (checked.request.request.token.startsWith("ak_")) {
    expectedApiPrincipal = await apiKeyPrincipal(
      checked.origin,
      checked.controlToken,
      checked.request.request.token,
    );
  }
  const response = await postPlatformRoleRequest(
    checked.origin,
    checked.controlToken,
    checked.request.request,
  );
  const result = decodePlatformRoleResponse(
    checked.request,
    response.text,
    response.status,
    response.contentType,
    expectedApiPrincipal,
  );
  if (checked.mode === "discard") {
    process.stdout.write(`${JSON.stringify({ ok: true, response_discarded: true })}\n`);
    return;
  }
  process.stdout.write(`${stringifyLossless(result)}\n`);
}

async function validatePublicInput() {
  const input = await readInput();
  if (
    exact(input, [
      "mode",
      "status",
      "content_type",
      "body",
      "target_user_id",
      "role",
      "actor_id",
    ]) &&
    input.mode === "public_preview"
  ) {
    const data = validatePublicPreview(
      input.body,
      Number(int64(input.status)),
      input.content_type,
      input.target_user_id,
      input.role,
      input.actor_id,
    );
    process.stdout.write(`${stringifyLossless(data)}\n`);
    return;
  }
  if (
    exact(input, ["mode", "status", "content_type", "body", "target_user_id", "role"]) &&
    input.mode === "public_user"
  ) {
    const user = validatePublicUser(
      input.body,
      Number(int64(input.status)),
      input.content_type,
      input.target_user_id,
      input.role,
    );
    process.stdout.write(`${stringifyLossless(user)}\n`);
    return;
  }
  fail();
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
      process.stderr.write("Identity platform-role peer self-check failed\n");
      process.exitCode = 1;
    }
  } else {
    const runner = process.argv[2] === "--validate-public" ? validatePublicInput : callPeer;
    runner().catch(() => {
      process.stderr.write("Identity platform-role peer failed\n");
      process.exitCode = 1;
    });
  }
}
