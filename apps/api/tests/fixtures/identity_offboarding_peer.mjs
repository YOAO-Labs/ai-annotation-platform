import { createHash } from "node:crypto";
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
} from "./identity_admission_peer.mjs";

const schema = "aap-identity-offboarding/v1";
const issuer = "aap-identity/v1";
const endpoint = "/v1/identity/offboarding/execute";
const authEndpoint = "/v1/identity/authenticate";
const maxInt64 = (1n << 63n) - 1n;
const maxEpochSeconds = 253402300799n;
const managerRoles = new Set(["super_admin", "project_admin"]);
const roles = ["owner", "annotator", "reviewer"];
const activeStatuses = ["pending", "in_progress", "review", "rejected"];
const historicalKinds = new Set(["deleted", "historical_unknown"]);
const reactivatableKinds = new Set(["suspended", "emergency_suspended"]);
const authFailures = new Set([
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
]);
const fixedFailures = {
  manager_required: [403n, "当前账号已无管理权限"],
  target_not_found: [404n, "用户不存在"],
  self_offboard: [400n, "不能交接自己"],
  last_super_admin: [400n, "不能停用最后一名超级管理员"],
  target_role_forbidden: [403n, "项目管理员仅能处理标注员/审核员账号"],
  inactive_unmanaged: [403n, "该用户已停用且不在你管理的项目内，须由超级管理员处理"],
  scope_forbidden: [403n, "该用户存在不在你管理范围内的项目"],
  historical_target: [409n, "历史未知或已删除账号不可再次交接"],
  state_forbidden: [409n, "账号当前状态不可交接"],
  duplicate_project: [422n, "同一项目不能重复提交交接映射"],
  account_busy: [409n, "账号正在更新，请刷新后重试"],
  operation_conflict: [409n, null],
  unavailable: [503n, null],
};
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
const nullableUserDates = [
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
  throw new Error("Invalid Identity offboarding request or response");
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
  const result = BigInt(value.raw);
  if (result > maxInt64) fail();
  return result;
}

function nullableString(value, max = Number.MAX_SAFE_INTEGER) {
  return value === null || (typeof value === "string" && Array.from(value).length <= max);
}

function pythonStrip(value) {
  const whitespace =
    /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu;
  return value.replace(whitespace, "");
}

function normalizeReason(value) {
  const clipped = Array.from(pythonStrip(value)).slice(0, 500).join("");
  return clipped || null;
}

function validateAuditContext(value, operation, targetId) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  const preview = operation === "preview_offboarding";
  const expectedMethod = preview ? "GET" : "POST";
  const expectedPath = `/api/v1/users/${targetId}/offboarding${preview ? "-preview" : ""}`;
  if (
    value.method !== expectedMethod ||
    value.path !== expectedPath ||
    !nullableString(value.ip, 64) ||
    !nullableString(value.request_id, 36) ||
    (value.ip !== null && value.ip !== "" && isIP(value.ip) === 0)
  ) {
    fail();
  }
  if (value.request_id !== null) uuid(value.request_id);
}

