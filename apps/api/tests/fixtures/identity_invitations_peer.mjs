import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { isIP } from "node:net";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

import {
  parseIdentityReceipt,
  postIdentityRequest,
  principalReceipt,
  uuid,
  validDateTime,
  validateInput,
} from "./identity_admission_peer.mjs";

const schema = "aap-identity-invitations/v1";
const issuer = "aap-identity/v1";
const endpoint = "/v1/identity/invitations/execute";
const authenticateEndpoint = "/v1/identity/authenticate";
const operations = new Set([
  "create_invitation",
  "preview_invitation_batch",
  "apply_invitation_batch",
  "list_invitations",
  "query_invitations",
  "invitation_stats",
  "export_invitations",
  "revoke_invitation",
  "resend_invitation",
  "send_invitation_email",
  "resolve_invitation",
  "register_invitation",
  "accept_invitation",
]);
const managerOperations = new Set([
  "create_invitation",
  "preview_invitation_batch",
  "apply_invitation_batch",
  "list_invitations",
  "query_invitations",
  "invitation_stats",
  "export_invitations",
  "revoke_invitation",
  "resend_invitation",
  "send_invitation_email",
]);
const statuses = new Set(["all", "pending", "accepted", "expired", "revoked"]);
const scopes = new Set(["me", "all"]);
const formats = new Set(["csv", "json"]);
const platformRoles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
const commonFailureCodes = new Set([
  "invalid_request",
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
  "current_scope_busy",
  "operation_conflict",
  "policy_rejected",
  "permission_denied",
  "mail_failed",
  "quota_exceeded",
  "invitation_not_found",
  "invitation_used",
  "invitation_revoked",
  "invitation_expired",
  "invitation_scope_invalid",
  "project_not_found",
  "email_exists",
  "already_member",
  "unavailable",
]);
const maxInt64 = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799;
const maxInputBytes = 8 * 1024 * 1024;

