const schema = "aap-labeling-mask-gc/v1";
const maxInputBytes = 64 * 1024;
const maxResponseBytes = 1024 * 1024;

function hasExactKeys(value, keys) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).sort().join(",") === [...keys].sort().join(",")
  );
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > maxInputBytes) throw new Error("peer request exceeds its bound");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function readBounded(response) {
  if (!response.body) throw new Error("HTTP response has no body");
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > maxResponseBytes) throw new Error("HTTP response exceeds its bound");
    chunks.push(Buffer.from(value));
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function run(origin, token, request) {
  const response = await fetch(`${origin}/v1/mask-gc/run`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(request),
    signal: AbortSignal.timeout(30000),
  });
  const document = JSON.parse(await readBounded(response));
  if (document.schema !== schema) throw new Error("unexpected Mask GC schema");
  if (
    document.ok === true &&
    response.ok &&
    hasExactKeys(document, ["schema", "ok", "data"]) &&
    document.data !== null &&
    typeof document.data === "object" &&
    !Array.isArray(document.data)
  ) {
    return {
      status: response.status,
      schema,
      ok: true,
      data: document.data,
    };
  }
  if (
    document.ok === false &&
    hasExactKeys(document, ["schema", "ok", "error"]) &&
    hasExactKeys(document.error, ["code"]) &&
    typeof document.error.code === "string"
  ) {
    return {
      status: response.status,
      schema,
      ok: false,
      error: { code: document.error.code },
    };
  }
  throw new Error("invalid Mask GC HTTP envelope");
}

async function main() {
  const origin = process.env.AAP_MASK_GC_PEER_URL;
  const token = process.env.AAP_MASK_GC_PEER_TOKEN;
  if (!origin || !token || token.length < 32) {
    throw new Error("Mask GC peer credentials are unavailable");
  }
  const input = await readInput();
  let responses;
  if (input.mode === "dry-run-continuation") {
    const request = {
      dry_run: true,
      scan_pages: input.scan_pages,
      delete_limit: input.delete_limit,
      after_key: input.after_key,
    };
    const first = await run(origin, token, request);
    if (!first.ok || typeof first.data.next_after_key !== "string") {
      throw new Error("dry-run page did not return a continuation key");
    }
    const second = await run(origin, token, {
      ...request,
      after_key: first.data.next_after_key,
    });
    responses = [first, second];
  } else if (input.mode === "run") {
    responses = [await run(origin, token, input.request)];
  } else {
    throw new Error("unsupported Mask GC peer mode");
  }
  process.stdout.write(JSON.stringify({ responses }));
}

main().catch(() => {
  process.stderr.write("Mask GC Node peer failed\n");
  process.exitCode = 1;
});
