import { createHash } from "node:crypto";

import {
  parseIdentityReceipt,
  parseJsonPreservingGeneration,
  postIdentityRequest,
  principalReceipt,
  readPeerInput,
  uuid,
  validateInput,
  validDateTime,
} from "./identity_admission_peer.mjs";

const operations = new Set([
  "list_api_keys",
  "create_api_key",
  "update_api_key",
  "rotate_api_key",
  "revoke_api_key",
]);
const failures = new Set([
  "invalid_request",
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
  "key_not_found",
  "key_busy",
  "operation_conflict",
]);

function fail() {
  throw new Error("Invalid Identity key command or response");
}

function exact(value, names) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).length === names.length &&
    names.every((name) => Object.hasOwn(value, name))
  );
}

function keyRecord(value) {
  if (
    !exact(value, [
      "id",
      "name",
      "key_prefix",
      "scopes",
      "expires_at",
      "last_used_at",
      "revoked_at",
      "created_at",
    ])
  )
    fail();
  if (
    typeof value.name !== "string" ||
    Array.from(value.name).length < 1 ||
    Array.from(value.name).length > 60 ||
    typeof value.key_prefix !== "string" ||
    !/^ak_[A-Za-z0-9_-]{9}$/.test(value.key_prefix) ||
    !Array.isArray(value.scopes) ||
    value.scopes.some((scope) => typeof scope !== "string") ||
    !validDateTime(value.created_at)
  )
    fail();
  uuid(value.id);
  for (const field of ["expires_at", "last_used_at", "revoked_at"]) {
    if (value[field] !== null && !validDateTime(value[field])) fail();
  }
  return value;
}

async function main() {
  const input = await readPeerInput();
  if (!exact(input, ["url", "control_token", "request"])) fail();
  const request = input.request;
  if (
    !exact(request, ["schema", "operation", "operation_id", "token", "payload"]) ||
    request.schema !== "aap-identity-keys/v1" ||
    !operations.has(request.operation)
  )
    fail();
  const operationId = uuid(request.operation_id);
  const { origin, controlToken } = validateInput({
    url: input.url,
    control_token: input.control_token,
    credential: request.token,
  });
  const response = await postIdentityRequest(
    origin,
    controlToken,
    "/v1/identity/api-keys/execute",
    request,
  );
  if (response.contentType?.split(";", 1)[0].trim().toLowerCase() !== "application/json") fail();
  const reply = parseJsonPreservingGeneration(response.text);
  if (reply.schema !== "aap-identity-keys/v1" || typeof reply.ok !== "boolean") fail();
  if (!reply.ok) {
    if (
      !exact(reply, ["schema", "ok", "error"]) ||
      !exact(reply.error, ["code"]) ||
      !failures.has(reply.error.code) ||
      ![200, 400].includes(response.status)
    )
      fail();
    process.stdout.write(
      `${JSON.stringify({ status: response.status, schema: reply.schema, ok: false, error: reply.error })}\n`,
    );
    return;
  }
  if (
    response.status !== 200 ||
    !exact(reply, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    reply.operation !== request.operation ||
    uuid(reply.operation_id) !== operationId
  )
    fail();
  const principal = principalReceipt(reply.principal);
  let data;
  let issued;
  if (request.operation === "list_api_keys") {
    if (!exact(reply.data, ["items"]) || !Array.isArray(reply.data.items)) fail();
    data = { items: reply.data.items.map(keyRecord) };
  } else if (request.operation === "revoke_api_key") {
    if (
      !exact(reply.data, ["key_id", "revoked"]) ||
      reply.data.revoked !== true ||
      uuid(reply.data.key_id) !== uuid(request.payload.key_id)
    )
      fail();
    data = reply.data;
  } else {
    const mint = ["create_api_key", "rotate_api_key"].includes(request.operation);
    if (!exact(reply.data, mint ? ["key", "plaintext"] : ["key"])) fail();
    const key = keyRecord(reply.data.key);
    if (
      uuid(key.id) !==
      (request.operation === "create_api_key" ? operationId : uuid(request.payload.key_id))
    )
      fail();
    data = { key };
    if (mint) {
      if (
        typeof reply.data.plaintext !== "string" ||
        !/^ak_[A-Za-z0-9_-]{32}$/.test(reply.data.plaintext) ||
        reply.data.plaintext.slice(0, 12) !== key.key_prefix
      )
        fail();
      const authenticated = await postIdentityRequest(
        origin,
        controlToken,
        "/v1/identity/authenticate",
        { token: reply.data.plaintext },
      );
      issued = parseIdentityReceipt(
        authenticated.text,
        authenticated.status,
        authenticated.contentType,
      );
      if (
        issued.ok &&
        (issued.principal.actor_id !== principal.actor_id ||
          issued.principal.credential_type !== "api_key" ||
          issued.principal.credential_id !== uuid(key.id))
      )
        fail();
      issued.plaintext_sha256 = createHash("sha256").update(reply.data.plaintext).digest("hex");
    }
  }
  process.stdout.write(
    `${JSON.stringify({
      status: response.status,
      schema: reply.schema,
      ok: true,
      operation: reply.operation,
      operation_id: operationId,
      principal,
      data,
      ...(issued ? { issued } : {}),
    })}\n`,
  );
}

main().catch(() => {
  process.stderr.write("Identity key peer failed\n");
  process.exitCode = 1;
});
