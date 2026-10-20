import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { createInterface } from "node:readline";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

const wireSchema = "aap-workspace-membership/v1";
const nativeSchema = "aap-workspace-membership-native/v1";
const nativeEndpoint = "/v1/workspace/membership/execute";
const nativeRole = "workspace_membership_client";
const operations = new Set(["add_member", "preview_role_change", "change_role", "remove_member"]);
const publicOperations = new Set([...operations, "list_members"]);
const roles = new Set(["annotator", "reviewer", "viewer"]);
let phase = "input";
const memberKeys = [
  "id",
  "project_id",
  "user_id",
  "role",
  "assigned_by",
  "version",
  "assigned_at",
  "updated_at",
  "weekly_target",
];
const snapshotKeys = [
  "member",
  "member_account",
  "project",
  "annotation_task_ids",
  "review_task_ids",
  "unassigned_review_task_ids",
  "lock_task_ids",
  "review_claim_task_ids",
  "materialization_annotation_task_ids",
  "materialization_review_task_ids",
  "batch_annotator_ids",
  "batch_reviewer_ids",
  "eligible_reviewer_ids",
  "tasks",
  "batches",
  "receivers",
];
const taskSnapshotKeys = [
  "status",
  "version",
  "batch_id",
  "assignee_id",
  "assignee_is_override",
  "reviewer_id",
  "reviewer_is_override",
  "reviewer_claimed",
  "review_round_id",
  "effective_assignee_id",
  "effective_reviewer_id",
  "review_submitter_id",
  "annotation_contributors",
  "review_contributors",
];
const batchSnapshotKeys = [
  "status",
  "annotator_id",
  "reviewer_id",
  "assigned_user_ids",
  "admin_locked",
];
const blockerCodes = new Set([
  "same_role",
  "target_role_incompatible",
  "unfinished_annotation_work",
  "unfinished_review_work",
  "active_locks",
  "reviewer_coverage_lost",
  "invalid_replacement_annotator",
  "invalid_replacement_reviewer",
  "identical_replacement_responsibility",
  "self_replacement",
  "replacement_annotator_reviewer_conflict",
  "replacement_reviewer_is_annotator",
  "replacement_reviewer_contributor",
  "review_contributors_unknown",
]);
const intPattern = /^-?(?:0|[1-9][0-9]*)$/;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

class JsonNumber {
  constructor(raw) {
    this.raw = raw;
  }
}

function fail() {
  throw new Error("Invalid Workspace membership peer input or response");
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

function scanJson(source) {
  let offset = 0;
  let depth = 0;
  const maxDepth = 256;
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
    depth += 1;
    if (depth > maxDepth) fail();
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
    } else if (source[offset] === "[") {
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
    } else if (source[offset] === '"') {
      string();
    } else {
      const start = offset;
      while (offset < source.length && !/[\t\n\r ,\]}]/.test(source[offset])) offset += 1;
      if (start === offset) fail();
    }
    depth -= 1;
  };

  value();
  whitespace();
  if (offset !== source.length) fail();
}

function parseStrictJson(source) {
  if (typeof source !== "string") fail();
  scanJson(source);
  try {
    return JSON.parse(source, (_key, value, context) => {
      if (typeof value !== "number") return value;
      if (typeof context?.source !== "string") fail();
      return new JsonNumber(context.source);
    });
  } catch {
    fail();
  }
}