function validateRequest(value) {
  if (
    !exact(value, ["schema", "operation", "operation_id", "token", "audit_context", "payload"]) ||
    value.schema !== schema ||
    !["preview_offboarding", "commit_offboarding"].includes(value.operation)
  ) {
    fail();
  }
  const operationId = uuid(value.operation_id);
  if (
    typeof value.token !== "string" ||
    Array.from(value.token).length < 1 ||
    Array.from(value.token).length > 16384
  )
    fail();
  const targetId = uuid(value.payload?.target_user_id);
  validateAuditContext(value.audit_context, value.operation, targetId);
  if (value.operation === "preview_offboarding") {
    if (!exact(value.payload, ["target_user_id"])) fail();
    return {
      request: { ...value, operation_id: operationId, payload: { target_user_id: targetId } },
      operationId,
      targetId,
    };
  }
  if (
    !exact(value.payload, ["target_user_id", "preview_version", "reason", "mode", "projects"]) ||
    typeof value.payload.preview_version !== "string" ||
    typeof value.payload.reason !== "string" ||
    Array.from(value.payload.reason).length > 500 ||
    !["handoff", "emergency_suspend"].includes(value.payload.mode) ||
    !Array.isArray(value.payload.projects)
  ) {
    fail();
  }
  const projects = value.payload.projects.map((project) => {
    if (
      !exact(project, [
        "project_id",
        "owner_receiver_id",
        "annotator_receiver_id",
        "reviewer_receiver_id",
      ])
    ) {
      fail();
    }
    return {
      project_id: uuid(project.project_id),
      owner_receiver_id:
        project.owner_receiver_id === null ? null : uuid(project.owner_receiver_id),
      annotator_receiver_id:
        project.annotator_receiver_id === null ? null : uuid(project.annotator_receiver_id),
      reviewer_receiver_id:
        project.reviewer_receiver_id === null ? null : uuid(project.reviewer_receiver_id),
    };
  });
  return {
    request: {
      ...value,
      operation_id: operationId,
      payload: {
        target_user_id: targetId,
        preview_version: value.payload.preview_version,
        reason: value.payload.reason,
        mode: value.payload.mode,
        projects,
      },
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
  if (typeof token !== "string" || Array.from(token).length > 16384) fail();
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
  if (expiry < 1n || expiry > maxEpochSeconds) fail();
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
    !managerRoles.has(value.role) ||
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
    BigInt(Math.floor(Date.parse(principal.expires_at) / 1000)) !== claims.expiry
  ) {
    fail();
  }
}

function validateUserRecord(value) {
  if (!exact(value, userFields)) fail();
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
  for (const field of nullableUserDates) {
    if (value[field] !== null && !validDateTime(value[field])) fail();
  }
  return value;
}

function blockerTextMatches(blocker, projectName = null) {
  if (!exact(blocker, ["code", "message"]) || typeof blocker.message !== "string") return false;
  if (blocker.code === "historical_inactive")
    return blocker.message === "该账号的停用来源无法可靠判定，不能恢复或再次交接";
  if (blocker.code === "historic_mixed_role")
    return blocker.message === "该账号在项目成员或批次历史中同时出现不同职责，需人工核对";
  const role = {
    no_owner_receiver: "owner",
    no_annotator_receiver: "annotator",
    no_reviewer_receiver: "reviewer",
  }[blocker.code];
  if (role === undefined) return false;
  if (typeof projectName === "string")
    return blocker.message === `项目「${projectName}」没有符合当前 ${role} 权限的启用接收人`;
  return new RegExp(`^项目「.+」没有符合当前 ${role} 权限的启用接收人$`).test(blocker.message);
}

function validateBlockers(value, projectName = null) {
  if (!Array.isArray(value)) fail();
  for (const blocker of value) {
    if (!blockerTextMatches(blocker, projectName)) fail();
  }
  return value;
}

function validateReceiver(value, targetId) {
  if (
    !exact(value, ["id", "name", "email", "role", "project_member_role"]) ||
    typeof value.name !== "string" ||
    typeof value.email !== "string" ||
    typeof value.role !== "string" ||
    !nullableString(value.project_member_role) ||
    uuid(value.id) === targetId
  ) {
    fail();
  }
  value.id = uuid(value.id);
  return value;
}

function validatePreview(value, targetId) {
  if (
    !exact(value, [
      "user",
      "preview_version",
      "generated_at",
      "projects",
      "api_keys",
      "blockers",
      "can_commit",
    ]) ||
    typeof value.preview_version !== "string" ||
    !/^[0-9a-f]{64}$/.test(value.preview_version) ||
    !validDateTime(value.generated_at) ||
    !Array.isArray(value.projects) ||
    !Array.isArray(value.api_keys) ||
    typeof value.can_commit !== "boolean"
  ) {
    fail();
  }
  value.user = validateUserRecord(value.user);
  if (value.user.id !== targetId) fail();
  const projectIds = [];
  const expectedGlobalBlockers = [];
  const historical = !value.user.is_active && historicalKinds.has(value.user.disabled_kind);
  if (historical) {
    expectedGlobalBlockers.push({
      code: "historical_inactive",
      message: "该账号的停用来源无法可靠判定，不能恢复或再次交接",
    });
  }
  const projectBlockers = [];
  for (const project of value.projects) {
    if (
      !exact(project, [
        "project_id",
        "project_name",
        "roles",
        "tasks",
        "locked_task_count",
        "blockers",
      ]) ||
      typeof project.project_name !== "string" ||
      !exact(project.roles, roles) ||
      !exact(project.tasks, ["annotator", "reviewer"])
    ) {
      fail();
    }
    project.project_id = uuid(project.project_id);
    projectIds.push(project.project_id);
    let expectedProjectBlockers = [];
    for (const role of roles) {
      const roleValue = project.roles[role];
      if (
        !exact(roleValue, ["present", "batches", "receiver_options"]) ||
        typeof roleValue.present !== "boolean" ||
        !Array.isArray(roleValue.batches) ||
        !Array.isArray(roleValue.receiver_options)
      ) {
        fail();
      }
      const batchIds = new Set();
      for (const batch of roleValue.batches) {
        if (!exact(batch, ["batch_id", "batch_name"]) || typeof batch.batch_name !== "string")
          fail();
        batch.batch_id = uuid(batch.batch_id);
        if (batchIds.has(batch.batch_id)) fail();
        batchIds.add(batch.batch_id);
      }
      const receiverIds = new Set();
      for (const receiver of roleValue.receiver_options) {
        validateReceiver(receiver, targetId);
        if (receiverIds.has(receiver.id)) fail();
        receiverIds.add(receiver.id);
      }
      if (roleValue.present && roleValue.receiver_options.length === 0) {
        expectedProjectBlockers.push({
          code: `no_${role}_receiver`,
          message: `项目「${project.project_name}」没有符合当前 ${role} 权限的启用接收人`,
        });
      }
    }
    for (const role of ["annotator", "reviewer"]) {
      const counts = project.tasks[role];
      if (!exact(counts, activeStatuses.concat("locked"))) fail();
      for (const status of activeStatuses.concat("locked")) int64(counts[status]);
    }
    int64(project.locked_task_count);
    if (!Array.isArray(project.blockers)) fail();
    validateBlockers(project.blockers, project.project_name);
    const roleBlockers = project.blockers.filter(
      (blocker) => blocker.code !== "historic_mixed_role",
    );
    if (stringifyLossless(roleBlockers) !== stringifyLossless(expectedProjectBlockers)) fail();
    const mixed = project.blockers.filter((blocker) => blocker.code === "historic_mixed_role");
    if (
      mixed.length > 1 ||
      (mixed.length === 1 && project.blockers.at(-1)?.code !== "historic_mixed_role")
    )
      fail();
    expectedProjectBlockers = [...expectedProjectBlockers, ...mixed];
    projectBlockers.push(...expectedProjectBlockers);
  }
  if (
    projectIds.some((id, index) => index > 0 && projectIds[index - 1] >= id) ||
    stringifyLossless(value.blockers) !==
      stringifyLossless([...expectedGlobalBlockers, ...projectBlockers])
  ) {
    fail();
  }
  validateBlockers(value.blockers);
  const expectedCanCommit =
    (value.user.is_active || reactivatableKinds.has(value.user.disabled_kind)) &&
    value.blockers.length === 0 &&
    !historicalKinds.has(value.user.disabled_kind);
  if (value.can_commit !== expectedCanCommit) fail();
  const keyIds = new Set();
  for (const key of value.api_keys) {
    if (
      !exact(key, ["id", "name", "key_prefix", "last_used_at", "revoked_at"]) ||
      typeof key.name !== "string" ||
      typeof key.key_prefix !== "string" ||
      (key.last_used_at !== null && !validDateTime(key.last_used_at)) ||
      (key.revoked_at !== null && !validDateTime(key.revoked_at))
    ) {
      fail();
    }
    key.id = uuid(key.id);
    if (keyIds.has(key.id)) fail();
    keyIds.add(key.id);
  }
  return value;
}

function validateTransfer(value) {
  if (
    !exact(value, ["project_id", "role", "receiver_id", "batch_ids", "task_count", "lock_count"]) ||
    !roles.includes(value.role) ||
    value.receiver_id === null ||
    !Array.isArray(value.batch_ids)
  ) {
    fail();
  }
  value.project_id = uuid(value.project_id);
  value.receiver_id = uuid(value.receiver_id);
  value.batch_ids = value.batch_ids.map(uuid);
  int64(value.task_count);
  int64(value.lock_count);
  return value;
}

function validateUnresolved(value) {
  if (
    !exact(value, ["project_id", "role", "reason", "batch_ids", "task_count", "lock_count"]) ||
    !roles.includes(value.role) ||
    value.reason !== "emergency_suspension_requires_later_handoff" ||
    !Array.isArray(value.batch_ids)
  ) {
    fail();
  }
  value.project_id = uuid(value.project_id);
  value.batch_ids = value.batch_ids.map(uuid);
  int64(value.task_count);
  int64(value.lock_count);
  return value;
}

function validateResult(value, request, actorId, requireAuditId = false) {
  if (
    !exact(value, [
      "user",
      "status",
      "mode",
      "transfers",
      "unresolved",
      "revoked_api_key_ids",
      "audit_id",
    ]) ||
    value.status !== "suspended" ||
    value.mode !== request.payload.mode ||
    !Array.isArray(value.transfers) ||
    !Array.isArray(value.unresolved) ||
    !Array.isArray(value.revoked_api_key_ids)
  ) {
    fail();
  }
  value.user = validateUserRecord(value.user);
  if (
    value.user.id !== request.payload.target_user_id ||
    value.user.is_active !== false ||
    value.user.status !== "offline" ||
    value.user.disabled_by !== actorId ||
    value.user.disabled_kind !==
      (value.mode === "emergency_suspend" ? "emergency_suspended" : "suspended") ||
    value.user.disabled_reason !== normalizeReason(request.payload.reason)
  ) {
    fail();
  }
  value.transfers = value.transfers.map(validateTransfer);
  value.unresolved = value.unresolved.map(validateUnresolved);
  value.revoked_api_key_ids = value.revoked_api_key_ids.map(uuid);
  if (new Set(value.revoked_api_key_ids).size !== value.revoked_api_key_ids.length) fail();
  if (requireAuditId && value.audit_id === null) fail();
  if (value.audit_id !== null && int64(value.audit_id) < 1n) fail();
  if (value.mode === "emergency_suspend" && value.transfers.length !== 0) fail();
  if (value.mode === "handoff" && value.unresolved.length !== 0) fail();
  return value;
}

function validateFailure(checked, value, actorId) {
  const codes = [
    ...authFailures,
    "invalid_request",
    "manager_required",
    "target_not_found",
    "self_offboard",
    "last_super_admin",
    "target_role_forbidden",
    "inactive_unmanaged",
    "scope_forbidden",
    "offboarding_preview_stale",
    "historical_target",
    "state_forbidden",
    "duplicate_project",
    "receiver_required",
    "receiver_ineligible",
    "offboarding_blocked",
    "account_busy",
    "offboarding_busy",
    "operation_conflict",
    "unavailable",
  ];
  if (!exact(value, ["code", "status_code", "detail"]) || !codes.includes(value.code)) fail();
  const status = value.status_code === null ? null : int64(value.status_code);
  const detail = value.detail;
  const request = checked.request;
  if (authFailures.has(value.code)) {
    if (status !== null || detail !== null) fail();
  } else if (value.code === "invalid_request") {
    if (status !== 400n || detail !== null) fail();
  } else if (actorId === null) {
    fail();
  } else if (Object.hasOwn(fixedFailures, value.code)) {
    const [expectedStatus, expectedDetail] = fixedFailures[value.code];
    if (status !== expectedStatus || detail !== expectedDetail) fail();
    if (value.code === "target_not_found" && checked.targetId === actorId) fail();
    if (value.code === "self_offboard" && checked.targetId !== actorId) fail();
    if (value.code === "last_super_admin" && checked.targetId === actorId) fail();
    if (
      ["target_role_forbidden", "inactive_unmanaged", "scope_forbidden"].includes(value.code) &&
      checked.targetId === actorId
    )
      fail();
  } else if (value.code === "offboarding_preview_stale") {
    if (
      request.operation !== "commit_offboarding" ||
      status !== 409n ||
      !exact(detail, ["code", "expected_preview_version", "current_preview_version", "blockers"]) ||
      detail.code !== "offboarding_preview_stale" ||
      detail.expected_preview_version !== request.payload.preview_version ||
      !/^[0-9a-f]{64}$/.test(detail.current_preview_version)
    ) {
      fail();
    }
    validateBlockers(detail.blockers);
  } else if (["receiver_required", "receiver_ineligible"].includes(value.code)) {
    if (
      request.operation !== "commit_offboarding" ||
      request.payload.mode !== "handoff" ||
      new Set(request.payload.projects.map((project) => project.project_id)).size !==
        request.payload.projects.length ||
      status !== 409n
    ) {
      fail();
    }
    const fields =
      value.code === "receiver_required"
        ? ["code", "project_id", "role"]
        : ["code", "project_id", "role", "receiver_id"];
    if (!exact(detail, fields) || detail.code !== value.code || !roles.includes(detail.role))
      fail();
    detail.project_id = uuid(detail.project_id);
    const mappings = request.payload.projects.filter(
      (project) => project.project_id === detail.project_id,
    );
    const mapping = mappings.length === 1 ? mappings[0] : null;
    if (value.code === "receiver_required") {
      if (mapping && mapping[`${detail.role}_receiver_id`] !== null) fail();
    } else {
      detail.receiver_id = uuid(detail.receiver_id);
      if (!mapping || mapping[`${detail.role}_receiver_id`] !== detail.receiver_id) fail();
    }
  } else if (value.code === "offboarding_blocked") {
    if (
      request.operation !== "commit_offboarding" ||
      request.payload.mode !== "handoff" ||
      new Set(request.payload.projects.map((project) => project.project_id)).size !==
        request.payload.projects.length ||
      status !== 409n ||
      !exact(detail, ["code", "blockers"]) ||
      detail.code !== "offboarding_blocked" ||
      !Array.isArray(detail.blockers) ||
      detail.blockers.length === 0
    ) {
      fail();
    }
    validateBlockers(detail.blockers);
  } else if (value.code === "offboarding_busy") {
    if (request.operation !== "commit_offboarding" || status !== 409n) fail();
    if (exact(detail, ["code"])) {
      if (detail.code !== "offboarding_busy") fail();
    } else if (
      !exact(detail, ["code", "message"]) ||
      detail.code !== "offboarding_busy" ||
      detail.message !== "职责正在变化，请刷新预览后重试"
    ) {
      fail();
    }
  } else if (value.code === "operation_conflict") {
    if (request.operation !== "commit_offboarding" || status !== 409n || detail !== null) fail();
  }
  return value;
}

function decodeOffboardingResponse(
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
    if (request.token.startsWith("ak_")) actorId = expectedApiPrincipal?.actor_id ?? null;
    else {
      try {
        actorId = incomingJwtClaims(request.token).actorId;
      } catch {
        actorId = null;
      }
    }
    return { schema, ok: false, error: validateFailure(checked, document.error, actorId) };
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
  if (request.operation === "preview_offboarding") {
    if (!exact(document.data, ["preview"])) fail();
    document.data.preview = validatePreview(document.data.preview, checked.targetId);
  } else {
    if (!exact(document.data, ["result"])) fail();
    document.data.result = validateResult(document.data.result, request, principal.actor_id, true);
  }
  return document;
}

function validatePublicPreview(body, status, contentType, targetId) {
  if (
    typeof body !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  return validatePreview(parseStrictJson(body), uuid(targetId));
}

function validatePublicResult(
  body,
  status,
  contentType,
  targetId,
  expectedMode,
  expectedReason,
  actorId,
) {
  if (
    typeof body !== "string" ||
    status !== 200 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const result = parseStrictJson(body);
  if (
    !exact(result, [
      "user",
      "status",
      "mode",
      "transfers",
      "unresolved",
      "revoked_api_key_ids",
      "audit_id",
    ]) ||
    result.mode !== expectedMode
  ) {
    fail();
  }
  const request = {
    payload: { target_user_id: uuid(targetId), mode: expectedMode, reason: expectedReason },
  };
  return validateResult(result, request, uuid(actorId));
}

function validateCallInput(value) {
  if (!exact(value, ["mode", "url", "control_token", "request"])) fail();
  if (!new Set(["call", "discard"]).has(value.mode)) fail();
  if (
    typeof value.url !== "string" ||
    value.url.length < 1 ||
    value.url.length > 2048 ||
    typeof value.control_token !== "string" ||
    !/^[\x21-\x7e]{1,4096}$/.test(value.control_token)
  ) {
    fail();
  }
  let origin;
  try {
    origin = new URL(value.url);
  } catch {
    fail();
  }
  if (
    !["http:", "https:"].includes(origin.protocol) ||
    !origin.hostname ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash
  ) {
    fail();
  }
  const checked = validateRequest(value.request);
  return { mode: value.mode, origin, controlToken: value.control_token, checked };
}

function postOffboardingRequest(origin, controlToken, request) {
  const endpointUrl = new URL(endpoint, origin);
  const transport = endpointUrl.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(JSON.stringify(request), "utf8");
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
        response.on("data", (chunk) => {
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
            rejectResponse(new Error("Invalid Identity offboarding response"));
          }
        });
        response.on("error", () =>
          rejectResponse(new Error("Identity offboarding request failed")),
        );
      },
    );
    outgoing.on("error", () => rejectResponse(new Error("Identity offboarding request failed")));
    outgoing.end(body);
  });
}

