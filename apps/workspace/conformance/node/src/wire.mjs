import { TextDecoder } from "node:util";

export const SCHEMA = "aap-workspace-owner-transfer/v1";
export const ENDPOINT = "/v1/workspace/owner-transfer/execute";
export const ROLE = "workspace_owner_transfer_client";
export const MAX_BODY_BYTES = 16 * 1024;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const BUSY_STATES = new Set(["55P03", "40001", "40P01"]);
const INTEGER = /^-?(?:0|[1-9][0-9]*)$/;

class JsonNumber {
  constructor(raw) {
    this.raw = raw;
  }
}

function invalid() {
  throw new Error("Invalid Workspace owner transfer wire value");
}

function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exact(value, keys) {
  return (
    record(value) &&
    Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
  );
}

function scanJson(source) {
  let offset = 0;
  let depth = 0;
  const whitespace = () => {
    while (offset < source.length && /[\t\n\r ]/.test(source[offset])) offset += 1;
  };
  const string = () => {
    if (source[offset] !== '"') invalid();
    const start = offset++;
    while (offset < source.length) {
      if (source[offset] === '"') {
        offset += 1;
        try {
          return JSON.parse(source.slice(start, offset));
        } catch {
          invalid();
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
    invalid();
  };
  const value = () => {
    whitespace();
    depth += 1;
    if (depth > 64) invalid();
    if (source[offset] === "{") {
      offset += 1;
      whitespace();
      const keys = new Set();
      if (source[offset] !== "}") {
        while (true) {
          whitespace();
          const key = string();
          if (keys.has(key)) invalid();
          keys.add(key);
          whitespace();
          if (source[offset++] !== ":") invalid();
          value();
          whitespace();
          if (source[offset] === "}") break;
          if (source[offset++] !== ",") invalid();
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
          if (source[offset++] !== ",") invalid();
        }
      }
      offset += 1;
    } else if (source[offset] === '"') {
      string();
    } else {
      const start = offset;
      while (offset < source.length && !/[\t\n\r ,\]}]/.test(source[offset])) offset += 1;
      if (start === offset) invalid();
    }
    depth -= 1;
  };

  value();
  whitespace();
  if (offset !== source.length) invalid();
}

export function parseJson(value) {
  let source;
  try {
    source =
      typeof value === "string" ? value : new TextDecoder("utf-8", { fatal: true }).decode(value);
    if (Buffer.byteLength(source, "utf8") > MAX_BODY_BYTES) invalid();
    scanJson(source);
    return JSON.parse(source, (_key, item, context) => {
      if (typeof item !== "number") return item;
      if (typeof context?.source !== "string") invalid();
      return new JsonNumber(context.source);
    });
  } catch {
    invalid();
  }
}

function uuid(value) {
  if (typeof value !== "string" || !UUID.test(value)) invalid();
  return value.toLowerCase();
}

function bool(value) {
  if (typeof value !== "boolean") invalid();
  return value;
}

function awareTimestamp(value) {
  if (typeof value !== "string") return false;
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|([+-])(\d{2}):(\d{2}))$/.exec(
      value,
    );
  if (!match) return false;
  const [
    ,
    yearText,
    monthText,
    dayText,
    hourText,
    minuteText,
    secondText,
    zone,
    ,
    zoneHourText,
    zoneMinuteText,
  ] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const zoneHour = zoneHourText === undefined ? 0 : Number(zoneHourText);
  const zoneMinute = zoneMinuteText === undefined ? 0 : Number(zoneMinuteText);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth =
    [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1] ?? 0;
  return (
    year >= 1 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= daysInMonth &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59 &&
    zone.length > 0 &&
    zoneHour < 24 &&
    zoneMinute <= 59 &&
    Number.isFinite(Date.parse(value))
  );
}

export function validateRequest(value) {
  const keys = ["schema", "operation_id", "actor_id", "project_id", "new_owner_id"];
  if (!exact(value, keys) || value.schema !== SCHEMA) invalid();
  return {
    schema: SCHEMA,
    operation_id: uuid(value.operation_id),
    actor_id: uuid(value.actor_id),
    project_id: uuid(value.project_id),
    new_owner_id: uuid(value.new_owner_id),
  };
}

export function validatePolicy(value) {
  if (!exact(value, ["schema"]) || value.schema !== SCHEMA) invalid();
  return value;
}

function strictStatusCode(value) {
  if (value instanceof JsonNumber) {
    if (!INTEGER.test(value.raw)) invalid();
    const number = BigInt(value.raw);
    return number >= 400n && number <= 599n;
  }
  return Number.isSafeInteger(value) && value >= 400 && value <= 599;
}

export function validateReply(requestValue, value) {
  const request = validateRequest(requestValue);
  if (
    !exact(value, [
      "schema",
      "ok",
      ...(value?.ok === true
        ? ["operation_id", "actor_id", "project_id", "new_owner_id", "data"]
        : ["error"]),
    ]) ||
    value.schema !== SCHEMA
  )
    invalid();
  if (value.ok === true) {
    if (
      uuid(value.operation_id) !== request.operation_id ||
      uuid(value.actor_id) !== request.actor_id ||
      uuid(value.project_id) !== request.project_id ||
      uuid(value.new_owner_id) !== request.new_owner_id ||
      !exact(value.data, ["previous_owner_id", "updated_at", "changed"])
    )
      invalid();
    const previous =
      value.data.previous_owner_id === null ? null : uuid(value.data.previous_owner_id);
    if (
      !awareTimestamp(value.data.updated_at) ||
      bool(value.data.changed) !== (previous !== request.new_owner_id)
    )
      invalid();
    return value;
  }
  if (value.ok !== false || !exact(value.error, ["status_code", "detail", "rollback"])) invalid();
  if (!strictStatusCode(value.error.status_code)) invalid();
  bool(value.error.rollback);
  return value;
}

export function lockFailure(error) {
  const state = typeof error?.code === "string" ? error.code : "";
  const marker = typeof error?.hint === "string" ? error.hint.split("\n", 1)[0] : "";
  if (!BUSY_STATES.has(state)) return null;
  if (marker === "aap.workspace.owner_transfer.account_lock") {
    return {
      schema: SCHEMA,
      ok: false,
      error: { status_code: 409, detail: "账号正在更新，请刷新后重试", rollback: true },
    };
  }
  if (marker === "aap.workspace.owner_transfer.project_lock") {
    return {
      schema: SCHEMA,
      ok: false,
      error: { status_code: 409, detail: { reason: "project_busy" }, rollback: false },
    };
  }
  return null;
}