function stringifyLossless(value, { sortKeys = false, pythonStrings = false } = {}) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") {
    if (!pythonStrings) return JSON.stringify(value);
    let result = '"';
    for (const character of value) {
      const point = character.codePointAt(0);
      if (character === '"') result += '\\"';
      else if (character === "\\") result += "\\\\";
      else if (character === "\b") result += "\\b";
      else if (character === "\f") result += "\\f";
      else if (character === "\n") result += "\\n";
      else if (character === "\r") result += "\\r";
      else if (character === "\t") result += "\\t";
      else if (point < 0x20) result += `\\u${point.toString(16).padStart(4, "0")}`;
      else if (point < 0x7f) result += character;
      else if (point <= 0xffff) result += `\\u${point.toString(16).padStart(4, "0")}`;
      else {
        const adjusted = point - 0x10000;
        const high = 0xd800 + (adjusted >> 10);
        const low = 0xdc00 + (adjusted & 0x3ff);
        result += `\\u${high.toString(16)}\\u${low.toString(16)}`;
      }
    }
    return `${result}"`;
  }
  if (value instanceof JsonNumber) return value.raw;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) fail();
    return String(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => stringifyLossless(item, { sortKeys, pythonStrings })).join(", ")}]`;
  }
  if (isRecord(value)) {
    const keys = Object.keys(value);
    if (sortKeys) keys.sort();
    return `{${keys
      .map(
        (key) =>
          `${stringifyLossless(key, { sortKeys, pythonStrings })}: ${stringifyLossless(value[key], { sortKeys, pythonStrings })}`,
      )
      .join(", ")}}`;
  }
  fail();
}

function uuid(value) {
  if (typeof value !== "string" || !uuidPattern.test(value)) fail();
  return value.toLowerCase();
}

function integer(value) {
  if (!(value instanceof JsonNumber) || !intPattern.test(value.raw)) fail();
  return BigInt(value.raw);
}

function bool(value) {
  if (typeof value !== "boolean") fail();
  return value;
}

function strictString(value) {
  if (typeof value !== "string") fail();
  return value;
}

function nullableUuid(value) {
  return value === null ? null : uuid(value);
}

function nullableInteger(value) {
  return value === null ? null : integer(value);
}

function nullableString(value) {
  return value === null ? null : strictString(value);
}

function awareDateTime(value) {
  const match =
    typeof value === "string" &&
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-](\d{2}):(\d{2}))$/.exec(
      value,
    );
  if (!match) fail();
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const offsetHour = match[8] === undefined ? 0 : Number(match[8]);
  const offsetMinute = match[9] === undefined ? 0 : Number(match[9]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    year < 1 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > monthDays[month - 1] ||
    hour > 23 ||
    minute > 59 ||
    second > 59 ||
    offsetHour > 23 ||
    offsetMinute > 59 ||
    !Number.isFinite(Date.parse(value))
  ) {
    fail();
  }
  return value;
}

function validateAuditContext(value) {
  if (!exact(value, ["method", "path", "ip", "request_id"])) fail();
  nullableString(value.method);
  nullableString(value.path);
  nullableString(value.ip);
  nullableString(value.request_id);
  return value;
}

function validateRequest(value) {
  if (
    !exact(value, [
      "schema",
      "operation_id",
      "operation",
      "actor_id",
      "project_id",
      "payload",
      "audit_context",
    ]) ||
    value.schema !== wireSchema ||
    !operations.has(value.operation)
  ) {
    fail();
  }
  const request = {
    schema: wireSchema,
    operation_id: uuid(value.operation_id),
    operation: value.operation,
    actor_id: uuid(value.actor_id),
    project_id: uuid(value.project_id),
    payload: null,
    audit_context: validateAuditContext(value.audit_context),
  };
  if (request.operation === "add_member") {
    if (!exact(value.payload, ["target_user_id", "project_role"])) fail();
    request.payload = {
      target_user_id: uuid(value.payload.target_user_id),
      project_role: strictString(value.payload.project_role),
    };
  } else if (request.operation === "preview_role_change") {
    if (
      !exact(value.payload, [
        "member_id",
        "target_role",
        "replacement_annotator_id",
        "replacement_reviewer_id",
      ])
    ) {
      fail();
    }
    request.payload = {
      member_id: uuid(value.payload.member_id),
      target_role: strictString(value.payload.target_role),
      replacement_annotator_id: nullableUuid(value.payload.replacement_annotator_id),
      replacement_reviewer_id: nullableUuid(value.payload.replacement_reviewer_id),
    };
  } else if (request.operation === "change_role") {
    if (
      !exact(value.payload, [
        "member_id",
        "target_role",
        "expected_version",
        "preview_token",
        "reason",
        "replacement_annotator_id",
        "replacement_reviewer_id",
      ])
    ) {
      fail();
    }
    integer(value.payload.expected_version);
    request.payload = {
      member_id: uuid(value.payload.member_id),
      target_role: strictString(value.payload.target_role),
      expected_version: value.payload.expected_version,
      preview_token: strictString(value.payload.preview_token),
      reason: strictString(value.payload.reason),
      replacement_annotator_id: nullableUuid(value.payload.replacement_annotator_id),
      replacement_reviewer_id: nullableUuid(value.payload.replacement_reviewer_id),
    };
  } else {
    if (!exact(value.payload, ["member_id"])) fail();
    request.payload = { member_id: uuid(value.payload.member_id) };
  }
  return request;
}

function validateMember(value) {
  if (!exact(value, memberKeys)) fail();
  uuid(value.id);
  uuid(value.project_id);
  uuid(value.user_id);
  strictString(value.role);
  nullableUuid(value.assigned_by);
  integer(value.version);
  awareDateTime(value.assigned_at);
  awareDateTime(value.updated_at);
  nullableInteger(value.weekly_target);
  return value;
}

function validateSnapshot(value) {
  if (!exact(value, snapshotKeys)) fail();
  if (!exact(value.member, ["id", "user_id", "role", "version"])) fail();
  uuid(value.member.id);
  uuid(value.member.user_id);
  strictString(value.member.role);
  integer(value.member.version);
  if (!exact(value.member_account, ["exists", "is_active", "platform_role"])) fail();
  bool(value.member_account.exists);
  bool(value.member_account.is_active);
  nullableString(value.member_account.platform_role);
  if (!exact(value.project, ["id", "owner_id"])) fail();
  uuid(value.project.id);
  nullableUuid(value.project.owner_id);
  const idArrays = snapshotKeys.slice(3, 13);
  for (const field of idArrays) {
    if (!Array.isArray(value[field])) fail();
    for (const item of value[field]) uuid(item);
  }
  if (
    !isRecord(value.tasks) ||
    !isRecord(value.batches) ||
    !exact(value.receivers, ["annotator", "reviewer"])
  )
    fail();
  for (const [key, task] of Object.entries(value.tasks)) {
    uuid(key);
    if (!exact(task, taskSnapshotKeys)) fail();
    strictString(task.status);
    integer(task.version);
    nullableUuid(task.batch_id);
    nullableUuid(task.assignee_id);
    bool(task.assignee_is_override);
    nullableUuid(task.reviewer_id);
    bool(task.reviewer_is_override);
    bool(task.reviewer_claimed);
    nullableUuid(task.review_round_id);
    nullableUuid(task.effective_assignee_id);
    nullableUuid(task.effective_reviewer_id);
    nullableUuid(task.review_submitter_id);
    for (const field of ["annotation_contributors", "review_contributors"]) {
      if (task[field] !== null) {
        if (!Array.isArray(task[field])) fail();
        for (const contributor of task[field]) strictString(contributor);
      }
    }
  }
  for (const [key, batch] of Object.entries(value.batches)) {
    uuid(key);
    if (!exact(batch, batchSnapshotKeys)) fail();
    strictString(batch.status);
    nullableUuid(batch.annotator_id);
    nullableUuid(batch.reviewer_id);
    if (!Array.isArray(batch.assigned_user_ids)) fail();
    for (const userId of batch.assigned_user_ids) strictString(userId);
    bool(batch.admin_locked);
  }
  for (const role of ["annotator", "reviewer"]) {
    const receiver = value.receivers[role];
    if (receiver === null) continue;
    if (
      !exact(receiver, [
        "user_id",
        "exists",
        "is_active",
        "platform_role",
        "membership_id",
        "membership_version",
        "membership_role",
      ])
    )
      fail();
    uuid(receiver.user_id);
    bool(receiver.exists);
    bool(receiver.is_active);
    nullableString(receiver.platform_role);
    nullableUuid(receiver.membership_id);
    nullableInteger(receiver.membership_version);
    nullableString(receiver.membership_role);
  }
  return value;
}

function validatePreview(value) {
  if (
    !exact(value, [
      "member_id",
      "user_id",
      "current_role",
      "current_version",
      "target_role",
      "requires_handoff",
      "blockers",
      "blocker_details",
      "resource_snapshot",
      "preview_token",
    ])
  ) {
    fail();
  }
  uuid(value.member_id);
  uuid(value.user_id);
  strictString(value.current_role);
  integer(value.current_version);
  strictString(value.target_role);
  bool(value.requires_handoff);
  if (!Array.isArray(value.blockers) || !Array.isArray(value.blocker_details)) fail();
  for (const code of value.blockers) {
    if (typeof code !== "string" || !blockerCodes.has(code)) fail();
  }
  for (const detail of value.blocker_details) {
    if (!isRecord(detail) || !blockerCodes.has(detail.code) || typeof detail.message !== "string")
      fail();
    const allowed = new Set(["code", "message"]);
    if (
      [
        "active_locks",
        "reviewer_coverage_lost",
        "replacement_annotator_reviewer_conflict",
        "replacement_reviewer_is_annotator",
        "replacement_reviewer_contributor",
        "review_contributors_unknown",
      ].includes(detail.code)
    )
      allowed.add("task_ids");
    if (
      [
        "self_replacement",
        "invalid_replacement_annotator",
        "invalid_replacement_reviewer",
      ].includes(detail.code)
    )
      allowed.add("project_role");
    if (Object.keys(detail).some((key) => !allowed.has(key))) fail();
    if (Object.hasOwn(detail, "task_ids")) {
      if (!Array.isArray(detail.task_ids)) fail();
      for (const taskId of detail.task_ids) uuid(taskId);
    }
    if (Object.hasOwn(detail, "project_role")) strictString(detail.project_role);
  }
  validateSnapshot(value.resource_snapshot);
  if (typeof value.preview_token !== "string" || !/^[0-9a-f]{64}$/.test(value.preview_token))
    fail();
  const digest = createHash("sha256")
    .update(
      stringifyLossless(value.resource_snapshot, { sortKeys: true, pythonStrings: true }),
      "utf8",
    )
    .digest("hex");
  if (digest !== value.preview_token) fail();
  return value;
}

function validatePublicReply(request, raw) {
  if (!isRecord(raw) || raw.schema !== wireSchema || typeof raw.ok !== "boolean") fail();
  const binding = [
    ["operation_id", uuid(raw.operation_id), request.operation_id],
    ["operation", raw.operation, request.operation],
    ["actor_id", uuid(raw.actor_id), request.actor_id],
    ["project_id", uuid(raw.project_id), request.project_id],
  ];
  if (binding.some(([, actual, expected]) => actual !== expected)) fail();
  if (raw.ok) {
    if (
      !exact(raw, ["schema", "ok", "operation_id", "operation", "actor_id", "project_id", "data"])
    )
      fail();
    if (request.operation === "add_member" || request.operation === "change_role") {
      if (!exact(raw.data, ["member"])) fail();
      validateMember(raw.data.member);
      if (raw.data.member.project_id !== request.project_id) fail();
      if (request.operation === "add_member") {
        if (
          raw.data.member.user_id !== request.payload.target_user_id ||
          raw.data.member.role !== request.payload.project_role
        )
          fail();
      } else if (
        raw.data.member.id !== request.payload.member_id ||
        raw.data.member.role !== request.payload.target_role
      ) {
        fail();
      }
    } else if (request.operation === "preview_role_change") {
      validatePreview(raw.data);
      if (
        raw.data.member_id !== request.payload.member_id ||
        raw.data.target_role !== request.payload.target_role
      )
        fail();
    } else {
      if (
        !exact(raw.data, ["member_id", "removed"]) ||
        uuid(raw.data.member_id) !== request.payload.member_id ||
        raw.data.removed !== true
      )
        fail();
    }
  } else {
    if (
      !exact(raw, ["schema", "ok", "operation_id", "operation", "actor_id", "project_id", "error"])
    )
      fail();
    if (!exact(raw.error, ["status_code", "detail", "headers", "rollback"])) fail();
    const status = integer(raw.error.status_code);
    if (status < 400n || status > 599n) fail();
    if (raw.error.headers !== null) {
      if (!isRecord(raw.error.headers)) fail();
      for (const [key, value] of Object.entries(raw.error.headers)) {
        strictString(key);
        strictString(value);
      }
    }
    bool(raw.error.rollback);
  }
  return raw;
}

function validatePublicMember(value) {
  if (
    !exact(value, [
      "id",
      "user_id",
      "user_name",
      "user_email",
      "role",
      "platform_role",
      "version",
      "assigned_at",
      "updated_at",
      "avatar_ref",
    ])
  )
    fail();
  uuid(value.id);
  uuid(value.user_id);
  strictString(value.user_name);
  strictString(value.user_email);
  strictString(value.role);
  strictString(value.platform_role);
  integer(value.version);
  awareDateTime(value.assigned_at);
  if (value.updated_at !== null) awareDateTime(value.updated_at);
  nullableString(value.avatar_ref);
  return value;
}

function validatePublicBody(operation, value) {
  if (operation === "list_members") {
    if (!Array.isArray(value)) fail();
    for (const member of value) validatePublicMember(member);
    return value;
  }
  if (operation === "add_member" || operation === "change_role") return validatePublicMember(value);
  if (operation === "remove_member") {
    if (value !== null) fail();
    return value;
  }
  return validatePreview(value);
}

function compareValue(actual, expected) {
  return (
    stringifyLossless(actual, { sortKeys: true }) ===
    stringifyLossless(expected, { sortKeys: true })
  );
}

function normalizePublic(value, aliases, key = null) {
  if (Array.isArray(value)) return value.map((item) => normalizePublic(item, aliases, key));
  if (isRecord(value)) {
    const result = {};
    for (const [childKey, child] of Object.entries(value)) {
      result[aliases[childKey] ?? childKey] = normalizePublic(child, aliases, childKey);
    }
    return result;
  }
  if (typeof value === "string") {
    if (aliases[value]) return `$${aliases[value]}`;
    if (["assigned_at", "updated_at", "created_at"].includes(key)) return "<timestamp>";
    if (key === "preview_token") return "<digest>";
  }
  return value;
}

function validateNativeMember(value) {
  validateMember(value);
  return value;
}

function validateNativeFacts(value, request) {
  if (
    !exact(value, [
      "operation_id",
      "operation",
      "actor_id",
      "project_id",
      "kind",
      "transaction_id",
      "actor",
      "project",
      "member",
      "accounts",
      "receiver_memberships",
      "eligible_reviewer_ids",
      "labeling",
    ])
  )
    fail();
  if (
    uuid(value.operation_id) !== request.operation_id ||
    value.operation !== request.operation ||
    uuid(value.actor_id) !== request.actor_id ||
    uuid(value.project_id) !== request.project_id ||
    value.kind !== "facts"
  )
    fail();
  strictString(value.transaction_id);
  if (!exact(value.actor, ["id", "name", "email", "role", "is_active"])) fail();
  uuid(value.actor.id);
  strictString(value.actor.name);
  strictString(value.actor.email);
  strictString(value.actor.role);
  bool(value.actor.is_active);
  if (uuid(value.actor.id) !== request.actor_id) fail();
  if (!exact(value.project, ["id", "owner_id"])) fail();
  if (uuid(value.project.id) !== request.project_id) fail();
  nullableUuid(value.project.owner_id);
  if (value.member !== null) validateNativeMember(value.member);
  if (
    !Array.isArray(value.accounts) ||
    !Array.isArray(value.receiver_memberships) ||
    !Array.isArray(value.eligible_reviewer_ids)
  )
    fail();
  for (const account of value.accounts) {
    if (!exact(account, ["id", "role", "is_active"])) fail();
    uuid(account.id);
    strictString(account.role);
    bool(account.is_active);
  }
  for (const member of value.receiver_memberships) {
    if (!exact(member, ["id", "project_id", "user_id", "role", "version"])) fail();
    uuid(member.id);
    uuid(member.project_id);
    uuid(member.user_id);
    strictString(member.role);
    integer(member.version);
  }
  for (const id of value.eligible_reviewer_ids) uuid(id);
  if (request.operation === "add_member") {
    if (value.member !== null || value.labeling !== null) fail();
  }
  return value;
}

function validateNativeReply(request, raw) {
  if (!isRecord(raw) || raw.schema !== nativeSchema || typeof raw.ok !== "boolean") fail();
  if (!raw.ok) {
    if (
      !exact(raw, ["schema", "ok", "error"]) ||
      !exact(raw.error, ["status_code", "detail", "headers", "rollback"])
    )
      fail();
    const status = integer(raw.error.status_code);
    if (status < 400n || status > 599n) fail();
    bool(raw.error.rollback);
    if (raw.error.headers !== null && !isRecord(raw.error.headers)) fail();
    return raw;
  }
  if (!exact(raw, ["schema", "ok", "data"])) fail();
  const data = raw.data;
  if (
    !isRecord(data) ||
    uuid(data.operation_id) !== request.operation_id ||
    data.operation !== request.operation ||
    uuid(data.actor_id) !== request.actor_id ||
    uuid(data.project_id) !== request.project_id
  )
    fail();
  if (data.kind === "replay") {
    if (!exact(data, ["operation_id", "operation", "actor_id", "project_id", "kind", "reply"]))
      fail();
    validatePublicReply(request, data.reply);
  } else if (data.kind === "facts") {
    validateNativeFacts(data, request);
  } else if (data.kind === "applied") {
    if (!exact(data, ["operation_id", "operation", "actor_id", "project_id", "kind", "result"]))
      fail();
    validateAppliedData(request, data.result);
  } else {
    fail();
  }
  return raw;
}

function validateAppliedData(request, value) {
  if (request.operation === "add_member" || request.operation === "change_role") {
    if (!exact(value, ["member"])) fail();
    validateMember(value.member);
    if (value.member.project_id !== request.project_id) fail();
    if (
      request.operation === "add_member" &&
      (value.member.user_id !== request.payload.target_user_id ||
        value.member.role !== request.payload.project_role)
    )
      fail();
    if (
      request.operation === "change_role" &&
      (value.member.id !== request.payload.member_id ||
        value.member.role !== request.payload.target_role)
    )
      fail();
  } else if (request.operation === "preview_role_change") {
    validatePreview(value);
  } else if (
    !exact(value, ["member_id", "removed"]) ||
    uuid(value.member_id) !== request.payload.member_id ||
    value.removed !== true
  ) {
    fail();
  }
}

function nativeRequest(request) {
  if (request.operation !== "change_role") return request;
  return {
    ...request,
    payload: {
      ...request.payload,
      preview_token: stringifyLossless(request.payload.preview_token, { pythonStrings: true }),
      reason: stringifyLossless(request.payload.reason, { pythonStrings: true }),
    },
  };
}

function validateNativePolicy(value) {
  if (
    !exact(value, ["schema", "operations", "opaque_text_codec"]) ||
    value.schema !== "aap-workspace-membership-native/v1" ||
    value.opaque_text_codec !== "json-string-ascii/v1" ||
    !Array.isArray(value.operations)
  )
    fail();
  const expected = ["add_member", "preview_role_change", "change_role", "remove_member"];
  if (
    value.operations.length !== expected.length ||
    expected.some((operation) => !value.operations.includes(operation)) ||
    new Set(value.operations).size !== expected.length
  )
    fail();
}

function validatePublicCall(value) {
  const match =
    /^\/api\/v1\/projects\/([0-9a-f-]{36})\/members(?:\/([0-9a-f-]{36})(?:\/(role(?:\/preview)?))?)?$/.exec(
      value.path,
    );
  if (!match) fail();
  uuid(match[1]);
  const memberId = match[2] === undefined ? null : uuid(match[2]);
  const suffix = match[3] ?? null;
  if (value.operation === "list_members") {
    if (value.method !== "GET" || memberId !== null || value.body !== null) fail();
    return;
  }
  if (value.operation === "add_member") {
    if (
      value.method !== "POST" ||
      memberId !== null ||
      value.body === null ||
      !exact(value.body, ["user_id", "role"])
    )
      fail();
    uuid(value.body.user_id);
    strictString(value.body.role);
    return;
  }
  if (value.operation === "preview_role_change") {
    if (
      value.method !== "POST" ||
      memberId === null ||
      suffix !== "role/preview" ||
      value.body === null
    )
      fail();
    const allowed = ["project_role", "replacement_annotator_id", "replacement_reviewer_id"];
    if (
      !isRecord(value.body) ||
      Object.keys(value.body).some((key) => !allowed.includes(key)) ||
      !Object.hasOwn(value.body, "project_role")
    )
      fail();
    strictString(value.body.project_role);
    for (const key of ["replacement_annotator_id", "replacement_reviewer_id"]) {
      if (Object.hasOwn(value.body, key)) nullableUuid(value.body[key]);
    }
    return;
  }
  if (value.operation === "change_role") {
    if (value.method !== "PATCH" || memberId === null || suffix !== "role" || value.body === null)
      fail();
    const required = ["project_role", "expected_version", "preview_token", "reason"];
    const allowed = [...required, "replacement_annotator_id", "replacement_reviewer_id"];
    if (
      !isRecord(value.body) ||
      required.some((key) => !Object.hasOwn(value.body, key)) ||
      Object.keys(value.body).some((key) => !allowed.includes(key))
    )
      fail();
    strictString(value.body.project_role);
    integer(value.body.expected_version);
    strictString(value.body.preview_token);
    strictString(value.body.reason);
    for (const key of ["replacement_annotator_id", "replacement_reviewer_id"]) {
      if (Object.hasOwn(value.body, key)) nullableUuid(value.body[key]);
    }
    return;
  }
  if (value.operation === "remove_member") {
    if (value.method !== "DELETE" || memberId === null || suffix !== null || value.body !== null)
      fail();
    return;
  }
  fail();
}

function validateCallInput(value) {
  if (!isRecord(value)) fail();
  if (value.mode === "public_http") {
    if (
      !exact(value, [
        "mode",
        "origin",
        "bearer_token",
        "method",
        "path",
        "body",
        "operation",
        "expected",
        "aliases",
      ])
    )
      fail();
    if (
      typeof value.origin !== "string" ||
      typeof value.bearer_token !== "string" ||
      value.bearer_token.length === 0
    )
      fail();
    const origin = new URL(value.origin);
    if (
      !["http:", "https:"].includes(origin.protocol) ||
      !["127.0.0.1", "localhost", "::1"].includes(origin.hostname)
    )
      fail();
    if (
      !["GET", "POST", "PATCH", "DELETE"].includes(value.method) ||
      typeof value.path !== "string" ||
      !value.path.startsWith("/api/v1/projects/")
    )
      fail();
    if (value.body !== null && !isRecord(value.body)) fail();
    if (
      !publicOperations.has(value.operation) ||
      !exact(value.expected, ["status", "body"]) ||
      !isRecord(value.aliases)
    )
      fail();
    integer(value.expected.status);
    validatePublicCall(value);
    return {
      mode: value.mode,
      origin: origin.origin,
      bearerToken: value.bearer_token,
      method: value.method,
      path: value.path,
      body: value.body,
      operation: value.operation,
      expected: value.expected,
      aliases: value.aliases,
    };
  }
  if (value.mode === "owner_http") {
    if (
      !exact(value, ["mode", "origin", "control_token", "request"]) ||
      typeof value.origin !== "string" ||
      typeof value.control_token !== "string" ||
      value.control_token.length === 0
    )
      fail();
    const origin = new URL(value.origin);
    if (
      !["http:", "https:"].includes(origin.protocol) ||
      !["127.0.0.1", "localhost", "::1"].includes(origin.hostname)
    )
      fail();
    return {
      mode: value.mode,
      origin: origin.origin,
      controlToken: value.control_token,
      request: validateRequest(value.request),
    };
  }
  if (value.mode === "native") {
    if (
      !exact(value, ["mode", "container_id", "database", "transaction", "request"]) ||
      typeof value.container_id !== "string" ||
      !/^[0-9a-f]{64}$/.test(value.container_id) ||
      typeof value.database !== "string" ||
      !/^[a-z0-9_]+_test$/.test(value.database) ||
      !["commit", "rollback"].includes(value.transaction)
    )
      fail();
    const request = validateRequest(value.request);
    if (request.operation !== "add_member") fail();
    return {
      mode: value.mode,
      containerId: value.container_id,
      database: value.database,
      transaction: value.transaction,
      request,
    };
  }
  fail();
}

function requestJson(origin, path, token, method, payload) {
  const target = new URL(path, origin);
  const body = payload === null ? "" : stringifyLossless(payload);
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/json",
  };
  if (body !== "") {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = Buffer.byteLength(body);
  }
  return new Promise((resolveResponse, reject) => {
    const request = (target.protocol === "https:" ? httpsRequest : httpRequest)(
      target,
      {
        method,
        headers,
        timeout: 15_000,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          try {
            const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
            resolveResponse({
              status: response.statusCode ?? 0,
              contentType: response.headers["content-type"] ?? "",
              text,
            });
          } catch {
            reject(new Error("invalid response"));
          }
        });
        response.on("error", () => reject(new Error("request failed")));
      },
    );
    request.on("error", () => reject(new Error("request failed")));
    request.on("timeout", () => request.destroy(new Error("request timeout")));
    request.end(body);
  });
}

function validateOwnerHttpResponse(request, response) {
  if (response.status !== 200 || !response.contentType.toLowerCase().includes("application/json"))
    fail();
  const reply = validatePublicReply(request, parseStrictJson(response.text));
  return { status: response.status, reply };
}

async function runPublicHttp(input) {
  phase = "public HTTP request";
  const response = await requestJson(
    input.origin,
    input.path,
    input.bearerToken,
    input.method,
    input.body,
  );
  const status = BigInt(response.status);
  phase = `public HTTP status ${response.status}`;
  if (status !== integer(input.expected.status)) fail();
  phase = "public HTTP response encoding";
  if (status !== 204n && !response.contentType.toLowerCase().includes("application/json")) fail();
  if (status === 204n && response.text !== "") fail();
  const body = response.text === "" ? null : parseStrictJson(response.text);
  phase = "public HTTP response contract";
  if (status >= 200n && status < 300n) validatePublicBody(input.operation, body);
  else if (!exact(body, ["detail"])) fail();
  if (
    input.operation === "add_member" &&
    status === 201n &&
    input.expected.body.id === "$membership_added" &&
    !Object.hasOwn(input.aliases, body.id)
  ) {
    input.aliases = { ...input.aliases, [body.id]: "membership_added" };
  }
  const actualNormalized = normalizePublic(body, input.aliases);
  const expectedNormalized = normalizePublic(input.expected.body, input.aliases);
  if (input.operation === "list_members" && status === 200n) {
    for (let index = 1; index < body.length; index += 1) {
      if (Date.parse(body[index].assigned_at) > Date.parse(body[index - 1].assigned_at)) fail();
    }
    // The legacy capture shares a transaction timestamp; tie order is unspecified.
    const byUser = (left, right) => left.user_id.localeCompare(right.user_id);
    actualNormalized.sort(byUser);
    expectedNormalized.sort(byUser);
  }
  phase = "public HTTP legacy comparison";
  if (!compareValue(actualNormalized, expectedNormalized)) fail();
  return { ok: true, status: response.status, body };
}

function linesReader(stream) {
  const queue = [];
  const waiters = [];
  let ended = false;
  const reader = createInterface({ input: stream, crlfDelay: Infinity });
  reader.on("line", (line) => {
    const waiter = waiters.shift();
    if (waiter) waiter(line);
    else queue.push(line);
  });
  reader.on("close", () => {
    ended = true;
    for (const waiter of waiters.splice(0)) waiter(null);
  });
  return {
    async next() {
      if (queue.length) return queue.shift();
      if (ended) return null;
      return new Promise((resolve) => waiters.push(resolve));
    },
    async until(marker) {
      const result = [];
      while (true) {
        const line = await this.next();
        if (line === null) fail();
        if (line === marker) return result;
        if (line.length > 0) result.push(line);
      }
    },
  };
}

async function runNative(input) {
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
    nativeRole,
    "-d",
    input.database,
    "-v",
    "ON_ERROR_STOP=1",
  ];
  const child = spawn("docker", args, {
    stdio: ["pipe", "pipe", "pipe"],
    env: { PATH: process.env.PATH ?? "" },
  });
  const stdout = linesReader(child.stdout);
  let stderr = "";
  const watchdog = setTimeout(() => child.kill("SIGKILL"), 45_000);
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString("utf8");
  });
  const exit = new Promise((resolveExit) => child.once("close", (code) => resolveExit(code)));
  let markerIndex = 0;
  async function query(sql) {
    const marker = `__workspace_membership_${markerIndex++}__`;
    child.stdin.write(`${sql}\n\\echo ${marker}\n`);
    return stdout.until(marker);
  }
  try {
    const probe = await query(
      [
        "SELECT current_user || '|' || session_user || '|' || current_database() || '|' ||",
        "CASE WHEN has_table_privilege(current_user, 'public.users', 'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user, 'public.users', 'SELECT') OR has_any_column_privilege(current_user, 'public.users', 'INSERT') OR has_any_column_privilege(current_user, 'public.users', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_table_privilege(current_user, 'public.projects', 'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user, 'public.projects', 'SELECT') OR has_any_column_privilege(current_user, 'public.projects', 'INSERT') OR has_any_column_privilege(current_user, 'public.projects', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_table_privilege(current_user, 'public.project_members', 'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user, 'public.project_members', 'SELECT') OR has_any_column_privilege(current_user, 'public.project_members', 'INSERT') OR has_any_column_privilege(current_user, 'public.project_members', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_table_privilege(current_user, 'public.tasks', 'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user, 'public.tasks', 'SELECT') OR has_any_column_privilege(current_user, 'public.tasks', 'INSERT') OR has_any_column_privilege(current_user, 'public.tasks', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_table_privilege(current_user, 'public.task_batches', 'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user, 'public.task_batches', 'SELECT') OR has_any_column_privilege(current_user, 'public.task_batches', 'INSERT') OR has_any_column_privilege(current_user, 'public.task_batches', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_table_privilege(current_user, 'public.task_locks', 'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user, 'public.task_locks', 'SELECT') OR has_any_column_privilege(current_user, 'public.task_locks', 'INSERT') OR has_any_column_privilege(current_user, 'public.task_locks', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_table_privilege(current_user, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user, 'public.audit_logs', 'SELECT') OR has_any_column_privilege(current_user, 'public.audit_logs', 'INSERT') OR has_any_column_privilege(current_user, 'public.audit_logs', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_table_privilege(current_user, 'public.project_templates', 'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user, 'public.project_templates', 'SELECT') OR has_any_column_privilege(current_user, 'public.project_templates', 'INSERT') OR has_any_column_privilege(current_user, 'public.project_templates', 'UPDATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_schema_privilege(current_user, 'aap_workspace_membership_v1', 'USAGE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_schema_privilege(current_user, 'aap_workspace_membership_v1', 'CREATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_database_privilege(current_user, current_database(), 'CREATE') THEN 'true' ELSE 'false' END || '|' ||",
        "CASE WHEN has_database_privilege(current_user, current_database(), 'TEMP') THEN 'true' ELSE 'false' END || '|' ||",
        "(SELECT count(*)::text FROM pg_catalog.pg_proc AS proc JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=proc.pronamespace WHERE namespace.nspname='aap_workspace_membership_v1' AND has_function_privilege(current_user, proc.oid, 'EXECUTE'));",
      ].join("\n"),
    );
    if (probe.length !== 1) fail();
    const fields = probe[0].split("|");
    if (
      fields.length !== 16 ||
      fields[0] !== nativeRole ||
      fields[1] !== nativeRole ||
      fields[2] !== input.database ||
      fields.slice(3, 11).some((field) => field !== "false") ||
      fields[11] !== "true" ||
      fields.slice(12, 15).some((field) => field !== "false") ||
      fields[15] !== "3"
    )
      fail();

    const begin = await query("BEGIN;");
    if (begin.length !== 0) fail();
    const policyLines = await query("SELECT aap_workspace_membership_v1.membership_policy();");
    if (policyLines.length !== 1) fail();
    const policy = parseStrictJson(policyLines[0]);
    validateNativePolicy(policy);

    const requestJson = stringifyLossless(nativeRequest(input.request));
    const requestHex = Buffer.from(requestJson, "utf8").toString("hex");
    const prepareLines = await query(
      `SELECT aap_workspace_membership_v1.prepare_membership_command(convert_from(decode('${requestHex}', 'hex'), 'UTF8')::jsonb);`,
    );
    if (prepareLines.length !== 1) fail();
    const prepared = validateNativeReply(input.request, parseStrictJson(prepareLines[0]));
    if (!prepared.ok) fail();
    if (prepared.data.kind === "replay") {
      const endLines = await query(input.transaction === "commit" ? "COMMIT;" : "ROLLBACK;");
      if (endLines.length !== 0) fail();
      child.stdin.end("\\q\n");
      const code = await exit;
      if (code !== 0) fail();
      return {
        ok: true,
        current_user: fields[0],
        session_user: fields[1],
        database: fields[2],
        raw_application_table_access: false,
        executable_membership_functions: 3,
        transaction: input.transaction,
        replayed: true,
        reply: prepared,
      };
    }
    const facts = prepared.data;
    const actor = facts.actor;
    const account = facts.accounts.find((row) => row.id === input.request.payload.target_user_id);
    const canManage =
      actor.is_active &&
      (actor.role === "super_admin" ||
        (actor.role === "project_admin" && facts.project.owner_id === actor.id));
    const compatible =
      account?.is_active === true &&
      account.role === "employee" &&
      roles.has(input.request.payload.project_role);
    const accountIds = facts.accounts.map((row) => row.id).sort();
    const expectedAccountIds = [
      input.request.actor_id,
      input.request.payload.target_user_id,
    ].sort();
    // This peer deliberately exercises one simple, valid add; manager policy is qualified by the Workspace owner HTTP calls.
    if (
      !canManage ||
      !compatible ||
      facts.member !== null ||
      facts.labeling !== null ||
      facts.receiver_memberships.length !== 0 ||
      stringifyLossless(accountIds) !== stringifyLossless(expectedAccountIds)
    )
      fail();

    const apply = {
      schema: nativeSchema,
      request: nativeRequest(input.request),
      transaction_id: facts.transaction_id,
    };
    const applyHex = Buffer.from(stringifyLossless(apply), "utf8").toString("hex");
    const applyLines = await query(
      `SELECT aap_workspace_membership_v1.apply_membership_command(convert_from(decode('${applyHex}', 'hex'), 'UTF8')::jsonb);`,
    );
    if (applyLines.length !== 1) fail();
    const applied = validateNativeReply(input.request, parseStrictJson(applyLines[0]));
    if (!applied.ok || applied.data.kind !== "applied") fail();
    validateAppliedData(input.request, applied.data.result);

    const endLines = await query(input.transaction === "commit" ? "COMMIT;" : "ROLLBACK;");
    if (endLines.length !== 0) fail();
    child.stdin.end("\\q\n");
    const code = await exit;
    if (code !== 0) fail();
    return {
      ok: true,
      current_user: fields[0],
      session_user: fields[1],
      database: fields[2],
      raw_application_table_access: false,
      executable_membership_functions: 3,
      transaction: input.transaction,
      reply: applied,
    };
  } catch {
    child.kill();
    await exit;
    void stderr;
    fail();
  } finally {
    clearTimeout(watchdog);
  }
}

function reject(callback) {
  try {
    callback();
  } catch {
    return;
  }
  fail();
}

function selfCheck() {
  let checks = 0;
  const ids = {
    operation: "11111111-1111-4111-8111-111111111111",
    actor: "22222222-2222-4222-8222-222222222222",
    project: "33333333-3333-4333-8333-333333333333",
    member: "44444444-4444-4444-8444-444444444444",
    target: "55555555-5555-4555-8555-555555555555",
  };
  const context = {
    method: null,
    path: null,
    ip: null,
    request_id: null,
  };
  const requests = [
    {
      schema: wireSchema,
      operation_id: ids.operation,
      operation: "add_member",
      actor_id: ids.actor,
      project_id: ids.project,
      payload: { target_user_id: ids.target, project_role: "annotator" },
      audit_context: context,
    },
    {
      schema: wireSchema,
      operation_id: ids.operation,
      operation: "preview_role_change",
      actor_id: ids.actor,
      project_id: ids.project,
      payload: {
        member_id: ids.member,
        target_role: "viewer",
        replacement_annotator_id: null,
        replacement_reviewer_id: null,
      },
      audit_context: context,
    },
    {
      schema: wireSchema,
      operation_id: ids.operation,
      operation: "change_role",
      actor_id: ids.actor,
      project_id: ids.project,
      payload: {
        member_id: ids.member,
        target_role: "reviewer",
        expected_version: new JsonNumber("1"),
        preview_token: "",
        reason: "  Unicode ß 😀  ",
        replacement_annotator_id: null,
        replacement_reviewer_id: null,
      },
      audit_context: context,
    },
    {
      schema: wireSchema,
      operation_id: ids.operation,
      operation: "remove_member",
      actor_id: ids.actor,
      project_id: ids.project,
      payload: { member_id: ids.member },
      audit_context: context,
    },
  ].map(validateRequest);
  if (requests.length !== 4 || requests[0].payload.project_role !== "annotator") fail();
  const legacyRoleRequest = validateRequest({
    ...requests[0],
    payload: { ...requests[0].payload, project_role: "legacy role" },
  });
  if (legacyRoleRequest.payload.project_role !== "legacy role") fail();
  checks += 5;
  for (const [value, encoded] of [
    ["x\u0000y", '"x\\u0000y"'],
    ["\\u0000", '"\\\\u0000"'],
    ["\u007f", '"\\u007f"'],
    ["ß 中 😀", '"\\u00df \\u4e2d \\ud83d\\ude00"'],
    [' "\\\n', '" \\"\\\\\\n"'],
    ["null", '"null"'],
  ]) {
    const request = {
      ...requests[2],
      payload: { ...requests[2].payload, reason: value, preview_token: value },
    };
    const carried = nativeRequest(request);
    if (
      carried.payload.reason !== encoded ||
      carried.payload.preview_token !== encoded ||
      request.payload.reason !== value
    )
      fail();
    checks += 1;
  }

  validateCallInput({
    mode: "public_http",
    origin: "http://127.0.0.1:8000",
    bearer_token: "private-test-token",
    method: "POST",
    path: `/api/v1/projects/${ids.project}/members`,
    body: { user_id: ids.target, role: "annotator" },
    operation: "add_member",
    expected: { status: new JsonNumber("201"), body: {} },
    aliases: {},
  });
  validateCallInput({
    mode: "owner_http",
    origin: "http://127.0.0.1:8172",
    control_token: "private-test-control",
    request: requests[0],
  });
  validateCallInput({
    mode: "native",
    container_id: "a".repeat(64),
    database: "workspace_test",
    transaction: "commit",
    request: requests[0],
  });
  checks += 3;

  reject(() => validateRequest({ ...requests[0], schema_version: requests[0].schema }));
  reject(() => validateRequest({ ...requests[0], extra: true }));
  reject(() =>
    validateRequest({
      ...requests[1],
      payload: { ...requests[1].payload, replacement_annotator_id: undefined },
    }),
  );
  reject(() =>
    validateRequest({
      ...requests[2],
      payload: { ...requests[2].payload, expected_version: true },
    }),
  );
  reject(() => parseStrictJson('{"operation_id":"a","operation_id":"b"}'));
  reject(() => parseStrictJson('{"nested":{"x":NaN}}'));
  reject(() => parseStrictJson(`${"[".repeat(257)}0${"]".repeat(257)}`));
  checks += 7;

  const sampleSnapshot = { z: "ß😀", a: new JsonNumber("1") };
  const digest = createHash("sha256")
    .update(stringifyLossless(sampleSnapshot, { sortKeys: true, pythonStrings: true }), "utf8")
    .digest("hex");
  if (digest !== "63f48b43bf378905b92b6a476b345432424c3f7efd2be69a5fb9f869505469a0") fail();
  checks += 1;

  const created = "2026-01-02T03:04:05.123456+00:00";
  const member = {
    id: ids.member,
    project_id: ids.project,
    user_id: ids.target,
    role: "annotator",
    assigned_by: ids.actor,
    version: new JsonNumber("1"),
    assigned_at: created,
    updated_at: created,
    weekly_target: null,
  };
  validateMember(member);
  const validSuccess = {
    schema: wireSchema,
    ok: true,
    operation_id: ids.operation,
    operation: "add_member",
    actor_id: ids.actor,
    project_id: ids.project,
    data: { member },
  };
  validatePublicReply(requests[0], validSuccess);
  reject(() => validatePublicReply(requests[0], { ...validSuccess, secret: "forbidden" }));
  reject(() => validatePublicReply(requests[0], { ...validSuccess, project_id: ids.target }));
  checks += 3;

  const snapshot = {
    member: {
      id: ids.member,
      user_id: ids.target,
      role: "annotator",
      version: new JsonNumber("1"),
    },
    member_account: { exists: true, is_active: true, platform_role: "employee" },
    project: { id: ids.project, owner_id: ids.actor },
    annotation_task_ids: [],
    review_task_ids: [],
    unassigned_review_task_ids: [],
    lock_task_ids: [],
    review_claim_task_ids: [],
    materialization_annotation_task_ids: [],
    materialization_review_task_ids: [],
    batch_annotator_ids: [],
    batch_reviewer_ids: [],
    eligible_reviewer_ids: [],
    tasks: {},
    batches: {},
    receivers: { annotator: null, reviewer: null },
  };
  const previewData = {
    member_id: ids.member,
    user_id: ids.target,
    current_role: "annotator",
    current_version: new JsonNumber("1"),
    target_role: "viewer",
    requires_handoff: false,
    blockers: [],
    blocker_details: [],
    resource_snapshot: snapshot,
    preview_token: createHash("sha256")
      .update(stringifyLossless(snapshot, { sortKeys: true, pythonStrings: true }), "utf8")
      .digest("hex"),
  };
  validatePreview(previewData);
  validatePublicReply(requests[1], {
    schema: wireSchema,
    ok: true,
    operation_id: ids.operation,
    operation: "preview_role_change",
    actor_id: ids.actor,
    project_id: ids.project,
    data: previewData,
  });
  checks += 2;

  const nativeFacts = {
    operation_id: ids.operation,
    operation: "add_member",
    actor_id: ids.actor,
    project_id: ids.project,
    kind: "facts",
    transaction_id: "42",
    actor: {
      id: ids.actor,
      name: "Owner",
      email: "owner@example.test",
      role: "project_admin",
      is_active: true,
    },
    project: { id: ids.project, owner_id: ids.actor },
    member: null,
    accounts: [
      { id: ids.actor, role: "project_admin", is_active: true },
      { id: ids.target, role: "employee", is_active: true },
    ],
    receiver_memberships: [],
    eligible_reviewer_ids: [],
    labeling: null,
  };
  validateNativeReply(requests[0], { schema: nativeSchema, ok: true, data: nativeFacts });
  const nativeApplied = {
    operation_id: ids.operation,
    operation: "add_member",
    actor_id: ids.actor,
    project_id: ids.project,
    kind: "applied",
    result: { member },
  };
  validateNativeReply(requests[0], { schema: nativeSchema, ok: true, data: nativeApplied });
  checks += 2;

  const legacyFixture = resolve(
    process.cwd(),
    "apps/api/tests/fixtures/workspace_membership_legacy.json",
  );
  let digestVectors = 0;
  try {
    const fixture = parseStrictJson(
      new TextDecoder("utf-8", { fatal: true }).decode(requireFile(legacyFixture)),
    );
    if (fixture.kind !== "actual-legacy-workspace-membership" || !isRecord(fixture.digest_vectors))
      fail();
    for (const vector of Object.values(fixture.digest_vectors)) {
      const actual = createHash("sha256")
        .update(stringifyLossless(vector.snapshot, { sortKeys: true, pythonStrings: true }), "utf8")
        .digest("hex");
      if (actual !== vector.preview_token) fail();
      digestVectors += 1;
    }
    if (digestVectors !== 8) fail();
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  checks += digestVectors;
  return { checks, digest_vectors: digestVectors };
}

function requireFile(path) {
  return readFileSync(path);
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
}

async function main() {
  if (process.argv[2] === "--self-check") {
    const result = selfCheck();
    process.stdout.write(`${stringifyLossless({ ok: true, ...result })}\n`);
    return;
  }
  const raw = parseStrictJson(await readStdin());
  const input = validateCallInput(raw);
  if (input.mode === "public_http") {
    const result = await runPublicHttp(input);
    process.stdout.write(`${stringifyLossless(result)}\n`);
    return;
  }
  if (input.mode === "owner_http") {
    const result = validateOwnerHttpResponse(
      input.request,
      await requestJson(input.origin, nativeEndpoint, input.controlToken, "POST", input.request),
    );
    process.stdout.write(`${stringifyLossless({ ok: true, ...result })}\n`);
    return;
  }
  const result = await runNative(input);
  process.stdout.write(`${stringifyLossless(result)}\n`);
}

if (process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch(() => {
    process.stderr.write(`Workspace membership peer failed (${phase})\n`);
    process.exitCode = 1;
  });
}