async function apiKeyPrincipal(origin, controlToken, token) {
  const response = await postIdentityRequest(origin, controlToken, authEndpoint, { token });
  scanJson(response.text);
  const admission = parseIdentityReceipt(response.text, response.status, response.contentType);
  return admission.ok && admission.principal.credential_type === "api_key"
    ? admission.principal
    : null;
}

async function callPeer(value) {
  const { mode, origin, controlToken, checked } = validateCallInput(value);
  const response = await postOffboardingRequest(origin, controlToken, checked.request);
  if (mode === "discard") return { ok: true, response_discarded: true };
  const expectedApiPrincipal = checked.request.token.startsWith("ak_")
    ? await apiKeyPrincipal(origin, controlToken, checked.request.token)
    : null;
  return decodeOffboardingResponse(
    checked,
    response.text,
    response.status,
    response.contentType,
    expectedApiPrincipal,
  );
}

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  try {
    return parseStrictJson(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
  } catch {
    fail();
  }
}

function testToken(actorId, credentialId, generation = 9_007_199_254_740_993n) {
  const header = Buffer.from(JSON.stringify(jwtHeader)).toString("base64url");
  const payload = Buffer.from(
    stringifyLossless({
      sub: actorId,
      role: "legacy-annotator",
      exp: 1893456000n,
      jti: credentialId,
      gen: generation,
      iss: issuer,
      legacy_extension: { preserved: true },
    }),
  ).toString("base64url");
  return `${header}.${payload}.${Buffer.alloc(32, 9).toString("base64url")}`;
}

