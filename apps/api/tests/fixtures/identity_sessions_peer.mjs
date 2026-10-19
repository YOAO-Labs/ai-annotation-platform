import { createHash } from "node:crypto";
import { isIP } from "node:net";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  parseIdentityReceipt,
  postIdentityRequest,
  principalReceipt,
  readPeerInput,
  uuid,
  validateInput,
  validDateTime,
} from "./identity_admission_peer.mjs";

const schema = "aap-identity-sessions/v1";
const issuer = "aap-identity/v1";
const roles = new Set(["super_admin", "project_admin", "employee", "viewer"]);
const operations = new Set(["login", "refresh", "logout", "logout_all"]);
const failureCodes = new Set([
  "invalid_request",
  "credential_invalid",
  "credential_revoked",
  "generation_outdated",
  "token_expired",
  "user_inactive",
  "invalid_account_role",
  "credential_busy",
  "invalid_credentials",
  "email_not_verified",
  "captcha_required",
  "invalid_token",
  "malformed_token",
  "operation_conflict",
]);
const maxGeneration = (1n << 63n) - 1n;
const maxPythonEpochSeconds = 253402300799n;

function fail() {
  throw new Error("Invalid Identity session request or response");
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

function parseSessionJson(source) {
  return JSON.parse(source, (key, value, context) => {
    if ((key === "gen" || key === "generation") && value !== null) {
      const decimal = context?.source;
      if (typeof decimal !== "string" || !/^(0|[1-9][0-9]*)$/.test(decimal)) fail();
      const generation = BigInt(decimal);
      if (generation > maxGeneration) fail();
      return generation;
    }
    if (key === "exp") {
      const decimal = context?.source;
      if (typeof decimal !== "string" || !/^[1-9][0-9]*$/.test(decimal)) fail();
      const epoch = BigInt(decimal);
      if (epoch > maxPythonEpochSeconds) fail();
      return Number(epoch);
    }
    if (key === "failed_count" && value !== null) {
      const decimal = context?.source;
      if (typeof decimal !== "string" || !/^(0|[1-9][0-9]*)$/.test(decimal)) fail();
      return BigInt(decimal);
    }
    return value;
  });
}

function validateAuditContext(value) {
  const names = ["method", "path", "ip", "request_id"];
  if (!exact(value, names)) fail();
  const limits = { method: 8, path: 256, ip: 64, request_id: 36 };
  for (const name of names) {
    const field = value[name];
    if (field !== null && !boundedString(field, 0, limits[name])) fail();
  }
  if (value.ip && isIP(value.ip) === 0) fail();
}

export function validateSessionRequest(value) {
  if (
    !exact(value, [
      "schema",
      "operation",
      "operation_id",
      "token",
      "payload",
      "audit_context",
      "user_agent",
    ]) ||
    value.schema !== schema ||
    !operations.has(value.operation)
  ) {
    fail();
  }
  const operationId = uuid(value.operation_id);
  if (!boundedString(value.user_agent, 0, 256)) fail();
  validateAuditContext(value.audit_context);

  if (value.operation === "login") {
    if (
      value.token !== null ||
      !exact(value.payload, ["email", "password", "captcha_verified", "client_ip"]) ||
      !boundedString(value.payload.email, 0, 16384) ||
      !boundedString(value.payload.password, 0, 16384) ||
      typeof value.payload.captcha_verified !== "boolean" ||
      !boundedString(value.payload.client_ip, 0, 64) ||
      (value.payload.client_ip !== "" && isIP(value.payload.client_ip) === 0)
    ) {
      fail();
    }
  } else if (!boundedString(value.token, 1, 16384) || !exact(value.payload, [])) {
    fail();
  }

  return { request: value, operationId };
}

function decodeBase64Url(segment) {
  if (
    typeof segment !== "string" ||
    !/^[A-Za-z0-9_-]+$/.test(segment) ||
    segment.length % 4 === 1
  ) {
    fail();
  }
  const bytes = Buffer.from(segment, "base64url");
  if (bytes.toString("base64url") !== segment) fail();
  return bytes;
}

function decodeJsonSegment(segment) {
  let source;
  try {
    source = new TextDecoder("utf-8", { fatal: true }).decode(decodeBase64Url(segment));
    return parseSessionJson(source);
  } catch {
    fail();
  }
}

function sessionClaims(value) {
  if (!exact(value, ["sub", "role", "exp", "jti", "gen", "iss"])) fail();
  if (
    typeof value.role !== "string" ||
    !roles.has(value.role) ||
    typeof value.exp !== "number" ||
    !Number.isSafeInteger(value.exp) ||
    value.exp <= 0 ||
    typeof value.gen !== "bigint" ||
    value.gen < 0n ||
    value.gen > maxGeneration ||
    value.iss !== issuer
  ) {
    fail();
  }
  const sub = uuid(value.sub);
  const jti = uuid(value.jti);
  const date = new Date(value.exp * 1000);
  if (!Number.isFinite(date.valueOf()) || !validDateTime(date.toISOString())) fail();
  return { sub, role: value.role, exp: value.exp, jti, gen: value.gen, iss: issuer };
}

function readJwtClaims(token) {
  if (typeof token !== "string" || token.length > 16384) fail();
  const parts = token.split(".");
  if (parts.length !== 3 || parts[2].length !== 43) fail();
  const header = decodeJsonSegment(parts[0]);
  if (
    !exact(header, ["alg", "typ"]) ||
    header.alg !== "HS256" ||
    header.typ !== "JWT" ||
    decodeBase64Url(parts[2]).length !== 32
  ) {
    fail();
  }
  return sessionClaims(decodeJsonSegment(parts[1]));
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

function principalExpiryMatches(principal, exp) {
  if (typeof principal.expires_at !== "string" || !validDateTime(principal.expires_at)) {
    return false;
  }
  return Date.parse(principal.expires_at) === exp * 1000;
}

function bindPrincipalToJwt(principal, claims) {
  if (
    principal.actor_id !== claims.sub ||
    principal.credential_type !== "jwt" ||
    principal.credential_id !== claims.jti ||
    principal.generation_decimal !== claims.gen.toString(10) ||
    !principalExpiryMatches(principal, claims.exp)
  ) {
    fail();
  }
}

function bindSuccessfulCommand(request, principal, claims, operationId) {
  if (claims.sub !== principal.actor_id || claims.jti !== operationId) fail();
  if (request.operation === "login") {
    if (
      principal.credential_type !== "jwt" ||
      principal.credential_id !== claims.jti ||
      principal.role !== claims.role ||
      principal.generation_decimal !== claims.gen.toString(10) ||
      !principalExpiryMatches(principal, claims.exp)
    ) {
      fail();
    }
    return;
  }

  if (request.token.startsWith("ak_")) {
    if (
      request.operation !== "logout_all" ||
      !/^ak_[A-Za-z0-9_-]{32}$/.test(request.token) ||
      principal.credential_type !== "api_key"
    ) {
      fail();
    }
    return;
  }
  bindPrincipalToJwt(principal, readJwtClaims(request.token));
}

export function decodeSessionResponse(requestContext, body, status, contentType) {
  const { request, operationId } = requestContext;
  if (
    typeof body !== "string" ||
    !Number.isInteger(status) ||
    ![200, 400].includes(status) ||
    typeof contentType !== "string" ||
    contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    fail();
  }
  const document = parseSessionJson(body);
  if (!isRecord(document) || document.schema !== schema || typeof document.ok !== "boolean") {
    fail();
  }

  if (document.ok === false) {
    if (
      !exact(document, ["schema", "ok", "error"]) ||
      !exact(document.error, ["code", "failed_count"]) ||
      typeof document.error.code !== "string" ||
      !failureCodes.has(document.error.code)
    ) {
      fail();
    }
    const counted = ["invalid_credentials", "captcha_required"].includes(document.error.code);
    const failedCount = document.error.failed_count;
    if (
      (counted && (typeof failedCount !== "bigint" || failedCount < 0n)) ||
      (!counted && failedCount !== null)
    ) {
      fail();
    }
    return {
      receipt: {
        status,
        schema,
        ok: false,
        error: {
          code: document.error.code,
          failed_count_decimal: counted ? failedCount.toString(10) : null,
        },
      },
      accessToken: null,
      claims: null,
    };
  }

  if (
    status !== 200 ||
    !exact(document, ["schema", "ok", "operation", "operation_id", "principal", "data"]) ||
    document.operation !== request.operation ||
    uuid(document.operation_id) !== operationId ||
    !exact(document.data, ["claims", "access_token"])
  ) {
    fail();
  }
  if (request.operation === "login" && document.principal.email !== request.payload.email) {
    fail();
  }
  const principal = principalReceipt(document.principal);
  const expectsToken = request.operation !== "logout";
  if (
    expectsToken !== (document.data.claims !== null) ||
    expectsToken !== (document.data.access_token !== null)
  ) {
    fail();
  }

  let claims = null;
  let accessToken = null;
  if (expectsToken) {
    if (typeof document.data.access_token !== "string") fail();
    accessToken = document.data.access_token;
    claims = sessionClaims(document.data.claims);
    const signedClaims = readJwtClaims(accessToken);
    if (
      signedClaims.sub !== claims.sub ||
      signedClaims.role !== claims.role ||
      signedClaims.exp !== claims.exp ||
      signedClaims.jti !== claims.jti ||
      signedClaims.gen !== claims.gen ||
      signedClaims.iss !== claims.iss
    ) {
      fail();
    }
    bindSuccessfulCommand(request, principal, claims, operationId);
  } else {
    if (request.operation !== "logout" || request.token.startsWith("ak_")) fail();
    bindPrincipalToJwt(principal, readJwtClaims(request.token));
  }

  return {
    receipt: {
      status,
      schema,
      ok: true,
      operation: document.operation,
      operation_id: operationId,
      principal,
      claims: expectsToken ? normalizedClaims(claims) : null,
      access_token_sha256: accessToken
        ? createHash("sha256").update(accessToken, "utf8").digest("hex")
        : null,
    },
    accessToken,
    claims,
  };
}

async function main() {
  const input = await readPeerInput();
  if (!exact(input, ["url", "control_token", "request"])) fail();
  const requestContext = validateSessionRequest(input.request);
  const { origin, controlToken } = validateInput({
    url: input.url,
    control_token: input.control_token,
    credential: requestContext.request.token ?? "session-login",
  });
  const response = await postIdentityRequest(
    origin,
    controlToken,
    "/v1/identity/sessions/execute",
    requestContext.request,
  );
  const result = decodeSessionResponse(
    requestContext,
    response.text,
    response.status,
    response.contentType,
  );
  const receipt = { ...result.receipt };
  if (result.accessToken !== null) {
    const issuedResponse = await postIdentityRequest(
      origin,
      controlToken,
      "/v1/identity/authenticate",
      { token: result.accessToken },
    );
    const issued = parseIdentityReceipt(
      issuedResponse.text,
      issuedResponse.status,
      issuedResponse.contentType,
    );
    if (issued.ok) bindPrincipalToJwt(issued.principal, result.claims);
    receipt.issued = issued;
  }
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
}

function stringifyLossless(value) {
  if (typeof value === "bigint") return value.toString(10);
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) {
    return `[${value.map((item) => stringifyLossless(item)).join(",")}]`;
  }
  return `{${Object.keys(value)
    .map((key) => `${JSON.stringify(key)}:${stringifyLossless(value[key])}`)
    .join(",")}}`;
}

function makeTestJwt(claimsJson) {
  const header = Buffer.from('{"alg":"HS256","typ":"JWT"}').toString("base64url");
  const payload = Buffer.from(claimsJson, "utf8").toString("base64url");
  const signature = Buffer.alloc(32, 7).toString("base64url");
  return `${header}.${payload}.${signature}`;
}

function makeTestRequest(operation, operationId, token, payload = {}) {
  return validateSessionRequest({
    schema,
    operation,
    operation_id: operationId,
    token,
    payload,
    audit_context: { method: "POST", path: "/session", ip: "127.0.0.1", request_id: null },
    user_agent: "identity-sessions-self-check",
  });
}

function makePrincipal(actorId, credentialId, generation, expiresAt, overrides = {}) {
  return {
    actor_id: actorId,
    name: "Fixture actor",
    email: "fixture@example.test",
    role: "viewer",
    is_active: true,
    credential_type: "jwt",
    credential_id: credentialId,
    generation,
    scopes: null,
    expires_at: expiresAt,
    ...overrides,
  };
}

function makeReplyJson(requestContext, principal, claimsJson, accessToken, overrides = {}) {
  const claims = parseSessionJson(claimsJson);
  const document = {
    schema,
    ok: true,
    operation: requestContext.request.operation,
    operation_id: requestContext.operationId,
    principal,
    data: {
      claims,
      access_token: accessToken,
    },
    ...overrides,
  };
  return stringifyLossless(document);
}

function expectFailure(callback) {
  let rejected = false;
  try {
    callback();
  } catch {
    rejected = true;
  }
  if (!rejected) throw new Error("Self-check accepted an invalid receipt");
}

function selfCheck() {
  const actorId = "11111111-1111-4111-8111-111111111111";
  const inputJti = "22222222-2222-4222-8222-222222222222";
  const operationId = "33333333-3333-4333-8333-333333333333";
  const nextOperationId = "44444444-4444-4444-8444-444444444444";
  const maxGenerationText = maxGeneration.toString(10);
  const inputExp = 1893456000;
  const outputExp = 1924992000;
  const expiresAt = new Date(inputExp * 1000).toISOString();
  const inputClaimsJson =
    `{"sub":"${actorId}","role":"viewer","exp":${inputExp},` +
    `"jti":"${inputJti}","gen":${maxGenerationText},"iss":"${issuer}"}`;
  const inputToken = makeTestJwt(inputClaimsJson);
  const outputClaimsJson =
    `{"sub":"${actorId}","role":"viewer","exp":${outputExp},` +
    `"jti":"${nextOperationId}","gen":${maxGenerationText},"iss":"${issuer}"}`;
  const outputToken = makeTestJwt(outputClaimsJson);
  const jwtPrincipal = makePrincipal(actorId, inputJti, maxGeneration, expiresAt);
  const refreshRequest = makeTestRequest("refresh", nextOperationId, inputToken);
  const refreshBody = makeReplyJson(refreshRequest, jwtPrincipal, outputClaimsJson, outputToken);
  const refresh = decodeSessionResponse(refreshRequest, refreshBody, 200, "application/json");
  if (refresh.receipt.claims.generation_decimal !== maxGenerationText) fail();
  if (JSON.stringify(refresh.receipt).includes(inputToken)) fail();
  if (JSON.stringify(refresh.receipt).includes(outputToken)) fail();

  const loginRequest = makeTestRequest("login", operationId, null, {
    email: "fixture@example.test",
    password: "self-check-password",
    captcha_verified: true,
    client_ip: "127.0.0.1",
  });
  const loginExp = 1956528000;
  const loginClaimsJson =
    `{"sub":"${actorId}","role":"viewer","exp":${loginExp},` +
    `"jti":"${operationId}","gen":${maxGenerationText},"iss":"${issuer}"}`;
  const loginToken = makeTestJwt(loginClaimsJson);
  const loginPrincipal = makePrincipal(
    actorId,
    operationId,
    maxGeneration,
    new Date(loginExp * 1000).toISOString(),
  );
  const loginBody = makeReplyJson(loginRequest, loginPrincipal, loginClaimsJson, loginToken);
  const login = decodeSessionResponse(loginRequest, loginBody, 200, "application/json");
  const loginOutput = JSON.stringify(login.receipt);
  for (const secret of [
    "self-check-password",
    loginToken,
    inputToken,
    outputToken,
    "control-secret-self-check",
  ]) {
    if (loginOutput.includes(secret)) fail();
  }
  expectFailure(() =>
    decodeSessionResponse(
      loginRequest,
      makeReplyJson(
        loginRequest,
        { ...loginPrincipal, email: "different@example.test" },
        loginClaimsJson,
        loginToken,
      ),
      200,
      "application/json",
    ),
  );

  const logoutRequest = makeTestRequest("logout", operationId, inputToken);
  const logoutBody = stringifyLossless({
    schema,
    ok: true,
    operation: "logout",
    operation_id: operationId,
    principal: jwtPrincipal,
    data: { claims: null, access_token: null },
  });
  if (!decodeSessionResponse(logoutRequest, logoutBody, 200, "application/json").receipt.ok) {
    fail();
  }

  const apiKey = `ak_${"k".repeat(32)}`;
  const logoutAllRequest = makeTestRequest("logout_all", operationId, apiKey);
  const apiPrincipal = {
    ...jwtPrincipal,
    credential_type: "api_key",
    credential_id: inputJti,
    generation: null,
    scopes: ["datasets:read"],
    expires_at: null,
  };
  const allClaimsJson =
    `{"sub":"${actorId}","role":"viewer","exp":${outputExp},` +
    `"jti":"${operationId}","gen":${maxGenerationText},"iss":"${issuer}"}`;
  const allToken = makeTestJwt(allClaimsJson);
  const logoutAllBody = makeReplyJson(logoutAllRequest, apiPrincipal, allClaimsJson, allToken);
  const logoutAll = decodeSessionResponse(logoutAllRequest, logoutAllBody, 200, "application/json");
  if (logoutAll.receipt.principal.credential_type !== "api_key") fail();
  if (JSON.stringify(logoutAll.receipt).includes(apiKey)) fail();
  expectFailure(() =>
    decodeSessionResponse(
      makeTestRequest("refresh", nextOperationId, apiKey),
      makeReplyJson(
        makeTestRequest("refresh", nextOperationId, apiKey),
        apiPrincipal,
        allClaimsJson.replace(`"jti":"${operationId}"`, `"jti":"${nextOperationId}"`),
        makeTestJwt(allClaimsJson.replace(`"jti":"${operationId}"`, `"jti":"${nextOperationId}"`)),
      ),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodeSessionResponse(
      makeTestRequest("logout", operationId, apiKey),
      stringifyLossless({
        schema,
        ok: true,
        operation: "logout",
        operation_id: operationId,
        principal: apiPrincipal,
        data: { claims: null, access_token: null },
      }),
      200,
      "application/json",
    ),
  );

  const badActor = { ...jwtPrincipal, actor_id: "55555555-5555-4555-8555-555555555555" };
  expectFailure(() =>
    decodeSessionResponse(
      refreshRequest,
      makeReplyJson(refreshRequest, badActor, outputClaimsJson, outputToken),
      200,
      "application/json",
    ),
  );
  const badJti = { ...jwtPrincipal, credential_id: "66666666-6666-4666-8666-666666666666" };
  expectFailure(() =>
    decodeSessionResponse(
      refreshRequest,
      makeReplyJson(refreshRequest, badJti, outputClaimsJson, outputToken),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodeSessionResponse(
      refreshRequest,
      makeReplyJson(refreshRequest, jwtPrincipal, outputClaimsJson, outputToken, {
        operation_id: inputJti,
      }),
      200,
      "application/json",
    ),
  );
  expectFailure(() =>
    decodeSessionResponse(
      refreshRequest,
      makeReplyJson(refreshRequest, jwtPrincipal, outputClaimsJson, outputToken, {
        unexpected: true,
      }),
      200,
      "application/json",
    ),
  );
  const mismatchedToken = makeTestJwt(
    outputClaimsJson.replace('"role":"viewer"', '"role":"employee"'),
  );
  expectFailure(() =>
    decodeSessionResponse(
      refreshRequest,
      makeReplyJson(refreshRequest, jwtPrincipal, outputClaimsJson, mismatchedToken),
      200,
      "application/json",
    ),
  );
  const badLoginClaims = loginClaimsJson.replace(`"gen":${maxGenerationText}`, '"gen":1.0');
  expectFailure(() => readJwtClaims(makeTestJwt(badLoginClaims)));
  const largeFailure = parseSessionJson(
    `{"schema":"${schema}","ok":false,"error":{"code":"captcha_required","failed_count":90071992547409931234567890}}`,
  );
  const failureRequest = makeTestRequest("login", operationId, null, {
    email: "",
    password: "",
    captcha_verified: false,
    client_ip: "",
  });
  const failure = decodeSessionResponse(
    failureRequest,
    stringifyLossless(largeFailure),
    200,
    "application/json",
  );
  if (failure.receipt.error.failed_count_decimal !== "90071992547409931234567890") {
    fail();
  }
  if (nextOperationId === operationId) fail();
  return { ok: true, checks: 15 };
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
      process.stderr.write("Identity session peer self-check failed\n");
      process.exitCode = 1;
    }
  } else {
    main().catch(() => {
      process.stderr.write("Identity session peer failed\n");
      process.exitCode = 1;
    });
  }
}