function fail() {
  throw new Error("Invalid Identity invitation request or response");
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exact(value, names) {
  return (
    isRecord(value) &&
    Object.keys(value).length === names.length &&
    names.every((name) => Object.hasOwn(value, name))
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

function strictInteger(source, spans, document, path, { min = 0n, max, allowNull = false }) {
  const value = getPath(document, path);
  if (value === null && allowNull) return null;
  const span = spans.get(pathKey(path));
  if (typeof value !== "number" || !span) fail();
  const raw = source.slice(span.start, span.end);
  if (!/^(0|[1-9][0-9]*)$/.test(raw)) fail();
  const integer = BigInt(raw);
  if (integer < min || integer > max) fail();
  setPath(document, path, integer);
  return integer;
}

function parseStrictJson(source, integerSpecs = []) {
  if (typeof source !== "string") fail();
  const spans = scanJson(source);
  let document;
  try {
    document = JSON.parse(source);
  } catch {
    fail();
  }
  for (const spec of integerSpecs) {
    if (spans.has(pathKey(spec.path))) strictInteger(source, spans, document, spec.path, spec);
  }
  return { document, spans };
}

function stringifyLossless(value) {
  if (typeof value === "bigint") return value.toString(10);
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stringifyLossless).join(",")}]`;
  return `{${Object.keys(value)
    .map((key) => `${JSON.stringify(key)}:${stringifyLossless(value[key])}`)
    .join(",")}}`;
}

function pythonStrip(value) {
  return value.replace(
    /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu,
    "",
  );
}

function nullableUuid(value) {
  return value === null ? null : uuid(value);
}

function nullableString(value, max) {
  return value === null || boundedString(value, 0, max);
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  const limits = { method: 8, path: 256, ip: 64, request_id: 36 };
  for (const [key, max] of Object.entries(limits)) {
    if (value[key] !== null && !boundedString(value[key], 0, max)) fail();
  }
  if (value.ip && isIP(value.ip) === 0) fail();
}

function validateCommonFilters(payload, { page = false, format = false } = {}) {
  const defaults = {
    status: "all",
    scope: "me",
    project_id: null,
    role: null,
    email: null,
    search: null,
    created_from: null,
    created_to: null,
    ...(page ? { page: 1n, page_size: 50n } : {}),
    ...(format ? { format: "csv" } : {}),
  };
  for (const [key, defaultValue] of Object.entries(defaults)) {
    if (!Object.hasOwn(payload, key)) payload[key] = defaultValue;
  }
  const fields = [
    ...(page ? ["page", "page_size"] : []),
    ...(format ? ["format"] : []),
    "status",
    "scope",
    "project_id",
    "role",
    "email",
    "search",
    "created_from",
    "created_to",
  ];
  if (!exact(payload, fields)) fail();
  if (
    !statuses.has(payload.status) ||
    !scopes.has(payload.scope) ||
    (format && !formats.has(payload.format)) ||
    (payload.role !== null && typeof payload.role !== "string") ||
    !nullableString(payload.email, 255) ||
    !nullableString(payload.search, 255)
  ) {
    fail();
  }
  payload.project_id = nullableUuid(payload.project_id);
  for (const field of ["created_from", "created_to"]) {
    if (payload[field] !== null && !validDateTime(payload[field])) fail();
  }
  if (page) {
    if (
      typeof payload.page !== "bigint" ||
      payload.page < 1n ||
      typeof payload.page_size !== "bigint" ||
      payload.page_size < 1n ||
      payload.page_size > 500n
    ) {
      fail();
    }
  }
}

function validateBatchItem(value) {
  if (!isRecord(value)) fail();
  for (const [key, defaultValue] of Object.entries({
    group_name: null,
    project_id: null,
    project_member_role: null,
  })) {
    if (!Object.hasOwn(value, key)) value[key] = defaultValue;
  }
  if (
    !exact(value, ["email", "role", "group_name", "project_id", "project_member_role"]) ||
    !boundedString(value.email, 0, 1000) ||
    !boundedString(value.role, 0, 100) ||
    !nullableString(value.group_name, 1000) ||
    !nullableString(value.project_member_role, 100)
  ) {
    fail();
  }
  value.project_id = nullableUuid(value.project_id);
  return value;
}

function expectedBatchEmail(raw) {
  const normalized = `${pythonStrip(raw.email)}`.toLowerCase();
  const emailValid =
    normalized.includes("@") &&
    codePointLength(normalized) >= 3 &&
    codePointLength(normalized) <= 255;
  const groupValid = raw.group_name === null || codePointLength(pythonStrip(raw.group_name)) <= 100;
  return emailValid && groupValid ? normalized : raw.email;
}

function safeBulkError(request, index, error) {
  if (typeof error !== "string" || !boundedString(error, 1, 1000)) return false;
  const raw = request.request.payload.items[index];
  const exactMessages = new Set([
    "邮箱格式不正确",
    "String should have at most 100 characters",
    "本批次中邮箱重复",
    "账号级邀请不得包含项目职责",
    "项目管理员仅能邀请员工或观察者",
    "目标项目不存在",
    "该账号已是目标项目成员",
    "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
    "当前账号不再具备邀请权限",
    "邀请中的数据组名称无效，请联系管理员重发邀请",
    "处理失败，请稍后再试",
  ]);
  if (exactMessages.has(error)) return true;
  if (error === `非法角色: ${raw.role}`) return true;
  if (
    /^邮箱 .{1,255} 已注册，请选择目标项目后邀请该用户加入$/.test(error) &&
    error.includes(expectedBatchEmail(raw))
  ) {
    return true;
  }
  return /^每位管理员滚动 24 小时最多邀请 (?:[1-9]|[1-9][0-9]{1,2}|1000) 人，请稍后再试$/.test(
    error,
  );
}

export function validateInvitationRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "token", "audit_context", "payload"]) ||
    value.schema !== schema ||
    !operations.has(value.operation)
  ) {
    fail();
  }
  value.operation_id = uuid(value.operation_id);
  if (
    value.token !== null &&
    (typeof value.token !== "string" || value.token.length < 1 || value.token.length > 16_384)
  ) {
    fail();
  }
  if (managerOperations.has(value.operation) || value.operation === "accept_invitation") {
    if (typeof value.token !== "string") fail();
  } else if (value.token !== null) {
    fail();
  }
  validateAuditContext(value.audit_context);
  const payload = value.payload;
  switch (value.operation) {
    case "create_invitation":
      for (const [key, defaultValue] of Object.entries({
        group_name: null,
        project_id: null,
        project_member_role: null,
      })) {
        if (!Object.hasOwn(payload, key)) payload[key] = defaultValue;
      }
      if (typeof payload.email === "string") {
        payload.email = pythonStrip(payload.email).toLowerCase();
      }
      if (typeof payload.group_name === "string") {
        payload.group_name = pythonStrip(payload.group_name) || null;
      }
      if (
        !exact(payload, ["email", "role", "group_name", "project_id", "project_member_role"]) ||
        !boundedString(payload.email, 3, 255) ||
        !payload.email.includes("@") ||
        typeof payload.role !== "string" ||
        !nullableString(payload.group_name, 100) ||
        (payload.project_member_role !== null && typeof payload.project_member_role !== "string")
      ) {
        fail();
      }
      payload.project_id = nullableUuid(payload.project_id);
      break;
    case "preview_invitation_batch":
    case "apply_invitation_batch":
      if (!exact(payload, ["items"]) || !Array.isArray(payload.items)) fail();
      if (payload.items.length < 1 || payload.items.length > 500) fail();
      payload.items = payload.items.map(validateBatchItem);
      break;
    case "list_invitations":
      if (!Object.hasOwn(payload, "status")) payload.status = "all";
      if (!Object.hasOwn(payload, "scope")) payload.scope = "me";
      if (
        !exact(payload, ["status", "scope"]) ||
        !statuses.has(payload.status) ||
        !scopes.has(payload.scope)
      )
        fail();
      break;
    case "query_invitations":
      validateCommonFilters(payload, { page: true });
      break;
    case "invitation_stats":
      validateCommonFilters(payload);
      break;
    case "export_invitations":
      validateCommonFilters(payload, { format: true });
      break;
    case "revoke_invitation":
    case "resend_invitation":
    case "send_invitation_email":
      if (!exact(payload, ["invitation_id"])) fail();
      payload.invitation_id = uuid(payload.invitation_id);
      break;
    case "resolve_invitation":
      if (!exact(payload, ["invitation_token"]) || !boundedString(payload.invitation_token, 1, 64))
        fail();
      break;
    case "register_invitation":
      if (
        !exact(payload, ["invitation_token", "name", "password"]) ||
        !boundedString(payload.invitation_token, 1, 64) ||
        !boundedString(payload.name, 1, 100) ||
        !boundedString(payload.password, 8, 128)
      ) {
        fail();
      }
      break;
    case "accept_invitation":
      if (!exact(payload, ["invitation_token"]) || !boundedString(payload.invitation_token, 1, 64))
        fail();
      break;
  }
  return { request: value, operationId: value.operation_id };
}

function decodeBase64Url(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) {
    fail();
  }
  const bytes = Buffer.from(value, "base64url");
  if (bytes.toString("base64url") !== value) fail();
  return bytes;
}

function decodeJwtJson(segment, integerSpecs = []) {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let source;
  try {
    source = decoder.decode(decodeBase64Url(segment));
  } catch {
    fail();
  }
  return parseStrictJson(source, integerSpecs).document;
}

function invitationJwtClaims(token) {
  if (typeof token !== "string" || token.length > 16_384) fail();
  const parts = token.split(".");
  if (parts.length !== 3 || decodeBase64Url(parts[2]).length !== 32) fail();
  const header = decodeJwtJson(parts[0]);
  if (!exact(header, ["alg", "typ"]) || header.alg !== "HS256" || header.typ !== "JWT") fail();
  const claims = decodeJwtJson(parts[1], [{ path: ["gen"], min: 0n, max: maxInt64 }]);
  if (
    !isRecord(claims) ||
    typeof claims.exp !== "number" ||
    !Number.isSafeInteger(claims.exp) ||
    claims.exp < 1 ||
    claims.exp > maxPythonEpochSeconds ||
    claims.iss !== issuer
  ) {
    fail();
  }
  const generation = claims.gen === undefined ? 0n : claims.gen;
  if (typeof generation !== "bigint") fail();
  const actorId = uuid(claims.sub);
  const credentialId = uuid(claims.jti);
  const expiry = new Date(claims.exp * 1000);
  if (!Number.isFinite(expiry.valueOf()) || !validDateTime(expiry.toISOString())) fail();
  return {
    actorId,
    credentialId,
    generation,
    expiry: claims.exp,
    role: claims.role,
    hasGeneration: Object.hasOwn(claims, "gen"),
  };
}

function validateUserRecord(value) {
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
  const userId = uuid(value.id);
  if (
    !boundedString(value.email, 3, 255) ||
    !boundedString(value.name, 1, 100) ||
    (!platformRoles.has(value.role) && !["annotator", "reviewer"].includes(value.role)) ||
    !nullableString(value.group_name, 100) ||
    (value.group_id !== null && typeof value.group_id !== "string") ||
    typeof value.status !== "string" ||
    typeof value.is_active !== "boolean" ||
    !nullableString(value.disabled_kind, 32) ||
    !nullableString(value.disabled_reason, 500) ||
    !isRecord(value.preferences) ||
    !nullableString(value.avatar_ref, 80) ||
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
  value.id = userId;
  return value;
}

function validateInvitationOut(value) {
  const fields = [
    "id",
    "email",
    "role",
    "group_name",
    "project_id",
    "project_name",
    "project_member_role",
    "status",
    "expires_at",
    "invited_by",
    "invited_by_name",
    "accepted_at",
    "revoked_at",
    "created_at",
  ];
  if (!exact(value, fields)) fail();
  value.id = uuid(value.id);
  value.invited_by = uuid(value.invited_by);
  value.project_id = nullableUuid(value.project_id);
  if (
    !boundedString(value.email, 3, 255) ||
    !boundedString(value.role, 1, 32) ||
    !nullableString(value.group_name, 128) ||
    !nullableString(value.project_name, 255) ||
    !nullableString(value.project_member_role, 32) ||
    !new Set(["pending", "accepted", "expired", "revoked"]).has(value.status) ||
    !nullableString(value.invited_by_name, 100) ||
    !validDateTime(value.expires_at) ||
    !validDateTime(value.created_at)
  ) {
    fail();
  }
  for (const field of ["accepted_at", "revoked_at"]) {
    if (value[field] !== null && !validDateTime(value[field])) fail();
  }
  if (value.project_id === null && value.project_member_role !== null) fail();
  return value;
}

function validateAcceptance(value) {
  if (
    !exact(value, [
      "project_id",
      "project_name",
      "project_member_role",
      "next_action",
      "next_action_label",
      "responsible_person_name",
      "active_batch_count",
    ]) ||
    typeof value.next_action !== "string" ||
    !["start_work", "wait_for_allocation"].includes(value.next_action) ||
    !boundedString(value.next_action_label, 1, 100) ||
    !nullableString(value.project_name, 255) ||
    !nullableString(value.project_member_role, 32) ||
    !nullableString(value.responsible_person_name, 100) ||
    typeof value.active_batch_count !== "bigint" ||
    value.active_batch_count < 0n ||
    value.active_batch_count > maxInt64
  ) {
    fail();
  }
  value.project_id = nullableUuid(value.project_id);
  if (value.project_id === null) {
    if (
      value.project_name !== null ||
      value.project_member_role !== null ||
      value.active_batch_count !== 0n ||
      value.next_action !== "wait_for_allocation" ||
      value.next_action_label !== "等待管理员分配项目" ||
      value.responsible_person_name !== null
    ) {
      fail();
    }
  } else if (
    value.project_member_role === null ||
    value.next_action !== (value.active_batch_count > 0n ? "start_work" : "wait_for_allocation") ||
    value.next_action_label !== (value.active_batch_count > 0n ? "开始工作" : "等待分派")
  ) {
    fail();
  }
  return value;
}

function validateInviteUrl(value, token) {
  if (typeof value !== "string" || value.length < 1) fail();
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    fail();
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    !parsed.pathname.endsWith("/register") ||
    parsed.searchParams.get("token") !== token
  ) {
    fail();
  }
  return value;
}

function validateCreatedInvitation(data) {
  if (
    !exact(data, [
      "invite_url",
      "token",
      "expires_at",
      "project_id",
      "project_name",
      "project_member_role",
    ]) ||
    !boundedString(data.token, 1, 64) ||
    !validDateTime(data.expires_at) ||
    !nullableString(data.project_name, 255) ||
    !nullableString(data.project_member_role, 32)
  ) {
    fail();
  }
  data.project_id = nullableUuid(data.project_id);
  validateInviteUrl(data.invite_url, data.token);
  if (
    data.project_id === null &&
    (data.project_name !== null || data.project_member_role !== null)
  ) {
    fail();
  }
  if (
    data.project_id !== null &&
    (data.project_name === null || data.project_member_role === null)
  ) {
    fail();
  }
  return data;
}

function validateBatchResult(request, data) {
  const isPreview = request.request.operation === "preview_invitation_batch";
  const items = request.request.payload.items;
  if (
    !exact(data, ["items", "succeeded", "failed", "preview"]) ||
    !Array.isArray(data.items) ||
    data.items.length !== items.length ||
    typeof data.succeeded !== "bigint" ||
    typeof data.failed !== "bigint" ||
    data.succeeded < 0n ||
    data.failed < 0n ||
    data.succeeded > 500n ||
    data.failed > 500n ||
    data.succeeded + data.failed !== BigInt(items.length) ||
    data.preview !== isPreview
  ) {
    fail();
  }
  data.items = data.items.map((item, index) => {
    if (
      !exact(item, [
        "index",
        "email",
        "ok",
        "retryable",
        "invitation_id",
        "token",
        "invite_url",
        "project_id",
        "project_name",
        "error",
      ]) ||
      typeof item.index !== "bigint" ||
      item.index !== BigInt(index) ||
      typeof item.email !== "string" ||
      item.email !== expectedBatchEmail(items[index]) ||
      typeof item.ok !== "boolean" ||
      typeof item.retryable !== "boolean" ||
      !nullableString(item.project_name, 255) ||
      !nullableString(item.error, 1000)
    ) {
      fail();
    }
    item.invitation_id = nullableUuid(item.invitation_id);
    item.project_id = nullableUuid(item.project_id);
    if (item.ok) {
      if (
        item.retryable ||
        item.error !== null ||
        item.invitation_id === null ||
        item.project_id !== items[index].project_id ||
        (item.project_id === null && item.project_name !== null) ||
        (item.project_id !== null && item.project_name === null)
      ) {
        fail();
      }
      if (isPreview) {
        if (item.token !== null || item.invite_url !== null) fail();
      } else {
        if (!boundedString(item.token, 1, 64) || item.invite_url === null) fail();
        validateInviteUrl(item.invite_url, item.token);
      }
    } else if (
      !item.retryable ||
      item.invitation_id !== null ||
      item.token !== null ||
      item.invite_url !== null ||
      (item.project_id !== null && item.project_id !== items[index].project_id) ||
      (item.project_id === null && item.project_name !== null) ||
      item.error === null ||
      !safeBulkError(request, index, item.error)
    ) {
      fail();
    }
    return item;
  });
  if (
    data.succeeded !== BigInt(data.items.filter((item) => item.ok).length) ||
    data.failed !== BigInt(data.items.filter((item) => !item.ok).length)
  ) {
    fail();
  }
  return data;
}

function validateInvitationData(request, data, principal) {
  const operation = request.request.operation;
  switch (operation) {
    case "create_invitation":
      data = validateCreatedInvitation(data);
      if (
        data.project_id !== request.request.payload.project_id ||
        data.project_member_role !== request.request.payload.project_member_role
      ) {
        fail();
      }
      return data;
    case "preview_invitation_batch":
    case "apply_invitation_batch":
      return validateBatchResult(request, data);
    case "list_invitations":
      if (!exact(data, ["items"]) || !Array.isArray(data.items)) fail();
      data.items = data.items.map(validateInvitationOut);
      return data;
    case "query_invitations": {
      if (
        !exact(data, ["items", "total", "page", "page_size", "pages"]) ||
        !Array.isArray(data.items) ||
        typeof data.total !== "bigint" ||
        typeof data.page !== "bigint" ||
        typeof data.page_size !== "bigint" ||
        typeof data.pages !== "bigint" ||
        data.total < 0n ||
        data.page < 1n ||
        data.page_size < 1n ||
        data.page_size > 500n ||
        data.page !== request.request.payload.page ||
        data.page_size !== request.request.payload.page_size ||
        BigInt(data.items.length) > data.page_size
      ) {
        fail();
      }
      data.items = data.items.map(validateInvitationOut);
      const pages = data.total === 0n ? 0n : (data.total + data.page_size - 1n) / data.page_size;
      if (data.pages !== pages) fail();
      return data;
    }
    case "invitation_stats":
      if (!exact(data, ["total", "pending", "accepted", "expired", "revoked"])) fail();
      for (const field of ["total", "pending", "accepted", "expired", "revoked"]) {
        if (typeof data[field] !== "bigint" || data[field] < 0n || data[field] > maxInt64) fail();
      }
      if (data.total !== data.pending + data.accepted + data.expired + data.revoked) {
        fail();
      }
      return data;
    case "export_invitations":
      if (!exact(data, ["items", "exported_by", "exported_at"]) || !Array.isArray(data.items))
        fail();
      if (!boundedString(data.exported_by, 3, 255) || !validDateTime(data.exported_at)) {
        fail();
      }
      data.items = data.items.map(validateInvitationOut);
      return data;
    case "revoke_invitation":
      if (!exact(data, [])) fail();
      return data;
    case "resend_invitation":
      if (
        !exact(data, ["invite_url", "token", "expires_at"]) ||
        !boundedString(data.token, 1, 64) ||
        !validDateTime(data.expires_at)
      ) {
        fail();
      }
      validateInviteUrl(data.invite_url, data.token);
      return data;
    case "send_invitation_email":
      return validateInvitationMailResult(request, data);
    case "resolve_invitation":
      return validateResolvedInvitation(data);
    case "register_invitation":
      return validateRegisteredInvitation(request, data);
    case "accept_invitation":
      return validateAcceptedInvitation(request, data, principal);
    default:
      fail();
  }
}

function validateResolvedInvitation(data) {
  const fields = [
    "email",
    "role",
    "group_name",
    "project_id",
    "project_name",
    "project_member_role",
    "expires_at",
    "invited_by_name",
  ];
  if (
    !exact(data, fields) ||
    !boundedString(data.email, 3, 255) ||
    !boundedString(data.role, 1, 32) ||
    !nullableString(data.group_name, 128) ||
    !nullableString(data.project_name, 255) ||
    !nullableString(data.project_member_role, 32) ||
    !validDateTime(data.expires_at) ||
    !nullableString(data.invited_by_name, 100)
  ) {
    fail();
  }
  data.project_id = nullableUuid(data.project_id);
  if (
    data.project_id === null &&
    (data.project_name !== null || data.project_member_role !== null)
  ) {
    fail();
  }
  return data;
}

function validateRegisteredInvitation(request, data) {
  if (
    !exact(data, [
      "access_token",
      "token_type",
      "user",
      "email_verification_required",
      "acceptance",
    ]) ||
    typeof data.access_token !== "string" ||
    data.access_token.length < 1 ||
    data.access_token.length > 16_384 ||
    data.token_type !== "bearer" ||
    data.email_verification_required !== false
  ) {
    fail();
  }
  const user = validateUserRecord(data.user);
  if (!user.is_active || user.status !== "online" || user.email_verified_at === null) fail();
  data.acceptance = validateAcceptance(data.acceptance);
  const claims = invitationJwtClaims(data.access_token);
  if (
    claims.actorId !== user.id ||
    claims.credentialId !== request.operationId ||
    user.id !== request.operationId ||
    claims.role !== user.role ||
    !claims.hasGeneration ||
    claims.generation !== 0n
  ) {
    fail();
  }
  return data;
}

function validateAcceptedInvitation(request, data, principal) {
  if (!exact(data, ["user", "acceptance"])) fail();
  const user = validateUserRecord(data.user);
  if (principal === null || user.id !== principal.actor_id || user.role !== principal.role) fail();
  data.acceptance = validateAcceptance(data.acceptance);
  if (data.acceptance.project_id === null) fail();
  return data;
}

function validateInvitationMailResult(request, data) {
  const mailReasons = new Set([
    "sent",
    "not_configured",
    "message_invalid",
    "tls_failed",
    "auth_failed",
    "recipient_rejected",
    "sender_rejected",
    "data_rejected",
    "connect_failed",
    "transport_failed",
    "derivation_unavailable",
  ]);
  if (
    !exact(data, [
      "ok",
      "invitation_id",
      "email",
      "invite_url",
      "message",
      "mail_status",
      "mail_failure_code",
    ]) ||
    data.ok !== true ||
    !["pending", "sent", "failed"].includes(data.mail_status) ||
    !nullableString(data.mail_failure_code, 64) ||
    (data.mail_failure_code !== null && !mailReasons.has(data.mail_failure_code)) ||
    !boundedString(data.email, 3, 255) ||
    !boundedString(data.message, 0, 255) ||
    typeof data.invite_url !== "string"
  ) {
    fail();
  }
  data.invitation_id = uuid(data.invitation_id);
  if (data.invitation_id !== request.request.payload.invitation_id) fail();
  const link = new URL(data.invite_url);
  const proof = link.searchParams.get("token");
  if (
    !["http:", "https:"].includes(link.protocol) ||
    !link.pathname.endsWith("/register") ||
    proof === null ||
    !boundedString(proof, 1, 64)
  ) {
    fail();
  }
  if (data.mail_status === "sent") {
    if (data.mail_failure_code !== null || data.message !== "邀请邮件已发送") fail();
  } else if (
    data.mail_status === "failed" &&
    (data.mail_failure_code === null || data.mail_failure_code === "sent")
  ) {
    fail();
  } else if (data.mail_status === "pending" && data.mail_failure_code === "sent") {
    fail();
  }
  return data;
}

function invitationResponseIntegerSpecs(request, document) {
  const specs = [
    { path: ["principal", "generation"], min: 0n, max: maxInt64, allowNull: true },
    { path: ["error", "status_code"], min: 100n, max: 599n, allowNull: true },
  ];
  const data = document?.data;
  const addArrayIndices = (items, prefix) => {
    if (Array.isArray(items)) {
      items.forEach((_, index) =>
        specs.push({ path: [...prefix, index, "index"], min: 0n, max: 499n }),
      );
    }
  };
  switch (request.request.operation) {
    case "preview_invitation_batch":
    case "apply_invitation_batch":
      specs.push(
        { path: ["data", "succeeded"], min: 0n, max: 500n },
        { path: ["data", "failed"], min: 0n, max: 500n },
      );
      addArrayIndices(data?.items, ["data", "items"]);
      break;
    case "query_invitations":
      specs.push(
        { path: ["data", "total"], min: 0n, max: maxInt64 },
        { path: ["data", "page"], min: 1n, max: maxInt64 },
        { path: ["data", "page_size"], min: 1n, max: 500n },
        { path: ["data", "pages"], min: 0n, max: maxInt64 },
      );
      break;
    case "invitation_stats":
      for (const field of ["total", "pending", "accepted", "expired", "revoked"]) {
        specs.push({ path: ["data", field], min: 0n, max: maxInt64 });
      }
      break;
    case "register_invitation":
    case "accept_invitation":
      specs.push({ path: ["data", "acceptance", "active_batch_count"], min: 0n, max: maxInt64 });
      break;
  }
  return specs;
}

function invitationPolicyDetailAllowed(request, statusCode, detail) {
  const operation = request.request.operation;
  const payload = request.request.payload;
  if (typeof detail !== "string" || !boundedString(detail, 1, 1000)) return false;
  const exactDetails = new Map([
    [
      "create_invitation",
      new Set([
        "账号级邀请不得包含项目职责",
        "项目管理员仅能邀请员工或观察者",
        "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
        "目标项目不存在",
        "该账号已是目标项目成员",
        "当前账号不再具备邀请权限",
      ]),
    ],
    ["revoke_invitation", new Set(["邀请不存在", "该邀请已被接受，无法撤销"])],
    [
      "resend_invitation",
      new Set(["邀请不存在", "项目管理员不能重发高权限邀请", "该邀请已被接受，无法重发"]),
    ],
    [
      "send_invitation_email",
      new Set(["邀请不存在", "邀请链接无效", "该邀请已撤销", "该邀请已过期"]),
    ],
    [
      "resolve_invitation",
      new Set([
        "邀请链接无效",
        "该邀请已被使用",
        "该邀请已撤销",
        "该邀请已过期",
        "目标项目已删除，无法接受邀请",
        "邀请人已停用或不再具备管理权限",
        "该邀请权限已失效",
        "目标项目当前没有可用的负责人",
        "邀请人已不再管理目标项目",
        "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
      ]),
    ],
    [
      "register_invitation",
      new Set([
        "邀请链接无效",
        "该邀请已被使用",
        "该邀请已撤销",
        "该邀请已过期",
        "目标项目已删除，无法接受邀请",
        "邀请人已停用或不再具备管理权限",
        "该邀请权限已失效",
        "目标项目当前没有可用的负责人",
        "邀请人已不再管理目标项目",
        "该邀请包含已失效的账号角色，请联系管理员重发",
        "该邮箱已注册，请让该用户登录后确认项目邀请",
        "邀请中的数据组名称无效，请联系管理员重发邀请",
      ]),
    ],
    [
      "accept_invitation",
      new Set([
        "邀请链接无效",
        "该邀请已被使用",
        "该邀请已撤销",
        "该邀请已过期",
        "目标项目已删除，无法接受邀请",
        "邀请人已停用或不再具备管理权限",
        "该邀请权限已失效",
        "目标项目当前没有可用的负责人",
        "邀请人已不再管理目标项目",
        "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
        "该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请",
        "当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录",
        "账号已停用，请重新登录",
      ]),
    ],
    ["list_invitations", new Set(["仅超级管理员可查看全部邀请"])],
    ["query_invitations", new Set(["仅超级管理员可查看全部邀请"])],
    ["invitation_stats", new Set(["仅超级管理员可查看全部邀请"])],
    ["export_invitations", new Set(["仅超级管理员可查看全部邀请"])],
    ["preview_invitation_batch", new Set([])],
    ["apply_invitation_batch", new Set([])],
  ]);
  if (exactDetails.get(operation)?.has(detail)) {
    const statusByDetail = new Map([
      ["账号级邀请不得包含项目职责", 400n],
      ["项目管理员仅能邀请员工或观察者", 403n],
      ["目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色", 400n],
      ["目标项目不存在", 404n],
      ["该账号已是目标项目成员", 409n],
      ["邀请不存在", 404n],
      ["该邀请已被接受，无法撤销", 400n],
      ["项目管理员不能重发高权限邀请", 403n],
      ["该邀请已被接受，无法重发", 400n],
      ["邀请链接无效", 404n],
      ["该邀请已被使用", 410n],
      ["该邀请已撤销", 410n],
      ["该邀请已过期", 410n],
      ["目标项目已删除，无法接受邀请", 410n],
      ["邀请人已停用或不再具备管理权限", 410n],
      ["该邀请权限已失效", 410n],
      ["目标项目当前没有可用的负责人", 410n],
      ["邀请人已不再管理目标项目", 410n],
      ["该邀请包含已失效的账号角色，请联系管理员重发", 410n],
      ["该邮箱已注册，请让该用户登录后确认项目邀请", 409n],
      ["邀请中的数据组名称无效，请联系管理员重发邀请", 409n],
      ["该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请", 409n],
      ["当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录", 403n],
      ["账号已停用，请重新登录", 401n],
      ["仅超级管理员可查看全部邀请", 403n],
    ]);
    return statusByDetail.get(detail) === statusCode;
  }
  if (operation === "create_invitation" && detail === `非法角色: ${payload.role}`)
    return statusCode === 400n;
  if (
    operation === "create_invitation" &&
    /^邮箱 .{3,255} 已注册，请选择目标项目后邀请该用户加入$/.test(detail)
  ) {
    return statusCode === 409n && detail.includes(payload.email);
  }
  if (
    operation === "accept_invitation" &&
    /^当前账号是.{1,32}，无法持有.{1,32}项目职责。为避免静默修改平台角色，请联系管理员调整账号角色后再接受邀请$/.test(
      detail,
    )
  ) {
    return statusCode === 409n;
  }
  if (
    ["create_invitation", "preview_invitation_batch", "apply_invitation_batch"].includes(
      operation,
    ) &&
    /^每位管理员滚动 24 小时最多邀请 [1-9][0-9]{0,3} 人，请稍后再试$/.test(detail)
  ) {
    return statusCode === 429n;
  }
  if (
    ["preview_invitation_batch", "apply_invitation_batch"].includes(operation) &&
    new Set([
      "邮箱格式不正确",
      "本批次中邮箱重复",
      "账号级邀请不得包含项目职责",
      "项目管理员仅能邀请员工或观察者",
      "目标项目不存在",
      "该账号已是目标项目成员",
      "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
      "处理失败，请稍后重试",
    ]).has(detail)
  ) {
    return true;
  }
  return false;
}

function operationAllowsFailure(operation, code) {
  if (
    [
      "credential_invalid",
      "credential_revoked",
      "generation_outdated",
      "token_expired",
      "user_inactive",
      "invalid_account_role",
      "credential_busy",
      "unavailable",
      "current_scope_busy",
      "operation_conflict",
      "permission_denied",
      "invalid_request",
      "policy_rejected",
    ].includes(code)
  ) {
    return true;
  }
  if (
    [
      "invitation_used",
      "invitation_revoked",
      "invitation_expired",
      "invitation_scope_invalid",
    ].includes(code)
  ) {
    return [
      "resolve_invitation",
      "register_invitation",
      "accept_invitation",
      "send_invitation_email",
    ].includes(operation);
  }
  if (code === "invitation_not_found") {
    return [
      "revoke_invitation",
      "resend_invitation",
      "send_invitation_email",
      "resolve_invitation",
      "register_invitation",
      "accept_invitation",
    ].includes(operation);
  }
  if (["project_not_found", "email_exists", "already_member", "quota_exceeded"].includes(code)) {
    return operation === "create_invitation" || operation === "apply_invitation_batch";
  }
  return code === "mail_failed" && operation === "send_invitation_email";
}

function validateInvitationFailure(request, error) {
  if (
    !exact(error, ["code", "status_code", "detail"]) ||
    typeof error.code !== "string" ||
    !commonFailureCodes.has(error.code) ||
    !operationAllowsFailure(request.request.operation, error.code)
  ) {
    fail();
  }
  const operation = request.request.operation;
  if (error.code === "policy_rejected") {
    if (
      typeof error.status_code !== "bigint" ||
      !invitationPolicyDetailAllowed(request, error.status_code, error.detail)
    ) {
      fail();
    }
    return error;
  }
  if (error.code === "invalid_request") {
    if (error.status_code !== 400n || error.detail !== "邀请请求无效") fail();
  } else if (error.code === "unavailable") {
    if (error.status_code !== 503n || error.detail !== "身份邀请服务暂不可用") fail();
  } else if (error.code === "current_scope_busy") {
    if (
      error.status_code !== 409n ||
      !["账号正在更新，请刷新后重试", "项目或邀请正在更新，请刷新后重试"].includes(error.detail)
    ) {
      fail();
    }
  } else if (error.code === "operation_conflict") {
    if (error.status_code !== 409n || error.detail !== "operation_conflict") fail();
  } else if (error.code === "invitation_not_found") {
    const publicLinkOperations = new Set([
      "resolve_invitation",
      "register_invitation",
      "accept_invitation",
    ]);
    const expectedDetail = publicLinkOperations.has(operation) ? "邀请链接无效" : "邀请不存在";
    if (error.status_code !== 404n || error.detail !== expectedDetail) fail();
  } else if (["invitation_used", "invitation_revoked", "invitation_expired"].includes(error.code)) {
    const details = {
      invitation_used: "该邀请已被使用",
      invitation_revoked: "该邀请已撤销",
      invitation_expired: "该邀请已过期",
    };
    if (error.status_code !== 410n || error.detail !== details[error.code]) fail();
  } else if (error.code === "invitation_scope_invalid") {
    if (
      error.status_code !== 410n ||
      ![
        "邀请人已停用或不再具备管理权限",
        "该邀请权限已失效",
        "目标项目当前没有可用的负责人",
        "邀请人已不再管理目标项目",
        "目标项目已删除，无法接受邀请",
        "该邀请包含已失效的账号角色，请联系管理员重发",
      ].includes(error.detail)
    ) {
      fail();
    }
  } else if (error.code === "project_not_found") {
    if (error.status_code !== 404n || error.detail !== "目标项目不存在") fail();
  } else if (error.code === "email_exists") {
    if (
      error.status_code !== 409n ||
      typeof error.detail !== "string" ||
      !/^邮箱 .{3,255} 已注册，请选择目标项目后邀请该用户加入$/.test(error.detail)
    ) {
      fail();
    }
  } else if (error.code === "already_member") {
    if (error.status_code !== 409n || error.detail !== "该账号已是目标项目成员") fail();
  } else if (error.code === "quota_exceeded") {
    if (
      error.status_code !== 429n ||
      typeof error.detail !== "string" ||
      !/^每位管理员滚动 24 小时最多邀请 (?:[1-9]|[1-9][0-9]{1,2}|1000) 人，请稍后再试$/.test(
        error.detail,
      )
    ) {
      fail();
    }
  } else if (error.code === "mail_failed") {
    const details = new Set([
      "SMTP 未完整配置（host / port / from 必填）",
      "SMTP 邮件内容无效",
      ...[
        "not_configured",
        "message_invalid",
        "tls_failed",
        "auth_failed",
        "recipient_rejected",
        "sender_rejected",
        "data_rejected",
        "connect_failed",
        "transport_failed",
        "derivation_unavailable",
      ].map((reason) => `SMTP 发送失败: ${reason}`),
    ]);
    if (error.status_code !== 502n || !details.has(error.detail)) fail();
  } else {
    const statuses = {
      credential_invalid: 401n,
      credential_revoked: 401n,
      generation_outdated: 401n,
      token_expired: 401n,
      user_inactive: 401n,
      invalid_account_role: 403n,
      credential_busy: 409n,
      permission_denied: 403n,
    };
    if (error.status_code !== statuses[error.code] || typeof error.detail !== "string") fail();
    if (
      [
        "credential_invalid",
        "credential_revoked",
        "generation_outdated",
        "token_expired",
        "user_inactive",
      ].includes(error.code) &&
      error.detail !== "无效的认证凭据"
    ) {
      fail();
    }
    if (error.code === "credential_busy" && error.detail !== "账号正在更新，请刷新后重试") fail();
    if (
      error.code === "permission_denied" &&
      ![
        "当前账号不再具备邀请权限",
        "项目管理员仅能邀请员工或观察者",
        "仅超级管理员可查看全部邀请",
        "项目管理员不能重发高权限邀请",
        "需要角色权限: super_admin",
      ].includes(error.detail)
    ) {
      fail();
    }
  }
  return error;
}

function bindPrincipal(raw, receipt, claims, expectedApiKey) {
  if (claims !== null) {
    if (
      receipt.actor_id !== claims.actorId ||
      receipt.credential_type !== "jwt" ||
      receipt.credential_id !== claims.credentialId ||
      receipt.generation_decimal !== claims.generation.toString(10) ||
      !validDateTime(raw.expires_at) ||
      Date.parse(raw.expires_at) !== claims.expiry * 1000
    ) {
      fail();
    }
    return;
  }
  if (
    expectedApiKey === null ||
    receipt.actor_id !== expectedApiKey.actor_id ||
    receipt.credential_type !== "api_key" ||
    receipt.credential_id !== expectedApiKey.credential_id
  ) {
    fail();
  }
}

function decodeInvitationResponse(request, source, status, contentType, expectedApiKey = null) {
  if (
    typeof source !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const preliminary = parseStrictJson(source).document;
  const parsed = parseStrictJson(
    source,
    invitationResponseIntegerSpecs(request, preliminary),
  ).document;
  if (!isRecord(parsed) || parsed.schema !== schema || typeof parsed.ok !== "boolean") fail();
  if (!parsed.ok) {
    if (
      !exact(parsed, ["schema", "ok", "operation", "operation_id", "error"]) ||
      parsed.operation !== request.request.operation ||
      uuid(parsed.operation_id) !== request.operationId
    ) {
      fail();
    }
    return {
      schema,
      ok: false,
      operation: request.request.operation,
      operation_id: request.operationId,
      error: validateInvitationFailure(request, parsed.error),
    };
  }
  if (
    !exact(parsed, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    parsed.operation !== request.request.operation ||
    uuid(parsed.operation_id) !== request.operationId
  ) {
    fail();
  }
  let principal = null;
  if (["resolve_invitation", "register_invitation"].includes(request.request.operation)) {
    if (parsed.principal !== null) fail();
  } else {
    if (parsed.principal === null) fail();
    const claims = request.request.token.startsWith("ak_")
      ? null
      : invitationJwtClaims(request.request.token);
    principal = principalReceipt(parsed.principal);
    bindPrincipal(parsed.principal, principal, claims, expectedApiKey);
  }
  const data = validateInvitationData(request, parsed.data, principal);
  return {
    schema,
    ok: true,
    operation: request.request.operation,
    operation_id: request.operationId,
    principal,
    data,
  };
}

function validatePeerInput(value) {
  if (!exact(value, ["url", "control_token", "request"])) fail();
  const request = validateInvitationRequest(value.request);
  const checked = validateInput({
    url: value.url,
    control_token: value.control_token,
    credential: request.request.token ?? "anonymous-invitation",
  });
  return { request, origin: checked.origin, controlToken: checked.controlToken };
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
  const preliminary = parseStrictJson(source).document;
  const operation = preliminary?.request?.operation;
  const integerSpecs = [];
  if (operation === "query_invitations") {
    integerSpecs.push(
      { path: ["request", "payload", "page"], min: 1n, max: maxInt64 },
      { path: ["request", "payload", "page_size"], min: 1n, max: 500n },
    );
  }
  return parseStrictJson(source, integerSpecs).document;
}

function postInvitationRequest(origin, controlToken, payload) {
  const endpointUrl = new URL(endpoint, origin);
  const transport = endpointUrl.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(stringifyLossless(payload), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const request = transport(
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
            const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              text,
            });
          } catch {
            rejectResponse(new Error("Invalid Identity invitation response"));
          }
        });
        response.on("error", () => rejectResponse(new Error("Identity invitation request failed")));
      },
    );
    request.on("error", () => rejectResponse(new Error("Identity invitation request failed")));
    request.end(body);
  });
}

async function apiKeyPrincipal(origin, controlToken, token) {
  if (!/^ak_[A-Za-z0-9_-]{32}$/.test(token)) fail();
  const response = await postIdentityRequest(origin, controlToken, authenticateEndpoint, { token });
  parseStrictJson(response.text, [
    { path: ["data", "generation"], min: 0n, max: maxInt64, allowNull: true },
  ]);
  const receipt = parseIdentityReceipt(response.text, response.status, response.contentType);
  return receipt.ok ? receipt.principal : null;
}

async function verifyIssuedAccessToken(origin, controlToken, data) {
  const claims = invitationJwtClaims(data.access_token);
  if (claims.actorId !== data.user.id || claims.role !== data.user.role) fail();
  const response = await postIdentityRequest(origin, controlToken, authenticateEndpoint, {
    token: data.access_token,
  });
  parseStrictJson(response.text, [
    { path: ["data", "generation"], min: 0n, max: maxInt64, allowNull: true },
  ]);
  const receipt = parseIdentityReceipt(response.text, response.status, response.contentType);
  if (!receipt.ok) fail();
  const principal = receipt.principal;
  if (
    principal.actor_id !== claims.actorId ||
    principal.credential_type !== "jwt" ||
    principal.credential_id !== claims.credentialId ||
    principal.generation_decimal !== claims.generation.toString(10) ||
    principal.role !== data.user.role ||
    !validDateTime(principal.expires_at) ||
    Date.parse(principal.expires_at) !== claims.expiry * 1000
  ) {
    fail();
  }
}

async function main() {
  const input = await readInput();
  const { request, origin, controlToken } = validatePeerInput(input);
  const expectedApiKey =
    request.request.token !== null && request.request.token.startsWith("ak_")
      ? await apiKeyPrincipal(origin, controlToken, request.request.token)
      : null;
  const response = await postInvitationRequest(origin, controlToken, request.request);
  const result = decodeInvitationResponse(
    request,
    response.text,
    response.status,
    response.contentType,
    expectedApiKey,
  );
  if (request.request.operation === "register_invitation" && result.ok) {
    await verifyIssuedAccessToken(origin, controlToken, result.data);
  }
  process.stdout.write(`${stringifyLossless(result)}\n`);
}

function makeAuditContext() {
  return { method: "POST", path: "/acceptance/invitations", ip: "127.0.0.1", request_id: null };
}

function makeTokenFromClaimsJson(claimsJson) {
  const input =
    Buffer.from('{"alg":"HS256","typ":"JWT"}').toString("base64url") +
    "." +
    Buffer.from(claimsJson).toString("base64url");
  return `${input}.${Buffer.alloc(32, 5).toString("base64url")}`;
}

function makeToken(actorId, credentialId, generation = 0n, role = "super_admin") {
  return makeTokenFromClaimsJson(
    stringifyLossless({
      sub: actorId,
      role,
      exp: 1_893_456_000n,
      jti: credentialId,
      gen: generation,
      iss: issuer,
    }),
  );
}

function makeRequest(operation, operationId, token, payload) {
  return validateInvitationRequest({
    schema,
    operation,
    operation_id: operationId,
    token,
    audit_context: makeAuditContext(),
    payload,
  });
}

function testPrincipal(actorId, credentialId, generation = 0n, role = "super_admin") {
  return {
    actor_id: actorId,
    name: "Current manager",
    email: "manager@example.test",
    role,
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation,
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
  };
}

function testInvitation(id, overrides = {}) {
  return {
    id,
    email: "invitee@example.test",
    role: "employee",
    group_name: null,
    project_id: null,
    project_name: null,
    project_member_role: null,
    status: "pending",
    expires_at: "2030-01-08T00:00:00Z",
    invited_by: "11111111-1111-4111-8111-111111111111",
    invited_by_name: "Current manager",
    accepted_at: null,
    revoked_at: null,
    created_at: "2030-01-01T00:00:00Z",
    ...overrides,
  };
}

function testUserRecord(id, overrides = {}) {
  return {
    id,
    email: "invitee@example.test",
    name: "New invited user",
    role: "employee",
    group_name: null,
    group_id: null,
    status: "online",
    is_active: true,
    disabled_kind: null,
    disabled_at: null,
    disabled_by: null,
    disabled_reason: null,
    email_verified_at: "2030-01-01T00:00:00Z",
    last_login_at: null,
    last_seen_at: null,
    password_admin_reset_at: null,
    deactivation_requested_at: null,
    deactivation_scheduled_at: null,
    preferences: {},
    avatar_ref: null,
    created_at: "2030-01-01T00:00:00Z",
    ...overrides,
  };
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
  const credentialId = "22222222-2222-4222-8222-222222222222";
  const inviteId = "33333333-3333-4333-8333-333333333333";
  const projectId = "44444444-4444-4444-8444-444444444444";
  const memberId = "55555555-5555-4555-8555-555555555555";
  const token = makeToken(actorId, credentialId);
  const principal = testPrincipal(actorId, credentialId);
  const invitation = testInvitation(inviteId);
  const listFilters = {
    status: "all",
    scope: "me",
    project_id: null,
    role: null,
    email: null,
    search: null,
    created_from: null,
    created_to: null,
  };
  const requestCases = [
    [
      "create_invitation",
      token,
      {
        email: "invitee@example.test",
        role: "employee",
        group_name: "Vision",
        project_id: projectId,
        project_member_role: "annotator",
      },
    ],
    [
      "preview_invitation_batch",
      token,
      {
        items: [
          {
            email: "invitee@example.test",
            role: "employee",
            group_name: null,
            project_id: null,
            project_member_role: null,
          },
        ],
      },
    ],
    [
      "apply_invitation_batch",
      token,
      {
        items: [
          {
            email: "invitee@example.test",
            role: "employee",
            group_name: null,
            project_id: null,
            project_member_role: null,
          },
        ],
      },
    ],
    ["list_invitations", token, { status: "pending", scope: "me" }],
    ["query_invitations", token, { ...listFilters, page: 1n, page_size: 50n }],
    ["invitation_stats", token, listFilters],
    ["export_invitations", token, { ...listFilters, format: "json" }],
    ["revoke_invitation", token, { invitation_id: inviteId }],
    ["resend_invitation", token, { invitation_id: inviteId }],
    ["send_invitation_email", token, { invitation_id: inviteId }],
    ["resolve_invitation", null, { invitation_token: "legacy_password-reset_邀请" }],
    [
      "register_invitation",
      null,
      {
        invitation_token: "legacy_password-reset_邀请",
        name: "New user",
        password: "InvitationStrong123",
      },
    ],
    ["accept_invitation", token, { invitation_token: "legacy_password-reset_邀请" }],
  ];
  for (let index = 0; index < requestCases.length; index += 1) {
    const [operation, bearer, payload] = requestCases[index];
    const request = makeRequest(operation, credentialId, bearer, payload);
    if (request.request.operation !== operation) fail();
    checks += 1;
  }

  const proofBearing = makeRequest("resolve_invitation", credentialId, null, {
    invitation_token: "password_reset",
  });
  if (
    proofBearing.request.token !== null ||
    proofBearing.request.payload.invitation_token !== "password_reset"
  ) {
    fail();
  }
  checks += 1;
  expectFailure(() =>
    validateInvitationRequest({
      schema,
      operation: "resolve_invitation",
      operation_id: credentialId,
      token,
      audit_context: makeAuditContext(),
      payload: { invitation_token: "opaque-proof" },
    }),
  );
  checks += 1;
  expectFailure(() =>
    validateInvitationRequest({
      schema,
      operation: "create_invitation",
      operation_id: credentialId,
      token: null,
      audit_context: makeAuditContext(),
      payload: {
        email: "invitee@example.test",
        role: "employee",
        group_name: null,
        project_id: null,
        project_member_role: null,
      },
    }),
  );
  checks += 1;
  expectFailure(() => parseStrictJson('{"schema":"x","schema":"y"}'));
  checks += 1;
  expectFailure(() =>
    parseStrictJson('{"request":{"payload":{"page":1.0}}}', [
      { path: ["request", "payload", "page"], min: 1n, max: maxInt64 },
    ]),
  );
  checks += 1;

  const create = makeRequest("create_invitation", credentialId, token, {
    email: "invitee@example.test",
    role: "employee",
    group_name: "Vision",
    project_id: projectId,
    project_member_role: "annotator",
  });
  const inviteToken = "opaque_password_reset_邀请";
  const createData = {
    invite_url: `https://app.example.test/register?token=${encodeURIComponent(inviteToken)}`,
    token: inviteToken,
    expires_at: "2030-01-08T00:00:00Z",
    project_id: projectId,
    project_name: "Vision Project",
    project_member_role: "annotator",
  };
  const created = decodeInvitationResponse(
    create,
    testReply(create, createData, principal),
    200,
    "application/json",
  );
  if (!created.ok || created.data.token !== inviteToken) fail();
  checks += 1;

  const batchRequest = makeRequest("preview_invitation_batch", credentialId, token, {
    items: [
      {
        email: "invitee@example.test",
        role: "employee",
        group_name: null,
        project_id: null,
        project_member_role: null,
      },
    ],
  });
  const previewData = {
    items: [
      {
        index: 0n,
        email: "invitee@example.test",
        ok: true,
        retryable: false,
        invitation_id: inviteId,
        token: null,
        invite_url: null,
        project_id: null,
        project_name: null,
        error: null,
      },
    ],
    succeeded: 1n,
    failed: 0n,
    preview: true,
  };
  if (
    !decodeInvitationResponse(
      batchRequest,
      testReply(batchRequest, previewData, principal),
      200,
      "application/json",
    ).ok
  ) {
    fail();
  }
  checks += 1;
  expectFailure(() =>
    decodeInvitationResponse(
      batchRequest,
      testReply(
        batchRequest,
        {
          ...previewData,
          items: [{ ...previewData.items[0], token: inviteToken }],
        },
        principal,
      ),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const listRequest = makeRequest("list_invitations", credentialId, token, {
    status: "all",
    scope: "me",
  });
  const longList = Array.from({ length: 1500 }, (_, index) =>
    testInvitation(`${String(index).padStart(8, "0")}-1111-4111-8111-111111111111`, {
      created_at: `2030-01-01T00:00:${String(index % 60).padStart(2, "0")}Z`,
    }),
  );
  const listResult = decodeInvitationResponse(
    listRequest,
    testReply(listRequest, { items: longList }, principal),
    200,
    "application/json",
  );
  if (listResult.data.items.length !== 1500) fail();
  checks += 1;

  const queryRequest = makeRequest("query_invitations", credentialId, token, {
    ...listFilters,
    page: 1n,
    page_size: 1n,
  });
  const wideTotal = 9007199254740993n;
  const queryResult = decodeInvitationResponse(
    queryRequest,
    testReply(
      queryRequest,
      {
        items: [invitation],
        total: wideTotal,
        page: 1n,
        page_size: 1n,
        pages: wideTotal,
      },
      principal,
    ),
    200,
    "application/json",
  );
  if (queryResult.data.total !== wideTotal) fail();
  checks += 1;
  expectFailure(() =>
    decodeInvitationResponse(
      queryRequest,
      testReply(
        queryRequest,
        {
          items: [invitation],
          total: 1n,
          page: 1n,
          page_size: 1n,
          pages: 1n,
          extra: true,
        },
        principal,
      ),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const statsRequest = makeRequest("invitation_stats", credentialId, token, listFilters);
  if (
    !decodeInvitationResponse(
      statsRequest,
      testReply(
        statsRequest,
        {
          total: 4n,
          pending: 1n,
          accepted: 1n,
          expired: 1n,
          revoked: 1n,
        },
        principal,
      ),
      200,
      "application/json",
    ).ok
  ) {
    fail();
  }
  checks += 1;

  const revokeRequest = makeRequest("revoke_invitation", credentialId, token, {
    invitation_id: inviteId,
  });
  if (
    !decodeInvitationResponse(
      revokeRequest,
      testReply(revokeRequest, {}, principal),
      200,
      "application/json",
    ).ok
  ) {
    fail();
  }
  checks += 1;

  for (const operation of ["revoke_invitation", "resend_invitation"]) {
    const missingRequest = makeRequest(operation, credentialId, token, {
      invitation_id: inviteId,
    });
    const missingReply = stringifyLossless({
      schema,
      ok: false,
      operation,
      operation_id: credentialId,
      error: { code: "invitation_not_found", status_code: 404, detail: "邀请不存在" },
    });
    if (decodeInvitationResponse(missingRequest, missingReply, 200, "application/json").ok) {
      fail();
    }
    checks += 1;
    const falsePositiveReply = stringifyLossless({
      schema,
      ok: false,
      operation,
      operation_id: credentialId,
      error: { code: "invitation_not_found", status_code: 404, detail: "邀请链接无效" },
    });
    expectFailure(() =>
      decodeInvitationResponse(missingRequest, falsePositiveReply, 200, "application/json"),
    );
    checks += 1;
  }

  const duplicateBatch = makeRequest("apply_invitation_batch", credentialId, token, {
    items: [
      {
        email: "same@example.test",
        role: "employee",
        group_name: null,
        project_id: null,
        project_member_role: null,
      },
      {
        email: "same@example.test",
        role: "employee",
        group_name: null,
        project_id: null,
        project_member_role: null,
      },
    ],
  });
  const appliedBatch = {
    items: [
      {
        index: 0n,
        email: "same@example.test",
        ok: true,
        retryable: false,
        invitation_id: inviteId,
        token: inviteToken,
        invite_url: `https://app.example.test/register?token=${encodeURIComponent(inviteToken)}`,
        project_id: null,
        project_name: null,
        error: null,
      },
      {
        index: 1n,
        email: "same@example.test",
        ok: false,
        retryable: true,
        invitation_id: null,
        token: null,
        invite_url: null,
        project_id: null,
        project_name: null,
        error: "本批次中邮箱重复",
      },
    ],
    succeeded: 1n,
    failed: 1n,
    preview: false,
  };
  if (
    !decodeInvitationResponse(
      duplicateBatch,
      testReply(duplicateBatch, appliedBatch, principal),
      200,
      "application/json",
    ).ok
  ) {
    fail();
  }
  checks += 1;

  const legacyToken = makeTokenFromClaimsJson(
    JSON.stringify({
      sub: actorId,
      role: "reviewer",
      exp: 1_893_456_000,
      jti: credentialId,
      iss: issuer,
      extra_legacy_claim: "accepted by server, ignored by role fence",
    }),
  );
  const legacyList = makeRequest("list_invitations", credentialId, legacyToken, {
    status: "all",
    scope: "me",
  });
  const legacyResult = decodeInvitationResponse(
    legacyList,
    testReply(legacyList, { items: [] }, principal),
    200,
    "application/json",
  );
  if (legacyResult.principal.generation_decimal !== "0") fail();
  checks += 1;

  const floatGeneration = makeTokenFromClaimsJson(
    `{"sub":"${actorId}","role":"super_admin","exp":1893456000,"jti":"${credentialId}","gen":0.0,"iss":"${issuer}"}`,
  );
  const floatList = makeRequest("list_invitations", credentialId, floatGeneration, {
    status: "all",
    scope: "me",
  });
  expectFailure(() =>
    decodeInvitationResponse(
      floatList,
      testReply(floatList, [], principal),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const registerToken = makeToken(credentialId, credentialId, 0n, "employee");
  const registerRequest = makeRequest("register_invitation", credentialId, null, {
    invitation_token: "opaque-proof_注册",
    name: "New invited user",
    password: "InvitationStrong123",
  });
  const registerData = {
    access_token: registerToken,
    token_type: "bearer",
    user: testUserRecord(credentialId),
    email_verification_required: false,
    acceptance: {
      project_id: null,
      project_name: null,
      project_member_role: null,
      next_action: "wait_for_allocation",
      next_action_label: "等待管理员分配项目",
      responsible_person_name: null,
      active_batch_count: 0n,
    },
  };
  const registerResult = decodeInvitationResponse(
    registerRequest,
    testReply(registerRequest, registerData, null),
    200,
    "application/json",
  );
  if (!registerResult.ok || registerResult.data.user.id !== credentialId) fail();
  checks += 1;
  const leakedProof = JSON.stringify({
    schema,
    ok: false,
    operation: "register_invitation",
    operation_id: credentialId,
    error: { code: "policy_rejected", status_code: 410, detail: "opaque-proof_注册" },
  });
  expectFailure(() =>
    decodeInvitationResponse(registerRequest, leakedProof, 200, "application/json"),
  );
  checks += 1;

  const duplicateReply = testReply(create, createData, principal).replace(
    '"ok":true',
    '"ok":true,"ok":true',
  );
  expectFailure(() => decodeInvitationResponse(create, duplicateReply, 200, "application/json"));
  checks += 1;
  const mismatched = testReply(create, createData, testPrincipal(memberId, credentialId));
  expectFailure(() => decodeInvitationResponse(create, mismatched, 200, "application/json"));
  checks += 1;

  return { ok: true, checks };
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
    } catch (error) {
      process.stderr.write(`Identity invitation peer self-check failed: ${error.stack}\n`);
      process.exitCode = 1;
    }
  } else {
    main().catch(() => {
      process.stderr.write("Identity invitation peer failed\n");
      process.exitCode = 1;
    });
  }
}
