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

const schema = "aap-identity-groups/v1";
const issuer = "aap-identity/v1";
const endpoint = "/v1/identity/groups/execute";
const authenticateEndpoint = "/v1/identity/authenticate";
const operations = new Set([
  "list_groups",
  "query_groups",
  "create_group",
  "update_group",
  "delete_group",
  "preview_group_assignments",
  "apply_group_assignments",
  "assign_user_group",
]);
const managerRoles = new Set(["super_admin", "project_admin"]);
const roles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
const failureCodes = new Set([
  "invalid_request",
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
  "group_not_found",
  "target_not_found",
  "group_busy",
  "account_busy",
  "operation_conflict",
  "policy_rejected",
]);
const maxInt64 = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799n;
const outOfScope = "用户不存在或不在管理范围内";
const groupNotFound = "数据组不存在";
const safeApplyErrors = new Set([outOfScope, "account_busy", "group_busy", "处理失败，请稍后重试"]);

function fail() {
  throw new Error("Invalid Identity group request or response");
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

function rawAtPath(source, spans, path) {
  const span = spans.get(pathKey(path));
  return span ? source.slice(span.start, span.end) : undefined;
}

function strictInteger(source, spans, document, path, { min = 0n, max = null, allowNull = false }) {
  const value = getPath(document, path);
  if (value === null && allowNull) return null;
  if (value === null || value === undefined) fail();
  if (typeof value !== "number") fail();
  const raw = rawAtPath(source, spans, path);
  if (typeof raw !== "string" || !/^(0|[1-9][0-9]*)$/.test(raw)) fail();
  const integer = BigInt(raw);
  if (integer < min || (max !== null && integer > max)) fail();
  setPath(document, path, integer);
  return integer;
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
    if (spans.has(pathKey(spec.path))) {
      strictInteger(source, spans, document, spec.path, spec);
    }
  }
  return { document, spans };
}

function responseIntegerPaths(request, document) {
  const paths = [];
  if (!isRecord(document) || document.ok !== true || !isRecord(document.data)) {
    paths.push({ path: ["error", "status_code"], max: 599n, allowNull: true });
    return paths;
  }
  if (isRecord(document.principal)) {
    paths.push({
      path: ["principal", "generation"],
      max: maxInt64,
      allowNull: true,
    });
  }
  const data = document.data;
  const addGroupCounts = (path, group) => {
    if (isRecord(group)) paths.push({ path: [...path, "member_count"], max: maxInt64 });
  };
  const addItems = (prefix, items, callback) => {
    if (Array.isArray(items)) {
      items.forEach((item, index) => callback([...prefix, index], item));
    }
  };
  switch (request.request.operation) {
    case "list_groups":
      addItems(["data", "items"], data.items, addGroupCounts);
      break;
    case "query_groups":
      paths.push(
        { path: ["data", "total"], min: 0n, max: maxInt64 },
        { path: ["data", "page"], min: 1n },
        { path: ["data", "page_size"], min: 1n, max: 500n },
        { path: ["data", "pages"], min: 0n, max: maxInt64 },
      );
      addItems(["data", "items"], data.items, addGroupCounts);
      break;
    case "create_group":
    case "update_group":
      addGroupCounts(["data", "group"], data.group);
      break;
    case "preview_group_assignments":
      paths.push(
        { path: ["data", "applicable"], max: maxInt64 },
        { path: ["data", "blocked"], max: maxInt64 },
      );
      break;
    case "apply_group_assignments":
      paths.push(
        { path: ["data", "succeeded"], max: maxInt64 },
        { path: ["data", "failed"], max: maxInt64 },
      );
      break;
  }
  return paths;
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  const limits = { method: 8, path: 256, ip: 64, request_id: 36 };
  for (const [key, max] of Object.entries(limits)) {
    if (value[key] !== null && !boundedString(value[key], 0, max)) fail();
  }
  if (value.ip && isIP(value.ip) === 0) fail();
}

function pythonStrip(value) {
  return value.replace(
    /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu,
    "",
  );
}

function nullableString(value, max) {
  return value === null || boundedString(value, 0, max);
}

function nullableUuid(value) {
  return value === null ? null : uuid(value);
}