function testPrincipal(actorId, credentialId, generation = 9_007_199_254_740_993n) {
  return {
    actor_id: actorId,
    name: "Offboarding manager",
    email: "offboarding-manager@example.test",
    role: "super_admin",
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation: new JsonNumber(generation.toString()),
    scopes: null,
    expires_at: "2030-01-01T00:00:00Z",
  };
}

function testUserRecord(id, preferences = {}) {
  return {
    id,
    email: "historical-employee@example.test",
    name: "离职员工 😀",
    role: "historical-annotator",
    group_name: null,
    group_id: null,
    status: "offline",
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
    created_at: "2029-01-01T00:00:00Z",
  };
}

function testRequest(operation, actorId, credentialId, targetId, operationId, payload) {
  const preview = operation === "preview_offboarding";
  return validateRequest({
    schema,
    operation,
    operation_id: operationId,
    token: testToken(actorId, credentialId),
    audit_context: {
      method: preview ? "GET" : "POST",
      path: `/api/v1/users/${targetId}/offboarding${preview ? "-preview" : ""}`,
      ip: "127.0.0.1",
      request_id: "44444444-4444-4444-8444-444444444444",
    },
    payload,
  });
}

function testReceiver(id, role, projectMemberRole) {
  return {
    id,
    name: `${role} receiver`,
    email: `${role}@example.test`,
    role,
    project_member_role: projectMemberRole,
  };
}

