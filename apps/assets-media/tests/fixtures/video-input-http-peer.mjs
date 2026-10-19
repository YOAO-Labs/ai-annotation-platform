import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const schema = "aap-assets-video-input-control/v1";
const check = (value) => {
  if (!value) throw new Error("contract");
};
async function bounded(response, maximum) {
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.byteLength;
    check(size <= maximum);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

try {
  let raw = "";
  for await (const chunk of process.stdin) {
    raw += chunk;
    check(Buffer.byteLength(raw) <= 65536);
  }
  const config = JSON.parse(raw);
  const origin = new URL(config.origin);
  check(origin.hostname === "127.0.0.1" && origin.protocol === "http:");
  const called = new Set();
  async function post(operation, request) {
    const response = await fetch(`${origin.origin}/v1/video-inputs/${operation}`, {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(15000),
      headers: {
        Authorization: `Bearer ${config.token}`,
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
      },
      body: JSON.stringify(request),
    });
    const reply = JSON.parse(await bounded(response, 2 * 1024 * 1024));
    check(response.status === 200 && reply.schema === schema && reply.ok === true);
    check(Object.keys(reply).sort().join(",") === "data,ok,schema");
    called.add(operation);
    return reply.data;
  }
  const scope = { job_id: config.job_id, actor_id: config.actor_id, project_id: config.project_id };
  const header = await post("manifest", scope);
  check(header.status === "sealed" && header.selected_count === 1);
  const selection = { ...scope, selection_digest: header.selection_digest };
  const page = await post("members", {
    ...selection,
    sealed_digest: header.sealed_digest,
    cursor: 0,
    page_size: 100,
  });
  check(page.members.length === 1 && page.members[0].ordinal === 1 && page.next_cursor === null);
  const member = page.members[0];
  check(member.state === "ready" && member.task.task_id === config.task_id);
  check(
    (await post("prepare", { ...selection, ordinal: 1, task_id: config.task_id })).member.input
      .input_id === config.input_id,
  );
  check((await post("seal", selection)).sealed_digest === header.sealed_digest);
  const input = {
    input_id: config.input_id,
    job_id: config.job_id,
    task_id: config.task_id,
    actor_id: config.actor_id,
  };
  const record = await post("input", input);
  check(record.status === "ready" && record.plan.plan_digest === config.plan_digest);
  const plan = record.plan;
  check(
    (await post("verify", { ...input, plan_digest: plan.plan_digest })).input_id ===
      config.input_id,
  );
  const frameRequest = { ...input, plan_digest: plan.plan_digest, ordinal: 1 };
  const entries = [];
  let cursor = 0;
  for (;;) {
    const times = await post("times", {
      ...input,
      plan_digest: plan.plan_digest,
      cursor,
      page_size: 100,
    });
    check(times.cursor === cursor && times.plan.plan_digest === plan.plan_digest);
    entries.push(...times.entries);
    if (times.next_cursor === null) break;
    check(times.next_cursor > cursor);
    cursor = times.next_cursor;
  }
  const hash = spawnSync(
    process.execPath,
    [fileURLToPath(new URL("./video-input-values-peer.mjs", import.meta.url))],
    {
      input: JSON.stringify({ plan, entries }),
      encoding: "utf8",
      timeout: 15000,
      maxBuffer: 65536,
    },
  );
  check(hash.status === 0 && JSON.parse(hash.stdout).plan_digest === plan.plan_digest);
  const required = await post("require-frame", frameRequest);
  const frame = await post("frame", frameRequest);
  check(
    required.frame_id === frame.frame_id && frame.status === "ready" && frame.entry.ordinal === 1,
  );
  const reference = await post("reference", frameRequest);
  const url = new URL(reference.url);
  check(
    reference.blob.version_id === frame.blob.version_id &&
      url.searchParams.get("versionId") === frame.blob.version_id,
  );
  check(decodeURIComponent(url.pathname).endsWith("/" + frame.blob.key));
  const bytes = await bounded(
    await fetch(url, { redirect: "error", signal: AbortSignal.timeout(15000) }),
    frame.blob.byte_size,
  );
  check(
    bytes.length === frame.blob.byte_size &&
      createHash("sha256").update(bytes).digest("hex") === frame.blob.sha256,
  );
  check(called.size === 10);
  process.stdout.write(
    JSON.stringify({
      schema,
      operations: called.size,
      input_id: config.input_id,
      selected_count: entries.length,
      plan_digest: plan.plan_digest,
      frame_sha256: frame.blob.sha256,
      version_id: frame.blob.version_id,
    }),
  );
} catch {
  process.stderr.write("Video input HTTP peer rejected the contract\n");
  process.exitCode = 1;
}