export function validateGroupRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "token", "audit_context", "payload"]) ||
    value.schema !== schema ||
    !operations.has(value.operation)
  ) {
    fail();
  }
  value.operation_id = uuid(value.operation_id);
  if (typeof value.token !== "string" || value.token.length < 1 || value.token.length > 16_384) {
    fail();
  }
  validateAuditContext(value.audit_context);
  const payload = value.payload;
  switch (value.operation) {
    case "list_groups":
      if (!exact(payload, [])) fail();
      break;
    case "query_groups":
      if (
        !exact(payload, ["page", "page_size", "search"]) ||
        typeof payload.page !== "bigint" ||
        payload.page < 1n ||
        typeof payload.page_size !== "bigint" ||
        payload.page_size < 1n ||
        payload.page_size > 500n ||
        (payload.search !== null && typeof payload.search !== "string")
      ) {
        fail();
      }
      break;
    case "create_group":
      if (
        !exact(payload, ["name", "description"]) ||
        !boundedString(payload.name, 1, 100) ||
        !nullableString(payload.description, 500)
      ) {
        fail();
      }
      break;
    case "update_group":
      if (
        !exact(payload, ["group_id", "name", "description"]) ||
        (payload.name !== null && !boundedString(payload.name, 1, 100)) ||
        !nullableString(payload.description, 500)
      ) {
        fail();
      }
      payload.group_id = uuid(payload.group_id);
      break;
    case "delete_group":
      if (!exact(payload, ["group_id"])) fail();
      payload.group_id = uuid(payload.group_id);
      break;
    case "preview_group_assignments":
    case "apply_group_assignments":
      if (
        !exact(payload, ["user_ids", "group_id"]) ||
        !Array.isArray(payload.user_ids) ||
        payload.user_ids.length < 1 ||
        payload.user_ids.length > 500
      ) {
        fail();
      }
      payload.user_ids = payload.user_ids.map((value) => uuid(value));
      payload.group_id = nullableUuid(payload.group_id);
      break;
    case "assign_user_group":
      if (!exact(payload, ["user_id", "group_id"])) fail();
      payload.user_id = uuid(payload.user_id);
      payload.group_id = nullableUuid(payload.group_id);
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

function decodeJwtJson(segment, intSpecs = []) {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let source;
  try {
    source = decoder.decode(decodeBase64Url(segment));
  } catch {
    fail();
  }
  return parseStrictJson(source, intSpecs).document;
}

function incomingJwtClaims(token) {
  if (typeof token !== "string" || token.length > 16_384) fail();
  const parts = token.split(".");
  if (parts.length !== 3 || decodeBase64Url(parts[2]).length !== 32) fail();
  const header = decodeJwtJson(parts[0]);
  if (!exact(header, ["alg", "typ"]) || header.alg !== "HS256" || header.typ !== "JWT") {
    fail();
  }
  const claims = decodeJwtJson(parts[1], [{ path: ["gen"], min: 0n, max: maxInt64 }]);
  if (
    !isRecord(claims) ||
    typeof claims.exp !== "number" ||
    !Number.isSafeInteger(claims.exp) ||
    claims.exp < 1 ||
    claims.exp > Number(maxPythonEpochSeconds) ||
    claims.iss !== issuer
  ) {
    fail();
  }
  const generation = claims.gen === undefined ? 0n : claims.gen;
  if (typeof generation !== "bigint") fail();
  const sub = uuid(claims.sub);
  const jti = uuid(claims.jti);
  const expiry = new Date(claims.exp * 1000);
  if (!Number.isFinite(expiry.valueOf()) || !validDateTime(expiry.toISOString())) fail();
  return {
    actorId: sub,
    credentialId: jti,
    generation,
    expiry: claims.exp,
  };
}

function validateGroupRecord(value) {
  if (!exact(value, ["id", "name", "description", "member_count", "created_at"])) fail();
  value.id = uuid(value.id);
  if (
    !boundedString(value.name, 1, 100) ||
    !nullableString(value.description, 500) ||
    typeof value.member_count !== "bigint" ||
    value.member_count < 0n ||
    !validDateTime(value.created_at)
  ) {
    fail();
  }
  return value;
}

function validateAccountRecord(value) {
  const names = [
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
  if (!exact(value, names)) fail();
  value.id = uuid(value.id);
  if (
    !boundedString(value.email, 0, 255) ||
    !boundedString(value.name, 0, 100) ||
    typeof value.role !== "string" ||
    !roles.has(value.role) ||
    !nullableString(value.group_name, 100) ||
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
  value.group_id = nullableUuid(value.group_id);
  if (value.disabled_by !== null) value.disabled_by = uuid(value.disabled_by);
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
  return value;
}

function validatePreviewItem(value) {
  const names = [
    "user_id",
    "email",
    "name",
    "ok",
    "current_group_id",
    "current_group_name",
    "next_group_id",
    "next_group_name",
    "error",
  ];
  if (!exact(value, names)) fail();
  value.user_id = uuid(value.user_id);
  value.current_group_id = nullableUuid(value.current_group_id);
  value.next_group_id = nullableUuid(value.next_group_id);
  if (
    !nullableString(value.email, 255) ||
    !nullableString(value.name, 100) ||
    typeof value.ok !== "boolean" ||
    !nullableString(value.current_group_name, 100) ||
    !nullableString(value.next_group_name, 100) ||
    !nullableString(value.error, 1000) ||
    (value.ok && value.error !== null) ||
    (!value.ok && value.error === null)
  ) {
    fail();
  }
  if (value.ok) {
    if (value.email === null || value.name === null || value.error !== null) fail();
  } else if (value.error === outOfScope) {
    if (
      [
        value.email,
        value.name,
        value.current_group_id,
        value.current_group_name,
        value.next_group_id,
        value.next_group_name,
      ].some((item) => item !== null)
    ) {
      fail();
    }
  } else if (value.error === groupNotFound) {
    if (
      value.email === null ||
      value.name === null ||
      [
        value.current_group_id,
        value.current_group_name,
        value.next_group_id,
        value.next_group_name,
      ].some((item) => item !== null)
    ) {
      fail();
    }
  } else {
    fail();
  }
  return value;
}

function validateApplyItem(value) {
  if (!exact(value, ["user_id", "ok", "retryable", "error"])) fail();
  value.user_id = uuid(value.user_id);
  if (
    typeof value.ok !== "boolean" ||
    typeof value.retryable !== "boolean" ||
    !nullableString(value.error, 1000) ||
    (value.ok && (value.error !== null || value.retryable)) ||
    (!value.ok && (!value.retryable || !safeApplyErrors.has(value.error)))
  ) {
    fail();
  }
  return value;
}

function validateGroupFailure(request, error) {
  if (
    !exact(error, ["code", "status_code", "detail"]) ||
    typeof error.code !== "string" ||
    !failureCodes.has(error.code)
  ) {
    fail();
  }
  if (
    typeof error.detail === "string" &&
    request.request.token.length >= 8 &&
    error.detail.includes(request.request.token)
  ) {
    fail();
  }
  if (error.code !== "policy_rejected") {
    if (error.status_code !== null || error.detail !== null) fail();
    const operation = request.request.operation;
    const payload = request.request.payload;
    if (error.code === "group_not_found") {
      if (
        !(
          ["update_group", "delete_group"].includes(operation) ||
          (operation === "assign_user_group" && payload.group_id !== null) ||
          (operation === "apply_group_assignments" && payload.group_id !== null)
        )
      ) {
        fail();
      }
    }
    if (
      error.code === "group_busy" &&
      ![
        "create_group",
        "update_group",
        "delete_group",
        "assign_user_group",
        "apply_group_assignments",
      ].includes(operation)
    ) {
      fail();
    }
    if (error.code === "target_not_found" && operation !== "assign_user_group") fail();
    if (
      error.code === "operation_conflict" &&
      ![
        "create_group",
        "update_group",
        "delete_group",
        "assign_user_group",
        "apply_group_assignments",
      ].includes(operation)
    ) {
      fail();
    }
    return error;
  }
  if (typeof error.status_code !== "bigint" || typeof error.detail !== "string") fail();
  if (error.status_code === 400n) {
    if (
      !["create_group", "update_group"].includes(request.request.operation) ||
      error.detail !== "名称不能为空"
    ) {
      fail();
    }
    const name = request.request.payload.name;
    if (name === null || pythonStrip(name) !== "") fail();
  } else if (error.status_code === 409n) {
    if (!["create_group", "update_group"].includes(request.request.operation)) fail();
    const rawName = request.request.payload.name;
    if (typeof rawName !== "string") fail();
    const expectedName =
      request.request.operation === "create_group" ? pythonStrip(rawName) : rawName;
    if (error.detail !== `已存在同名数据组: ${expectedName}`) fail();
  } else if (error.status_code === 403n) {
    if (error.detail !== "当前账号已无管理权限") fail();
  } else {
    fail();
  }
  return error;
}

function validateGroupData(request, data) {
  const payload = request.request.payload;
  switch (request.request.operation) {
    case "list_groups":
      if (!exact(data, ["items"]) || !Array.isArray(data.items)) fail();
      data.items = data.items.map(validateGroupRecord);
      return data;
    case "query_groups": {
      if (
        !exact(data, ["items", "total", "page", "page_size", "pages"]) ||
        !Array.isArray(data.items)
      ) {
        fail();
      }
      data.items = data.items.map(validateGroupRecord);
      if (
        typeof data.total !== "bigint" ||
        data.total < 0n ||
        typeof data.page !== "bigint" ||
        data.page < 1n ||
        typeof data.page_size !== "bigint" ||
        data.page_size < 1n ||
        data.page_size > 500n ||
        typeof data.pages !== "bigint" ||
        data.pages < 0n ||
        data.page !== payload.page ||
        data.page_size !== payload.page_size ||
        BigInt(data.items.length) > data.page_size
      ) {
        fail();
      }
      const expectedPages =
        data.total === 0n ? 0n : (data.total + data.page_size - 1n) / data.page_size;
      if (data.pages !== expectedPages) fail();
      return data;
    }
    case "create_group":
      if (!exact(data, ["group"])) fail();
      validateGroupRecord(data.group);
      if (
        data.group.id !== request.operationId ||
        data.group.name !== pythonStrip(payload.name) ||
        data.group.description !== (payload.description || null)
      ) {
        fail();
      }
      return data;
    case "update_group":
      if (!exact(data, ["group"])) fail();
      validateGroupRecord(data.group);
      if (
        data.group.id !== payload.group_id ||
        (payload.name !== null && data.group.name !== pythonStrip(payload.name)) ||
        (payload.description !== null && data.group.description !== (payload.description || null))
      ) {
        fail();
      }
      return data;
    case "delete_group":
      if (
        !exact(data, ["group_id", "deleted"]) ||
        uuid(data.group_id) !== payload.group_id ||
        data.deleted !== true
      ) {
        fail();
      }
      data.group_id = payload.group_id;
      return data;
    case "preview_group_assignments": {
      if (
        !exact(data, ["group_id", "group_name", "items", "applicable", "blocked"]) ||
        !Array.isArray(data.items) ||
        nullableUuid(data.group_id) !== payload.group_id ||
        !nullableString(data.group_name, 100) ||
        typeof data.applicable !== "bigint" ||
        data.applicable < 0n ||
        typeof data.blocked !== "bigint" ||
        data.blocked < 0n ||
        data.items.length !== payload.user_ids.length
      ) {
        fail();
      }
      data.group_id = payload.group_id;
      data.items = data.items.map((item, index) => {
        validatePreviewItem(item);
        if (item.user_id !== payload.user_ids[index]) fail();
        if (
          item.ok &&
          (item.next_group_id !== payload.group_id ||
            (payload.group_id === null && item.next_group_name !== null) ||
            (payload.group_id !== null && item.next_group_name === null))
        ) {
          fail();
        }
        if (!item.ok && item.error === groupNotFound && payload.group_id === null) {
          fail();
        }
        return item;
      });
      const applicable = BigInt(data.items.filter((item) => item.ok).length);
      const blocked = BigInt(data.items.length) - applicable;
      if (data.applicable !== applicable || data.blocked !== blocked) fail();
      const expectedGroupName = data.items.find((item) => item.ok)?.next_group_name ?? null;
      if (data.group_name !== expectedGroupName) fail();
      return data;
    }
    case "apply_group_assignments": {
      if (
        !exact(data, ["items", "succeeded", "failed"]) ||
        !Array.isArray(data.items) ||
        typeof data.succeeded !== "bigint" ||
        data.succeeded < 0n ||
        typeof data.failed !== "bigint" ||
        data.failed < 0n ||
        data.items.length !== payload.user_ids.length
      ) {
        fail();
      }
      data.items = data.items.map((item, index) => {
        validateApplyItem(item);
        if (item.user_id !== payload.user_ids[index]) fail();
        return item;
      });
      const succeeded = BigInt(data.items.filter((item) => item.ok).length);
      const failed = BigInt(data.items.length) - succeeded;
      if (data.succeeded !== succeeded || data.failed !== failed) fail();
      return data;
    }
    case "assign_user_group":
      if (!exact(data, ["account"])) fail();
      validateAccountRecord(data.account);
      if (
        data.account.id !== payload.user_id ||
        data.account.is_active !== true ||
        data.account.group_id !== payload.group_id ||
        (payload.group_id === null && data.account.group_name !== null)
      ) {
        fail();
      }
      return data;
  }
  fail();
}

function bindPrincipal(raw, receipt, claims, expectedApiKey) {
  if (!managerRoles.has(receipt.role)) fail();
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

export function decodeGroupResponse(request, source, status, contentType, expectedApiKey = null) {
  if (
    typeof source !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const parsed = parseStrictJson(source);
  const document = parsed.document;
  const integerPaths = responseIntegerPaths(request, document);
  for (const spec of integerPaths) {
    if (parsed.spans.has(pathKey(spec.path))) {
      strictInteger(source, parsed.spans, document, spec.path, spec);
    }
  }
  if (!isRecord(document) || document.schema !== schema || typeof document.ok !== "boolean") fail();
  if (document.ok === false) {
    if (!exact(document, ["schema", "ok", "error"])) fail();
    const error = validateGroupFailure(request, document.error);
    return {
      receipt: {
        status,
        schema,
        ok: false,
        operation: request.request.operation,
        operation_id: request.operationId,
        error: {
          code: error.code,
          status_code: error.status_code,
          detail: error.detail,
        },
      },
    };
  }
  if (
    !exact(document, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    document.operation !== request.request.operation ||
    uuid(document.operation_id) !== request.operationId
  ) {
    fail();
  }
  const claims = request.request.token.startsWith("ak_")
    ? null
    : incomingJwtClaims(request.request.token);
  const principal = principalReceipt(document.principal);
  bindPrincipal(document.principal, principal, claims, expectedApiKey);
  const data = validateGroupData(request, document.data);
  return {
    receipt: {
      status,
      schema,
      ok: true,
      operation: document.operation,
      operation_id: request.operationId,
      principal,
      data,
    },
  };
}

function stringifyLossless(value) {
  if (typeof value === "bigint") return value.toString(10);
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stringifyLossless).join(",")}]`;
  return `{${Object.keys(value)
    .map((key) => `${JSON.stringify(key)}:${stringifyLossless(value[key])}`)
    .join(",")}}`;
}

async function postGroupRequest(origin, controlToken, path, payload) {
  const endpointUrl = new URL(path, origin);
  const transport = endpointUrl.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(stringifyLossless(payload), "utf8");
  return new Promise((resolveResponse, rejectResponse) => {
    const request = transport(
      endpointUrl,
      {
        method: "POST",
        agent: false,
        signal: AbortSignal.timeout(60_000),
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
            const source = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              text: source,
            });
          } catch {
            rejectResponse(new Error("Invalid Identity group response"));
          }
        });
        response.on("error", () => rejectResponse(new Error("Identity group request failed")));
      },
    );
    request.on("error", () => rejectResponse(new Error("Identity group request failed")));
    request.end(body);
  });
}

function validatePeerInput(value) {
  if (!exact(value, ["url", "control_token", "request"])) fail();
  const request = validateGroupRequest(value.request);
  const checked = validateInput({
    url: value.url,
    control_token: value.control_token,
    credential: request.request.token,
  });
  return { request, origin: checked.origin, controlToken: checked.controlToken };
}

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  let source;
  try {
    source = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch {
    fail();
  }
  const parsed = parseStrictJson(source, [
    { path: ["request", "payload", "page"], min: 1n },
    { path: ["request", "payload", "page_size"], min: 1n, max: 500n },
  ]);
  return parsed.document;
}

async function apiKeyPrincipal(origin, controlToken, token) {
  if (!/^ak_[A-Za-z0-9_-]{32}$/.test(token)) fail();
  const response = await postIdentityRequest(origin, controlToken, "/v1/identity/authenticate", {
    token,
  });
  parseStrictJson(response.text, [
    { path: ["data", "generation"], max: maxInt64, allowNull: true },
  ]);
  const identity = parseIdentityReceipt(response.text, response.status, response.contentType);
  return identity.ok ? identity.principal : null;
}

async function main() {
  const input = await readInput();
  const { request, origin, controlToken } = validatePeerInput(input);
  const expectedApiKey = request.request.token.startsWith("ak_")
    ? await apiKeyPrincipal(origin, controlToken, request.request.token)
    : null;
  const response = await postGroupRequest(origin, controlToken, endpoint, request.request);
  const result = decodeGroupResponse(
    request,
    response.text,
    response.status,
    response.contentType,
    expectedApiKey,
  );
  process.stdout.write(`${stringifyLossless(result.receipt)}\n`);
}

function makeAuditContext() {
  return {
    method: "POST",
    path: "/api/v1/groups",
    ip: "127.0.0.1",
    request_id: null,
  };
}

function makeTokenFromClaimsJson(claimsJson) {
  const input =
    Buffer.from('{"alg":"HS256","typ":"JWT"}').toString("base64url") +
    "." +
    Buffer.from(claimsJson).toString("base64url");
  return `${input}.${Buffer.alloc(32, 3).toString("base64url")}`;
}

function makeToken(actorId, credentialId) {
  const claims = {
    sub: actorId,
    role: "super_admin",
    exp: 1_893_456_000n,
    jti: credentialId,
    gen: 0n,
    iss: issuer,
  };
  return makeTokenFromClaimsJson(stringifyLossless(claims));
}

function makeRequest(operation, operationId, token, payload = {}) {
  return validateGroupRequest({
    schema,
    operation,
    operation_id: operationId,
    token,
    audit_context: makeAuditContext(),
    payload,
  });
}

function testGroup(id, overrides = {}) {
  return {
    id,
    name: "Vision",
    description: "Computer vision team",
    member_count: 9007199254740993n,
    created_at: "2025-01-01T00:00:00Z",
    ...overrides,
  };
}

function testAccount(id, overrides = {}) {
  return {
    id,
    email: "target@example.test",
    name: "Target user",
    role: "employee",
    group_name: "Vision",
    group_id: "22222222-2222-4222-8222-222222222222",
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
    created_at: "2024-01-01T00:00:00Z",
    ...overrides,
  };
}

function testPrincipal(actorId, credentialId) {
  return {
    actor_id: actorId,
    name: "Current manager",
    email: "manager@example.test",
    role: "super_admin",
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation: 0n,
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
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
  const targetId = "33333333-3333-4333-8333-333333333333";
  const groupId = "44444444-4444-4444-8444-444444444444";
  const token = makeToken(actorId, credentialId);
  const principal = testPrincipal(actorId, credentialId);
  const group = testGroup(groupId);
  const previewItem = {
    user_id: targetId,
    email: "target@example.test",
    name: "Target user",
    ok: true,
    current_group_id: null,
    current_group_name: null,
    next_group_id: groupId,
    next_group_name: "Vision",
    error: null,
  };
  const operationsToCheck = [
    ["list_groups", {}, { items: [group] }],
    [
      "query_groups",
      { page: 1n, page_size: 1n, search: "Vision" },
      {
        items: [group],
        total: 9007199254740993n,
        page: 1n,
        page_size: 1n,
        pages: 9007199254740993n,
      },
    ],
    [
      "create_group",
      { name: " New Vision ", description: "Computer vision team" },
      { group: testGroup("55555555-5555-4555-8555-555555555555", { name: "New Vision" }) },
    ],
    [
      "update_group",
      { group_id: groupId, name: "Renamed Vision", description: "" },
      { group: testGroup(groupId, { name: "Renamed Vision", description: null }) },
    ],
    ["delete_group", { group_id: groupId }, { group_id: groupId, deleted: true }],
    [
      "preview_group_assignments",
      { user_ids: [targetId], group_id: groupId },
      {
        group_id: groupId,
        group_name: "Vision",
        items: [previewItem],
        applicable: 1n,
        blocked: 0n,
      },
    ],
    [
      "apply_group_assignments",
      { user_ids: [targetId], group_id: groupId },
      {
        items: [{ user_id: targetId, ok: true, retryable: false, error: null }],
        succeeded: 1n,
        failed: 0n,
      },
    ],
    [
      "assign_user_group",
      { user_id: targetId, group_id: groupId },
      { account: testAccount(targetId, { group_id: groupId }) },
    ],
  ];
  for (let index = 0; index < operationsToCheck.length; index += 1) {
    const [operation, payload, data] = operationsToCheck[index];
    const operationId = index === 2 ? "55555555-5555-4555-8555-555555555555" : credentialId;
    const request = makeRequest(operation, operationId, token, payload);
    const receipt = decodeGroupResponse(
      request,
      testReply(request, data, principal),
      200,
      "application/json",
    ).receipt;
    if (!receipt.ok || receipt.operation !== operation) fail();
    checks += 1;
  }

  const unassignPreview = makeRequest("preview_group_assignments", credentialId, token, {
    user_ids: [targetId],
    group_id: null,
  });
  const unassignPreviewItem = {
    ...previewItem,
    current_group_id: groupId,
    current_group_name: "Vision",
    next_group_id: null,
    next_group_name: null,
  };
  const unassignPreviewResult = decodeGroupResponse(
    unassignPreview,
    testReply(
      unassignPreview,
      {
        group_id: null,
        group_name: null,
        items: [unassignPreviewItem],
        applicable: 1n,
        blocked: 0n,
      },
      principal,
    ),
    200,
    "application/json",
  );
  if (unassignPreviewResult.receipt.data.group_id !== null) fail();
  checks += 1;

  const missingGroupPreview = makeRequest("preview_group_assignments", credentialId, token, {
    user_ids: [targetId],
    group_id: groupId,
  });
  const missingGroupItem = {
    ...previewItem,
    ok: false,
    current_group_id: null,
    current_group_name: null,
    next_group_id: null,
    next_group_name: null,
    error: groupNotFound,
  };
  const missingGroupData = {
    group_id: groupId,
    group_name: null,
    items: [missingGroupItem],
    applicable: 0n,
    blocked: 1n,
  };
  const missingGroupReceipt = decodeGroupResponse(
    missingGroupPreview,
    testReply(missingGroupPreview, missingGroupData, principal),
    200,
    "application/json",
  );
  if (missingGroupReceipt.receipt.data.items[0].next_group_id !== null) fail();
  checks += 1;
  expectFailure(() =>
    decodeGroupResponse(
      missingGroupPreview,
      testReply(
        missingGroupPreview,
        {
          ...missingGroupData,
          items: [{ ...missingGroupItem, next_group_id: groupId }],
        },
        principal,
      ),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const unassign = makeRequest("assign_user_group", credentialId, token, {
    user_id: targetId,
    group_id: null,
  });
  const unassignResult = decodeGroupResponse(
    unassign,
    testReply(
      unassign,
      {
        account: testAccount(targetId, { group_id: null, group_name: null }),
      },
      principal,
    ),
    200,
    "application/json",
  );
  if (unassignResult.receipt.data.account.group_id !== null) fail();
  checks += 1;

  const trimRequest = makeRequest("create_group", credentialId, token, {
    name: "\u3000New Vision\u3000",
    description: null,
  });
  if (pythonStrip(trimRequest.request.payload.name) !== "New Vision") fail();
  checks += 1;

  const widePage = 9007199254740993n;
  const widePageRequest = makeRequest("query_groups", credentialId, token, {
    page: widePage,
    page_size: 1n,
    search: null,
  });
  const widePageReply = decodeGroupResponse(
    widePageRequest,
    testReply(
      widePageRequest,
      { items: [], total: 0n, page: widePage, page_size: 1n, pages: 0n },
      principal,
    ),
    200,
    "application/json",
  );
  if (widePageReply.receipt.data.page !== widePage) fail();
  checks += 1;

  const duplicateRequest = '{"operation":"list_groups","operation":"query_groups"}';
  expectFailure(() => parseStrictJson(duplicateRequest));
  checks += 1;
  const duplicateReply = `{"schema":"${schema}","schema":"${schema}"}`;
  expectFailure(() => decodeGroupResponse(list, duplicateReply, 200, "application/json"));
  checks += 1;
  expectFailure(() =>
    validateGroupRequest({
      schema,
      operation: "query_groups",
      operation_id: credentialId,
      token,
      audit_context: makeAuditContext(),
      payload: { page: 1, page_size: 1, search: null },
    }),
  );
  checks += 1;
  expectFailure(() =>
    parseStrictJson('{"request":{"payload":{"page":1.0}}}', [
      { path: ["request", "payload", "page"], min: 1n },
    ]),
  );
  checks += 1;

  const manyGroups = Array.from({ length: 1500 }, (_, index) =>
    testGroup(`${String(index).padStart(8, "0")}-1111-4111-8111-111111111111`),
  );
  const list = makeRequest("list_groups", credentialId, token);
  const manyReceipt = decodeGroupResponse(
    list,
    testReply(list, { items: manyGroups }, principal),
    200,
    "application/json",
  );
  if (manyReceipt.receipt.data.items.length !== 1500) fail();
  checks += 1;
  expectFailure(() =>
    decodeGroupResponse(
      list,
      testReply(list, { items: [{ ...group, extra: true }] }, principal),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const wrongAccount = testAccount(actorId);
  const assign = makeRequest("assign_user_group", credentialId, token, {
    user_id: targetId,
    group_id: groupId,
  });
  expectFailure(() =>
    decodeGroupResponse(
      assign,
      testReply(assign, { account: wrongAccount }, principal),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const wrongGroupName = makeRequest("create_group", credentialId, token, {
    name: "New group",
    description: null,
  });
  expectFailure(() =>
    decodeGroupResponse(
      wrongGroupName,
      testReply(
        wrongGroupName,
        { group: testGroup("55555555-5555-4555-8555-555555555555", { name: "Other" }) },
        principal,
      ),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const partial = makeRequest("apply_group_assignments", credentialId, token, {
    user_ids: [targetId, actorId, targetId],
    group_id: groupId,
  });
  const partialData = {
    items: [
      { user_id: targetId, ok: true, retryable: false, error: null },
      { user_id: actorId, ok: false, retryable: true, error: outOfScope },
      { user_id: targetId, ok: true, retryable: false, error: null },
    ],
    succeeded: 2n,
    failed: 1n,
  };
  if (
    decodeGroupResponse(
      partial,
      testReply(partial, partialData, principal),
      200,
      "application/json",
    ).receipt.data.failed !== 1n
  ) {
    fail();
  }
  checks += 1;
  expectFailure(() =>
    decodeGroupResponse(
      partial,
      testReply(
        partial,
        {
          ...partialData,
          items: [partialData.items[1], partialData.items[0], partialData.items[2]],
        },
        principal,
      ),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const mismatchPrincipal = testPrincipal(targetId, credentialId);
  const listRequest = makeRequest("list_groups", credentialId, token);
  expectFailure(() =>
    decodeGroupResponse(
      listRequest,
      testReply(listRequest, { items: [] }, mismatchPrincipal),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const conflict = {
    schema,
    ok: false,
    error: { code: "policy_rejected", status_code: 409, detail: "已存在同名数据组: New group" },
  };
  const createConflict = makeRequest("create_group", credentialId, token, {
    name: " New group ",
    description: null,
  });
  if (
    decodeGroupResponse(createConflict, JSON.stringify(conflict), 200, "application/json").receipt
      .error.code !== "policy_rejected"
  ) {
    fail();
  }
  checks += 1;
  expectFailure(() =>
    decodeGroupResponse(
      createConflict,
      JSON.stringify({
        ...conflict,
        error: { ...conflict.error, detail: "duplicate" },
      }),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const ordinaryFailure = makeRequest("update_group", credentialId, token, {
    group_id: groupId,
    name: null,
    description: null,
  });
  const busyReply = decodeGroupResponse(
    ordinaryFailure,
    JSON.stringify({
      schema,
      ok: false,
      error: { code: "group_busy", status_code: null, detail: null },
    }),
    200,
    "application/json",
  );
  if (busyReply.receipt.error.code !== "group_busy") fail();
  checks += 1;
  expectFailure(() =>
    decodeGroupResponse(
      ordinaryFailure,
      JSON.stringify({
        schema,
        ok: false,
        error: { code: "group_busy", status_code: null, detail: token },
      }),
      200,
      "application/json",
    ),
  );
  checks += 1;

  const busyEnvelope = JSON.stringify({
    schema,
    ok: false,
    error: { code: "group_busy", status_code: null, detail: null },
  });
  const busyWrites = [
    ["create_group", { name: "Busy group", description: null }],
    ["update_group", { group_id: groupId, name: null, description: null }],
    ["delete_group", { group_id: groupId }],
    ["assign_user_group", { user_id: targetId, group_id: null }],
    ["apply_group_assignments", { user_ids: [targetId], group_id: null }],
  ];
  for (const [operation, payload] of busyWrites) {
    const request = makeRequest(operation, credentialId, token, payload);
    const result = decodeGroupResponse(request, busyEnvelope, 200, "application/json");
    if (result.receipt.error.code !== "group_busy") fail();
    checks += 1;
  }
  expectFailure(() => decodeGroupResponse(listRequest, busyEnvelope, 200, "application/json"));
  checks += 1;

  for (const legacyRole of ["annotator", "reviewer"]) {
    const legacyToken = makeTokenFromClaimsJson(
      JSON.stringify({
        sub: actorId,
        role: legacyRole,
        exp: 1_893_456_000,
        jti: credentialId,
        iss: issuer,
        legacy_claim: "preserved by authentication, ignored for authorization",
      }),
    );
    const legacyRequest = makeRequest("list_groups", credentialId, legacyToken);
    const legacyResult = decodeGroupResponse(
      legacyRequest,
      testReply(legacyRequest, { items: [] }, principal),
      200,
      "application/json",
    );
    if (legacyResult.receipt.principal.generation_decimal !== "0") fail();
    checks += 1;
  }
  const integralFloatExpiry = makeTokenFromClaimsJson(
    `{"sub":"${actorId}","role":"reviewer","exp":1893456000.0,"jti":"${credentialId}","iss":"${issuer}","gen":0}`,
  );
  const floatExpiryRequest = makeRequest("list_groups", credentialId, integralFloatExpiry);
  decodeGroupResponse(
    floatExpiryRequest,
    testReply(floatExpiryRequest, { items: [] }, principal),
    200,
    "application/json",
  );
  checks += 1;
  const floatGeneration = makeTokenFromClaimsJson(
    `{"sub":"${actorId}","role":"annotator","exp":1893456000,"jti":"${credentialId}","iss":"${issuer}","gen":0.0}`,
  );
  const floatGenerationRequest = makeRequest("list_groups", credentialId, floatGeneration);
  expectFailure(() =>
    decodeGroupResponse(
      floatGenerationRequest,
      testReply(floatGenerationRequest, { items: [] }, principal),
      200,
      "application/json",
    ),
  );
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
    } catch {
      process.stderr.write("Identity groups peer self-check failed\n");
      process.exitCode = 1;
    }
  } else {
    main().catch(() => {
      process.stderr.write("Identity groups peer failed\n");
      process.exitCode = 1;
    });
  }
}