function testPreview(targetId) {
  const projectId = "44444444-4444-4444-8444-444444444444";
  return {
    user: testUserRecord(targetId, {
      token: "opaque preference value",
      password: new JsonNumber("9007199254740993"),
      nested: { decimal: new JsonNumber("0.1234567890123456789") },
    }),
    preview_version: "0123456789abcdef".repeat(4),
    generated_at: "2030-01-01T00:00:00Z",
    projects: [
      {
        project_id: projectId,
        project_name: "交接项目 😀",
        roles: {
          owner: {
            present: true,
            batches: [],
            receiver_options: [
              testReceiver("55555555-5555-4555-8555-555555555555", "project_admin", null),
            ],
          },
          annotator: {
            present: true,
            batches: [
              {
                batch_id: "66666666-6666-4666-8666-666666666666",
                batch_name: "标注批次",
              },
            ],
            receiver_options: [
              testReceiver("77777777-7777-4777-8777-777777777777", "employee", "annotator"),
            ],
          },
          reviewer: {
            present: false,
            batches: [],
            receiver_options: [
              testReceiver("88888888-8888-4888-8888-888888888888", "employee", "reviewer"),
            ],
          },
        },
        tasks: {
          annotator: {
            pending: new JsonNumber("0"),
            in_progress: new JsonNumber("1"),
            review: new JsonNumber("0"),
            rejected: new JsonNumber("1"),
            locked: new JsonNumber("1"),
          },
          reviewer: {
            pending: new JsonNumber("0"),
            in_progress: new JsonNumber("0"),
            review: new JsonNumber("0"),
            rejected: new JsonNumber("0"),
            locked: new JsonNumber("0"),
          },
        },
        locked_task_count: new JsonNumber("2"),
        blockers: [],
      },
    ],
    api_keys: [
      {
        id: "99999999-9999-4999-8999-999999999999",
        name: "Synthetic key",
        key_prefix: "ak_fixture",
        last_used_at: null,
        revoked_at: null,
      },
    ],
    blockers: [],
    can_commit: true,
  };
}

function testResult(targetId, actorId, mode, reason) {
  const projectId = "44444444-4444-4444-8444-444444444444";
  const transfers =
    mode === "handoff"
      ? [
          {
            project_id: projectId,
            role: "owner",
            receiver_id: "55555555-5555-4555-8555-555555555555",
            batch_ids: [],
            task_count: new JsonNumber("0"),
            lock_count: new JsonNumber("0"),
          },
          {
            project_id: projectId,
            role: "annotator",
            receiver_id: "77777777-7777-4777-8777-777777777777",
            batch_ids: ["66666666-6666-4666-8666-666666666666"],
            task_count: new JsonNumber("2"),
            lock_count: new JsonNumber("1"),
          },
        ]
      : [];
  const unresolved =
    mode === "emergency_suspend"
      ? [
          {
            project_id: projectId,
            role: "owner",
            reason: "emergency_suspension_requires_later_handoff",
            batch_ids: [],
            task_count: new JsonNumber("0"),
            lock_count: new JsonNumber("0"),
          },
        ]
      : [];
  const user = {
    ...testUserRecord(targetId, { token: "opaque preference value" }),
    is_active: false,
    disabled_kind: mode === "emergency_suspend" ? "emergency_suspended" : "suspended",
    disabled_at: "2030-01-01T00:00:00Z",
    disabled_by: actorId,
    disabled_reason: normalizeReason(reason),
  };
  return {
    user,
    status: "suspended",
    mode,
    transfers,
    unresolved,
    revoked_api_key_ids: ["99999999-9999-4999-8999-999999999999"],
    audit_id: new JsonNumber("7"),
  };
}

