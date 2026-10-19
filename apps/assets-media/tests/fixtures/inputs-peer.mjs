import { createHash } from "node:crypto";

try {
  let input = "";
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > 65536) throw new Error("request budget exceeded");
  }
  const config = JSON.parse(input);
  const identity = {
    input_id: config.input_id,
    job_id: config.job_id,
    task_id: config.task_id,
  };
  async function call(operation, value) {
    const response = await fetch(`${config.origin}/v1/inputs/${operation}`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${config.token}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(value),
    });
    const wire = await response.json();
    if (wire.schema !== "aap-assets-inputs/v1" || wire.ok !== true || response.status !== 200)
      throw new Error(`input operation failed: ${operation}`);
    return wire.data;
  }
  const selector = {
    job_id: config.job_id,
    task_id: config.task_id,
    actor_id: config.actor_id,
  };
  const found = await call("find", selector);
  const accepted = await call("accept", selector);
  const record = await call("record", identity);
  const verified = await call("verify-source", identity);
  const reference = await call("reference", identity);
  for (const value of [found.input, accepted, record, verified, reference.input]) {
    if (
      value.input_id !== config.input_id ||
      value.job_id !== config.job_id ||
      value.task_id !== config.task_id ||
      value.status !== "ready" ||
      value.sha256 !== config.sha256 ||
      value.byte_size !== config.byte_size
    )
      throw new Error("input receipt scope changed");
  }
  const url = new URL(reference.url);
  if (
    url.searchParams.getAll("versionId").length !== 1 ||
    url.searchParams.get("versionId") !== reference.version_id
  )
    throw new Error("input URL does not bind its version");
  const response = await fetch(url, { credentials: "omit", redirect: "error" });
  if (response.status !== 200) throw new Error("input version unavailable");
  const digest = createHash("sha256");
  let byteSize = 0;
  for await (const chunk of response.body) {
    byteSize += chunk.byteLength;
    if (byteSize > config.byte_size) throw new Error("input byte budget exceeded");
    digest.update(chunk);
  }
  const measured = digest.digest("hex");
  if (byteSize !== config.byte_size || measured !== config.sha256)
    throw new Error("input bytes changed");
  process.stdout.write(
    JSON.stringify({
      schema: "aap-assets-inputs/v1",
      input_id: config.input_id,
      version_id: reference.version_id,
      sha256: measured,
      byte_size: byteSize,
    }),
  );
} catch {
  process.stderr.write("input peer contract check failed\n");
  process.exitCode = 1;
}
