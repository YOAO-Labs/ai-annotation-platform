import { createServer } from "node:http";

const schema = "aap-assets-mask-gc/v1";
const token = process.env.MASK_GC_PEER_TOKEN;
const maxRequestBytes = 1024 * 1024;

function respond(response, status, value) {
  const body = Buffer.from(JSON.stringify(value), "utf8");
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": body.length,
  });
  response.end(body);
}

function success(data) {
  return { schema, ok: true, data };
}

function failure(code) {
  return { schema, ok: false, error: { code } };
}

async function readJson(request) {
  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > maxRequestBytes) throw new Error("resource_limit");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function exactKeys(value, keys) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).sort().join(",") === [...keys].sort().join(",")
  );
}

async function handle(request, response) {
  if (
    request.method !== "POST" ||
    request.headers.authorization !== `Bearer ${token}` ||
    request.headers["content-type"]?.split(";", 1)[0].trim().toLowerCase() !== "application/json"
  ) {
    respond(response, 401, failure("permission_denied"));
    return;
  }
  const match = /^\/v1\/mask-gc\/(scan|execute|status)$/.exec(request.url ?? "");
  if (!match) {
    respond(response, 403, failure("permission_denied"));
    return;
  }
  const operation = match[1];
  try {
    const body = await readJson(request);
    if (operation === "scan") {
      if (
        !exactKeys(body, ["after_key", "limit"]) ||
        (body.after_key !== null && typeof body.after_key !== "string") ||
        !Number.isSafeInteger(body.limit) ||
        body.limit < 1 ||
        body.limit > 1000
      ) {
        throw new Error("invalid_request");
      }
      respond(response, 200, success({ items: [], after_key: body.after_key, complete: true }));
      return;
    }
    if (operation === "execute") {
      if (
        !exactKeys(body, ["operation_id", "object_key", "etag", "last_modified"]) ||
        typeof body.operation_id !== "string" ||
        typeof body.object_key !== "string" ||
        !body.object_key.startsWith("raster-masks/sha256/") ||
        typeof body.etag !== "string" ||
        typeof body.last_modified !== "string"
      ) {
        throw new Error("invalid_request");
      }
      respond(
        response,
        200,
        success({
          operation_id: body.operation_id,
          request: body,
          state: "indeterminate",
          outcome: null,
        }),
      );
      return;
    }
    if (!exactKeys(body, ["operation_id"]) || typeof body.operation_id !== "string") {
      throw new Error("invalid_request");
    }
    respond(
      response,
      200,
      success({
        operation_id: body.operation_id,
        request: null,
        state: "not_found",
        outcome: null,
      }),
    );
  } catch (error) {
    const code = error?.message === "resource_limit" ? "resource_limit" : "invalid_request";
    respond(response, code === "resource_limit" ? 413 : 422, failure(code));
  }
}

const server = createServer((request, response) => {
  void handle(request, response);
});
server.listen(0, "127.0.0.1", () => {
  process.stdout.write(JSON.stringify({ port: server.address().port }) + "\n");
});

function close() {
  server.close();
}

process.once("SIGTERM", close);
process.once("SIGINT", close);