function testReply(checked, principal, data) {
  return stringifyLossless({
    schema,
    ok: true,
    operation: checked.request.operation,
    operation_id: checked.operationId,
    principal,
    data,
  });
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
  const legacyCanonical = String.raw`{"api_keys": [{"id": "1e4e1a1d-c7c4-4b9f-a043-74fb50d6b8f2", "name": "\u6d4b\u8bd5 Key \ud83d\ude00", "prefix": "ak_9QJNmfxm2", "revoked_at": null}], "batches": [{"annotator_id": "76a0f523-7244-4ec3-bae2-1326b45d4761", "assigned_user_ids": ["76a0f523-7244-4ec3-bae2-1326b45d4761", "82651db2-95e6-4077-a75a-b2071c7171e7"], "id": "8f06b404-16f2-4dd6-bfa2-1f5dfb5b8b7a", "project_id": "a28afbc7-fc9b-4979-90f6-7b0b6ef31340", "reviewer_id": "82651db2-95e6-4077-a75a-b2071c7171e7", "status": "active"}], "locks": [{"expire_at": "2027-01-02 03:04:05.246912+00:00", "id": "6da26024-314d-44e3-b065-c0981cf3c251", "task_id": "2d457612-ba94-4554-9c0c-2964d045aac0"}, {"expire_at": "2027-01-02 03:04:05+00:00", "id": "e4f95c77-fa09-4bef-a16d-fe8cb1c37e14", "task_id": "ad6171e1-4d23-418b-8b4e-fe94d40de667"}], "members": [{"id": "b4905c47-79fa-43d0-a4b3-88df1b87d8d4", "project_id": "a28afbc7-fc9b-4979-90f6-7b0b6ef31340", "role": "annotator"}], "projects": [{"id": "a28afbc7-fc9b-4979-90f6-7b0b6ef31340", "owner_id": "76a0f523-7244-4ec3-bae2-1326b45d4761"}], "receivers": {"a28afbc7-fc9b-4979-90f6-7b0b6ef31340": {"annotator": [{"email": "ann-73921890c61b4c549c0f9bbff8588a59@baseline.test", "id": "bf931609-dd07-48d2-bb51-02b6f073d1ce", "name": "\u6807\u6ce8\n\u63a5\u6536\u8005", "project_member_role": "annotator", "role": "employee"}], "owner": [{"email": "owner-73921890c61b4c549c0f9bbff8588a59@baseline.test", "id": "1bd7e8cb-1ad5-4006-940d-ab5d6d8356c8", "name": "\u8d1f\u8d23\u4eba", "project_member_role": null, "role": "project_admin"}], "reviewer": [{"email": "rev-73921890c61b4c549c0f9bbff8588a59@baseline.test", "id": "82651db2-95e6-4077-a75a-b2071c7171e7", "name": "\u5ba1\u6838\u63a5\u6536\u8005", "project_member_role": "reviewer", "role": "employee"}]}}, "target": {"disabled_at": null, "disabled_by": null, "disabled_kind": null, "disabled_reason": null, "id": "76a0f523-7244-4ec3-bae2-1326b45d4761", "is_active": true, "role": "employee"}, "tasks": [{"assignee_id": "76a0f523-7244-4ec3-bae2-1326b45d4761", "batch_id": "8f06b404-16f2-4dd6-bfa2-1f5dfb5b8b7a", "id": "2d457612-ba94-4554-9c0c-2964d045aac0", "project_id": "a28afbc7-fc9b-4979-90f6-7b0b6ef31340", "reviewer_id": "82651db2-95e6-4077-a75a-b2071c7171e7", "status": "completed", "version": 1}, {"assignee_id": "76a0f523-7244-4ec3-bae2-1326b45d4761", "batch_id": "8f06b404-16f2-4dd6-bfa2-1f5dfb5b8b7a", "id": "3c5d1ac0-d976-4802-9dea-5f8185c179da", "project_id": "a28afbc7-fc9b-4979-90f6-7b0b6ef31340", "reviewer_id": "82651db2-95e6-4077-a75a-b2071c7171e7", "status": "rejected", "version": 1}, {"assignee_id": "76a0f523-7244-4ec3-bae2-1326b45d4761", "batch_id": "8f06b404-16f2-4dd6-bfa2-1f5dfb5b8b7a", "id": "ad6171e1-4d23-418b-8b4e-fe94d40de667", "project_id": "a28afbc7-fc9b-4979-90f6-7b0b6ef31340", "reviewer_id": "82651db2-95e6-4077-a75a-b2071c7171e7", "status": "in_progress", "version": 1}]}`;
  if (
    createHash("sha256").update(legacyCanonical, "utf8").digest("hex") !==
    "22377e1b9f071a85ada1364c91aaf3d2e70ff94de723d93574a6c29754f47d28"
  ) {
    fail();
  }
  checks += 1;
  const actorId = "11111111-1111-4111-8111-111111111111";
  const credentialId = "22222222-2222-4222-8222-222222222222";
  const targetId = "33333333-3333-4333-8333-333333333333";
  const operationId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const principal = testPrincipal(actorId, credentialId);
  const version = "0123456789abcdef".repeat(4);
  const previewRequest = testRequest(
    "preview_offboarding",
    actorId,
    credentialId,
    targetId,
    operationId,
    { target_user_id: targetId },
  );
  const previewDocument = testReply(previewRequest, principal, {
    preview: testPreview(targetId),
  });
  const decodedPreview = decodeOffboardingResponse(
    previewRequest,
    previewDocument,
    200,
    "application/json",
  );
  if (
    decodedPreview.data.preview.preview_version !== version ||
    decodedPreview.data.preview.projects[0].roles.reviewer.present !== false ||
    decodedPreview.data.preview.user.preferences.token !== "opaque preference value"
  ) {
    fail();
  }
  const opaquePreferences = stringifyLossless(decodedPreview.data.preview.user.preferences);
  if (
    !opaquePreferences.includes('"password":9007199254740993') ||
    !opaquePreferences.includes('"decimal":0.1234567890123456789')
  ) {
    fail();
  }
  checks += 1;

  const handoffReason = "  handoff 😀  ";
  const handoffRequest = testRequest(
    "commit_offboarding",
    actorId,
    credentialId,
    targetId,
    operationId,
    {
      target_user_id: targetId,
      preview_version: version,
      reason: handoffReason,
      mode: "handoff",
      projects: [
        {
          project_id: "44444444-4444-4444-8444-444444444444",
          owner_receiver_id: "55555555-5555-4555-8555-555555555555",
          annotator_receiver_id: "77777777-7777-4777-8777-777777777777",
          reviewer_receiver_id: null,
        },
      ],
    },
  );
  const handoffDocument = testReply(handoffRequest, principal, {
    result: testResult(targetId, actorId, "handoff", handoffReason),
  });
  const decodedHandoff = decodeOffboardingResponse(
    handoffRequest,
    handoffDocument,
    200,
    "application/json",
  );
  if (
    decodedHandoff.data.result.user.disabled_reason !== "handoff 😀" ||
    decodedHandoff.data.result.transfers.length !== 2
  ) {
    fail();
  }
  checks += 1;

  const emergencyReason = " \u2003\u00a0 ";
  const emergencyRequest = testRequest(
    "commit_offboarding",
    actorId,
    credentialId,
    targetId,
    operationId,
    {
      target_user_id: targetId,
      preview_version: version,
      reason: emergencyReason,
      mode: "emergency_suspend",
      projects: [],
    },
  );
  const emergencyResult = testResult(targetId, actorId, "emergency_suspend", emergencyReason);
  const emergencyDocument = testReply(emergencyRequest, principal, { result: emergencyResult });
  const decodedEmergency = decodeOffboardingResponse(
    emergencyRequest,
    emergencyDocument,
    200,
    "application/json",
  );
  if (
    decodedEmergency.data.result.user.disabled_kind !== "emergency_suspended" ||
    decodedEmergency.data.result.transfers.length !== 0
  ) {
    fail();
  }
  checks += 1;

  const longVersion = `\0${"v".repeat(70_000)}`;
  const staleRequest = testRequest(
    "commit_offboarding",
    actorId,
    credentialId,
    targetId,
    operationId,
    {
      target_user_id: targetId,
      preview_version: longVersion,
      reason: "",
      mode: "handoff",
      projects: [],
    },
  );
  const staleReply = stringifyLossless({
    schema,
    ok: false,
    error: {
      code: "offboarding_preview_stale",
      status_code: 409,
      detail: {
        code: "offboarding_preview_stale",
        expected_preview_version: longVersion,
        current_preview_version: "0123456789abcdef".repeat(4),
        blockers: [],
      },
    },
  });
  const stale = decodeOffboardingResponse(staleRequest, staleReply, 200, "application/json");
  if (stale.error.detail.expected_preview_version !== longVersion) fail();
  checks += 1;

  expectFailure(() => parseStrictJson('{"schema":"aap-identity-offboarding/v1","schema":"x"}'));
  checks += 1;
  expectFailure(() => parseStrictJson('{"value":NaN}'));
  checks += 1;
  expectFailure(() =>
    validateRequest({
      ...staleRequest.request,
      extra: "unexpected",
    }),
  );
  checks += 1;
  expectFailure(() =>
    validateRequest({
      ...staleRequest.request,
      payload: { ...staleRequest.request.payload, reason: "x".repeat(501) },
    }),
  );
  checks += 1;

  const duplicateRequest = testRequest(
    "commit_offboarding",
    actorId,
    credentialId,
    targetId,
    operationId,
    {
      target_user_id: targetId,
      preview_version: version,
      reason: "",
      mode: "handoff",
      projects: [
        {
          project_id: "44444444-4444-4444-8444-444444444444",
          owner_receiver_id: null,
          annotator_receiver_id: null,
          reviewer_receiver_id: null,
        },
        {
          project_id: "44444444-4444-4444-8444-444444444444",
          owner_receiver_id: null,
          annotator_receiver_id: null,
          reviewer_receiver_id: null,
        },
      ],
    },
  );
  const duplicateFailure = stringifyLossless({
    schema,
    ok: false,
    error: {
      code: "duplicate_project",
      status_code: 422,
      detail: "同一项目不能重复提交交接映射",
    },
  });
  if (
    decodeOffboardingResponse(duplicateRequest, duplicateFailure, 200, "application/json").error
      .code !== "duplicate_project"
  ) {
    fail();
  }
  checks += 1;

  const invalidDuplicate = stringifyLossless({
    schema,
    ok: false,
    error: {
      code: "duplicate_project",
      status_code: 409,
      detail: "同一项目不能重复提交交接映射",
    },
  });
  expectFailure(() =>
    decodeOffboardingResponse(duplicateRequest, invalidDuplicate, 200, "application/json"),
  );
  checks += 1;

  const unsafeCount = previewDocument.replace('"in_progress":1', '"in_progress":1.0');
  expectFailure(() =>
    decodeOffboardingResponse(previewRequest, unsafeCount, 200, "application/json"),
  );
  checks += 1;

  const wrongPrincipal = testReply(
    previewRequest,
    { ...principal, actor_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" },
    { preview: testPreview(targetId) },
  );
  expectFailure(() =>
    decodeOffboardingResponse(previewRequest, wrongPrincipal, 200, "application/json"),
  );
  checks += 1;

  const unexpectedSecret = testReply(previewRequest, principal, {
    preview: { ...testPreview(targetId), access_token: "must reject" },
  });
  expectFailure(() =>
    decodeOffboardingResponse(previewRequest, unexpectedSecret, 200, "application/json"),
  );
  checks += 1;

  const missingPrivateAudit = testReply(handoffRequest, principal, {
    result: { ...testResult(targetId, actorId, "handoff", handoffReason), audit_id: null },
  });
  expectFailure(() =>
    decodeOffboardingResponse(handoffRequest, missingPrivateAudit, 200, "application/json"),
  );
  checks += 1;

  const selfPreview = testRequest(
    "preview_offboarding",
    actorId,
    credentialId,
    actorId,
    operationId,
    { target_user_id: actorId },
  );
  const selfFailure = stringifyLossless({
    schema,
    ok: false,
    error: { code: "self_offboard", status_code: 400, detail: "不能交接自己" },
  });
  if (
    decodeOffboardingResponse(selfPreview, selfFailure, 200, "application/json").error.code !==
    "self_offboard"
  ) {
    fail();
  }
  checks += 1;

  const legacyClaims = Buffer.from(
    stringifyLossless({
      sub: actorId,
      role: "reviewer",
      exp: 1893456000n,
      jti: credentialId,
      iss: issuer,
      extension: true,
    }),
  ).toString("base64url");
  const legacyToken = `${Buffer.from(JSON.stringify(jwtHeader)).toString("base64url")}.${legacyClaims}.${Buffer.alloc(32, 3).toString("base64url")}`;
  const legacyRequest = validateRequest({ ...previewRequest.request, token: legacyToken });
  const legacyPrincipal = testPrincipal(actorId, credentialId, 0n);
  const legacyReply = testReply(legacyRequest, legacyPrincipal, { preview: testPreview(targetId) });
  if (
    decodeOffboardingResponse(legacyRequest, legacyReply, 200, "application/json").principal
      .generation !== 0n
  ) {
    fail();
  }
  checks += 1;

  const apiKeyRequest = validateRequest({
    ...previewRequest.request,
    token: "ak_offboarding-self-check",
  });
  const apiKeyPrincipalValue = {
    ...principal,
    credential_type: "api_key",
    generation: null,
    scopes: ["*"],
    expires_at: null,
  };
  const apiKeyReply = testReply(apiKeyRequest, apiKeyPrincipalValue, {
    preview: testPreview(targetId),
  });
  if (
    decodeOffboardingResponse(
      apiKeyRequest,
      apiKeyReply,
      200,
      "application/json",
      apiKeyPrincipalValue,
    ).principal.credential_type !== "api_key"
  ) {
    fail();
  }
  checks += 1;

  const publicPreview = validatePublicInput({
    mode: "preview",
    status: new JsonNumber("200"),
    content_type: "application/json; charset=utf-8",
    body: stringifyLossless(testPreview(targetId)),
    target_user_id: targetId,
  });
  if (publicPreview.preview_version !== version) fail();
  checks += 1;

  const publicResult = validatePublicInput({
    mode: "result",
    status: new JsonNumber("200"),
    content_type: "application/json",
    body: stringifyLossless(testResult(targetId, actorId, "handoff", handoffReason)),
    target_user_id: targetId,
    expected_mode: "handoff",
    expected_reason: handoffReason,
    actor_id: actorId,
  });
  if (publicResult.mode !== "handoff") fail();
  checks += 1;

  const publicStaleVersion = `\0${"v".repeat(70_000)}`;
  const publicStale = validatePublicStaleError({
    status: new JsonNumber("409"),
    content_type: "application/json",
    body: stringifyLossless({
      detail: {
        code: "offboarding_preview_stale",
        expected_preview_version: publicStaleVersion,
        current_preview_version: version,
        blockers: [],
      },
    }),
    expected_preview_version: publicStaleVersion,
  });
  if (publicStale.expected_preview_version !== publicStaleVersion) fail();
  checks += 1;

  return checks;
}

function validatePublicInput(value) {
  if (!isRecord(value)) fail();
  const status = int64(value.status);
  if (value.mode === "preview") {
    if (!exact(value, ["mode", "status", "content_type", "body", "target_user_id"])) fail();
    return validatePublicPreview(
      value.body,
      Number(status),
      value.content_type,
      value.target_user_id,
    );
  }
  if (value.mode === "result") {
    if (
      !exact(value, [
        "mode",
        "status",
        "content_type",
        "body",
        "target_user_id",
        "expected_mode",
        "expected_reason",
        "actor_id",
      ])
    ) {
      fail();
    }
    return validatePublicResult(
      value.body,
      Number(status),
      value.content_type,
      value.target_user_id,
      value.expected_mode,
      value.expected_reason,
      value.actor_id,
    );
  }
  fail();
}

function validatePublicStaleError(value) {
  if (!exact(value, ["status", "content_type", "body", "expected_preview_version"])) fail();
  if (
    int64(value.status) !== 409n ||
    typeof value.content_type !== "string" ||
    value.content_type.split(";", 1)[0].trim().toLowerCase() !== "application/json" ||
    typeof value.body !== "string" ||
    typeof value.expected_preview_version !== "string"
  ) {
    fail();
  }
  const document = parseStrictJson(value.body);
  if (!exact(document, ["detail"])) fail();
  const detail = document.detail;
  if (
    !exact(detail, ["code", "expected_preview_version", "current_preview_version", "blockers"]) ||
    detail.code !== "offboarding_preview_stale" ||
    detail.expected_preview_version !== value.expected_preview_version ||
    !/^[0-9a-f]{64}$/.test(detail.current_preview_version)
  ) {
    fail();
  }
  validateBlockers(detail.blockers);
  return detail;
}

async function main() {
  if (process.argv[2] === "--self-check") {
    process.stdout.write(`${JSON.stringify({ ok: true, checks: selfCheck() })}\n`);
    return;
  }
  const input = await readInput();
  if (process.argv[2] === "--validate-public") {
    process.stdout.write(`${stringifyLossless(validatePublicInput(input))}\n`);
    return;
  }
  if (process.argv[2] === "--validate-public-stale") {
    process.stdout.write(`${stringifyLossless(validatePublicStaleError(input))}\n`);
    return;
  }
  process.stdout.write(`${stringifyLossless(await callPeer(input))}\n`);
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch(() => {
    process.stderr.write("Identity offboarding peer validation failed\n");
    process.exitCode = 1;
  });
}
