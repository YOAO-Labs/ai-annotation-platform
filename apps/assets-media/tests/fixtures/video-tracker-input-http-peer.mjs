const CONTROL_SCHEMA = "aap-assets-video-tracker-control/v1";
const assert = (value, message = "tracker contract") => {
  if (!value) throw new Error(message);
};

async function bounded(response, maximum) {
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.byteLength;
    assert(size <= maximum, "response budget");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

try {
  let raw = "";
  for await (const chunk of process.stdin) {
    raw += chunk;
    assert(Buffer.byteLength(raw) <= 65_536, "request budget");
  }
  const config = JSON.parse(raw);
  const origin = new URL(config.origin);
  assert(origin.protocol === "http:" && origin.hostname === "127.0.0.1", "origin");
  const operations = [];
  const post = async (operation, request) => {
    const response = await fetch(`${origin.origin}/v1/video-tracker-inputs/${operation}`, {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(15_000),
      headers: {
        Authorization: `Bearer ${config.token}`,
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
      },
      body: JSON.stringify(request),
    });
    const reply = JSON.parse(await bounded(response, 2 * 1024 * 1024));
    assert(
      response.status === 200 && reply.schema === CONTROL_SCHEMA && reply.ok,
      `http ${operation}: ${response.status}`,
    );
    assert(Object.keys(reply).sort().join(",") === "data,ok,schema", `envelope ${operation}`);
    operations.push(operation);
    return reply.data;
  };

  const input = await post("input", config.input);
  assert(
    input.status === "ready" && input.input_id === config.input_id,
    `input status ${input.status}/${input.input_id}`,
  );
  assert(input.plan_digest === config.plan_digest, "input plan digest mismatch");
  const prepared = await post("prepare", config.prepare);
  assert(
    prepared.status === "ready" && prepared.request_digest === input.request_digest,
    "prepared status",
  );
  const reference = await post("reference", config.reference);
  assert(reference.input.plan_digest === config.plan_digest, "reference plan digest");
  assert(reference.source_version.version_id === config.version_id, "reference version");
  assert(reference.url && reference.expires_in > 0, "reference url");
  assert(
    new URL(reference.url).searchParams.get("versionId") === config.version_id,
    "url version binding",
  );

  const serialized = JSON.stringify({ input, prepared, reference });
  for (const forbidden of ["file_path", "source_facts", "object_key", "blob_key"]) {
    assert(!serialized.includes(`"${forbidden}"`), `leaked ${forbidden}`);
  }
  assert(!Object.hasOwn(reference, "blob"));
  assert(!Object.hasOwn(reference.source_version, "key"));
  process.stdout.write(
    JSON.stringify({
      operations,
      version_id: reference.source_version.version_id,
      plan_digest: reference.input.plan_digest,
    }),
  );
} catch (error) {
  process.stderr.write(`${error?.message ?? "tracker peer failed"}\n`);
  process.exitCode = 1;
}
