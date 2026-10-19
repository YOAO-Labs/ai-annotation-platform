import { createHash } from "node:crypto";
import { createServer } from "node:http";

const schema = "aap-assets-mask-content/v1";
const maxRequestBytes = 4 * 1024 * 1024 + 16 * 1024;
const objects = new Map();
const tokens = {
  content: process.env.MASK_CONTENT_PEER_CONTENT_TOKEN,
  cleanup: process.env.MASK_CONTENT_PEER_CLEANUP_TOKEN,
};

function envelope(data) {
  return { schema, ok: true, data };
}

function failure(code) {
  return { schema, ok: false, error: { code } };
}

function respond(response, status, value) {
  const body = Buffer.from(JSON.stringify(value), "utf8");
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": body.length,
  });
  response.end(body);
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

function referenceFor(content) {
  if (
    content?.encoding !== "coco_rle" ||
    !Array.isArray(content.size) ||
    content.size.length !== 2 ||
    !content.size.every((part) => Number.isSafeInteger(part) && part > 0) ||
    !Array.isArray(content.counts) ||
    content.counts.length === 0 ||
    !content.counts.every((part) => Number.isSafeInteger(part) && part >= 0) ||
    content.counts.reduce((sum, part) => sum + part, 0) !== content.size[0] * content.size[1]
  ) {
    throw new Error("invalid_request");
  }
  const canonical = JSON.stringify({
    encoding: "coco_rle",
    size: content.size,
    counts: content.counts,
  });
  const bytes = Buffer.from(canonical, "utf8");
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  return {
    encoding: "coco_rle_ref",
    storage_encoding: null,
    size: content.size,
    object_key: `raster-masks/sha256/${sha256.slice(0, 2)}/${sha256.slice(2, 4)}/${sha256}.json`,
    sha256,
    runs: content.counts.length,
    bytes: bytes.length,
    body: bytes,
  };
}

async function handle(role, request, response) {
  const token = tokens[role];
  if (!token || request.headers.authorization !== `Bearer ${token}` || request.method !== "POST") {
    respond(response, 403, failure("permission_denied"));
    return;
  }
  const prefix = "/v1/mask-content/";
  if (!request.url?.startsWith(prefix)) {
    respond(response, 404, failure("invalid_request"));
    return;
  }
  const operation = request.url.slice(prefix.length);
  if (
    (role === "content" && !["put", "get"].includes(operation)) ||
    (role === "cleanup" && !["list", "delete"].includes(operation))
  ) {
    respond(response, 403, failure("permission_denied"));
    return;
  }
  try {
    const body = await readJson(request);
    if (operation === "put") {
      const value = referenceFor(body.content);
      let stored = objects.get(value.object_key);
      if (!stored) {
        stored = {
          reference: Object.fromEntries(Object.entries(value).filter(([key]) => key !== "body")),
          content: {
            encoding: "coco_rle",
            size: body.content.size,
            counts: body.content.counts,
          },
          bytes: value.body,
          etag: createHash("md5").update(value.body).digest("hex"),
          lastModified: new Date().toISOString(),
        };
        objects.set(value.object_key, stored);
      }
      respond(response, 200, envelope({ reference: stored.reference }));
      return;
    }
    if (operation === "get") {
      const stored = objects.get(body.reference?.object_key);
      if (!stored) {
        respond(response, 404, failure("object_missing"));
        return;
      }
      respond(response, 200, envelope({ reference: stored.reference, content: stored.content }));
      return;
    }
    if (operation === "list") {
      const sorted = [...objects.entries()].sort(([left], [right]) => left.localeCompare(right));
      const start = body.cursor === undefined || body.cursor === null ? 0 : Number(body.cursor);
      const limit = Math.min(Number(body.limit ?? 1000), 1000);
      const page = sorted.slice(start, start + limit).map(([key, stored]) => ({
        key,
        etag: stored.etag,
        byte_size: stored.bytes.length,
        last_modified: stored.lastModified,
      }));
      const next = start + page.length < sorted.length ? String(start + page.length) : null;
      respond(response, 200, envelope({ items: page, cursor: next }));
      return;
    }
    if (operation === "delete") {
      const stored = objects.get(body.object_key);
      if (!stored) {
        respond(response, 200, envelope({ object_key: body.object_key, deleted: false }));
        return;
      }
      if (
        body.etag !== stored.etag ||
        Date.parse(body.last_modified) !== Date.parse(stored.lastModified)
      ) {
        respond(response, 409, failure("content_changed"));
        return;
      }
      objects.delete(body.object_key);
      respond(response, 200, envelope({ object_key: body.object_key, deleted: true }));
      return;
    }
    respond(response, 404, failure("invalid_request"));
  } catch (error) {
    const code = error?.message === "resource_limit" ? "resource_limit" : "invalid_request";
    respond(response, code === "resource_limit" ? 413 : 422, failure(code));
  }
}

function listen(role) {
  return new Promise((resolve, reject) => {
    const server = createServer((request, response) => {
      void handle(role, request, response);
    });
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve({ server, port: server.address().port });
    });
  });
}

const [content, cleanup] = await Promise.all([listen("content"), listen("cleanup")]);
process.stdout.write(
  JSON.stringify({ content_port: content.port, cleanup_port: cleanup.port }) + "\n",
);

function close() {
  content.server.close();
  cleanup.server.close();
}

process.once("SIGTERM", close);
process.once("SIGINT", close);
