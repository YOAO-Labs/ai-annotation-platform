import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

const schema = "aap-identity/v1";
const maxInputBytes = 64 * 1024;
const maxResponseBytes = 64 * 1024;
const requestDeadlineMs = 15_000;
const maxGeneration = (1n << 63n) - 1n;
const failureCodes = new Set([
  "invalid_request",
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
]);

function fail() {
  throw new Error("Invalid Identity peer input or response");
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value, keys) {
  return (
    isRecord(value) &&
    Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
  );
}

function parseJsonPreservingGeneration(source) {
  return JSON.parse(source, (key, value, context) => {
    if (key !== "generation" || value === null) return value;
    const numberSource = context?.source;
    if (typeof numberSource !== "string" || !/^(0|[1-9][0-9]*)$/.test(numberSource)) {
      fail();
    }
    const generation = BigInt(numberSource);
    if (generation > maxGeneration) fail();
    return generation;
  });
}

function uuid(value) {
  if (
    typeof value !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
  ) {
    fail();
  }
  return value.toLowerCase();
}

function validDateTime(value) {
  const match =
    typeof value === "string" &&
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(
      value,
    );
  if (!match) return false;

  const [, yearText, monthText, dayText, hourText, minuteText, secondText, , zone] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    year < 1 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > days[month - 1] ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    return false;
  }
  if (zone !== "Z") {
    const offsetHour = Number(zone.slice(1, 3));
    const offsetMinute = Number(zone.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) return false;
  }
  return Number.isFinite(Date.parse(value));
}

function principalReceipt(value) {
  const fields = [
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
  ];
  if (!hasExactKeys(value, fields)) fail();
  if (
    typeof value.name !== "string" ||
    typeof value.email !== "string" ||
    !["super_admin", "project_admin", "employee", "viewer"].includes(value.role) ||
    value.is_active !== true ||
    !["jwt", "api_key"].includes(value.credential_type)
  ) {
    fail();
  }

  const actorId = uuid(value.actor_id);
  const credentialId = uuid(value.credential_id);
  let generationDecimal;
  if (value.credential_type === "jwt") {
    if (
      typeof value.generation !== "bigint" ||
      value.scopes !== null ||
      !validDateTime(value.expires_at)
    ) {
      fail();
    }
    generationDecimal = value.generation.toString(10);
  } else {
    if (
      value.generation !== null ||
      !Array.isArray(value.scopes) ||
      value.scopes.some((scope) => typeof scope !== "string") ||
      (value.expires_at !== null && !validDateTime(value.expires_at))
    ) {
      fail();
    }
    generationDecimal = null;
  }

  return {
    actor_id: actorId,
    role: value.role,
    credential_type: value.credential_type,
    credential_id: credentialId,
    generation_decimal: generationDecimal,
    scopes: value.scopes,
    expires_at: value.expires_at,
  };
}

export function parseIdentityReceipt(body, status, contentType = "application/json") {
  if (
    typeof body !== "string" ||
    !Number.isInteger(status) ||
    status < 100 ||
    status > 599 ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }

  let document;
  try {
    document = parseJsonPreservingGeneration(body);
  } catch {
    fail();
  }
  if (!isRecord(document) || document.schema !== schema || typeof document.ok !== "boolean") {
    fail();
  }
  if (document.ok === true) {
    if (status !== 200 || !hasExactKeys(document, ["schema", "ok", "data"])) fail();
    return {
      status,
      schema,
      ok: true,
      principal: principalReceipt(document.data),
    };
  }
  if (!hasExactKeys(document, ["schema", "ok", "error"])) fail();
  if (
    !hasExactKeys(document.error, ["code"]) ||
    typeof document.error.code !== "string" ||
    !failureCodes.has(document.error.code)
  ) {
    fail();
  }
  return {
    status,
    schema,
    ok: false,
    error: { code: document.error.code },
  };
}

function validateInput(value) {
  if (!hasExactKeys(value, ["url", "control_token", "credential"])) fail();
  if (
    typeof value.url !== "string" ||
    value.url.length < 1 ||
    value.url.length > 2048 ||
    typeof value.control_token !== "string" ||
    !/^[\x21-\x7e]{1,4096}$/.test(value.control_token) ||
    typeof value.credential !== "string" ||
    value.credential.length < 1 ||
    value.credential.length > 16384
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
  return { origin, controlToken: value.control_token, credential: value.credential };
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > maxInputBytes) fail();
    chunks.push(chunk);
  }
  const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  return validateInput(JSON.parse(text));
}

function postCredential(origin, controlToken, credential) {
  const endpoint = new URL("/v1/identity/authenticate", origin);
  const transport = endpoint.protocol === "https:" ? httpsRequest : httpRequest;
  const body = Buffer.from(JSON.stringify({ token: credential }), "utf8");

  return new Promise((resolveResponse, rejectResponse) => {
    const request = transport(
      endpoint,
      {
        method: "POST",
        agent: false,
        signal: AbortSignal.timeout(requestDeadlineMs),
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
            const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
            resolveResponse({
              status: response.statusCode,
              contentType: response.headers["content-type"],
              text,
            });
          } catch {
            rejectResponse(new Error("invalid response"));
          }
        });
        response.on("error", () => rejectResponse(new Error("request failed")));
      },
    );
    request.on("error", () => rejectResponse(new Error("request failed")));
    request.end(body);
  });
}

async function main() {
  const { origin, controlToken, credential } = await readInput();
  const response = await postCredential(origin, controlToken, credential);
  process.stdout.write(
    `${JSON.stringify(
      parseIdentityReceipt(response.text, response.status, response.contentType),
    )}\n`,
  );
}

function isDirectExecution() {
  return (
    process.argv[1] !== undefined && fileURLToPath(import.meta.url) === resolve(process.argv[1])
  );
}

if (isDirectExecution()) {
  main().catch(() => {
    process.stderr.write("Identity admission peer failed\n");
    process.exitCode = 1;
  });
}
